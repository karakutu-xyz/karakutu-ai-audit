"""The job worker: one job = the batch flow for one article's audit.

The archive is a FakeArchive, the six-step pipeline is stubbed at `src.archive_flow.runner.run_audit`
(so the REAL `build_case`, `run_case`, `apply_record`, rules and comment templates run), and jobs are
claimed and processed one at a time so every transition can be asserted.
"""
import asyncio
import json
import logging

import pytest
from pydantic_ai.models.test import TestModel

from src.api.correlation import CORRELATION_ID
from src.archive_flow import applier, comments, rules, runner
from src.jobs import models as M
from src.jobs.gates import archive_degradation, write_blockers
from src.jobs.store import MemoryJobStore, SqliteJobStore
from src.jobs.worker import JobWorker
from tests._service_support import (
    FakeArchive, FakeProvider, art, fake_runtime, open_audit, record_by, response_for, service_settings,
)

GOOD = 232794            # INCONCLUSIVE + RELATED_REMOVALS, archive fully readable
DEGRADED = 232777        # the archive was unreachable during the audit


@pytest.fixture(autouse=True)
def frozen_stamp(monkeypatch):
    monkeypatch.setattr(comments, "_stamp", lambda: "2026-10-05")


@pytest.fixture
def pipeline(monkeypatch):
    """Stub the six-step pipeline: returns the golden record's response for the case's audit."""
    state = {"calls": 0, "source": GOOD, "raise": None, "seen_cid": [], "content_len": []}

    async def fake_run_audit(request, *, agents, search_provider, catalog, **kw):
        state["calls"] += 1
        state["seen_cid"].append(CORRELATION_ID.get())
        state["content_len"].append(len(request.removed_article.content))
        state["kw"] = kw
        if state["raise"] is not None:
            raise state["raise"]
        return response_for(record_by(state["source"]))

    monkeypatch.setattr(runner, "run_audit", fake_run_audit)
    return state


def make(archive, monkeypatch, *, store=None, rt=None, **settings):
    cfg = service_settings(monkeypatch, **settings)
    store = store or MemoryJobStore()
    worker = JobWorker(store, rt or fake_runtime(archive), cfg, idle_poll_s=0.01)
    return worker, store


async def enqueue(store, aid=None, *, article_id=None, dry_run=False, writes=False):
    job = M.Job(id=M.new_job_id(), article_id=article_id or art(aid),
                dry_run=bool(dry_run or not writes), requested_dry_run=dry_run)
    await store.create_job(job)
    return job


async def run_once(worker, store):
    job = await store.claim_next(60)
    assert job is not None, "nothing claimable"
    return await worker.process(job)


async def drain(worker, store, limit=10):
    job = None
    for _ in range(limit):
        nxt = await store.claim_next(60)
        if nxt is None:
            break
        job = await worker.process(nxt)
    return job


# --- happy paths ---------------------------------------------------------------------------------

async def test_shadow_mode_runs_the_whole_flow_renders_comment_c_and_writes_nothing(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)])
    worker, store = make(archive, monkeypatch)
    await enqueue(store, GOOD)
    job = await run_once(worker, store)

    assert (job.status, job.outcome, job.phase) == (M.SUCCEEDED, M.DRY_RUN, "done")
    assert job.dry_run is True and job.data["write_blockers"] == ["writes_disabled"]
    assert archive.writes() == [], "a dry run sends no PATCH and no POST"
    rec = job.data["record"]
    assert rec["verdict"] == "INCONCLUSIVE" and rec["flags"] == ["RELATED_REMOVALS"]
    assert list(rec) == ["audit_id", "article_id", "url", "title", "newspaper", "publish_date", "http_status",
                         "created_at", "content_chars", "list", "probe", "elapsed_s", "stage_times_s",
                         "error", "logs", "verdict", "flags", "classification", "response", "model"]
    assert rec["model"] == {"requested": "synthetic", "served": []} == job.data["model"]   # TestModel
    expected = comments.render("C", rec, tags_state=rules.TAGS_SET)    # projected write → "set"
    assert job.data["comment"]["text"] == expected
    assert job.data["comment"]["decision"] == "write" and job.data["comment"]["posted"] is False
    assert job.data["tags"]["decision"] == "write" and job.data["tags"]["written"] is False
    assert await store.get_ledger(GOOD) == {}, "a dry run never touches the ledger"
    assert job.error is None


