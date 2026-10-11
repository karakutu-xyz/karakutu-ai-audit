"""Karakutu Server API client — request shape and failure degradation.

No network: `httpx.AsyncClient` is stubbed via MockTransport, so every request is answered
in-process. The load-bearing property throughout: **the client never raises.** An audit must
not 500 because the archive is down, and it must never fabricate evidence.
"""
import httpx
import pytest

from src.clients import karakutu as karakutu_module
from src.clients.karakutu import KarakutuClient
from src.clients.models import ArticleRecord, SimilarArticle

TOKEN = "test.jwt.token"


class FakeAuth:
    """Stands in for ArweaveWalletAuth: no keyfile, no network, deterministic token."""

    def __init__(self, token=TOKEN, available=True):
        self._token, self.available = token, available
        self.mints = 0

    async def get_token(self, force: bool = False):
        if not self.available:
            return None
        if force:
            self.mints += 1
        return self._token

ARTICLE_BODY = {
    "id": 4242,
    "url": "https://t24.com.tr/haber/x,1",
    "title": "Bir haber",
    "description": "d",
    "news_text": "gövde metni",
    "status": 200,
    "newspaper": {"slug": "t24", "display_name": "T24", "country": "TR"},
    "authors": [{"id": 12, "name": "Muhabir"}],
    # fields we do not model must be ignored, not raise
    "content_hash": "abc", "change_metrics": None, "access_right": "open_public",
}

SIMILAR_BODY = {
    "total": 2,
    "articles": [
        {"id": 1, "link": "https://birgun.net/haber/a", "title": "A",
         "similarity_score": 0.91, "url_similarity": 0.8, "title_similarity": 0.95,
         "date_proximity_days": 1, "date_proximity_score": 0.99},
        {"id": 2, "link": "https://evrensel.net/haber/b", "title": "B",
         "similarity_score": 0.72, "url_similarity": 0.6, "title_similarity": 0.8,
         "date_proximity_days": 3, "date_proximity_score": 0.9},
    ],
}


@pytest.fixture
def api(monkeypatch):
    """Route client requests to an in-process handler. Returns a dict capturing the request
    and letting the test set the response."""
    box = {"routes": {}, "requests": [], "status": 200, "body": None, "raises": None}
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        box["requests"].append(request)
        if box["raises"] is not None:
            raise box["raises"]
        path = request.url.path
        if path in box["routes"]:
            status, body = box["routes"][path]
            return httpx.Response(status, json=body)
        if box["body"] is not None or box["status"] != 200:
            return httpx.Response(box["status"], json=box["body"] or {})
        return httpx.Response(404, json={})

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(karakutu_module.httpx, "AsyncClient", make_client)
    return box


def client(auth=None, **kw) -> KarakutuClient:
    return KarakutuClient(base_url="https://api.example", timeout=5,
                          auth=auth or FakeAuth(), **kw)


# --- enablement --------------------------------------------------------------


def test_client_is_disabled_without_a_wallet():
    # Zero-config default: no keyfile → the client makes no calls at all.
    assert KarakutuClient(base_url="https://api.example", timeout=5,
                          auth=FakeAuth(available=False)).enabled is False


def test_client_is_enabled_with_a_wallet():
    assert client().enabled is True


async def test_a_disabled_client_makes_no_request_and_returns_none(api):
    disabled = KarakutuClient(base_url="https://api.example", timeout=5,
                              auth=FakeAuth(available=False))

    assert await disabled.get_article_by_url("https://x/y") is None
    assert await disabled.get_similar_articles(1) == []
    assert api["requests"] == [], "a client with no token must not call the API"


# --- request shape -----------------------------------------------------------


async def test_lookup_by_url_sends_the_bearer_token_and_url_param(api):
    api["routes"]["/articles"] = (200, ARTICLE_BODY)

    record = await client().get_article_by_url("https://t24.com.tr/haber/x,1")

    req = api["requests"][0]
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert dict(req.url.params)["url"] == "https://t24.com.tr/haber/x,1"
    assert isinstance(record, ArticleRecord)
    assert record.id == 4242 and record.newspaper.slug == "t24"


async def test_similar_articles_are_parsed_and_link_is_the_url(api):
    api["routes"]["/articles/4242/similar"] = (200, SIMILAR_BODY)

    similar = await client().get_similar_articles(4242, limit=5)

    assert [s.link for s in similar] == ["https://birgun.net/haber/a",
                                         "https://evrensel.net/haber/b"]
    assert all(isinstance(s, SimilarArticle) for s in similar)
    assert similar[0].similarity_score == 0.91


