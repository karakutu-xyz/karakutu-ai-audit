"""Arweave wallet authentication for the Karakutu Server API.

The backend issues **15-minute** JWTs via an Arweave wallet signature challenge. A token
pasted into `.env` is therefore useless in practice — it dies mid-audit-run. This module
mints tokens from the wallet keyfile instead and caches them until just before expiry.

Flow (backend guide §5): `GET /login?wallet_address=` → sign the challenge → `POST
/authenticate` → JWT.

**The double-hash trap.** The backend verifies with `public_key.verify(sig, digest, PSS,
hashes.SHA256())` — passing a pre-computed SHA-256 digest as the *data* while also naming
SHA256 as the algorithm, so the library hashes it a second time. The on-wire contract is
therefore: sign `SHA-256(challenge)` and let your signer add the outer SHA-256. Getting this
wrong yields a silent 401 "Authentication failed" (the double-hash gotcha in AGENTS.md).
"""
import asyncio
import base64
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

logger = logging.getLogger(__name__)

# The server issues 900s tokens. Re-mint this many seconds early so a request never starts
# with a token that expires mid-flight.
EXPIRY_MARGIN_S = 120
# /login is rate-limited to 5/min and /authenticate to 3/min per IP, so minting must be rare.
# Caching is not an optimisation here — hammering re-auth would get us throttled.
MIN_SECONDS_BETWEEN_MINTS = 5


def _b64url_decode(value: str) -> bytes:
    value = value.replace("-", "+").replace("_", "/")
    return base64.b64decode(value + "=" * (-len(value) % 4))


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class WalletAuthError(RuntimeError):
    """Authentication could not be completed (bad keyfile, not whitelisted, server down)."""


def _json_field(resp: httpx.Response, field: str, endpoint: str):
    """`resp.json()[field]`, or WalletAuthError when the body is not a JSON object — a wrong
    base URL answers 200 with the frontend's HTML (see `KarakutuClient.check_health`)."""
    try:
        body = resp.json()
    except ValueError:
        raise WalletAuthError(f"{endpoint} returned a non-JSON body") from None
    if not isinstance(body, dict):
        raise WalletAuthError(f"{endpoint} returned an unexpected JSON shape")
    return body.get(field)


