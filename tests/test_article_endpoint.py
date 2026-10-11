"""POST /api/v1/articles/{article_id}/audit and the job reads.

Exercised against the real `app` with a MemoryJobStore and NO worker, so every accepted job stays
queued and the idempotency rules can be read straight from the store.
"""
import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.app import app, lifespan
from src.config import settings as settings_module
from src.jobs import models as M
from src.jobs.store import MemoryJobStore
from tests._service_support import AUTH, TOKEN, record_by, service_settings


def path(article_id, query=""):
    return f"/api/v1/articles/{article_id}/audit{query}"


@asynccontextmanager
async def api(store, *, raise_app_exceptions=True):
    saved = {k: getattr(app.state, k, None) for k in ("job_store", "job_worker", "intake_lock")}
    app.state.job_store, app.state.job_worker, app.state.intake_lock = store, None, None
    try:
        t = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
        async with AsyncClient(transport=t, base_url="http://test") as c:
            yield c
    finally:
        for k, v in saved.items():
            setattr(app.state, k, v)


async def post(c, article_id, query="", headers=AUTH):
    return await c.post(path(article_id, query), headers=headers)


@pytest.fixture
def store():
    return MemoryJobStore()


@pytest.fixture
def cfg(monkeypatch):
    return service_settings(monkeypatch)


# --- accept ---------------------------------------------------------------------------------------

async def test_a_post_creates_a_queued_dry_run_job_in_shadow_mode(cfg, store):
    async with api(store) as c:
        r = await post(c, 1832857)
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"job_id", "article_id", "status", "dry_run", "duplicate", "status_url"}
    assert (body["article_id"], body["status"], body["duplicate"]) == (1832857, "queued", False)
    assert body["dry_run"] is True, "ARCHIVE_WRITES_ENABLED=false forces a dry run"
    assert body["status_url"] == f"/api/v1/jobs/{body['job_id']}"
    job = await store.get_job(body["job_id"])
    assert (job.article_id, job.audit_id, job.requested_dry_run) == (1832857, None, False)


async def test_with_writes_on_the_job_is_real_unless_dry_run_is_asked(monkeypatch, store):
    service_settings(monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    async with api(store) as c:
        real = await post(c, 1)
        dry = await post(c, 2, "?dry_run=true")
        explicit = await post(c, 3, "?dry_run=false")
    assert real.json()["dry_run"] is False and explicit.json()["dry_run"] is False
    assert dry.json()["dry_run"] is True
    assert (await store.get_job(dry.json()["job_id"])).requested_dry_run is True


async def test_a_request_body_is_ignored(cfg, store):
    async with api(store) as c:
        r = await c.post(path(5), headers=AUTH, json={"audit_id": 99, "template": "A"})
    assert r.status_code == 202 and (await store.get_job(r.json()["job_id"])).audit_id is None


# --- auth and fail-closed --------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer "}, {"Authorization": "Bearer wrong-token-0123456789abcdef-0123"},
    {"Authorization": f"Basic {TOKEN}"}, {"Authorization": TOKEN},
])
async def test_a_missing_or_wrong_token_is_401_and_creates_nothing(cfg, store, headers):
    async with api(store) as c:
        r = await post(c, 1, headers=headers)
        g = await c.get("/api/v1/jobs/x", headers=headers)
        latest = await c.get("/api/v1/articles/1/jobs/latest", headers=headers)
        bad_id = await post(c, "abc", headers=headers)
    for resp in (r, g, latest, bad_id):
        assert resp.status_code == 401 and resp.headers["WWW-Authenticate"] == "Bearer"
        assert resp.json()["error"] == "unauthorized" and TOKEN not in resp.text
    assert store.jobs == {}


async def test_every_v1_route_is_a_plain_404_while_the_token_is_unset(monkeypatch, store):
    service_settings(monkeypatch, API_AUTH_TOKEN="")
    async with api(store) as c:
        r1 = await post(c, 1)
        r2 = await c.get("/api/v1/jobs/abc", headers=AUTH)
        r3 = await c.get("/api/v1/articles/1/jobs/latest", headers=AUTH)
        r4 = await c.get("/api/v1/no-such-route")
    assert r1.status_code == r2.status_code == r3.status_code == 404
    assert r1.json() == r4.json() == {"detail": "Not Found"}, "indistinguishable from an unknown path"
    assert store.jobs == {}


