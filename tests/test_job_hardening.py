"""Regression tests for the job hardening: result reuse, held comments, unknown discussion counts,
the fail-streak pause, leases and restart recovery, readiness caching and the /api/audit 422 body.
None of these touches a result: the pipeline is stubbed and the rendered comments come from the
unchanged templates.
"""
import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from src.api import app as app_module
from src.api.app import app
from src.archive_flow import comments, runner
from src.jobs import models as M
from src.jobs.store import MemoryJobStore, SqliteJobStore
from src.jobs.worker import MAX_INTERRUPTIONS, JobWorker
from tests._service_support import (
    AUTH, INCONCLUSIVE_RELATED, FakeArchive, FakeProvider, art, fake_runtime, open_audit, record_by,
    response_for, service_settings,
)

GOOD = INCONCLUSIVE_RELATED


@pytest.fixture(autouse=True)
def frozen_stamp(monkeypatch):
    monkeypatch.setattr(comments, "_stamp", lambda: "2026-10-05")


@pytest.fixture
def pipeline(monkeypatch):
    state = {"calls": 0, "raise": None}

    async def fake_run_audit(request, *, agents, search_provider, catalog, **kw):
        state["calls"] += 1
        if state["raise"] is not None:
            raise state["raise"]
        return response_for(record_by(GOOD))

    monkeypatch.setattr(runner, "run_audit", fake_run_audit)
    return state


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


async def post(c, article_id, query=""):
    return await c.post(f"/api/v1/articles/{article_id}/audit{query}", headers=AUTH)


def make(archive, monkeypatch, *, provider=None, **settings):
    cfg = service_settings(monkeypatch, **settings)
    store = MemoryJobStore()
    worker = JobWorker(store, fake_runtime(archive, provider=provider), cfg, idle_poll_s=0.01)
    return worker, store


async def run_specific(worker, store, job_id):
    """Claim THIS job (as a second worker loop would) and process it."""
    j = await store.get_job(job_id)
    j.status, j.attempts = M.RUNNING, j.attempts + 1
    await store.save_job(j)
    return await worker.process(j)


# --- JOB_RESULT_REUSE_S: only records made with every publish gate open except the write switch ----

async def _seed_dry(store, aid, blockers):
    data = {"record": record_by(GOOD)}
    if blockers is not None:
        data["write_blockers"] = blockers
    dry = M.Job(id=M.new_job_id(), article_id=art(aid), audit_id=aid, status=M.SUCCEEDED,
                outcome=M.DRY_RUN, dry_run=True,
                finished_at=time.time(), data=data)
    await store.create_job(dry)
    return dry


@pytest.mark.parametrize("blockers,reused", [
    (["writes_disabled"], True),
    ([], True),
    (["writes_disabled", "synthetic_model", "stub_search"], False),
    (["no_concept_catalog"], False),
    (None, False),                       # no recorded blocker list: fail closed
])
async def test_reuse_only_takes_a_record_made_under_a_real_configuration(monkeypatch, blockers, reused):
    service_settings(monkeypatch, ARCHIVE_WRITES_ENABLED=True, JOB_RESULT_REUSE_S=86400)
    store = MemoryJobStore()
    dry = await _seed_dry(store, GOOD, blockers)
    async with api(store) as c:
        r = await post(c, art(GOOD))
    new = await store.get_job(r.json()["job_id"])
    assert (new.data.get("reused_from") == dry.id) is reused
    assert ("record" in new.data) is reused


async def test_a_synthetic_era_record_is_audited_again_not_published(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)])
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True, JOB_RESULT_REUSE_S=86400)
    await _seed_dry(store, GOOD, ["writes_disabled", "synthetic_model"])
    async with api(store) as c:
        r = await post(c, art(GOOD))
    job = await run_specific(worker, store, r.json()["job_id"])
    assert pipeline["calls"] == 1 and job.outcome == M.APPLIED


async def test_a_reused_record_for_another_audit_is_dropped_and_the_audit_runs(monkeypatch, pipeline):
    X, Y, ART = GOOD, 555001, 7777
    archive = FakeArchive([open_audit(X, article_id=ART)])
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True, JOB_RESULT_REUSE_S=3600)
    async with api(store) as c:
        r = await post(c, ART, "?dry_run=true")
    d = await run_specific(worker, store, r.json()["job_id"])
    assert (d.audit_id, d.outcome) == (X, M.DRY_RUN)
    archive.audits = {Y: open_audit(Y, article_id=ART)}     # the archive now answers with audit Y
    async with api(store) as c:
        r2 = await post(c, ART)
    assert (await store.get_job(r2.json()["job_id"])).data.get("reused_from") == d.id
    job = await run_specific(worker, store, r2.json()["job_id"])
    assert job.audit_id == Y and "reused_from" not in job.data
    assert pipeline["calls"] == 2, "the audit ran for Y instead of reusing X's record"
    # The stubbed pipeline answers with GOOD's response; what matters is that no write went to an
    # audit this job never pre-checked through a reused record.
    assert job.data["record"]["audit_id"] == Y