async def test_with_writes_on_the_job_patches_posts_and_records_the_ledger(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)])
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    await enqueue(store, GOOD, writes=True)
    job = await run_once(worker, store)

    assert (job.status, job.outcome, job.dry_run) == (M.SUCCEEDED, M.APPLIED, False)
    rec = job.data["record"]
    payload = applier.classification_payload(rec)
    assert [c[0] for c in archive.writes()] == ["PATCH", "POST"]
    assert archive.writes()[0][2] == payload
    assert archive.writes()[1][2] == comments.render("C", rec, tags_state=rules.TAGS_SET)
    ledger = await store.get_ledger(GOOD)
    assert ledger["tags"]["ok"] is True and ledger["tags"]["payload"] == payload
    assert {k: v for k, v in ledger["comment"].items() if k != "at"} == {
        "ok": True, "template": "C", "tags_state": "set", "discussion_id": 1792,
        "view_url": f"https://karakutu.xyz/audits/{GOOD}/discussions#/news/{rec['url']}"}
    assert job.data["comment"]["discussion_id"] == 1792


async def test_the_pipeline_gets_the_capped_content_a_per_job_client_and_the_job_correlation_id(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)], body="x" * 150_000)
    worker, store = make(archive, monkeypatch)
    seeded = await enqueue(store, GOOD)
    await run_once(worker, store)
    assert pipeline["content_len"] == [runner.CONTENT_LIMIT]
    assert pipeline["seen_cid"] == [seeded.id], "stage log lines stitch to the job"
    assert pipeline["kw"] == {}, "an unknown client type: the stages build their own, as the batch did"
    assert CORRELATION_ID.get() == "-"


def test_the_default_job_client_is_fresh_but_shares_the_jwt_cache():
    from src.clients.karakutu import KarakutuClient
    from src.jobs.worker import default_job_client
    base = KarakutuClient(base_url="https://archive.invalid", wallet_path="/nonexistent", timeout=3)
    base.__dict__.setdefault("_window_cache", {})["stale"] = (0, [])
    c = default_job_client(fake_runtime(base))
    assert c is not base and c.auth is base.auth and c.base_url == base.base_url and c.timeout == 3
    assert not c.__dict__.get("_window_cache"), "no step-4 window carried over from an earlier audit"


# --- publish gates ---------------------------------------------------------------------------------

@pytest.mark.parametrize("breaker,blocker", [
    ("model", "synthetic_model"), ("search", "stub_search"), ("catalog", "no_concept_catalog"),
    ("wallet", "archive_client_disabled"),
])
async def test_each_publish_gate_forces_a_dry_run_and_says_why(monkeypatch, pipeline, breaker, blocker):
    archive = FakeArchive([open_audit(GOOD)])
    rt = fake_runtime(archive, model=TestModel() if breaker == "model" else None,
                      catalog=None if breaker == "catalog" else "CATALOG")
    over = {"ARCHIVE_WRITES_ENABLED": True}
    if breaker == "search":
        over["WEB_SEARCH_PROVIDER"] = "stub"
    if breaker == "wallet":
        archive.enabled = False
    worker, store = make(archive, monkeypatch, rt=rt, **over)
    await enqueue(store, GOOD, writes=True)
    job = await run_once(worker, store)
    assert job.data["write_blockers"] == [blocker]
    assert job.dry_run is True and job.outcome == M.DRY_RUN
    assert archive.writes() == []


def test_write_blockers_all_clear_only_when_every_gate_passes(monkeypatch):
    cfg = service_settings(monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    assert write_blockers(cfg, fake_runtime(FakeArchive())) == []
    cfg2 = service_settings(monkeypatch, ARCHIVE_WRITES_ENABLED=False, WEB_SEARCH_PROVIDER="stub")
    assert write_blockers(cfg2, None) == ["writes_disabled", "synthetic_model", "stub_search",
                                          "no_concept_catalog", "archive_client_disabled"]


async def test_a_requested_dry_run_is_honoured_even_with_writes_on(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)])
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    await enqueue(store, GOOD, dry_run=True, writes=True)
    job = await run_once(worker, store)
    assert job.outcome == M.DRY_RUN and job.data["write_blockers"] == [] and archive.writes() == []