async def test_the_lifespan_starts_no_worker_without_a_token(monkeypatch, no_archive):
    service_settings(monkeypatch, API_AUTH_TOKEN="")
    async with lifespan(app):
        assert app.state.job_store is None and app.state.job_worker is None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post(path(1), headers=AUTH)
    assert r.status_code == 404


async def test_the_lifespan_starts_the_worker_with_a_token_and_stops_it(monkeypatch, no_archive):
    service_settings(monkeypatch)
    async with lifespan(app):
        assert isinstance(app.state.job_store, MemoryJobStore)
        worker = app.state.job_worker
        assert worker.running
    assert not worker.running, "shutdown stops the worker"


@pytest.mark.parametrize("token", ["tiny-tok-value", "x" * 31])
async def test_a_short_token_refuses_to_boot_without_echoing_it(monkeypatch, no_archive, token):
    service_settings(monkeypatch, API_AUTH_TOKEN=token)
    with pytest.raises(settings_module.SettingsError, match="API_AUTH_TOKEN is shorter than 32") as exc:
        async with lifespan(app):
            pass
    assert token not in str(exc.value)
    assert app.state.job_worker is None or not app.state.job_worker.running


def test_a_32_character_token_is_accepted(monkeypatch):
    service_settings(monkeypatch, API_AUTH_TOKEN="x" * 32).validate()
    service_settings(monkeypatch, API_AUTH_TOKEN="").validate()


@pytest.fixture
def no_archive(monkeypatch):
    """Never touch the real archive from these tests (the repo root may hold a keyfile)."""
    from src.api import app as app_module

    class _NoWallet:
        enabled = False

        async def check_health(self):
            return False
    monkeypatch.setattr(app_module, "get_client", lambda: _NoWallet())


# --- parameters ------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["0", "abc", "-1", "1.5", "%C2%B2", "1" * 13, "%201"])
async def test_an_invalid_article_id_is_422(cfg, store, raw):
    async with api(store, raise_app_exceptions=False) as c:
        r = await post(c, raw)
        latest = await c.get(f"/api/v1/articles/{raw}/jobs/latest", headers=AUTH)
    assert (r.status_code, r.json()["error"]) == (422, "invalid_article_id")
    assert (latest.status_code, latest.json()["error"]) == (422, "invalid_article_id")
    assert store.jobs == {}


@pytest.mark.parametrize("value", ["yes", "maybe", "TRUEISH", "on"])
async def test_an_invalid_dry_run_is_422_without_echoing_it(cfg, store, value):
    async with api(store) as c:
        r = await post(c, 1, f"?dry_run={value}")
    assert (r.status_code, r.json()["error"]) == (422, "invalid_dry_run")
    assert value not in r.text and store.jobs == {}


# --- idempotency per article -------------------------------------------------------------------------

async def _seed(store, article_id, status, *, dry_run=False, outcome=None, created=None):
    j = M.Job(id=M.new_job_id(), article_id=article_id, status=status, dry_run=dry_run, outcome=outcome)
    if created is not None:
        j.created_at = created
    if status in (M.SUCCEEDED, M.FAILED):
        j.finished_at = time.time()
    await store.create_job(j)
    return j


