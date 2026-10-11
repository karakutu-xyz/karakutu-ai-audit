"""Service-level pieces: GET /ready, the /api/audit guards (optional bearer, absurd-size cap), CORS
scoping, and the settings defaults."""
import importlib
from unittest.mock import AsyncMock

import dotenv
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic_ai.models.test import TestModel

from src.api import app as app_module
from src.api.app import PathScopedCORS, app, configure_cors, lifespan
from src.api.models import AuditResponse
from src.api.readiness import readiness
from src.config import settings as settings_module
from src.jobs.store import MemoryJobStore
from tests._service_support import (
    AUTH, TOKEN, FakeArchive, FakeProvider, fake_runtime, record_by, service_settings,
)


class _NoWallet:
    enabled = False

    async def check_health(self):
        return False


@pytest.fixture
def no_archive(monkeypatch):
    """Never touch the real archive from these tests (the repo root may hold a keyfile)."""
    monkeypatch.setattr(app_module, "get_client", lambda: _NoWallet())


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --- /ready ----------------------------------------------------------------------------------

async def test_ready_reports_synthetic_parts_but_is_ready_without_writes(monkeypatch, no_archive):
    service_settings(monkeypatch, WEB_SEARCH_PROVIDER="stub")
    async with lifespan(app):
        async with await _client() as c:
            r = await c.get("/ready", headers=AUTH)
            h = await c.get("/health")
    assert h.status_code == 200 and h.json()["status"] == "ok", "/health is unchanged"
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready" and body["writes_enabled"] is False
    assert body["checks"]["store"] == "ok" and body["checks"]["worker"] == "ok"
    assert body["checks"]["wallet"] == "missing"
    assert body["checks"]["model"] == "synthetic" and body["checks"]["search"] == "synthetic"
    assert body["write_blockers"][0] == "writes_disabled"


@pytest.mark.parametrize("token,auth", [("", None), ("", "Bearer "), (TOKEN, None),
                                        (TOKEN, "Bearer wrong"), (TOKEN, f"Basic {TOKEN}"), (TOKEN, TOKEN)])
async def test_ready_shows_anonymous_callers_the_status_only(monkeypatch, no_archive, token, auth):
    service_settings(monkeypatch, API_AUTH_TOKEN=token, WEB_SEARCH_PROVIDER="stub")
    async with lifespan(app):
        async with await _client() as c:
            r = await c.get("/ready", headers={"Authorization": auth} if auth else {})
    assert r.status_code == 200 and r.json() == {"status": "ready"}


async def test_ready_anonymous_view_keeps_the_503(monkeypatch):
    service_settings(monkeypatch, WEB_SEARCH_PROVIDER="searxng")
    monkeypatch.setattr("src.api.readiness._probe_searxng", AsyncMock(return_value=False))
    saved = {k: getattr(app.state, k, None) for k in ("runtime", "job_store", "job_worker", "ready_cache")}
    app.state.runtime, app.state.job_store, app.state.job_worker = fake_runtime(FakeArchive()), None, None
    app.state.ready_cache = None
    try:
        async with await _client() as c:
            anon = await c.get("/ready")
            full = await c.get("/ready", headers=AUTH)
    finally:
        for k, v in saved.items():
            setattr(app.state, k, v)
    assert anon.status_code == full.status_code == 503
    assert anon.json() == {"status": "not_ready"}
    assert full.json()["checks"]["search"] == "fail" and "write_blockers" in full.json()


async def test_ready_refuses_while_writes_are_on_and_the_configuration_is_synthetic(monkeypatch):
    cfg = service_settings(monkeypatch, ARCHIVE_WRITES_ENABLED=True, WEB_SEARCH_PROVIDER="stub")
    state = type("S", (), {})()
    state.runtime = fake_runtime(FakeArchive(), model=TestModel())
    state.job_store, state.job_worker = MemoryJobStore(), None
    code, body = await readiness(state, cfg)
    assert code == 503 and body.status == "not_ready"
    assert {"synthetic_model", "stub_search"} <= set(body.write_blockers)
    assert body.checks["worker"] == "fail"