# --- archive degraded during the audit ---------------------------------------------------

def test_archive_degradation_reads_the_exact_stage_signals():
    assert archive_degradation(record_by(DEGRADED)) == ["related_removals_unreadable", "similarity_unreachable"]
    assert archive_degradation(record_by(GOOD)) == []

    def rec(sim, rr):
        return {"response": {"stages": [{"stage": "similarity", "details": sim},
                                        {"stage": "related_removals", "details": rr}]}}
    empty = {"available": False, "reason": "the archive returned no similar articles (URL not archived, "
                                           "nothing similar, or archive unreachable)"}
    readable = {"available": True, "reason": "277 removal(s) in the window; none shares this article's key terms"}
    unreadable = {"available": False, "reason": "the archive's audit list could not be read"}
    disabled = {"available": False, "reason": "archive client disabled (no wallet keyfile)"}
    assert archive_degradation(rec(empty, readable)) == [], "step 4 proves the archive answered: a genuine empty"
    assert archive_degradation(rec(empty, unreadable)) == ["related_removals_unreadable", "similarity_unreachable"]
    assert archive_degradation(rec({"available": True}, unreadable)) == ["related_removals_unreadable"]
    assert archive_degradation(rec(empty, disabled)) == ["similarity_unreachable"]
    assert archive_degradation(rec({"available": True}, {"available": False, "reason": "crashed"})) == []
    assert archive_degradation({"response": None}) == []


async def test_a_degraded_record_is_withheld_retried_and_never_posted(monkeypatch, pipeline):
    pipeline["source"] = DEGRADED
    archive = FakeArchive([open_audit(DEGRADED)])
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True, JOB_MAX_ATTEMPTS=2)
    await enqueue(store, DEGRADED, writes=True)
    first = await run_once(worker, store)
    assert first.status == M.QUEUED and first.error["code"] == "archive_degraded"
    assert first.data["record_degraded"] is True
    last = await run_once(worker, store)
    assert (last.status, last.outcome) == (M.FAILED, M.ARCHIVE_DEGRADED)
    assert pipeline["calls"] == 2, "a degraded record is never re-used: the audit re-runs"
    assert archive.writes() == [] and await store.get_ledger(DEGRADED) == {}
    assert record_by(DEGRADED)["response"] == last.data["record"]["response"], "the record is not altered"


async def test_a_degraded_record_that_recovers_on_retry_is_applied(monkeypatch, pipeline):
    pipeline["source"] = DEGRADED
    archive = FakeArchive([open_audit(DEGRADED)])
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    await enqueue(store, DEGRADED, writes=True)
    await run_once(worker, store)
    pipeline["source"] = GOOD          # the archive is back: the re-run reads it fine
    job = await run_once(worker, store)
    assert job.outcome == M.APPLIED and len(archive.writes()) == 2


async def test_a_degraded_dry_run_still_shows_the_full_result(monkeypatch, pipeline):
    pipeline["source"] = DEGRADED
    archive = FakeArchive([open_audit(DEGRADED)])
    worker, store = make(archive, monkeypatch)
    await enqueue(store, DEGRADED)
    job = await run_once(worker, store)
    assert job.outcome == M.DRY_RUN and job.data["archive_degradation"]
    assert job.data["comment"]["text"]


# --- pre-check ---------------------------------------------------------------------------------------

async def test_the_audit_is_looked_up_by_article_and_recorded_on_the_job(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD, article_id=7777)])
    worker, store = make(archive, monkeypatch)
    await enqueue(store, article_id=7777)
    job = await run_once(worker, store)
    assert (job.audit_id, job.outcome) == (GOOD, M.DRY_RUN)
    assert archive.calls[0] == ("BY_ARTICLE", 7777), "one exact lookup, no listing scan"
    assert job.data["record"]["audit_id"] == GOOD