@pytest.mark.parametrize("prior_status,prior_dry,writes,request_dry,expect", [
    # an active job for the article
    (M.QUEUED, False, True, False, ("dup", 202)),
    (M.RUNNING, False, True, False, ("dup", 202)),
    (M.QUEUED, True, True, False, ("new", 202)),      # a dry job never blocks a real request
    (M.QUEUED, True, True, True, ("dup", 202)),
    (M.QUEUED, False, True, True, ("dup", 202)),      # a real job also answers a dry request
    # the latest job finished
    (M.SUCCEEDED, False, True, False, ("dup", 200)),
    (M.SUCCEEDED, True, True, False, ("new", 202)),
    (M.SUCCEEDED, True, False, False, ("dup", 200)),  # shadow mode: every request is dry
    (M.FAILED, False, True, False, ("new", 202)),     # a failed job → a new one
    (M.FAILED, True, False, False, ("new", 202)),
])
async def test_idempotency_per_article(monkeypatch, store, prior_status, prior_dry, writes, request_dry, expect):
    service_settings(monkeypatch, ARCHIVE_WRITES_ENABLED=writes)
    prior = await _seed(store, 500, prior_status, dry_run=prior_dry)
    async with api(store) as c:
        r = await post(c, 500, "?dry_run=true" if request_dry else "")
    kind, code = expect
    assert r.status_code == code
    if kind == "dup":
        assert r.json()["job_id"] == prior.id and r.json()["duplicate"] is True
        assert len(store.jobs) == 1
    else:
        assert r.json()["job_id"] != prior.id and r.json()["duplicate"] is False
        assert len(store.jobs) == 2


async def test_a_job_for_another_article_is_not_a_duplicate(cfg, store):
    async with api(store) as c:
        r1 = await post(c, 555)
        r2 = await post(c, 555)
        r3 = await post(c, 556)
    assert r2.json()["job_id"] == r1.json()["job_id"] and r2.json()["duplicate"] is True
    assert r3.json()["job_id"] != r1.json()["job_id"]


async def test_no_audit_for_article_is_a_failure_so_a_later_request_runs_again(cfg, store):
    gone = await _seed(store, 77, M.FAILED, outcome=M.NO_AUDIT_FOR_ARTICLE)
    async with api(store) as c:
        r = await post(c, 77)
    assert r.status_code == 202 and r.json()["job_id"] != gone.id


