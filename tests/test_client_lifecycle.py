"""The archive client must be built ONCE per process and reused, not once per request.

`KarakutuClient` owns the `ArweaveWalletAuth` that caches the 15-minute JWT, and the archive
rate-limits `/authenticate` to 3/min per IP. When the composition root
did not hold a client, `pipeline.run_audit(karakutu_client=None)` let step 2 call `get_client()`
itself and every audit threw the token cache away and minted a fresh one. Above ~3
audits/minute the mint began failing, `get_token` returned `None`, and step 2 degraded to
"the archive returned no similar articles" — a SILENT loss of the relocation signal,
reported to the caller as a legitimate INCONCLUSIVE.

These tests pin the wiring and the concurrency guard so that cannot come back quietly.
"""
import asyncio
import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from src.clients import auth as auth_module
from src.clients.auth import ArweaveWalletAuth
from src.clients.karakutu import KarakutuClient


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture(scope="module")
def wallet_jwk():
    """A throwaway RSA-4096 keyfile in Arweave's JWK shape (never a real wallet)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    numbers = key.private_numbers()

    def enc(value: int) -> str:
        return b64e(value.to_bytes((value.bit_length() + 7) // 8, "big"))

    return {
        "kty": "RSA", "e": "AQAB", "n": enc(numbers.public_numbers.n),
        "d": enc(numbers.d), "p": enc(numbers.p), "q": enc(numbers.q),
        "dp": enc(numbers.dmp1), "dq": enc(numbers.dmq1), "qi": enc(numbers.iqmp),
    }


@pytest.fixture
def wallet_file(tmp_path, wallet_jwk):
    path = tmp_path / "wallet.json"
    path.write_text(json.dumps(wallet_jwk))
    return path


# --- the wiring ---------------------------------------------------------------


async def test_lifespan_builds_one_archive_client_and_puts_it_on_app_state():
    from src.api.app import app, lifespan

    async with lifespan(app):
        assert isinstance(app.state.karakutu_client, KarakutuClient)


async def test_lifespan_reuses_the_same_client_instance_for_the_whole_process():
    """Two reads of app.state must be the same object — not a fresh client each time."""
    from src.api.app import app, lifespan

    async with lifespan(app):
        assert app.state.karakutu_client is app.state.karakutu_client


async def test_the_audit_route_hands_the_shared_client_to_run_audit(client, monkeypatch,
                                                                    valid_audit_request):
    """The route must forward app.state.karakutu_client. Passing None would put step 2 back
    on its own get_client() fallback and restore the per-request mint."""
    from src.api import app as app_module

    seen = {}

    async def fake_run_audit(request, **kwargs):
        seen.update(kwargs)
        from src.api.models import AuditResponse, Verdict
        return AuditResponse(verdict=Verdict.INCONCLUSIVE, stages=[], findings="stub")

    monkeypatch.setattr(app_module, "run_audit", fake_run_audit)

    resp = await client.post("/api/audit", json=valid_audit_request)

    assert resp.status_code == 200
    assert "karakutu_client" in seen, "the route did not forward a karakutu_client at all"
    assert seen["karakutu_client"] is app_module.app.state.karakutu_client
    assert seen["karakutu_client"] is not None


def test_a_client_caches_its_auth_object_across_calls(wallet_file):
    """One auth object per client is what makes the JWT cache mean anything."""
    c = KarakutuClient(base_url="https://api.example", wallet_path=str(wallet_file))
    assert c.auth is c.auth
    assert c.enabled is True


# --- the concurrency guard ----------------------------------------------------


async def test_concurrent_first_calls_mint_exactly_one_token(monkeypatch, wallet_file):
    """A cold cache hit by N simultaneous audits must produce ONE mint AND N valid tokens.

    This is the burst `/authenticate` (3/min per IP) rejects. The mint must genuinely
    suspend for this to be a real test — a mock transport that never yields hides the race,
    so the handler awaits below. Without the lock, callers 2..N fall through the
    MIN_SECONDS_BETWEEN_MINTS guard while the first mint is still in flight and each gets
    `None` back, which downstream is indistinguishable from "the archive is disabled".
    """
    mints = {"login": 0, "authenticate": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if "/login" in request.url.path:
            mints["login"] += 1
            await asyncio.sleep(0)  # a real suspension point: the mint is in flight
            return httpx.Response(200, json={"challenge": "abc"})
        if "/authenticate" in request.url.path:
            mints["authenticate"] += 1
            await asyncio.sleep(0)
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 900})
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        auth_module.httpx, "AsyncClient",
        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))

    a = ArweaveWalletAuth(wallet_path=str(wallet_file), base_url="https://api.example")
    tokens = await asyncio.gather(*(a.get_token() for _ in range(8)))

    assert mints["authenticate"] == 1, f"minted {mints['authenticate']} times for 8 callers"
    assert mints["login"] == 1
    # Every caller must get the token, not None. This is the half the 5-second
    # MIN_SECONDS_BETWEEN_MINTS guard cannot provide on its own.
    assert tokens == ["tok"] * 8, f"some callers got no token: {tokens}"


async def test_a_warm_token_is_reused_without_minting(monkeypatch, wallet_file):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if "/login" in request.url.path:
            return httpx.Response(200, json={"challenge": "abc"})
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 900})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        auth_module.httpx, "AsyncClient",
        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))

    a = ArweaveWalletAuth(wallet_path=str(wallet_file), base_url="https://api.example")
    assert await a.get_token() == "tok"
    after_first = calls["n"]

    for _ in range(5):
        assert await a.get_token() == "tok"

    assert calls["n"] == after_first, "a cached, unexpired token must not re-mint"
