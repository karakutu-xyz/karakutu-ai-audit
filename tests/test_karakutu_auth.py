"""Arweave wallet auth — address derivation, the double-hash signing contract, token caching.

No network: httpx is stubbed. A throwaway RSA key is generated per test session; no real
wallet keyfile is read.
"""
import base64
import hashlib
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.clients import auth as auth_module
from src.clients.auth import ArweaveWalletAuth, WalletAuthError

CHALLENGE = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"   # synthetic challenge


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture(scope="module")
def wallet_jwk():
    """A throwaway RSA-4096 keyfile in Arweave's JWK shape (never a real wallet)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    n = key.private_numbers().public_numbers.n
    p = key.private_numbers()

    def enc(value: int) -> str:
        return b64e(value.to_bytes((value.bit_length() + 7) // 8, "big"))

    return {
        "kty": "RSA", "e": "AQAB", "n": enc(n),
        "d": enc(p.d), "p": enc(p.p), "q": enc(p.q),
        "dp": enc(p.dmp1), "dq": enc(p.dmq1), "qi": enc(p.iqmp),
    }


@pytest.fixture
def wallet_file(tmp_path, wallet_jwk):
    path = tmp_path / "wallet.json"
    path.write_text(json.dumps(wallet_jwk))
    return path


def make_auth(wallet_file, **kw) -> ArweaveWalletAuth:
    return ArweaveWalletAuth(wallet_path=str(wallet_file), base_url="https://api.example", **kw)


def stub_http(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(auth_module.httpx, "AsyncClient",
                        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))


# --- wallet ------------------------------------------------------------------


def test_availability_tracks_the_keyfile(wallet_file, tmp_path):
    assert make_auth(wallet_file).available is True
    assert ArweaveWalletAuth(wallet_path=str(tmp_path / "nope.json"),
                             base_url="https://x").available is False
    assert ArweaveWalletAuth(wallet_path="", base_url="https://x").available is False


def test_address_is_43_char_base64url_of_sha256_of_the_modulus(wallet_file, wallet_jwk):
    addr = make_auth(wallet_file).address

    raw_n = base64.urlsafe_b64decode(wallet_jwk["n"] + "=" * (-len(wallet_jwk["n"]) % 4))
    assert addr == b64e(hashlib.sha256(raw_n).digest())
    assert len(addr) == 43


def test_a_keyfile_missing_rsa_fields_is_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"kty": "RSA", "n": "abc", "e": "AQAB"}))  # no private parts

    with pytest.raises(WalletAuthError, match="missing RSA fields"):
        _ = ArweaveWalletAuth(wallet_path=str(bad), base_url="https://x").address


# --- the signing contract ----------------------------------------------------


def test_signature_matches_the_backends_double_hash_verification(wallet_file, wallet_jwk):
    """THE load-bearing test. The backend calls `verify(sig, sha256(challenge), PSS, SHA256())`
    — passing a digest as the *data* while also naming SHA256 as the algorithm, so the library
    hashes it AGAIN. Signing the raw challenge instead yields a silent 401. This asserts our
    signature verifies under exactly the backend's call (the double-hash gotcha in AGENTS.md)."""
    a = make_auth(wallet_file)
    a._load()
    signature = base64.urlsafe_b64decode(
        (sig := a._sign_challenge(CHALLENGE)) + "=" * (-len(sig) % 4))

    n = int.from_bytes(base64.urlsafe_b64decode(wallet_jwk["n"] + "=" * (-len(wallet_jwk["n"]) % 4)), "big")
    public = rsa.RSAPublicNumbers(65537, n).public_key()
    # Reproduce the backend's verification verbatim — this must NOT raise.
    public.verify(
        signature,
        hashlib.sha256(CHALLENGE.encode()).digest(),   # pre-computed digest passed as data
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),                                # ...and hashed again by verify()
    )