async def test_a_newer_dry_run_does_not_hide_an_older_real_success(monkeypatch, store):
    service_settings(monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    real = await _seed(store, 9, M.SUCCEEDED, dry_run=False, outcome=M.APPLIED, created=100.0)
    await _seed(store, 9, M.SUCCEEDED, dry_run=True, outcome=M.DRY_RUN, created=200.0)
    async with api(store) as c:
        r = await post(c, 9)
    assert r.status_code == 200 and r.json()["job_id"] == real.id


async def test_concurrent_requests_for_one_article_make_one_job(cfg, store):
    async with api(store) as c:
        rs = await asyncio.gather(*(post(c, 77) for _ in range(8)))
    assert {r.status_code for r in rs} == {202}
    assert len({r.json()["job_id"] for r in rs}) == 1 and len(store.jobs) == 1


async def test_a_full_queue_is_429_with_retry_after(monkeypatch, store):
    service_settings(monkeypatch, JOB_QUEUE_MAX=2, JOB_RETRY_BACKOFF_S=60)
    async with api(store) as c:
        assert (await post(c, 1)).status_code == 202
        assert (await post(c, 2)).status_code == 202
        r = await post(c, 3)
        dup = await post(c, 1)
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"
    assert r.json()["error"] == "queue_full"
    assert dup.status_code == 202 and dup.json()["duplicate"] is True, "a duplicate is not rejected by the cap"


async def test_a_real_request_can_reuse_a_recent_reviewed_dry_run(monkeypatch, store):
    service_settings(monkeypatch, ARCHIVE_WRITES_ENABLED=True, JOB_RESULT_REUSE_S=3600)
    dry = await _seed(store, 31, M.SUCCEEDED, dry_run=True, outcome=M.DRY_RUN)
    dry.data["record"] = {"verdict": "INCONCLUSIVE", "response": {"verdict": "INCONCLUSIVE"}}
    dry.data["write_blockers"] = ["writes_disabled"]   # shadow mode with a real configuration
    await store.save_job(dry)
    async with api(store) as c:
        r = await post(c, 31)
    job = await store.get_job(r.json()["job_id"])
    assert job.data["reused_from"] == dry.id and job.data["record"] == dry.data["record"]


async def test_by_default_a_real_request_reruns_instead_of_reusing(monkeypatch, store):
    service_settings(monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    dry = await _seed(store, 32, M.SUCCEEDED, dry_run=True, outcome=M.DRY_RUN)
    dry.data["record"] = {"verdict": "INCONCLUSIVE"}
    await store.save_job(dry)
    async with api(store) as c:
        r = await post(c, 32)
    assert "record" not in (await store.get_job(r.json()["job_id"])).data


# --- status reads ----------------------------------------------------------------------------------

async def test_job_reads_need_the_token_and_hide_the_record_unless_asked(cfg, store):
    j = await _seed(store, 40, M.SUCCEEDED, dry_run=True, outcome=M.DRY_RUN)
    j.audit_id = 232794
    j.data.update(record=record_by(232794),
                  comment={"requested": True, "decision": "write", "reason": "eligible", "tags_state": "set",
                           "chars": 12, "text": "Rendered C.", "posted": False},
                  write_blockers=["writes_disabled"], correlation_id=j.id)
    await store.save_job(j)
    async with api(store) as c:
        plain = await c.get(f"/api/v1/jobs/{j.id}", headers=AUTH)
        full = await c.get(f"/api/v1/jobs/{j.id}?include=result", headers=AUTH)
        latest = await c.get("/api/v1/articles/40/jobs/latest", headers=AUTH)
        latest_full = await c.get("/api/v1/articles/40/jobs/latest?include=result", headers=AUTH)
        missing = await c.get("/api/v1/jobs/nope", headers=AUTH)
        missing_article = await c.get("/api/v1/articles/41/jobs/latest", headers=AUTH)
    assert plain.status_code == 200
    p = plain.json()
    assert (p["article_id"], p["audit_id"], p["outcome"]) == (40, 232794, "dry_run")
    assert p["verdict"] == "INCONCLUSIVE" and p["flags"] == ["RELATED_REMOVALS"]
    assert p["write_blockers"] == ["writes_disabled"] and p["result"] is None
    assert p["comment"]["text"] is None, "the comment text only with include=result"
    for resp in (full, latest_full):
        assert resp.json()["comment"]["text"] == "Rendered C."
        assert resp.json()["result"]["verdict"] == "INCONCLUSIVE"
    assert latest.json()["job_id"] == j.id and latest.json()["result"] is None
    assert (missing.status_code, missing.json()["error"]) == (404, "job_not_found")
    assert (missing_article.status_code, missing_article.json()["error"]) == (404, "job_not_found")


# --- end to end: POST → worker → archive writes → GET ------------------------------------------

async def test_post_then_the_worker_posts_the_comment_and_the_job_reads_applied(monkeypatch, store):
    from src.archive_flow import comments, rules, runner
    from src.jobs.worker import JobWorker
    from tests._service_support import FakeArchive, art, fake_runtime, open_audit, response_for

    async def fake_run_audit(request, **kw):
        return response_for(record_by(232794))
    monkeypatch.setattr(runner, "run_audit", fake_run_audit)
    monkeypatch.setattr(comments, "_stamp", lambda: "2026-10-05")
    cfg = service_settings(monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    archive = FakeArchive([open_audit(232794)])
    worker = JobWorker(store, fake_runtime(archive), cfg)
    async with api(store) as c:
        r = await post(c, art(232794))
        job = await worker.process(await store.claim_next(60))
        got = await c.get(r.json()["status_url"] + "?include=result", headers=AUTH)
        again = await post(c, art(232794))
    assert r.status_code == 202 and r.json()["dry_run"] is False
    assert job.outcome == M.APPLIED and [w[0] for w in archive.writes()] == ["PATCH", "POST"]
    body = got.json()
    assert (body["status"], body["outcome"], body["audit_id"]) == ("succeeded", "applied", 232794)
    assert body["comment"]["posted"] is True and body["comment"]["discussion_id"] == 1792
    assert archive.writes()[1][2] == body["comment"]["text"] == comments.render(
        "C", body["result"], tags_state=rules.TAGS_SET)
    assert (again.status_code, again.json()["job_id"], again.json()["duplicate"]) == (200, job.id, True)
