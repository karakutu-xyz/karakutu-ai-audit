import httpx
import pytest

from src.search import (
    SEARCH_REGISTRY,
    get_search_provider,
)
from src.search import duckduckgo as ddg_module
from src.search.base import SearchResult
from src.search.duckduckgo import DuckDuckGoSearchProvider
from src.search.stub import StubSearchProvider

# ddgs 9.x `text()` yields dicts keyed title/href/body (see ddgs/results.py::TextResult).
CANNED_HITS = [
    {"title": f"Result {i}", "href": f"https://news.example/{i}", "body": f"Snippet {i}"}
    for i in range(8)
]


def _stub_ddgs(monkeypatch, *, hits=None, raises: Exception | None = None):
    """Replace ddgs.DDGS so no test ever opens a real socket. Returns recorded calls."""
    calls: list[tuple[str, int]] = []

    class _FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query: str, max_results: int = 10):
            calls.append((query, max_results))
            if raises is not None:
                raise raises
            return list(hits or [])

    monkeypatch.setattr(ddg_module, "DDGS", _FakeDDGS)
    return calls


# --- registry resolution -----------------------------------------------------


def test_get_search_provider_stub():
    assert isinstance(get_search_provider("stub"), StubSearchProvider)


def test_get_search_provider_duckduckgo():
    assert isinstance(get_search_provider("duckduckgo"), DuckDuckGoSearchProvider)


def test_get_search_provider_normalizes_name():
    assert isinstance(get_search_provider("  DuckDuckGo "), DuckDuckGoSearchProvider)


def test_get_search_provider_unknown_raises_valueerror():
    with pytest.raises(ValueError) as exc:
        get_search_provider("bogus")
    msg = str(exc.value)
    assert "Available" in msg
    assert "duckduckgo" in msg
    assert "stub" in msg


def test_registry_contains_all_providers():
    assert set(SEARCH_REGISTRY.keys()) == {"stub", "duckduckgo", "searxng"}


# --- DuckDuckGoSearchProvider.search -----------------------------------------


async def test_search_maps_hits_to_search_results(monkeypatch):
    _stub_ddgs(monkeypatch, hits=CANNED_HITS[:2])

    results = await DuckDuckGoSearchProvider().search("removed headline", max_results=5)

    assert results == [
        SearchResult(url="https://news.example/0", title="Result 0", snippet="Snippet 0"),
        SearchResult(url="https://news.example/1", title="Result 1", snippet="Snippet 1"),
    ]


async def test_search_passes_query_and_max_results_to_client(monkeypatch):
    calls = _stub_ddgs(monkeypatch, hits=CANNED_HITS[:1])

    await DuckDuckGoSearchProvider().search("removed headline", max_results=3)

    assert calls == [("removed headline", 3)]


async def test_search_truncates_to_max_results(monkeypatch):
    # Defence in depth: even if the backend over-delivers, we never exceed max_results.
    _stub_ddgs(monkeypatch, hits=CANNED_HITS)  # 8 hits

    results = await DuckDuckGoSearchProvider().search("q", max_results=5)

    assert len(results) == 5


async def test_search_default_max_results_is_five(monkeypatch):
    calls = _stub_ddgs(monkeypatch, hits=CANNED_HITS)

    results = await DuckDuckGoSearchProvider().search("q")

    assert calls == [("q", 5)]
    assert len(results) == 5


async def test_search_tolerates_missing_keys(monkeypatch):
    # A partial hit must not raise. A hit with no usable href is dropped rather than
    # passed on as an empty URL: stage 3 fetches every hit, and "" is not fetchable.
    _stub_ddgs(monkeypatch, hits=[
        {"title": "Only a title"},
        {"title": "Usable", "href": "https://real.example/a"},
    ])

    results = await DuckDuckGoSearchProvider().search("q")

    assert results == [SearchResult(url="https://real.example/a", title="Usable", snippet="")]


async def test_search_returns_empty_on_backend_error(monkeypatch):
    # Rate limit / network / parse failure must degrade to [], never propagate:
    # an exception here would 500 the audit endpoint.
    _stub_ddgs(monkeypatch, raises=RuntimeError("ratelimit 202"))

    assert await DuckDuckGoSearchProvider().search("q") == []


async def test_search_returns_empty_on_no_hits(monkeypatch):
    _stub_ddgs(monkeypatch, hits=[])

    assert await DuckDuckGoSearchProvider().search("q") == []


# --- lifespan wiring ---------------------------------------------------------


async def test_lifespan_selects_provider_from_settings(monkeypatch):
    # AC: WEB_SEARCH_PROVIDER=duckduckgo selects it at startup with no app.py change.
    # Settings fields are evaluated at import, so patch the singleton, not the env.
    from src.api.app import app, lifespan
    from src.config import settings as settings_module

    monkeypatch.setattr(settings_module.settings, "WEB_SEARCH_PROVIDER", "duckduckgo")

    async with lifespan(app):
        assert isinstance(app.state.search_provider, DuckDuckGoSearchProvider)


async def test_lifespan_defaults_to_stub():
    # The default must stay `stub` — conftest's `client` fixture runs the real
    # lifespan, so a duckduckgo default would put the whole suite on the network.
    from src.api.app import app, lifespan
    from src.config import settings as settings_module

    assert settings_module.settings.WEB_SEARCH_PROVIDER == "stub"

    async with lifespan(app):
        assert isinstance(app.state.search_provider, StubSearchProvider)