async def test_limit_and_date_range_are_clamped_to_the_spec_maxima(api):
    # Over-max values are a 422 server-side; clamp so a caller can't turn a bad argument
    # into a failed lookup.
    api["routes"]["/articles/7/similar"] = (200, {"total": 0, "articles": []})

    await client().get_similar_articles(7, limit=999, date_range_days=99999)

    params = dict(api["requests"][0].url.params)
    assert params["limit"] == "20"
    assert params["date_range_days"] == "365"


async def test_find_similar_by_url_chains_lookup_then_similar(api):
    # The API has no "similar by URL" — it must be resolved URL → id → similar.
    api["routes"]["/articles"] = (200, ARTICLE_BODY)
    api["routes"]["/articles/4242/similar"] = (200, SIMILAR_BODY)

    similar = await client().find_similar_by_url("https://t24.com.tr/haber/x,1")

    assert [r.url.path for r in api["requests"]] == ["/articles", "/articles/4242/similar"]
    assert len(similar) == 2


# --- degradation: the client must never raise --------------------------------


async def test_unknown_url_returns_none_not_an_error(api):
    api["status"] = 404

    assert await client().get_article_by_url("https://x/never-archived") is None


async def test_find_similar_returns_empty_when_the_url_is_not_archived(api):
    api["status"] = 404

    assert await client().find_similar_by_url("https://x/never-archived") == []
    assert len(api["requests"]) == 1, "must not attempt the similar call without an id"


@pytest.mark.parametrize("status", [401, 403])
async def test_an_expired_or_unauthorized_token_degrades_quietly(api, status):
    # A bad JWT must not 500 the audit — it yields no evidence, like an unreachable search.
    api["status"] = status

    assert await client().get_article_by_url("https://x/y") is None
    assert await client().get_similar_articles(1) == []


@pytest.mark.parametrize("status", [422, 500, 503])
async def test_server_errors_degrade_quietly(api, status):
    api["status"] = status

    assert await client().get_article_by_url("https://x/y") is None
    assert await client().get_similar_articles(1) == []


async def test_an_unreachable_archive_degrades_quietly(api):
    api["raises"] = httpx.ConnectError("connection refused")

    assert await client().get_article_by_url("https://x/y") is None
    assert await client().get_similar_articles(1) == []
    assert await client().find_similar_by_url("https://x/y") == []


async def test_a_malformed_payload_degrades_quietly(api):
    # A shape change server-side must not break an audit.
    api["routes"]["/articles"] = (200, {"unexpected": "shape"})

    assert await client().get_article_by_url("https://x/y") is None


async def test_unknown_response_fields_are_ignored_not_fatal(api):
    api["routes"]["/articles"] = (200, {**ARTICLE_BODY, "brand_new_column": [1, 2, 3]})

    record = await client().get_article_by_url("https://x/y")

    assert record is not None and record.id == 4242


async def test_health_never_raises(api):
    api["raises"] = httpx.ConnectError("down")

    assert await client().check_health() is False


def _stub_transport(monkeypatch, handler):
    """Point the client's httpx at an in-process handler (monkeypatched, so it is undone)."""
    real = httpx.AsyncClient
    monkeypatch.setattr(
        karakutu_module.httpx, "AsyncClient",
        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}),
    )


async def test_health_rejects_a_non_json_200(monkeypatch):
    """A 2xx alone is not health. karakutu.xyz serves a single-page app on a catch-all, so a
    wrong base URL returns 200 + HTML; treating that as healthy hides a misconfigured URL —
    it did exactly that against the real deployment before this check existed."""
    _stub_transport(monkeypatch, lambda r: httpx.Response(
        200, text="<!DOCTYPE html><html>SPA</html>", headers={"content-type": "text/html"}))

    assert await client().check_health() is False


async def test_health_accepts_a_json_200(monkeypatch):
    _stub_transport(monkeypatch, lambda r: httpx.Response(200, json={"status": "ok"}))

    assert await client().check_health() is True


async def test_health_is_false_on_a_502(monkeypatch):
    # nginx proxying /api to a downed upstream — what the live deployment returns today.
    _stub_transport(monkeypatch, lambda r: httpx.Response(
        502, text="<html>502 Bad Gateway</html>", headers={"content-type": "text/html"}))

    assert await client().check_health() is False


# --- token lifecycle ---------------------------------------------------------