async def test_ready_is_ready_with_a_real_configuration_and_fails_on_a_paused_worker(monkeypatch):
    cfg = service_settings(monkeypatch, ARCHIVE_WRITES_ENABLED=True, WEB_SEARCH_PROVIDER="duckduckgo")

    class Worker:
        running, paused, model_unavailable = True, False, False
    state = type("S", (), {})()
    state.runtime = fake_runtime(FakeArchive(), provider=FakeProvider(True))
    state.job_store, state.job_worker = MemoryJobStore(), Worker()
    code, body = await readiness(state, cfg)
    assert code == 200, body
    assert set(body.checks.values()) == {"ok"} and body.write_blockers == []
    Worker.paused = True
    code, body = await readiness(state, cfg)
    assert code == 503 and body.checks["model"] == "fail"


async def test_ready_probes_searxng(monkeypatch):
    cfg = service_settings(monkeypatch, API_AUTH_TOKEN="", WEB_SEARCH_PROVIDER="searxng")
    state = type("S", (), {})()
    state.runtime = fake_runtime(FakeArchive())
    state.job_store = state.job_worker = None
    monkeypatch.setattr("src.api.readiness._probe_searxng", AsyncMock(return_value=False))
    code, body = await readiness(state, cfg)
    assert code == 503 and body.checks["search"] == "fail"


# --- /api/audit guards -------------------------------------------------------------------------

AUDIT_BODY = {"removed_article": {"url": "https://example.com/removed", "title": "T", "content": "c"}}


@pytest.fixture
def stub_pipeline(monkeypatch):
    calls = []

    async def fake(body, **kw):
        calls.append(body)
        return AuditResponse.model_validate(record_by(232794)["response"])
    monkeypatch.setattr(app_module, "run_audit", fake)
    return calls


async def test_api_audit_is_open_when_no_token_is_configured(monkeypatch, no_archive, stub_pipeline):
    service_settings(monkeypatch, API_AUTH_TOKEN="")
    async with lifespan(app):
        async with await _client() as c:
            r = await c.post("/api/audit", json=AUDIT_BODY)
    assert r.status_code == 200 and len(stub_pipeline) == 1


async def test_api_audit_requires_the_bearer_token_when_set(monkeypatch, no_archive, stub_pipeline):
    service_settings(monkeypatch)
    async with lifespan(app):
        async with await _client() as c:
            missing = await c.post("/api/audit", json=AUDIT_BODY)
            wrong = await c.post("/api/audit", json=AUDIT_BODY, headers={"Authorization": f"Bearer {TOKEN}x"})
            basic = await c.post("/api/audit", json=AUDIT_BODY, headers={"Authorization": f"Basic {TOKEN}"})
            unauth_bad_body = await c.post("/api/audit", json={"nope": 1})
            ok = await c.post("/api/audit", json=AUDIT_BODY, headers=AUTH)
            health = await c.get("/health")
    for r in (missing, wrong, basic):
        assert r.status_code == 401 and r.headers["WWW-Authenticate"] == "Bearer"
        assert r.json()["error"] == "unauthorized" and TOKEN not in r.text
    assert unauth_bad_body.status_code == 401, "auth runs before body validation"
    assert ok.status_code == 200 and len(stub_pipeline) == 1
    assert health.status_code == 200, "/health stays open"


async def test_api_audit_refuses_only_absurd_field_sizes(monkeypatch, no_archive, stub_pipeline):
    service_settings(monkeypatch, API_AUTH_TOKEN="")
    near = {"removed_article": {"url": "https://example.com/r", "title": "T",
                                "content": "x" * (app_module.MAX_ARTICLE_FIELD_CHARS - 1)}}
    over = {"removed_article": {"url": "https://example.com/r", "title": "T",
                                "content": "x" * app_module.MAX_ARTICLE_FIELD_CHARS}}
    over_similar = {**AUDIT_BODY, "similar_articles": [{"url": "u", "title": "t" * app_module.MAX_ARTICLE_FIELD_CHARS,
                                                        "content": "c"}]}
    async with lifespan(app):
        async with await _client() as c:
            r_near = await c.post("/api/audit", json=near)
            r_over = await c.post("/api/audit", json=over)
            r_sim = await c.post("/api/audit", json=over_similar)
    assert r_near.status_code == 200, "anything below 2,000,000 characters is accepted as before"
    assert len(stub_pipeline[0].removed_article.content) == app_module.MAX_ARTICLE_FIELD_CHARS - 1, \
        "the route does not truncate; CONTENT_LIMIT semantics stay where they were"
    assert r_over.status_code == 413 and r_over.json()["detail"].startswith("removed_article.content")
    assert r_sim.status_code == 413 and r_sim.json()["detail"].startswith("similar_articles[0].title")
    assert len(stub_pipeline) == 1


