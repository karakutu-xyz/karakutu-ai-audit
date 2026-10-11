"""HTTP client for the Karakutu Server API (the archive's own backend).

Why this exists: the similarity stage has been dormant because it needs "a real similarity
backend AND an article store" (see `TODO(similarity)`). The archive itself is both — it
stores every article and scores similarity server-side via
`GET /articles/{id}/similar`. This client is the bridge.

Auth is an **Arweave wallet signature challenge** that mints a 15-minute JWT (see
`src/clients/auth.py`). Tokens are minted on demand and cached; a 401 mid-run re-mints once
and retries, so a run longer than the token lifetime self-heals.

**Reads everywhere; two explicit-opt-in writes.** Article endpoints are read-only by nature.
Since 2026-09-11 the client can also (a) PATCH the step-1 classification onto an audit record
and (b) post a discussion message — see the WRITES section below. Audit state, decisions,
evidence and relocation remain the human auditor's and are deliberately not implemented.

**Failure policy — every method degrades, none raises.** A lookup that fails returns `None`
(or `[]`), exactly like the search providers. The archive being unreachable must never 500
an audit or fabricate evidence; the pipeline simply proceeds as if the stage were dormant.
"""
import asyncio
import contextlib
import contextvars
import logging
import re
import time
from collections import OrderedDict
from typing import Iterator, List, Optional, Tuple

import httpx

from src.clients.auth import ArweaveWalletAuth
from src.clients.concepts import CATALOG_LOCALE, Concept
from src.clients.models import ArticleRecord, SimilarArticle, SimilarArticlesResult
from src.config.settings import get_settings

logger = logging.getLogger(__name__)

# Server-side caps from the spec; sending more is a 422.
MAX_SIMILAR_LIMIT = 20
MAX_DATE_RANGE_DAYS = 365
MAX_SUBJECT_TAGS = 20
MAX_COMMENT_CHARS = 10_000
NOTATION_RE = re.compile(r"^(medtop|genre):[A-Za-z0-9_-]+$")   # the API's own validator
GEOGRAPHIC_SCOPES = ("global_scope", "national", "regional", "platform_specific", "local")


# The step-4 window cache is scoped to ONE audit (2026-10-05). Batch runs built a
# fresh client per stage, so a window was never reused across audits; a long-lived client
# (the API's and the job worker's process singleton) must reproduce that, or a later
# audit could be judged against a window up to 30 min stale. Outside an `audit_scope()` the
# cache is bypassed entirely — every call reads the archive, exactly like the batch runs.
_AUDIT_SCOPE: contextvars.ContextVar[Optional[object]] = contextvars.ContextVar(
    "karakutu_audit_scope", default=None)


@contextlib.contextmanager
def audit_scope() -> Iterator[object]:
    """Let window reads inside this block (one audit) share a cache entry. Nothing cached in
    one scope is ever visible to another. Nesting starts a new, inner scope."""
    token = _AUDIT_SCOPE.set(object())
    try:
        yield _AUDIT_SCOPE.get()
    finally:
        _AUDIT_SCOPE.reset(token)