async def test_a_401_re_mints_the_token_and_retries_once(monkeypatch):
    """JWTs last 15 minutes and a trace run is longer, so a token WILL expire mid-run.
    That must self-heal rather than fail the audit."""
    auth = FakeAuth()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, json={"detail": "Token has expired"})
        return httpx.Response(200, json=ARTICLE_BODY)

    _stub_transport(monkeypatch, handler)

    record = await client(auth=auth).get_article_by_url("https://x/y")

    assert calls["n"] == 2, "should retry once after re-minting"
    assert auth.mints == 1, "should have force-minted a fresh token"
    assert record is not None and record.id == 4242


async def test_a_persistent_401_gives_up_without_looping(monkeypatch):
    auth = FakeAuth()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={"detail": "nope"})

    _stub_transport(monkeypatch, handler)

    assert await client(auth=auth).get_article_by_url("https://x/y") is None
    assert calls["n"] == 2, "exactly one retry — must not loop"


async def test_no_token_means_no_request(api):
    # Auth available but unable to mint (server down, wallet rejected) → skip, don't fail.
    result = await client(auth=FakeAuth(token=None)).get_article_by_url("https://x/y")

    assert result is None
    assert api["requests"] == []


async def test_authors_are_objects_not_strings(api):
    """Regression: the API returns `authors: [{"id":.., "name":..}]`, not a list of strings.
    Modelling them as `List[str]` made every real lookup fail validation (caught live)."""
    api["routes"]["/articles"] = (200, ARTICLE_BODY)

    record = await client().get_article_by_url("https://x/y")

    assert record is not None, "an object-shaped author must parse"
    assert [a.name for a in record.authors] == ["Muhabir"]


async def test_a_missing_byline_is_tolerated(api):
    # The archive's caveat: `authors` is sometimes absent/empty even when the page had a byline.
    api["routes"]["/articles"] = (200, {**ARTICLE_BODY, "authors": []})

    record = await client().get_article_by_url("https://x/y")

    assert record is not None and record.authors == []


# --- writes (explicit opt-in, 2026-09-11) --------------------------------------------
# The IPTC fields live on the AUDIT record (PATCH /audits/{id}); comments go to
# POST /audits/{id}/discussions. Both must validate locally, send exactly the intended
# fields, and degrade to False/None — never raise, never send a partial write.

import json as _json  # noqa: E402


async def test_update_classification_patches_exactly_the_three_fields(api):
    api["routes"]["/audits/231022"] = (200, {"id": 231022, "status": 0})

    ok = await client().update_audit_classification(
        231022, subject_tag_notations=["medtop:20001324", "medtop:20001124"],
        content_type_notation="genre:Analysis", geographic_scope="national")

    assert ok is True
    req = api["requests"][-1]
    assert req.method == "PATCH" and req.url.path == "/audits/231022"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert _json.loads(req.content) == {
        "subject_tag_notations": ["medtop:20001324", "medtop:20001124"],
        "content_type_notation": "genre:Analysis", "geographic_scope": "national",
    }


async def test_update_classification_omits_absent_genre_and_scope(api):
    api["routes"]["/audits/1"] = (200, {"id": 1})
    assert await client().update_audit_classification(
        1, subject_tag_notations=["medtop:20000093"], content_type_notation=None, geographic_scope=None)
    assert _json.loads(api["requests"][-1].content) == {"subject_tag_notations": ["medtop:20000093"]}


async def test_update_classification_refuses_invalid_input_without_a_request(api):
    c = client()
    assert await c.update_audit_classification(
        1, subject_tag_notations=["medtop:20001324", "corruption"], content_type_notation=None,
        geographic_scope=None) is False
    assert await c.update_audit_classification(
        1, subject_tag_notations=["medtop:20001324"], content_type_notation="Analysis",
        geographic_scope=None) is False
    assert await c.update_audit_classification(
        1, subject_tag_notations=["medtop:20001324"], content_type_notation=None,
        geographic_scope="everywhere") is False
    assert await c.update_audit_classification(
        1, subject_tag_notations=[], content_type_notation=None, geographic_scope=None) is False
    assert api["requests"] == []


async def test_update_classification_degrades_on_403_and_5xx(api):
    api["status"] = 403
    assert await client().update_audit_classification(
        1, subject_tag_notations=["medtop:1"], content_type_notation=None, geographic_scope=None) is False
    api["status"] = 500
    assert await client().update_audit_classification(
        1, subject_tag_notations=["medtop:1"], content_type_notation=None, geographic_scope=None) is False