async def test_an_article_without_an_audit_fails_without_running_the_model(monkeypatch, pipeline):
    worker, store = make(FakeArchive([]), monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    await enqueue(store, article_id=404404, writes=True)
    job = await run_once(worker, store)
    assert (job.status, job.outcome, job.error["code"]) == (M.FAILED, M.NO_AUDIT_FOR_ARTICLE,
                                                            "no_audit_for_article")
    assert job.audit_id is None and pipeline["calls"] == 0


async def test_an_unreachable_archive_is_retried_then_archive_unavailable(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)], precheck_status=(None, {"error": "ConnectError"}))
    worker, store = make(archive, monkeypatch, JOB_MAX_ATTEMPTS=3)
    await enqueue(store, GOOD)
    j1 = await run_once(worker, store)
    assert j1.status == M.QUEUED and j1.error == M.error("archive_unavailable")
    last = await drain(worker, store)
    assert (last.status, last.outcome, last.data["failures"]) == (M.FAILED, M.ARCHIVE_UNAVAILABLE, 3)
    assert pipeline["calls"] == 0


async def test_a_disabled_archive_client_fails_without_retrying(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)], precheck_status=(None, {"error": "disabled"}))
    worker, store = make(archive, monkeypatch)
    await enqueue(store, GOOD)
    job = await run_once(worker, store)
    assert (job.status, job.outcome, job.error["code"]) == (M.FAILED, M.ARCHIVE_UNAVAILABLE, "archive_disabled")


async def test_an_ineligible_audit_is_left_alone(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD, status=4)])
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    await enqueue(store, GOOD, writes=True)
    job = await run_once(worker, store)
    assert job.outcome == M.SKIPPED_INELIGIBLE and pipeline["calls"] == 0 and archive.writes() == []


async def test_nothing_to_do_skips_the_run_but_a_dry_run_still_shows_the_result(monkeypatch, pipeline):
    audit = open_audit(GOOD, subject_tag_notations=["medtop:01000000"])
    archive = FakeArchive([audit], discussions={GOOD: 1})
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    await enqueue(store, GOOD, writes=True)
    job = await run_once(worker, store)
    assert job.outcome == M.SKIPPED_NOTHING_TO_DO and pipeline["calls"] == 0
    await enqueue(store, GOOD, dry_run=True, writes=True)
    dry = await run_once(worker, store)
    assert dry.outcome == M.DRY_RUN and pipeline["calls"] == 1
    assert dry.data["precheck"]["nothing_to_do"] is True
    assert dry.data["comment"]["decision"] == "skip" and dry.data["tags"]["decision"] == "skip"
    assert dry.data["comment"]["tags_state"] == rules.TAGS_PROPOSED_EXISTING


class Answers(FakeArchive):
    """The by-article lookup answers with a fixed audit, whatever article was asked for."""

    def __init__(self, answer, **kw):
        super().__init__([open_audit(GOOD)], **kw)
        self.answer = answer

    async def get_audit_by_article(self, article_id):
        self.calls.append(("BY_ARTICLE", article_id))
        return 200, self.answer


@pytest.mark.parametrize("answer", [
    open_audit(GOOD, article_id=111),                                  # another article's audit
    {**open_audit(GOOD), "article": None},                             # no article embedded
    {**open_audit(GOOD), "article": {"id": "222"}},                    # a string id
    {**open_audit(GOOD), "article": {"id": True}},                     # a bool is not an id
    {**open_audit(GOOD, article_id=222), "id": None},                  # no audit id
])
async def test_an_answer_not_tied_to_the_requested_article_fails_closed(monkeypatch, pipeline, answer):
    archive = Answers(answer)
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    await enqueue(store, article_id=222, writes=True)
    job = await run_once(worker, store)
    assert (job.status, job.outcome, job.error["code"]) == (M.FAILED, M.REJECTED_ARTICLE_MISMATCH, "article_mismatch")
    assert pipeline["calls"] == 0 and archive.writes() == [] and job.audit_id is None