def test_signature_is_unpadded_base64url(wallet_file):
    a = make_auth(wallet_file)
    a._load()
    sig = a._sign_challenge(CHALLENGE)

    assert "=" not in sig and "+" not in sig and "/" not in sig
    assert len(sig) == 683, "RSA-4096 PSS signature, base64url-encoded"


# --- token lifecycle ---------------------------------------------------------


def _auth_handler(monkeypatch, *, authenticate_status=200, expires_in=900, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        if request.url.path == "/login":
            return httpx.Response(200, json={"challenge": CHALLENGE, "expires_in": 300})
        if request.url.path == "/authenticate":
            if authenticate_status != 200:
                return httpx.Response(authenticate_status, json={"detail": "nope"})
            return httpx.Response(200, json={"Result": "Success", "Role": "auditor",
                                             "access_token": "jwt-token",
                                             "expires_in": expires_in})
        return httpx.Response(404, json={})
    stub_http(monkeypatch, handler)


async def test_mints_a_token_via_challenge_then_authenticate(wallet_file, monkeypatch):
    calls = []
    _auth_handler(monkeypatch, calls=calls)

    token = await make_auth(wallet_file).get_token()

    assert token == "jwt-token"
    assert calls == ["/login", "/authenticate"]


async def test_the_token_is_cached_not_re_minted_each_call(wallet_file, monkeypatch):
    # /authenticate is rate-limited to 3/min per IP — re-minting per request would throttle us.
    calls = []
    _auth_handler(monkeypatch, calls=calls)
    a = make_auth(wallet_file)

    for _ in range(5):
        assert await a.get_token() == "jwt-token"

    assert calls == ["/login", "/authenticate"], "must mint exactly once"


async def test_a_token_near_expiry_is_re_minted(wallet_file, monkeypatch):
    # The margin exists so a request never starts with a token that dies mid-flight.
    calls = []
    _auth_handler(monkeypatch, expires_in=10, calls=calls)   # < EXPIRY_MARGIN_S
    a = make_auth(wallet_file)

    await a.get_token()
    monkeypatch.setattr(auth_module, "MIN_SECONDS_BETWEEN_MINTS", 0)
    await a.get_token()

    assert calls.count("/authenticate") == 2, "a short-lived token must be re-minted"


async def test_a_non_whitelisted_wallet_degrades_to_none(wallet_file, monkeypatch):
    # 403 "Not authorized in AO smart contract" — must not raise into the audit.
    _auth_handler(monkeypatch, authenticate_status=403)

    assert await make_auth(wallet_file).get_token() is None


async def test_an_unreachable_server_degrades_to_none(wallet_file, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("down")
    stub_http(monkeypatch, boom)

    assert await make_auth(wallet_file).get_token() is None


async def test_no_wallet_means_no_token_and_no_calls(tmp_path, monkeypatch):
    calls = []
    _auth_handler(monkeypatch, calls=calls)

    a = ArweaveWalletAuth(wallet_path=str(tmp_path / "absent.json"), base_url="https://x")

    assert await a.get_token() is None
    assert calls == [], "a missing keyfile must not hit the network"


# --- get_token never raises; keyfile from content ------------------------------------


@pytest.mark.parametrize("content", [
    pytest.param("not json at all {", id="not-json"),
    pytest.param("[1, 2, 3]", id="json-array"),
    pytest.param(json.dumps({"kty": "RSA", "n": "!!!", "e": "AQAB", "d": "x", "p": "x", "q": "x",
                             "dp": "x", "dq": "x", "qi": "x"}), id="bad-base64"),
    pytest.param(json.dumps({"kty": "RSA", "n": 5, "e": "AQAB", "d": "x", "p": "x", "q": "x",
                             "dp": "x", "dq": "x", "qi": "x"}), id="non-string-field"),
    pytest.param(json.dumps({"kty": "RSA", "n": "AQAB", "e": "AQAB", "d": "AQAB", "p": "AQAB",
                             "q": "AQAB", "dp": "AQAB", "dq": "AQAB", "qi": "AQAB"}), id="not-a-valid-key"),
])
async def test_a_garbled_keyfile_degrades_to_none_and_never_logs_its_content(
        tmp_path, monkeypatch, caplog, content):
    calls = []
    _auth_handler(monkeypatch, calls=calls)
    bad = tmp_path / "bad.json"
    bad.write_text(content)

    with caplog.at_level("DEBUG", logger="src.clients.auth"):
        assert await make_auth(bad).get_token() is None
        assert await ArweaveWalletAuth(wallet_path="", base_url="https://x",
                                       wallet_json=content).get_token() is None
    assert calls == []
    assert content not in caplog.text


def test_load_raises_only_wallet_auth_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"kty": "RSA", "n": "!!!", "e": "AQAB", "d": "x", "p": "x", "q": "x",
                               "dp": "x", "dq": "x", "qi": "x"}))
    with pytest.raises(WalletAuthError) as info:
        ArweaveWalletAuth(wallet_path=str(bad), base_url="https://x")._load()
    assert "!!!" not in str(info.value)