class ArweaveWalletAuth:
    """Mints and caches a Karakutu JWT from an Arweave RSA-4096 keyfile.

    The keyfile is private key material: it is read from disk (`wallet_path`) or handed over
    as content (`wallet_json`, e.g. from a secret manager — it wins when both are set) and is
    never logged, nor is any exception text raised while parsing it. Only the wallet address
    (public) appears in logs.
    """

    def __init__(self, wallet_path: str, base_url: str, timeout: int = 15,
                 wallet_json: str = ""):
        self.wallet_path = Path(wallet_path)
        # Held privately and never echoed (repr, logs, error messages).
        self._wallet_json = (wallet_json or "").strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._jwk: Optional[dict] = None
        self._key: Optional[rsa.RSAPrivateKey] = None
        self._address: Optional[str] = None
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._last_mint_attempt: float = 0.0
        # Serialises minting across concurrent audits. The cache check below is a
        # check-then-act: without this, N requests arriving on a cold cache all see "no
        # token" and all mint, which is exactly the burst /authenticate (3/min per IP)
        # rejects. Created lazily so constructing the client outside a running loop is safe.
        self._mint_lock: Optional[asyncio.Lock] = None

    # --- wallet ---------------------------------------------------------------

    @property
    def available(self) -> bool:
        """True when keyfile content was given, or a keyfile is configured and on disk."""
        if self._wallet_json:
            return True
        return bool(str(self.wallet_path).strip()) and self.wallet_path.is_file()

    @property
    def _source(self) -> str:
        """Where the key comes from, for messages — never the content itself."""
        return "wallet JSON (content)" if self._wallet_json else f"wallet keyfile {self.wallet_path}"

    def _load(self) -> None:
        """Parse the keyfile into an RSA key. Raises WalletAuthError ONLY — never a raw
        JSON/base64/cryptography error, and never with exception text (a decode error can
        quote key material)."""
        if self._key is not None:
            return
        try:
            raw = self._wallet_json or self.wallet_path.read_text()
            jwk = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            raise WalletAuthError(
                f"could not read {self._source} ({type(exc).__name__})") from None
        if not isinstance(jwk, dict):
            raise WalletAuthError(f"{self._source} is not a JSON object")

        missing = {"kty", "n", "e", "d", "p", "q", "dp", "dq", "qi"} - set(jwk)
        if missing:
            raise WalletAuthError(f"{self._source} is missing RSA fields: {sorted(missing)}")

        def num(field: str) -> int:
            return int.from_bytes(_b64url_decode(jwk[field]), "big")

        try:
            public = rsa.RSAPublicNumbers(num("e"), num("n"))
            key = rsa.RSAPrivateNumbers(
                num("p"), num("q"), num("d"), num("dp"), num("dq"), num("qi"), public,
            ).private_key()
            # Arweave address = base64url(SHA-256(raw modulus bytes)) — always 43 chars.
            address = _b64url_encode(hashlib.sha256(_b64url_decode(jwk["n"])).digest())
        except Exception as exc:  # noqa: BLE001 — binascii.Error, ValueError, TypeError…
            raise WalletAuthError(
                f"{self._source} holds an invalid RSA key ({type(exc).__name__})") from None
        self._key, self._jwk, self._address = key, jwk, address

    @property
    def address(self) -> str:
        """The wallet's 43-char Arweave address (public — safe to log). Raises WalletAuthError
        (only) when the keyfile cannot be loaded."""
        self._load()
        return self._address or ""

    # --- token ----------------------------------------------------------------

    def _sign_challenge(self, challenge: str) -> str:
        """Sign SHA-256(challenge); the signer adds the outer SHA-256 (see module docstring)."""
        assert self._key is not None
        pre_hash = hashlib.sha256(challenge.encode("utf-8")).digest()
        signature = self._key.sign(
            pre_hash,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256(),
        )
        return _b64url_encode(signature)

    async def _mint(self) -> str:
        """Run the full challenge → sign → authenticate flow. Raises WalletAuthError."""
        self._load()
        address = self._address
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.get(f"{self.base_url}/login",
                                        params={"wallet_address": address})
            except Exception as exc:  # noqa: BLE001
                raise WalletAuthError(f"/login unreachable: {type(exc).__name__}") from None
            if resp.status_code != 200:
                raise WalletAuthError(f"/login returned HTTP {resp.status_code}: {resp.text[:200]}")
            challenge = _json_field(resp, "challenge", "/login")
            if not challenge:
                raise WalletAuthError("/login returned no challenge")

            # The challenge is single-use and expires in 300s — sign and submit immediately,
            # and never retry /authenticate with the same one.
            payload = {
                "wallet_address": address,
                "challenge": challenge,
                "signature": self._sign_challenge(challenge),
                "public_jwk": {"kty": "RSA", "e": self._jwk["e"], "n": self._jwk["n"]},
            }
            try:
                resp = await client.post(f"{self.base_url}/authenticate", json=payload)
            except Exception as exc:  # noqa: BLE001
                raise WalletAuthError(
                    f"/authenticate unreachable: {type(exc).__name__}") from None

        if resp.status_code == 403:
            raise WalletAuthError(
                f"wallet {address} is not whitelisted in the AO coordinator "
                f"(HTTP 403: {resp.text[:150]})"
            )
        if resp.status_code != 200:
            raise WalletAuthError(f"/authenticate returned HTTP {resp.status_code}: "
                                  f"{resp.text[:200]}")

        token = _json_field(resp, "access_token", "/authenticate")
        if not token:
            raise WalletAuthError("/authenticate returned no access_token")
        body = resp.json()
        try:
            expires_in = int(body.get("expires_in") or 900)
        except (TypeError, ValueError):
            raise WalletAuthError("/authenticate returned a non-numeric expires_in") from None
        self._token = token
        self._expires_at = time.monotonic() + expires_in
        logger.info("karakutu-auth: minted JWT for %s (role=%s, expires_in=%ds)",
                    address, body.get("Role"), expires_in)
        return token

    async def get_token(self, force: bool = False) -> Optional[str]:
        """A valid JWT, minting or re-minting as needed. None when auth is unavailable.

        Never raises — not on an unreadable/garbled keyfile, not on an HTML 200 from a wrong
        base URL (the SPA catch-all), not on anything else: callers (`KarakutuClient._request`,
        `load_catalog` at boot) treat a missing token the same as a disabled client, so an
        unreachable archive degrades instead of breaking an audit or the boot.
        """
        if not self.available:
            logger.debug("karakutu-auth: no wallet keyfile at %s", self.wallet_path)
            return None
        if not force and self._token and time.monotonic() < self._expires_at - EXPIRY_MARGIN_S:
            return self._token
        if self._mint_lock is None:
            self._mint_lock = asyncio.Lock()
        async with self._mint_lock:
            # Re-check inside the lock: whoever held it may have just minted for us, in which
            # case this request rides that token instead of spending another mint on it.
            if not force and self._token and time.monotonic() < self._expires_at - EXPIRY_MARGIN_S:
                return self._token
            # Guard the rate limit (/authenticate is 3/min per IP): if a mint was just
            # attempted, reuse whatever we have rather than hammering the endpoint.
            if time.monotonic() - self._last_mint_attempt < MIN_SECONDS_BETWEEN_MINTS:
                return self._token
            self._last_mint_attempt = time.monotonic()
            try:
                return await self._mint()
            except WalletAuthError as exc:
                # Our own composed message: no key material, no raw exception text.
                logger.warning("karakutu-auth: could not obtain a token: %s", exc)
            except Exception as exc:  # noqa: BLE001 — the "never raises" contract is absolute
                logger.warning("karakutu-auth: could not obtain a token (%s)", type(exc).__name__)
            self._token = None
            return None
