"""Concept catalog: the IPTC vocabulary pulled from the archive's GET /concepts.

No network: the client is driven through httpx.MockTransport; load_catalog is exercised with a
fake client. The catalog is what grounds every tag the classifier emits, so the helpers here
must be exact.
"""
import json

import httpx
import pytest

from src.clients import karakutu as karakutu_module
from src.clients.concepts import (
    CATALOG_LOCALE, SCHEME_GENRES, SCHEME_TOPICS, Concept, ConceptCatalog, load_catalog,
)
from src.clients.karakutu import KarakutuClient


def topic(notation, label, parent=None, path=None, definition="def"):
    return Concept(notation=notation, scheme=SCHEME_TOPICS, pref_label=label,
                   parent_notation=parent, hierarchy_path=path, definition=definition)


def genre(name, label=None):
    return Concept(notation=f"genre:{name}", scheme=SCHEME_GENRES, pref_label=label or name,
                   definition="g")


TOPICS = [
    topic("medtop:02000000", "crime, law and justice", None, "02000000"),
    topic("medtop:20000082", "crime", "medtop:02000000", "02000000.20000082"),
    topic("medtop:20000093", "corruption", "medtop:20000082", "02000000.20000082.20000093"),
    topic("medtop:20000084", "sexual misconduct", "medtop:20000082", "02000000.20000082.20000084"),
    topic("medtop:20000085", "sex crime", "medtop:20000084", "02000000.20000082.20000084.20000085"),
    topic("medtop:11000000", "politics and government", None, "11000000"),
    topic("medtop:20000593", "government", "medtop:11000000", "11000000.20000593"),
]
GENRES = [genre("Current", "Current Events"), genre("Opinion")]


@pytest.fixture
def catalog():
    return ConceptCatalog(TOPICS, GENRES)


def test_top_level_and_children(catalog):
    assert [c.notation for c in catalog.top_level()] == ["medtop:02000000", "medtop:11000000"]
    assert [c.notation for c in catalog.children("medtop:20000082")] == ["medtop:20000084", "medtop:20000093"]


def test_subtree_is_depth_bounded_and_in_hierarchy_order(catalog):
    assert [c.notation for c in catalog.subtree("medtop:02000000", max_depth=3)] == [
        "medtop:20000082", "medtop:20000084", "medtop:20000093",
    ]
    assert [c.notation for c in catalog.subtree("medtop:02000000", max_depth=4)] == [
        "medtop:20000082", "medtop:20000084", "medtop:20000085", "medtop:20000093",
    ]


def test_depth_comes_from_hierarchy_path(catalog):
    assert catalog.get("medtop:02000000").depth == 1
    assert catalog.get("medtop:20000085").depth == 4


def test_lookup_helpers(catalog):
    assert catalog.is_topic("medtop:20000093") and not catalog.is_topic("genre:Current")
    assert catalog.is_genre("genre:Current") and not catalog.is_genre("medtop:20000093")
    assert catalog.label("medtop:20000093") == "corruption"
    assert catalog.label("medtop:nope") == "medtop:nope"
    assert catalog.get("nope") is None
    assert [g.notation for g in catalog.genres()] == ["genre:Current", "genre:Opinion"]
    assert catalog.topic_count == 7 and catalog.genre_count == 2


def test_round_trips_through_json(catalog):
    again = ConceptCatalog.from_json(json.loads(json.dumps(catalog.to_json())))
    assert [c.notation for c in again.subtree("medtop:02000000", 4)] == \
           [c.notation for c in catalog.subtree("medtop:02000000", 4)]
    assert again.genre_count == 2


# --- client ---------------------------------------------------------------

class FakeAuth:
    def __init__(self, available=True):
        self.available = available

    async def get_token(self, force=False):
        return "t" if self.available else None


@pytest.fixture
def api(monkeypatch):
    state = {"requests": [], "status": 200, "body": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        body = state["body"](request) if callable(state["body"]) else state["body"]
        return httpx.Response(state["status"], json=body)

    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(karakutu_module.httpx, "AsyncClient", patched)
    return state


def client():
    return KarakutuClient(base_url="https://api.test", wallet_path="", timeout=5, auth=FakeAuth())


async def test_list_concepts_sends_scheme_and_locale(api):
    api["body"] = {"concepts": [{"notation": "medtop:01000000", "scheme": "iptc_mediatopic",
                                 "pref_label": "arts", "definition": "d", "label_source": "iptc_en-GB",
                                 "hierarchy_path": "01000000", "parent_notation": None}],
                   "_meta": {"count": 1}}
    out = await client().list_concepts(SCHEME_TOPICS)
    assert [c.notation for c in out] == ["medtop:01000000"]
    req = api["requests"][0]
    assert req.url.path == "/concepts"
    assert req.url.params["scheme"] == SCHEME_TOPICS
    assert req.url.params["locale"] == CATALOG_LOCALE
    assert req.headers["authorization"] == "Bearer t"


async def test_list_concepts_degrades_to_empty(api):
    api["status"] = 500
    assert await client().list_concepts(SCHEME_TOPICS) == []
    api["status"] = 200
    api["body"] = {"concepts": "not a list"}
    assert await client().list_concepts(SCHEME_TOPICS) == []


# --- load_catalog ---------------------------------------------------------

class FakeClient:
    def __init__(self, topics, genres, enabled=True):
        self.enabled, self._topics, self._genres, self.calls = enabled, topics, genres, []

    async def list_concepts(self, scheme, locale=CATALOG_LOCALE):
        self.calls.append(scheme)
        return self._topics if scheme == SCHEME_TOPICS else self._genres


async def test_load_catalog_pulls_both_schemes_and_writes_the_cache(tmp_path):
    cache = tmp_path / "c.json"
    cat = await load_catalog(FakeClient(TOPICS, GENRES), cache)
    assert cat is not None and cat.topic_count == 7 and cat.genre_count == 2
    assert cache.exists()
    data = json.loads(cache.read_text())
    assert data["locale"] == CATALOG_LOCALE and len(data["topics"]) == 7


async def test_load_catalog_falls_back_to_the_cache_when_the_api_fails(tmp_path):
    cache = tmp_path / "c.json"
    await load_catalog(FakeClient(TOPICS, GENRES), cache)
    cat = await load_catalog(FakeClient([], [], enabled=True), cache)
    assert cat is not None and cat.topic_count == 7


async def test_load_catalog_returns_none_without_api_or_cache(tmp_path):
    assert await load_catalog(FakeClient([], []), tmp_path / "missing.json") is None


async def test_load_catalog_makes_no_call_when_the_client_is_disabled(tmp_path):
    fake = FakeClient(TOPICS, GENRES, enabled=False)
    assert await load_catalog(fake, tmp_path / "c.json") is None
    assert fake.calls == []


async def test_load_catalog_needs_both_schemes(tmp_path):
    # Topics without genres is a half-vocabulary; refuse rather than classify half-blind.
    assert await load_catalog(FakeClient(TOPICS, []), tmp_path / "c.json") is None