class KarakutuClient:
    """Client for the archive's API: reads everywhere, plus exactly two explicit-opt-in
    writes (`update_audit_classification`, `post_discussion` — see WRITES below).

    Only the endpoints this service needs are implemented (articles, similarity, concepts,
    the audit list/record/discussions). The API has 54 paths — broker, measurements,
    search — deliberately not wrapped until something actually calls them.
    """

    def __init__(self, base_url: Optional[str] = None, wallet_path: Optional[str] = None,
                 timeout: Optional[int] = None, auth: Optional[ArweaveWalletAuth] = None,
                 wallet_json: Optional[str] = None):
        settings = get_settings()
        self.base_url = (base_url if base_url is not None else settings.KARAKUTU_API_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.KARAKUTU_API_TIMEOUT
        wallet = wallet_path if wallet_path is not None else settings.KARAKUTU_WALLET_PATH
        # Keyfile CONTENT (e.g. from a secret manager) — wins over the path when set. An
        # explicit `wallet_path` pins the file and ignores the setting. `getattr` keeps this
        # working against a Settings class that predates KARAKUTU_WALLET_JSON.
        if wallet_json is None:
            wallet_json = getattr(settings, "KARAKUTU_WALLET_JSON", "") if wallet_path is None else ""
        # One auth object per client so the minted JWT is cached across calls — /authenticate
        # is rate-limited to 3/min per IP, so re-minting per request would get us throttled.
        self.auth = auth if auth is not None else ArweaveWalletAuth(
            wallet_path=wallet, base_url=self.base_url, timeout=self.timeout,
            wallet_json=wallet_json or "",
        )
        self._window_cache: "OrderedDict[tuple, Tuple[object, float, List[dict]]]" = OrderedDict()

    @property
    def enabled(self) -> bool:
        """False when no wallet keyfile is present — callers skip rather than fail.

        This is what keeps the service bootable with zero config: no wallet, no calls.
        """
        return self.auth.available

    async def _request(self, method: str, path: str, params: Optional[dict] = None,
                       json: Optional[dict] = None, _retried: bool = False) -> Optional[dict]:
        """Send one JSON request. Returns the decoded body, or None on ANY failure.

        On a 401 the token is re-minted once and the request retried — a 15-minute JWT can
        expire mid-run, and that must self-heal rather than fail the audit.
        """
        if not self.enabled:
            logger.debug("karakutu-api: skipped %s %s (no wallet keyfile configured)", method, path)
            return None
        token = await self.auth.get_token()
        if not token:
            logger.debug("karakutu-api: skipped %s %s (no token could be minted)", method, path)
            return None
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.request(method, url, params=params, json=json, headers=headers)
        except Exception as exc:  # noqa: BLE001 — unreachable, TLS, timeout, DNS…
            logger.warning("karakutu-api: %s %s unreachable (%s)", method, path,
                           type(exc).__name__)
            return None

        if resp.status_code == 401 and not _retried:
            # Token expired mid-flight (they last 15 min). Mint a fresh one and retry once.
            logger.info("karakutu-api: %s %s returned 401 — re-minting the JWT and retrying",
                        method, path)
            if await self.auth.get_token(force=True):
                return await self._request(method, path, params=params, json=json, _retried=True)
            return None
        if resp.status_code in (401, 403):
            logger.warning(
                "karakutu-api: %s %s returned %d — token rejected or wallet not authorized "
                "(check KARAKUTU_WALLET_PATH and that the address is whitelisted)",
                method, path, resp.status_code,
            )
            return None
        if resp.status_code == 404:
            logger.info("karakutu-api: %s %s not found (404)", method, path)
            return None
        if not resp.is_success:
            logger.warning("karakutu-api: %s %s returned HTTP %d: %s", method, path,
                           resp.status_code, resp.text[:200])
            return None

        try:
            return resp.json()
        except ValueError as exc:
            logger.warning("karakutu-api: %s %s returned undecodable JSON (%s)", method, path,
                           type(exc).__name__)
            return None

    async def _get(self, path: str, params: Optional[dict] = None,
                   _retried: bool = False) -> Optional[dict]:
        """GET a JSON endpoint. Returns the decoded body, or None on ANY failure."""
        return await self._request("GET", path, params=params, _retried=_retried)

    # --- articles -------------------------------------------------------------

    async def get_article_by_url(self, url: str) -> Optional[ArticleRecord]:
        """Look up an archived article by its URL (`GET /articles?url=…`).

        This is the entry point for everything else: an audit request carries a URL, and the
        similarity endpoint needs a numeric id. Returns None when the archive has no record
        of the URL (which is normal — not every audited URL is archived).
        """
        body = await self._get("/articles", params={"url": url})
        if body is None:
            return None
        try:
            return ArticleRecord.model_validate(body)
        except Exception as exc:  # noqa: BLE001 — a shape change must not break the audit
            logger.warning("karakutu-api: could not parse article for %s: %s", url,
                           type(exc).__name__)
            return None

    async def get_article(self, article_id: int,
                          expand: Optional[List[str]] = None) -> Optional[ArticleRecord]:
        """Fetch one archived article by id (`GET /articles/{id}`).

        `expand` accepts the spec's values: `versions`, `checks`, `redirect`.
        """
        params = {"expand": expand} if expand else None
        body = await self._get(f"/articles/{article_id}", params=params)
        if body is None:
            return None
        try:
            return ArticleRecord.model_validate(body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("karakutu-api: could not parse article %s: %s", article_id,
                           type(exc).__name__)
            return None

    async def get_similar_articles(self, article_id: int, limit: int = 5,
                                   date_range_days: int = 30) -> List[SimilarArticle]:
        """Server-scored similar articles (`GET /articles/{id}/similar`).

        Returns `[]` on any failure — an unavailable archive yields no evidence, never an
        error. `limit` and `date_range_days` are clamped to the spec's maxima so a caller
        cannot turn a bad argument into a 422.
        """
        params = {
            "limit": max(1, min(limit, MAX_SIMILAR_LIMIT)),
            "date_range_days": max(1, min(date_range_days, MAX_DATE_RANGE_DAYS)),
        }
        body = await self._get(f"/articles/{article_id}/similar", params=params)
        if body is None:
            return []
        try:
            return SimilarArticlesResult.model_validate(body).articles
        except Exception as exc:  # noqa: BLE001
            logger.warning("karakutu-api: could not parse similar articles for %s: %s",
                           article_id, type(exc).__name__)
            return []

    async def list_concepts(self, scheme: str, locale: str = CATALOG_LOCALE) -> List[Concept]:
        """The IPTC vocabulary (`GET /concepts?scheme=…&locale=…`). `[]` on any failure.

        `scheme` is `iptc_mediatopic` or `iptc_genre` (the endpoint 422s otherwise). The
        endpoint needs the JWT like everything else — `_get` handles it.
        """
        body = await self._get("/concepts", params={"scheme": scheme, "locale": locale})
        if body is None:
            return []
        items = body.get("concepts") if isinstance(body, dict) else None
        if not isinstance(items, list):
            logger.warning("karakutu-api: /concepts returned an unexpected shape for %s", scheme)
            return []
        try:
            return [Concept.model_validate(c) for c in items]
        except Exception as exc:  # noqa: BLE001
            logger.warning("karakutu-api: could not parse concepts for %s: %s", scheme,
                           type(exc).__name__)
            return []

    async def find_similar_by_url(self, url: str, limit: int = 5,
                                  date_range_days: int = 30) -> List[SimilarArticle]:
        """URL → archived article → its similar articles, in one call for the pipeline.

        The two-step (`/articles?url=` then `/articles/{id}/similar`) is what the API
        requires; this wraps it so the workflow only has to know about URLs. Returns `[]`
        when the archive has no record of the URL or is unreachable.
        """
        record = await self.get_article_by_url(url)
        if record is None:
            logger.info("karakutu-api: no archived article for %s — no similarity lookup", url)
            return []
        similar = await self.get_similar_articles(
            record.id, limit=limit, date_range_days=date_range_days,
        )
        logger.info("karakutu-api: %d similar article(s) for %s (archive id %d)",
                    len(similar), url, record.id)
        return similar

    async def get_article_with_body(self, url: str) -> Optional[ArticleRecord]:
        """Article by URL WITH its snapshots, so the body can be recovered.

        A removed article's top-level `news_text` is usually empty (the last fetch 404'd);
        the text survives only in `versions[]`. Callers should use `longest_body()`.
        """
        body = await self._get("/articles", params={"url": url, "expand": ["versions"]})
        if body is None:
            return None
        try:
            return ArticleRecord.model_validate(body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("karakutu-api: could not parse article+versions for %s: %s", url,
                           type(exc).__name__)
            return None

    # --- audits: the removal window (step 4, related removals) --------------------------

    async def list_audits(self, status: int, *, date_from: int, date_to: int,
                          page_size: int = 100, max_pages: int = 30) -> Optional[List[dict]]:
        """Every audit in one status created in [date_from, date_to] (unix seconds), public view,
        newest first (`GET /audits?view=public&status=&date_from=&date_to=`). None when the
        first page could not be read; [] when there are none. A failure on a LATER page
        returns the rows read so far (historical behaviour — see `_list_audits`)."""
        rows, _complete = await self._list_audits(status, date_from=date_from, date_to=date_to,
                                                  page_size=page_size, max_pages=max_pages)
        return rows

    async def _list_audits(self, status: int, *, date_from: int, date_to: int,
                           page_size: int = 100, max_pages: int = 30) -> Tuple[Optional[List[dict]], bool]:
        """`list_audits` plus whether the read was COMPLETE (every page read, no failure).
        Only a complete read may be cached."""
        rows: List[dict] = []
        for page in range(1, max_pages + 1):
            body = await self._get("/audits", params={
                "view": "public", "status": status, "date_from": int(date_from), "date_to": int(date_to),
                "page": page, "page_size": page_size, "sort_by": "created_at", "sort_order": "desc"})
            if body is None:
                return (None if page == 1 else rows), False
            items = body.get("items") or []
            for it in items:
                art = it.get("article") or {}
                rows.append({"audit_id": it.get("id"), "article_id": art.get("id"), "url": art.get("link") or "",
                             "title": art.get("title") or "", "newspaper": art.get("newspaper") or "",
                             "status": it.get("status", status), "decision": it.get("decision"),
                             "created_at": it.get("created_at"), "publish_date": art.get("publish_date")})
            if len(items) < page_size:
                return rows, True
        # max_pages exhausted with full pages: there may be more — not a complete read.
        return rows, False

    WINDOW_CACHE_TTL = 1800   # seconds, within one audit scope
    WINDOW_CACHE_MAX = 8      # entries; bounds a long-lived client's memory

    async def audits_created_between(self, date_from: int, date_to: int,
                                     statuses=(0, 1, 2, 3, 4)) -> Optional[List[dict]]:
        """All audits created in the window across the given statuses — the removal WINDOW the
        related-removals step compares against. None when the archive could not be read at all.

        Caching: only inside an `audit_scope()`, keyed by that scope and the EXACT
        window, and only for a COMPLETE read (every status, every page). Outside a scope —
        which is every caller today — each call reads the archive, i.e. the batch semantics:
        a fresh window per audit. Partial results are still returned (unchanged), never
        stored."""
        statuses = tuple(statuses)
        scope = _AUDIT_SCOPE.get()
        key = (id(scope), int(date_from), int(date_to), statuses) if scope is not None else None
        if key is not None:
            hit = self._window_cache.get(key)
            if hit and hit[0] is scope and time.time() - hit[1] < self.WINDOW_CACHE_TTL:
                return list(hit[2])
        rows: List[dict] = []
        any_ok = False
        complete = True
        for st in statuses:
            part, part_complete = await self._list_audits(st, date_from=date_from, date_to=date_to)
            complete = complete and part_complete
            if part is not None:
                any_ok = True
                rows.extend(part)
        if not any_ok:
            return None
        if key is not None and complete:
            # The scope object itself is stored so a recycled id() can never match.
            self._window_cache[key] = (scope, time.time(), list(rows))
            self._window_cache.move_to_end(key)
            while len(self._window_cache) > self.WINDOW_CACHE_MAX:
                self._window_cache.popitem(last=False)
        return rows

    async def first_dead_check(self, article_id: int) -> Optional[int]:
        """The DETECTION moment: the first 404/410/451 check after the last 200 in the article's
        check history (`GET /articles/{id}?expand=checks`). None when unknown."""
        body = await self._get(f"/articles/{article_id}", params={"expand": ["checks"]})
        entries = ((body or {}).get("checks") or {}).get("entries") or []
        entries = sorted((e for e in entries if e.get("check_date")), key=lambda e: e["check_date"])
        first_dead = None
        for e in entries:
            st = e.get("status")
            if st == 200:
                first_dead = None
            elif st in (404, 410, 451) and first_dead is None:
                first_dead = e["check_date"]
        return first_dead

    # --- audits: reads used by the write guards ----------------------------

    async def get_audit(self, audit_id: int) -> Optional[dict]:
        """`GET /audits/{id}` — the audit record (status, decision, tag fields…). None on failure."""
        return await self._get(f"/audits/{audit_id}")

    async def get_audit_by_article(self, article_id: int) -> Tuple[Optional[int], Optional[dict]]:
        """`GET /audits/by-article/{article_id}`: the article's audit (the archive keeps one per
        article), same shape as `GET /audits/{id}`, with the HTTP status kept (`get_with_status`):
        `(200, audit)`, `(404, body)` when the article has no audit, `(None, {"error": …})` when
        the archive could not be read. The caller must still check `audit["article"]["id"]`."""
        return await self.get_with_status(f"/audits/by-article/{int(article_id)}")

    async def get_with_status(self, path: str, params: Optional[dict] = None, *,
                              attempts: int = 4, backoff_s: float = 3.0) -> Tuple[Optional[int], Optional[dict]]:
        """GET that keeps the HTTP status, so a caller can tell **gone (404)** from **unreachable**.

        `_request` collapses both to None, which is right for evidence lookups but wrong for a
        pre-check that must decide "skip, the audit was deleted" vs "retry later". Semantics are
        those of `scripts/open_audit/probe_audits.py::get` (moved here, additive):

          * any status < 500 is final → `(status, decoded body or None)`; a 401 on the first
            attempt re-mints the JWT once and retries;
          * a 5xx or a transport error is retried up to `attempts` times with a `backoff_s × n`
            sleep → `(None, {"error": "<ExceptionType>" | "HTTP <code>"})` when all fail;
          * disabled client / no token → `(None, {"error": "disabled"|"no_token"})`.

        Never raises, and never puts raw exception text in the result (only the type name).
        """
        if not self.enabled:
            return None, {"error": "disabled"}
        err = "unreachable"
        for attempt in range(1, attempts + 1):
            token = await self.auth.get_token()
            if not token:
                return None, {"error": "no_token"}
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.get(f"{self.base_url}{path}", params=params,
                                            headers={"Authorization": f"Bearer {token}",
                                                     "Accept": "application/json"})
            except Exception as exc:  # noqa: BLE001 — unreachable, TLS, timeout, DNS…
                err = type(exc).__name__
                logger.warning("karakutu-api: GET %s unreachable (%s), attempt %d/%d", path, err,
                               attempt, attempts)
            else:
                if resp.status_code == 401 and attempt == 1:
                    await self.auth.get_token(force=True)
                    continue
                if resp.status_code < 500:
                    try:
                        body = resp.json()
                    except ValueError:
                        body = None
                    return resp.status_code, body
                err = f"HTTP {resp.status_code}"
                logger.warning("karakutu-api: GET %s returned %s, attempt %d/%d", path, err,
                               attempt, attempts)
            if attempt < attempts:
                await asyncio.sleep(backoff_s * attempt)
        return None, {"error": err}

    async def count_discussions(self, audit_id: int) -> Optional[int]:
        """Number of discussion messages on an audit (`GET /audits/{id}/discussions` → total).

        None when the count could not be read — callers must treat None as "unknown", not 0,
        or a flaky read would let a second comment through.
        """
        body = await self._get(f"/audits/{audit_id}/discussions", params={"page_size": 1})
        if not isinstance(body, dict) or not isinstance(body.get("total"), int):
            return None
        return body["total"]

    # --- WRITES — explicit opt-in (2026-09-11). Everything above only reads. ------------
    #
    # The archive keeps the IPTC fields on the AUDIT record, not on the article (the article
    # endpoints are read-only), so "set the tags on the article" means PATCH /audits/{id}
    # (AuditUpdate: last-write-wins per field, AO-whitelisted wallets only). Only the three
    # classification fields are ever sent from here — never status, decision or new_url;
    # those remain the human auditor's. Both writes degrade to False/None, never raise.

    async def update_audit_classification(self, audit_id: int, *,
                                          subject_tag_notations: List[str],
                                          content_type_notation: Optional[str],
                                          geographic_scope: Optional[str]) -> bool:
        """Write step-1's classification onto the audit (`PATCH /audits/{id}`).

        Notations are validated against the API's own pattern before sending; an invalid one
        aborts the write (nothing partial is sent). Returns True only on a 2xx.
        """
        tags = [t for t in subject_tag_notations if t]
        if not tags and not content_type_notation and not geographic_scope:
            logger.info("karakutu-api: nothing to write for audit %s (empty classification)", audit_id)
            return False
        bad = [t for t in tags + ([content_type_notation] if content_type_notation else [])
               if not NOTATION_RE.match(t)]
        if bad:
            logger.warning("karakutu-api: refusing to PATCH audit %s — invalid notation(s) %s",
                           audit_id, bad)
            return False
        if geographic_scope is not None and geographic_scope not in GEOGRAPHIC_SCOPES:
            logger.warning("karakutu-api: refusing to PATCH audit %s — unknown geographic_scope %r",
                           audit_id, geographic_scope)
            return False
        payload: dict = {"subject_tag_notations": tags[:MAX_SUBJECT_TAGS]}
        if content_type_notation:
            payload["content_type_notation"] = content_type_notation
        if geographic_scope:
            payload["geographic_scope"] = geographic_scope
        body = await self._request("PATCH", f"/audits/{audit_id}", json=payload)
        ok = body is not None
        logger.info("karakutu-api: PATCH audit %s classification → %s", audit_id,
                    "ok" if ok else "FAILED")
        return ok

    async def post_discussion(self, audit_id: int, comment: str,
                              parent_id: Optional[int] = None) -> Optional[dict]:
        """Post one discussion message (`POST /audits/{id}/discussions`, 201 → the message).

        `parent_id` makes it a threaded REPLY under an existing message (the API's only way to
        follow up: discussion text cannot be edited — PATCH accepts `resolved` alone).
        Refuses an empty or over-long comment locally (the API's limit is 10,000 chars).
        Returns the created message dict, or None on any failure.
        """
        text = (comment or "").strip()
        if not text or len(text) > MAX_COMMENT_CHARS:
            logger.warning("karakutu-api: refusing to post to audit %s — comment length %d",
                           audit_id, len(text))
            return None
        payload = {"comment": text}
        if parent_id is not None:
            payload["parent_id"] = parent_id
        body = await self._request("POST", f"/audits/{audit_id}/discussions", json=payload)
        if body is None:
            return None
        logger.info("karakutu-api: posted discussion %s on audit %s", body.get("id"), audit_id)
        return body

    # --- health ---------------------------------------------------------------

    async def check_health(self) -> bool:
        """GET /health (unauthenticated in the spec). Never raises.

        A 2xx is NOT enough: `karakutu.xyz` serves a single-page app on a catch-all route, so
        a wrong base URL returns 200 with the SPA's HTML and a naive `is_success` check calls
        that "healthy". Require a JSON content-type so this answers the question actually
        being asked — "is the API behind this URL?" — not "did something reply?".
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.base_url}/health")
        except Exception:  # noqa: BLE001
            return False
        if not resp.is_success:
            return False
        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            logger.warning(
                "karakutu-api: %s/health replied %d but with content-type %r, not JSON — the "
                "base URL is probably wrong (a frontend catch-all, not the API)",
                self.base_url, resp.status_code, content_type or "<none>",
            )
            return False
        return True


def get_client() -> KarakutuClient:
    """Build a client from settings (reads them at call time, like the providers do)."""
    return KarakutuClient()