# --- apply phase: unknown discussion count, held comment ----------------------------------------

class CountDown(FakeArchive):
    async def count_discussions(self, aid):
        self.calls.append(("COUNT", aid))
        return None


async def test_an_unknown_discussion_count_retries_instead_of_finishing(monkeypatch, pipeline):
    archive = CountDown([open_audit(GOOD, subject_tag_notations=["medtop:01000000"])])
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    async with api(store) as c:
        r = await post(c, art(GOOD))
    job = await run_specific(worker, store, r.json()["job_id"])
    assert (job.status, job.error["code"]) == (M.QUEUED, "archive_unavailable")
    archive.__class__ = FakeArchive            # the count is readable again
    job = await run_specific(worker, store, job.id)
    assert job.outcome == M.APPLIED and [w[0] for w in archive.writes()] == ["POST"]
    assert pipeline["calls"] == 1, "the apply retry reuses the stored record"


async def test_tags_written_with_an_unknown_count_retry_only_the_comment(monkeypatch, pipeline):
    archive = CountDown([open_audit(GOOD)])
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    async with api(store) as c:
        r = await post(c, art(GOOD))
    job = await run_specific(worker, store, r.json()["job_id"])
    assert job.status == M.QUEUED and [w[0] for w in archive.writes()] == ["PATCH"]
    archive.__class__ = FakeArchive
    job = await run_specific(worker, store, job.id)
    assert job.outcome == M.APPLIED and [w[0] for w in archive.writes()] == ["PATCH", "POST"]


async def test_an_unknown_count_on_every_attempt_ends_failed_not_succeeded(monkeypatch, pipeline):
    archive = CountDown([open_audit(GOOD, subject_tag_notations=["medtop:01000000"])])
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    async with api(store) as c:
        r = await post(c, art(GOOD))
    job = await store.get_job(r.json()["job_id"])
    for _ in range(5):
        job = await run_specific(worker, store, job.id)
        if not job.active:
            break
    assert (job.status, job.outcome) == (M.FAILED, M.ARCHIVE_UNAVAILABLE)
    assert archive.writes() == []


async def test_the_comment_is_held_while_the_tag_write_is_retried(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)], patch_ok=False)
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    async with api(store) as c:
        r = await post(c, art(GOOD))
    j1 = await run_specific(worker, store, r.json()["job_id"])
    assert (j1.status, j1.error["code"]) == (M.QUEUED, "apply_failed")
    assert [w[0] for w in archive.writes()] == ["PATCH"], "no comment while the tags are pending"
    assert j1.data["comment"]["reason"].startswith("held")
    archive.patch_ok = True
    j2 = await run_specific(worker, store, j1.id)
    assert j2.outcome == M.APPLIED
    posts = [w for w in archive.writes() if w[0] == "POST"]
    assert len(posts) == 1 and "PROPOSED ONLY" not in posts[0][2]
    assert j2.data["comment"]["tags_state"] == "set"


async def test_on_the_last_apply_attempt_the_comment_is_posted_with_honest_wording(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)], patch_ok=False)
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True, JOB_APPLY_MAX_ATTEMPTS=2)
    async with api(store) as c:
        r = await post(c, art(GOOD))
    j = await run_specific(worker, store, r.json()["job_id"])
    assert [w[0] for w in archive.writes()] == ["PATCH"]
    j = await run_specific(worker, store, j.id)
    assert (j.status, j.outcome) == (M.FAILED, M.APPLY_FAILED)
    posts = [w for w in archive.writes() if w[0] == "POST"]
    assert len(posts) == 1 and "PROPOSED ONLY" in posts[0][2], "tags did not land: the comment says so"


async def test_a_dry_run_never_holds_the_comment(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)])
    worker, store = make(archive, monkeypatch)
    async with api(store) as c:
        r = await post(c, art(GOOD))
    j = await run_specific(worker, store, r.json()["job_id"])
    assert j.outcome == M.DRY_RUN and j.data["comment"]["text"] and archive.writes() == []


# --- the fail-streak pause does not burn the job's attempts -------------------------------------

