"""JobStore — the same semantics from SqliteJobStore and MemoryJobStore (design §3.2)."""
import time

import pytest

from src.jobs import models as M
from src.jobs.store import MemoryJobStore, SqliteJobStore, open_store


@pytest.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path):
    s = MemoryJobStore() if request.param == "memory" else SqliteJobStore(str(tmp_path / "d" / "jobs.sqlite3"))
    yield s
    await s.close()


def _job(article_id=1, *, created=None, **kw):
    j = M.Job(id=M.new_job_id(), article_id=article_id, **kw)
    if created is not None:
        j.created_at = created
    return j


async def test_create_get_and_save_round_trip(store):
    j = _job(5, audit_id=900101)
    j.data["record"] = {"verdict": "INCONCLUSIVE", "nested": {"ünicode": "ş"}}
    await store.create_job(j)
    got = await store.get_job(j.id)
    assert got == j
    got.status, got.outcome = M.SUCCEEDED, M.DRY_RUN
    await store.save_job(got)
    assert (await store.get_job(j.id)).outcome == M.DRY_RUN
    assert await store.get_job("nope") is None


async def test_jobs_for_an_article_are_newest_first(store):
    a = _job(7, created=100.0)
    b = _job(7, created=200.0, audit_id=55)              # after the worker's lookup
    c = _job(99, created=150.0)
    for j in (a, b, c):
        await store.create_job(j)
    assert [j.id for j in await store.jobs_for(7)] == [b.id, a.id]
    assert (await store.latest_for_article(7)).id == b.id
    assert [j.id for j in await store.jobs_for(99)] == [c.id]
    assert await store.latest_for_article(12345) is None


async def test_claim_is_fifo_honours_run_after_and_counts_attempts(store):
    first = _job(1, created=10.0)
    later = _job(2, created=20.0)
    backoff = _job(3, created=5.0)
    backoff.run_after = time.time() + 3600
    for j in (first, later, backoff):
        await store.create_job(j)
    assert await store.count_queued() == 3
    got = await store.claim_next(60)
    assert got.id == first.id and got.status == M.RUNNING and got.attempts == 1
    assert got.started_at is not None and got.lease_until is not None
    assert (await store.claim_next(60)).id == later.id
    assert await store.claim_next(60) is None, "a job in backoff is not claimable yet"
    assert await store.count_queued() == 1


async def test_requeue_running_puts_crashed_jobs_back(store):
    j = _job(1)
    await store.create_job(j)
    claimed = await store.claim_next(60)
    assert claimed.status == M.RUNNING
    assert await store.requeue_running() == 1
    again = await store.claim_next(60)
    assert again.id == j.id and again.attempts == 2


async def test_ledger_round_trip_keeps_the_apply_log_shape(store):
    assert await store.get_ledger(42) == {}
    entry = {"tags": {"ok": True, "at": 1.5, "payload": {"subject_tag_notations": ["medtop:1"]}},
             "comment": {"ok": True, "at": 2.5, "template": "C", "tags_state": "set",
                         "discussion_id": 7, "view_url": "https://karakutu.xyz/audits/42/discussions#/news/x"}}
    await store.put_ledger(42, entry)
    assert await store.get_ledger(42) == entry
    entry["tags"]["ok"] = False
    assert (await store.get_ledger(42))["tags"]["ok"] is True, "the store keeps a copy"


async def test_prune_drops_old_finished_jobs_only(store):
    old = _job(1)
    old.status, old.finished_at = M.SUCCEEDED, 100.0
    fresh = _job(2)
    fresh.status, fresh.finished_at = M.FAILED, 10_000.0
    queued = _job(3)
    for j in (old, fresh, queued):
        await store.create_job(j)
    await store.prune(jobs_finished_before=5_000.0)
    assert await store.get_job(old.id) is None
    assert await store.get_job(fresh.id) is not None and await store.get_job(queued.id) is not None


async def test_check_reports_writable(store):
    assert await store.check() is True


async def test_sqlite_survives_a_reopen(tmp_path):
    path = str(tmp_path / "jobs.sqlite3")
    s1 = SqliteJobStore(path)
    j = _job(11)
    await s1.create_job(j)
    await s1.claim_next(60)
    await s1.put_ledger(11, {"tags": {"ok": True}})
    await s1.close()
    s2 = open_store(f"sqlite:///{path}")
    assert (await s2.get_job(j.id)).status == M.RUNNING
    assert await s2.requeue_running() == 1, "a restart re-queues what the crash left running"
    assert (await s2.get_ledger(11)) == {"tags": {"ok": True}}
    await s2.close()


def test_open_store_rejects_unknown_urls():
    assert isinstance(open_store("memory://"), MemoryJobStore)
    with pytest.raises(ValueError):
        open_store("postgres://x")