async def test_a_thin_body_is_skipped_and_a_missing_body_during_an_outage_is_retried(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)], body="too short")
    worker, store = make(archive, monkeypatch)
    await enqueue(store, GOOD)
    job = await run_once(worker, store)
    assert job.outcome == M.SKIPPED_THIN_BODY and job.data["fetch"]["body_chars"] == 9

    class Flaky(FakeArchive):
        async def get_with_status(self, path, params=None, **kw):
            if path == "/articles":
                return None, {"error": "ConnectError"}
            return await super().get_with_status(path, params)

    flaky = Flaky([open_audit(GOOD)], body=None)
    worker2, store2 = make(flaky, monkeypatch)
    await enqueue(store2, GOOD)
    claimed = await store2.claim_next(60)
    # build_case's default retry sleep is bound at definition; pass a fast one via the worker hook.
    from src.archive_flow import intake

    async def fast_build(client, **kw):
        return await intake.build_case(client, retry_sleep_s=0, **kw)
    worker2._build_case = fast_build
    j2 = await worker2.process(claimed)
    assert j2.status == M.QUEUED and j2.error["code"] == "archive_unavailable"


# --- unusable results, fail streak, model-health pause --------------------------------------------------

async def test_an_unusable_result_is_retried_and_never_written(monkeypatch, pipeline):
    pipeline["raise"] = RuntimeError("upstream said: secret-internal-host.local:8443 refused")
    archive = FakeArchive([open_audit(GOOD)])
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True, JOB_MAX_ATTEMPTS=2)
    await enqueue(store, GOOD, writes=True)
    j1 = await run_once(worker, store)
    assert j1.status == M.QUEUED and j1.error["code"] == "audit_unusable"
    j2 = await run_once(worker, store)
    assert (j2.status, j2.outcome) == (M.FAILED, M.AUDIT_FAILED)
    assert archive.writes() == []
    assert "secret-internal-host" not in json.dumps(j2.error)


async def test_a_case_timeout_is_reported_as_such(monkeypatch, pipeline):
    async def slow(*a, **k):
        await asyncio.sleep(5)
    monkeypatch.setattr(runner, "run_audit", slow)
    worker, store = make(FakeArchive([open_audit(GOOD)]), monkeypatch, JOB_CASE_TIMEOUT_S=0.01)
    await enqueue(store, GOOD)
    job = await run_once(worker, store)
    assert job.error["code"] == "case_timeout" and job.data["record"]["verdict"] == "ERROR"


async def test_a_fail_streak_pauses_the_worker_until_the_model_is_healthy(monkeypatch, pipeline):
    pipeline["raise"] = RuntimeError("model went away")
    provider = FakeProvider(healthy=False)
    archive = FakeArchive([open_audit(a) for a in (1, 2, 3)])
    worker, store = make(archive, monkeypatch, rt=fake_runtime(archive, provider=provider),
                         JOB_FAIL_STREAK=3, JOB_MAX_ATTEMPTS=1)
    for a in (1, 2, 3):
        await enqueue(store, a)
    await run_once(worker, store)
    await run_once(worker, store)
    assert worker.paused is False
    await run_once(worker, store)
    assert worker.paused is True and worker.fail_streak == 3
    await worker._wait_for_model_health()
    assert worker.paused is True and worker.model_unavailable is True, "the wait expired: stay paused"
    provider.healthy = True
    await worker._wait_for_model_health()
    assert worker.paused is False and worker.model_unavailable is False and worker.fail_streak == 0


async def test_a_paused_worker_claims_nothing(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)])
    worker, store = make(archive, monkeypatch)
    worker.paused = True
    waits = []

    async def never_healthy(provider, **kw):
        waits.append(kw)
        await asyncio.sleep(0)
        return False
    worker._wait_for_model = never_healthy
    await enqueue(store, GOOD)
    await worker.start()
    await asyncio.sleep(0.05)
    await worker.stop(1)
    assert waits and pipeline["calls"] == 0
    assert (await store.jobs_for(art(GOOD)))[0].status == M.QUEUED


# --- apply-phase retries -------------------------------------------------------------------------------

async def test_a_failed_write_retries_only_the_apply_phase(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)], post_ok=False)
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    await enqueue(store, GOOD, writes=True)
    j1 = await run_once(worker, store)
    assert j1.status == M.QUEUED and j1.error["code"] == "apply_failed" and j1.apply_attempts == 1
    ledger = await store.get_ledger(GOOD)
    assert ledger["tags"]["ok"] is True and ledger["comment"]["ok"] is False
    archive.post_ok = True
    j2 = await run_once(worker, store)
    assert j2.outcome == M.APPLIED
    assert pipeline["calls"] == 1, "the stored record is re-used: the model is not re-run for a write retry"
    assert [c[0] for c in archive.writes()] == ["PATCH", "POST", "POST"], "tags are not re-sent"
    assert (await store.get_ledger(GOOD))["comment"]["ok"] is True