async def test_a_lone_job_on_a_dead_model_waits_for_health_instead_of_failing(monkeypatch, pipeline):
    pipeline["raise"] = ConnectionError("model down")
    prov = FakeProvider(healthy=False)
    archive = FakeArchive([open_audit(GOOD)])
    worker, store = make(archive, monkeypatch, provider=prov)
    async with api(store) as c:
        r = await post(c, art(GOOD))
    jid = r.json()["job_id"]
    await worker.start()
    for _ in range(300):
        if worker.paused and prov.health_calls:
            break
        await asyncio.sleep(0.01)
    await worker.stop(0.1)
    j = await store.get_job(jid)
    assert worker.paused and prov.health_calls >= 1
    assert j.status == M.QUEUED and j.data["failures"] == 2, "the pausing attempt is not counted"


# --- leases and restart recovery -----------------------------------------------------------------

@pytest.fixture(params=["memory", "sqlite"])
def any_store(request, tmp_path):
    return MemoryJobStore() if request.param == "memory" else SqliteJobStore(str(tmp_path / "j.sqlite3"))


async def test_an_expired_lease_is_reclaimed(any_store):
    job = M.Job(id=M.new_job_id(), article_id=1)
    await any_store.create_job(job)
    now = time.time()
    assert (await any_store.claim_next(lease_s=60, now=now)).id == job.id
    assert await any_store.claim_next(lease_s=60, now=now + 30) is None, "lease still held"
    again = await any_store.claim_next(lease_s=60, now=now + 120)
    assert again.id == job.id and again.attempts == 2 and again.data["interrupted"] == 1


async def test_restart_recovery_fails_a_job_interrupted_too_often(any_store):
    job = M.Job(id=M.new_job_id(), article_id=1)
    await any_store.create_job(job)
    for _ in range(MAX_INTERRUPTIONS):
        assert await any_store.claim_next(lease_s=60) is not None
        assert await any_store.requeue_running(MAX_INTERRUPTIONS) == 1
    assert await any_store.claim_next(lease_s=60) is not None
    assert await any_store.requeue_running(MAX_INTERRUPTIONS) == 0
    j = await any_store.get_job(job.id)
    assert (j.status, j.outcome, j.error["code"]) == (M.FAILED, M.FAILED_OUTCOME, "internal")
    assert await any_store.claim_next(lease_s=60) is None


async def test_every_phase_renews_the_lease(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)])
    worker, store = make(archive, monkeypatch, JOB_CASE_TIMEOUT_S=600)
    leases = []
    orig = store.save_job

    async def spy(job):
        if job.status == M.RUNNING:
            leases.append(job.lease_until)
        await orig(job)
    store.save_job = spy
    job = M.Job(id=M.new_job_id(), article_id=art(GOOD), dry_run=True)
    await store.create_job(job)
    t0 = time.time()
    await worker.process(await store.claim_next(1))
    assert leases and all(lu >= t0 + 600 for lu in leases)


# --- /ready caching ------------------------------------------------------------------------------

async def test_ready_is_evaluated_at_most_once_per_cache_window(monkeypatch):
    service_settings(monkeypatch, WEB_SEARCH_PROVIDER="duckduckgo", API_AUTH_TOKEN="ready-tok")

    class CountingStore(MemoryJobStore):
        checks = 0

        async def check(self):
            CountingStore.checks += 1
            return True
    rt = fake_runtime(FakeArchive([]))
    saved = {k: getattr(app.state, k, None) for k in ("runtime", "ready_cache")}
    app.state.runtime, app.state.ready_cache = rt, None
    try:
        async with api(CountingStore()) as c:
            codes = [(await c.get("/ready")).status_code for _ in range(5)]
            anon = await c.get("/ready")
            full = await c.get("/ready", headers={"Authorization": "Bearer ready-tok"})
            monkeypatch.setattr(app_module, "READY_CACHE_S", 0.0)
            app.state.ready_cache = None
            await c.get("/ready")
    finally:
        for k, v in saved.items():
            setattr(app.state, k, v)
    assert len(set(codes)) == 1
    assert set(anon.json()) == {"status"} and full.json()["status"] == anon.json()["status"]
    assert {"checks", "writes_enabled", "write_blockers"} <= set(full.json())
    assert CountingStore.checks == 2 and rt.provider.health_calls == 2, "both views share one evaluation"


# --- /api/audit 422 never echoes the submitted values -------------------------------------------

async def test_the_audit_endpoint_422_names_fields_not_values(monkeypatch):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/audit", json={"zz": "SECRETVALUE"})
    assert r.status_code == 422
    assert "SECRETVALUE" not in r.text and "removed_article" in r.json()["detail"]