async def test_discards_malformed_relative_hits(monkeypatch):
    """Under rate limiting ddgs rotates to a fallback backend that emitted a relative
    tracking redirect ("/clev?event=StartpageResultClick..."). Such a hit cannot be
    fetched and must never reach the pipeline."""
    _stub_ddgs(monkeypatch, hits=[
        {"title": "Good", "href": "https://real.example/a", "body": "b"},
        {"title": "Junk", "href": "/clev?event=StartpageResultClick&sc=AR5c", "body": "b"},
        {"title": "Empty", "href": "", "body": "b"},
    ])

    results = await DuckDuckGoSearchProvider().search("q", max_results=5)

    assert [r.url for r in results] == ["https://real.example/a"]


# --- SearXNGSearchProvider ----------------------------------------------------
#
# SearXNG exists because querying DuckDuckGo alone missed relocations that are plainly live
# (evrensel/98981 -> 200, jinnews/270809 -> 200). It fans out to several engines and merges.
# No test opens a socket: httpx is stubbed.

from src.search import searxng as searxng_module          # noqa: E402
from src.search.searxng import SearXNGSearchProvider      # noqa: E402


def _stub_searxng(monkeypatch, *, payload=None, raises: Exception | None = None,
                  status: int = 200):
    """Stub the SearXNG HTTP call. Returns a dict that captures the request."""
    captured: dict = {}
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        if raises is not None:
            raise raises
        return httpx.Response(status, json=payload or {"results": []})

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(searxng_module.httpx, "AsyncClient", make_client)
    return captured


def test_get_search_provider_searxng():
    assert isinstance(get_search_provider("searxng"), SearXNGSearchProvider)


async def test_searxng_maps_results_to_search_results(monkeypatch):
    # SearXNG calls the snippet `content`, not `body`/`snippet`.
    _stub_searxng(monkeypatch, payload={"results": [
        {"url": "https://birgun.net/haber/x", "title": "Bir", "content": "snippet one"},
        {"url": "https://evrensel.net/yazi/98981/y", "title": "Iki", "content": "snippet two"},
    ]})

    results = await SearXNGSearchProvider().search("q")

    assert results == [
        SearchResult(url="https://birgun.net/haber/x", title="Bir", snippet="snippet one"),
        SearchResult(url="https://evrensel.net/yazi/98981/y", title="Iki", snippet="snippet two"),
    ]


async def test_searxng_requests_json_and_the_configured_engines(monkeypatch):
    captured = _stub_searxng(monkeypatch, payload={"results": []})

    await SearXNGSearchProvider().search("PKK ateşkes")

    assert captured["params"]["format"] == "json", "the JSON API is the whole point"
    assert captured["params"]["q"] == "PKK ateşkes"
    # Aggregating engines is why this provider exists.
    assert "google" in captured["params"]["engines"]
    assert "/search" in captured["url"]


async def test_searxng_truncates_to_max_results(monkeypatch):
    _stub_searxng(monkeypatch, payload={"results": [
        {"url": f"https://n{i}.example/a", "title": "t", "content": "c"} for i in range(10)
    ]})

    results = await SearXNGSearchProvider().search("q", max_results=3)

    assert len(results) == 3


async def test_searxng_discards_malformed_hits(monkeypatch):
    _stub_searxng(monkeypatch, payload={"results": [
        {"url": "/relative/redirect", "title": "junk", "content": "c"},
        {"url": "", "title": "empty", "content": "c"},
        {"url": "https://real.example/a", "title": "ok", "content": "c"},
    ]})

    results = await SearXNGSearchProvider().search("q")

    assert [r.url for r in results] == ["https://real.example/a"]


async def test_searxng_tolerates_missing_fields(monkeypatch):
    _stub_searxng(monkeypatch, payload={"results": [{"url": "https://a.example/x"}]})

    results = await SearXNGSearchProvider().search("q")

    assert results == [SearchResult(url="https://a.example/x", title="", snippet="")]


async def test_searxng_unreachable_degrades_to_empty_list(monkeypatch):
    # An unreachable search backend must never 500 the audit.
    _stub_searxng(monkeypatch, raises=httpx.ConnectError("connection refused"))

    assert await SearXNGSearchProvider().search("q") == []


async def test_searxng_http_error_degrades_to_empty_list(monkeypatch):
    # 403 is what SearXNG returns when `formats: [json]` is missing from settings.yml.
    _stub_searxng(monkeypatch, status=403, payload={})

    assert await SearXNGSearchProvider().search("q") == []


# --- language routing ---------------------------------------------------------
#
# Measured (2026-08-28/29 run): 35/75 INCONCLUSIVE audits were Azerbaijani articles
# searched with language=tr. The provider interface now takes a keyword-only `language`;
# SearXNG uses it (falling back to SEARXNG_LANGUAGE), the others accept and ignore it.


def test_search_results_default_to_web_source():
    assert SearchResult(url="https://x.com/a", title="t", snippet="s").source == "web"


async def test_searxng_uses_the_requested_language(monkeypatch):
    captured = _stub_searxng(monkeypatch, payload={"results": []})

    await SearXNGSearchProvider().search("qarli hava", language="az")

    assert captured["params"]["language"] == "az"


async def test_searxng_falls_back_to_the_configured_language(monkeypatch):
    # No guess -> the operator-configured default (SEARXNG_LANGUAGE, "tr" by default).
    captured = _stub_searxng(monkeypatch, payload={"results": []})

    await SearXNGSearchProvider().search("q")

    assert captured["params"]["language"] == "tr"


async def test_duckduckgo_accepts_and_ignores_language(monkeypatch):
    calls = _stub_ddgs(monkeypatch, hits=CANNED_HITS[:1])

    results = await DuckDuckGoSearchProvider().search("q", language="az")

    assert len(results) == 1
    assert calls == [("q", 5)]


async def test_stub_accepts_and_ignores_language():
    results = await StubSearchProvider().search("q", language="az")

    assert len(results) == 3