async def test_write_retries_are_bounded(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)], patch_ok=False, post_ok=False)
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True, JOB_APPLY_MAX_ATTEMPTS=2)
    await enqueue(store, GOOD, writes=True)
    last = await drain(worker, store)
    assert (last.status, last.outcome, last.apply_attempts) == (M.FAILED, M.APPLY_FAILED, 2)


async def test_a_second_job_for_an_applied_audit_writes_nothing_more(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)])
    worker, store = make(archive, monkeypatch, ARCHIVE_WRITES_ENABLED=True)
    await enqueue(store, GOOD, writes=True)
    await run_once(worker, store)
    await enqueue(store, GOOD, writes=True)
    again = await run_once(worker, store)
    assert again.outcome == M.SKIPPED_NOTHING_TO_DO and len(archive.writes()) == 2


# --- lifecycle: restart requeue, graceful shutdown, loop -------------------------------------------------

async def test_the_loop_processes_queued_jobs_and_notify_wakes_it(monkeypatch, pipeline):
    archive = FakeArchive([open_audit(GOOD)])
    worker, store = make(archive, monkeypatch)
    worker.idle_poll_s = 10            # only notify() can wake it quickly
    await worker.start()
    try:
        await enqueue(store, GOOD)
        worker.notify()
        for _ in range(200):
            j = (await store.jobs_for(art(GOOD)))[0]
            if j.status == M.SUCCEEDED:
                break
            await asyncio.sleep(0.01)
        assert j.outcome == M.DRY_RUN
    finally:
        await worker.stop(1)
    assert not worker.running


async def test_a_job_left_running_by_a_crash_is_requeued_and_finished_on_restart(monkeypatch, pipeline, tmp_path):
    archive = FakeArchive([open_audit(GOOD)])
    store = SqliteJobStore(str(tmp_path / "jobs.sqlite3"))
    await enqueue(store, GOOD)
    crashed = await store.claim_next(60)
    crashed.phase = "auditing"
    await store.save_job(crashed)                      # the process died here
    worker, _ = make(archive, monkeypatch, store=store)
    await worker.start()
    try:
        for _ in range(200):
            j = await store.get_job(crashed.id)
            if j.status == M.SUCCEEDED:
                break
            await asyncio.sleep(0.01)
    finally:
        await worker.stop(1)
        await store.close()
    assert (j.status, j.outcome, j.attempts) == (M.SUCCEEDED, M.DRY_RUN, 2)


async def test_graceful_shutdown_leaves_an_unfinished_job_running_for_the_next_start(monkeypatch, pipeline):
    started = asyncio.Event()

    async def slow(*a, **k):
        started.set()
        await asyncio.sleep(30)
    monkeypatch.setattr(runner, "run_audit", slow)
    archive = FakeArchive([open_audit(GOOD)])
    worker, store = make(archive, monkeypatch)
    job = await enqueue(store, GOOD)
    await worker.start()
    await asyncio.wait_for(started.wait(), 2)
    await worker.stop(0.05)
    left = await store.get_job(job.id)
    assert left.status == M.RUNNING and left.phase == "auditing"
    assert await store.requeue_running() == 1


async def test_logs_carry_job_ids_but_never_exception_text(monkeypatch, pipeline, caplog):
    archive = FakeArchive([open_audit(GOOD)])
    worker, store = make(archive, monkeypatch)

    async def boom(*a, **k):
        raise ValueError("token=SUPERSECRET at http://10.0.0.5/internal")
    worker._apply_record = boom
    await enqueue(store, GOOD)
    with caplog.at_level(logging.INFO, logger="src.jobs.worker"):
        job = await run_once(worker, store)
    assert (job.status, job.outcome, job.error["code"]) == (M.FAILED, M.FAILED_OUTCOME, "internal")
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "src.jobs.worker")
    assert "ValueError" in text and job.id in text and "test_jobs_worker.py" in text
    assert "SUPERSECRET" not in text and "10.0.0.5" not in text
    assert "SUPERSECRET" not in json.dumps(job.error)