async def test_post_discussion_sends_the_comment_and_returns_the_message(api):
    api["routes"]["/audits/7/discussions"] = (201, {"id": 1341, "audit_id": 7, "comment": "hello"})

    out = await client().post_discussion(7, "hello")

    assert out["id"] == 1341
    req = api["requests"][-1]
    assert req.method == "POST" and req.url.path == "/audits/7/discussions"
    assert _json.loads(req.content) == {"comment": "hello"}


async def test_post_discussion_refuses_empty_or_oversized_without_a_request(api):
    assert await client().post_discussion(7, "   ") is None
    assert await client().post_discussion(7, "x" * 10_001) is None
    assert api["requests"] == []


async def test_post_discussion_degrades_on_failure(api):
    api["status"] = 500
    assert await client().post_discussion(7, "hello") is None


async def test_get_audit_and_count_discussions(api):
    api["routes"]["/audits/7"] = (200, {"id": 7, "status": 0, "decision": None, "subject_tag_notations": None})
    assert (await client().get_audit(7))["status"] == 0
    api["routes"]["/audits/7/discussions"] = (200, {"total": 2, "items": []})
    assert await client().count_discussions(7) == 2
    api["routes"]["/audits/7/discussions"] = (200, {"items": []})
    assert await client().count_discussions(7) is None, "an unreadable count is unknown, never zero"


async def test_get_audit_by_article_keeps_the_status(api):
    audit = {"id": 232794, "status": 0, "article": {"id": 1832857, "link": "https://x/y"}}
    api["routes"]["/audits/by-article/1832857"] = (200, audit)
    assert await client().get_audit_by_article(1832857) == (200, audit)
    req = api["requests"][-1]
    assert req.method == "GET" and req.headers["Authorization"] == f"Bearer {TOKEN}"
    assert await client().get_audit_by_article(5) == (404, {}), "no audit for the article: 404, not None"


async def test_get_audit_by_article_reports_an_unreachable_archive_as_none(api):
    api["raises"] = httpx.ConnectError("refused by 10.0.0.5")
    status, body = await client().get_with_status("/audits/by-article/1", attempts=1, backoff_s=0)
    assert status is None and body == {"error": "ConnectError"}
    api["raises"], api["status"] = None, 503
    c = client()

    async def no_retry(path, params=None, **kw):
        return await KarakutuClient.get_with_status(c, path, params, attempts=1, backoff_s=0)
    c.get_with_status = no_retry
    assert await c.get_audit_by_article(1) == (None, {"error": "HTTP 503"})
    disabled = KarakutuClient(base_url="https://api.example", timeout=5, auth=FakeAuth(available=False))
    assert await disabled.get_audit_by_article(1) == (None, {"error": "disabled"})


async def test_post_discussion_can_reply_in_a_thread(api):
    api["routes"]["/audits/7/discussions"] = (201, {"id": 9, "audit_id": 7, "comment": "re", "parent_id": 5})
    out = await client().post_discussion(7, "re", parent_id=5)
    assert out["id"] == 9
    req = api["requests"][-1]
    assert req.method == "POST" and _json.loads(req.content) == {"comment": "re", "parent_id": 5}



# --- (2026-10-05): the step-4 window cache --------------------------------------
#
# Batch runs built a fresh client per stage, so the window was read fresh for every audit.
# A long-lived client must reproduce that: no reuse across audits, never a partial read
# stored, bounded memory.

from src.clients.karakutu import audit_scope  # noqa: E402


def _window_api(monkeypatch, *, fail_pages=()):
    """/audits answering one short page per status; `fail_pages` = {(status, page)} → 503."""
    state = {"calls": 0, "created": 1}

    def handler(request):
        if request.url.path != "/audits":
            return httpx.Response(404, json={})
        state["calls"] += 1
        st, page = int(request.url.params["status"]), int(request.url.params["page"])
        if (st, page) in state.get("fail", set(fail_pages)):
            return httpx.Response(503, json={})
        if st == 0 and page == 1 and state.get("two_pages"):
            items = [{"id": 1000 + i, "status": 0, "article": {"id": i}} for i in range(100)]
            return httpx.Response(200, json={"items": items})
        return httpx.Response(200, json={"items": [
            {"id": st * 10 + state["created"], "status": st, "article": {"id": st}}]})

    _stub_transport(monkeypatch, handler)
    return state