# --- CORS -------------------------------------------------------------------------------------

def test_the_app_has_no_cors_middleware_by_default():
    configured = bool(settings_module.get_settings().cors_origins)
    assert any(m.cls is PathScopedCORS for m in app.user_middleware) is configured
    assert configure_cors(FastAPI(), []) is False


async def test_configured_cors_covers_api_audit_but_never_api_v1():
    probe = FastAPI()

    @probe.post("/api/audit")
    async def a():
        return {"ok": True}

    @probe.post("/api/v1/articles/1/audit")
    async def w():
        return {"ok": True}

    assert configure_cors(probe, ["https://ui.example"]) is True
    pre = {"Origin": "https://ui.example", "Access-Control-Request-Method": "POST"}
    async with AsyncClient(transport=ASGITransport(app=probe), base_url="http://t") as c:
        audit = await c.options("/api/audit", headers=pre)
        hook = await c.options("/api/v1/articles/1/audit", headers=pre)
        hook_post = await c.post("/api/v1/articles/1/audit", headers={"Origin": "https://ui.example"})
        audit_post = await c.post("/api/audit", headers={"Origin": "https://ui.example"})
    assert audit.headers.get("access-control-allow-origin") == "https://ui.example"
    assert "access-control-allow-origin" not in hook.headers
    assert "access-control-allow-origin" not in hook_post.headers
    assert audit_post.headers.get("access-control-allow-origin") == "https://ui.example"


# --- settings defaults --------------------------------------------------------------------------

NEW_KEYS = ["ARCHIVE_WRITES_ENABLED", "JOB_STORE_URL", "DATA_DIR", "JOB_CONCURRENCY", "API_AUTH_TOKEN",
            "CORS_ALLOW_ORIGINS", "SEARXNG_ENGINES", "KARAKUTU_WALLET_JSON"]


@pytest.fixture
def fresh_settings(monkeypatch):
    def _reload(**env):
        for k in NEW_KEYS:
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
        return importlib.reload(settings_module).get_settings()
    yield _reload
    monkeypatch.undo()
    importlib.reload(settings_module)


def test_new_settings_default_to_fail_closed(fresh_settings):
    s = fresh_settings()
    assert s.ARCHIVE_WRITES_ENABLED is False and s.jobs_enabled is False
    assert s.JOB_CONCURRENCY == 1
    assert s.job_store_url == "sqlite:///data/karakutu-audit.sqlite3"
    assert s.API_AUTH_TOKEN == "" and s.cors_origins == []
    assert s.SEARXNG_ENGINES == "google,bing,duckduckgo", "the reference profile"
    s.validate()


def test_settings_parse_lists_and_derive_the_store_path(fresh_settings):
    s = fresh_settings(API_AUTH_TOKEN="a" * 32, CORS_ALLOW_ORIGINS="https://x, https://y",
                       DATA_DIR="/var/lib/kk", ARCHIVE_WRITES_ENABLED="yes")
    assert s.jobs_enabled is True and s.cors_origins == ["https://x", "https://y"]
    assert s.job_store_url == "sqlite:////var/lib/kk/karakutu-audit.sqlite3"
    assert s.ARCHIVE_WRITES_ENABLED is True
    s.validate()


def test_validate_rejects_a_wallet_json_that_is_not_an_object_without_echoing_it(fresh_settings):
    s = fresh_settings(KARAKUTU_WALLET_JSON='["d-secret-material"]')
    with pytest.raises(settings_module.SettingsError) as exc:   # the reloaded module's class
        s.validate()
    assert "d-secret-material" not in str(exc.value)
    s2 = fresh_settings(JOB_STORE_URL="redis://x")
    with pytest.raises(settings_module.SettingsError, match="JOB_STORE_URL"):
        s2.validate()