@pytest.mark.parametrize("where", ["/login", "/authenticate"])
async def test_an_html_200_from_a_wrong_base_url_degrades_to_none(wallet_file, monkeypatch, where):
    """karakutu.xyz serves a SPA catch-all: a wrong base URL answers 200 with HTML. That used
    to raise a JSONDecodeError straight through `get_token` (and crash the lifespan boot)."""
    def handler(request):
        if request.url.path == where:
            return httpx.Response(200, text="<!doctype html><html>app</html>",
                                  headers={"content-type": "text/html"})
        if request.url.path == "/login":
            return httpx.Response(200, json={"challenge": CHALLENGE})
        return httpx.Response(200, json={"access_token": "jwt-token", "expires_in": 900})
    stub_http(monkeypatch, handler)

    assert await make_auth(wallet_file).get_token() is None


async def test_a_non_numeric_expires_in_degrades_to_none(wallet_file, monkeypatch):
    stub_http(monkeypatch, lambda r: httpx.Response(
        200, json={"challenge": CHALLENGE} if r.url.path == "/login"
        else {"access_token": "t", "expires_in": "soon"}))
    assert await make_auth(wallet_file).get_token() is None


async def test_an_unexpected_error_inside_minting_degrades_to_none(wallet_file, monkeypatch):
    a = make_auth(wallet_file)

    async def explode():
        raise KeyError("anything at all")
    monkeypatch.setattr(a, "_mint", explode)
    assert await a.get_token() is None


async def test_unreachable_errors_are_logged_by_type_not_text(wallet_file, monkeypatch, caplog):
    def boom(request):
        raise httpx.ConnectError("connect to 10.9.8.7:443 failed: internal detail")
    stub_http(monkeypatch, boom)
    with caplog.at_level("WARNING", logger="src.clients.auth"):
        assert await make_auth(wallet_file).get_token() is None
    assert "ConnectError" in caplog.text and "10.9.8.7" not in caplog.text


async def test_the_keyfile_can_be_supplied_as_content(wallet_jwk, monkeypatch):
    calls = []
    _auth_handler(monkeypatch, calls=calls)
    a = ArweaveWalletAuth(wallet_path="", base_url="https://api.example",
                          wallet_json=json.dumps(wallet_jwk))
    assert a.available is True
    assert len(a.address) == 43
    assert await a.get_token() == "jwt-token"
    assert calls == ["/login", "/authenticate"]
    assert wallet_jwk["d"] not in repr(a.__dict__.get("_source", "")) and wallet_jwk["d"] not in a._source


def test_content_wins_over_the_path(wallet_jwk, tmp_path):
    other = tmp_path / "other.json"
    other.write_text("garbage")
    a = ArweaveWalletAuth(wallet_path=str(other), base_url="https://x",
                          wallet_json=json.dumps(wallet_jwk))
    assert len(a.address) == 43