async def test_without_an_audit_scope_every_call_reads_the_archive(monkeypatch):
    """The batch semantics: two audits on one long-lived client each get a fresh window —
    an audit created in between is visible to the second one."""
    state = _window_api(monkeypatch)
    c = client()
    first = await c.audits_created_between(1_000_000, 2_000_000)
    state["created"] = 2   # a new audit appears in the archive
    second = await c.audits_created_between(1_000_000, 2_000_000)
    assert state["calls"] == 10, "both calls must hit the archive (5 statuses each)"
    assert [r["audit_id"] for r in first] != [r["audit_id"] for r in second]
    assert len(c._window_cache) == 0


async def test_inside_one_audit_scope_the_window_is_read_once(monkeypatch):
    state = _window_api(monkeypatch)
    c = client()
    with audit_scope():
        a = await c.audits_created_between(1_000_000, 2_000_000)
        b = await c.audits_created_between(1_000_000, 2_000_000)
    assert state["calls"] == 5 and a == b


async def test_a_window_cached_in_one_audit_is_never_seen_by_the_next(monkeypatch):
    state = _window_api(monkeypatch)
    c = client()
    with audit_scope():
        await c.audits_created_between(1_000_000, 2_000_000)
    with audit_scope():
        await c.audits_created_between(1_000_000, 2_000_000)
    assert state["calls"] == 10


async def test_a_partial_read_is_returned_but_never_cached(monkeypatch):
    """A failing status, or a failing later page, still returns what was read (unchanged
    behaviour) — but must never be stored for reuse."""
    state = _window_api(monkeypatch, fail_pages={(3, 1)})
    c = client()
    with audit_scope():
        rows = await c.audits_created_between(1_000_000, 2_000_000)
        assert {r["status"] for r in rows} == {0, 1, 2, 4}
        assert len(c._window_cache) == 0
        state["fail"] = set()
        rows = await c.audits_created_between(1_000_000, 2_000_000)
        assert {r["status"] for r in rows} == {0, 1, 2, 3, 4}
    assert state["calls"] == 10


async def test_a_failed_later_page_is_partial_and_not_cached(monkeypatch):
    state = _window_api(monkeypatch, fail_pages={(0, 2)})
    state["two_pages"] = True
    c = client()
    with audit_scope():
        rows = await c.audits_created_between(1_000_000, 2_000_000)
    assert sum(1 for r in rows if r["status"] == 0) == 100   # page 1 kept, as before
    assert len(c._window_cache) == 0


async def test_the_window_cache_is_bounded(monkeypatch):
    _window_api(monkeypatch)
    c = client()
    with audit_scope():
        for i in range(KarakutuClient.WINDOW_CACHE_MAX + 5):
            await c.audits_created_between(1_000_000 + i, 2_000_000)
    assert len(c._window_cache) == KarakutuClient.WINDOW_CACHE_MAX


async def test_list_audits_keeps_its_partial_read_contract(monkeypatch):
    state = _window_api(monkeypatch, fail_pages={(0, 2)})
    state["two_pages"] = True
    c = client()
    assert len(await c.list_audits(0, date_from=1, date_to=2)) == 100
    state["fail"] = {(0, 1)}
    assert await c.list_audits(0, date_from=1, date_to=2) is None


# --- the keyfile content setting --------------------------------------------------------


def test_the_client_passes_wallet_json_from_settings(monkeypatch):
    from src.config.settings import get_settings
    monkeypatch.setattr(get_settings(), "KARAKUTU_WALLET_JSON", '{"kty": "RSA"}', raising=False)
    c = KarakutuClient(base_url="https://api.example", timeout=5)
    assert c.auth._wallet_json == '{"kty": "RSA"}' and c.enabled is True
    # An explicit path pins the file and ignores the setting.
    pinned = KarakutuClient(base_url="https://api.example", timeout=5, wallet_path="/nonexistent.json")
    assert pinned.auth._wallet_json == "" and pinned.enabled is False


def test_the_client_tolerates_settings_without_wallet_json(monkeypatch):
    from src.config.settings import get_settings
    monkeypatch.delattr(type(get_settings()), "KARAKUTU_WALLET_JSON", raising=False)
    monkeypatch.delattr(get_settings(), "KARAKUTU_WALLET_JSON", raising=False)
    c = KarakutuClient(base_url="https://api.example", timeout=5)
    assert c.auth._wallet_json == ""


async def test_transport_errors_are_logged_by_type_not_text(api, caplog):
    api["raises"] = httpx.ConnectError("connect to 10.9.8.7 failed: internal detail")
    with caplog.at_level("WARNING", logger="src.clients.karakutu"):
        assert await client().get_article(1) is None
    assert "ConnectError" in caplog.text and "10.9.8.7" not in caplog.text
