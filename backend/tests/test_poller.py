"""Offline polling, bounded automatic work and safe public diagnostics."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from audit.api import create_app
from audit.clients.models import ArticleRecord
from audit.config import Settings
from audit.db import ArticleRow, Run
from test_api import FakeEngine


class Archive:
    enabled = True
    state = "ok"

    def __init__(self):
        self.items = [{"article_id": i, "url": f"https://paper.example/fiction-{i}",
                       "title": f"Invented report {i}"} for i in range(1, 9)]
        self.windows = []
        self.reads = []
        self.failure = None
        self.empty = False

    async def audits_created_between(self, start, end):
        self.windows.append((start, end))
        if self.failure == "exception":
            raise RuntimeError("PRIVATE upstream text must never escape")
        return None if self.failure == "none" else self.items

    async def get_article(self, article_id):
        self.reads.append(article_id)
        return ArticleRecord(id=int(article_id),
            url=f"https://paper.example/fiction-{article_id}", versions=[{
                "title": "Invented report", "news_text": "" if self.empty else "Invented prose.",
                "check_date": 1, "content_hash": "fiction"}])


@asynccontextmanager
async def service(tmp_path, **kwargs):
    archive = Archive()
    engine = FakeEngine(wait=True)
    engine.archive = archive
    config = Settings(data_dir=tmp_path, **kwargs)
    app = create_app(config, engine)
    async with app.router.lifespan_context(app):
        # Exercise cycles directly, without racing the startup delay or sleeping.
        tasks = list(app.state.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        yield app, archive, tasks


def runs(app):
    with Session(app.state.store.engine) as session:
        return list(session.scalars(select(Run).order_by(Run.created_at, Run.id)))


async def test_register_only_and_status_and_windows(tmp_path):
    async with service(tmp_path) as (app, archive, tasks):
        assert len(tasks) == 1
        poller = app.state.poller
        stamp = datetime(2026, 11, 20, tzinfo=timezone.utc)
        poller.clock = lambda: stamp
        cycle = await poller.run_cycle()
        assert cycle == {"started_at": stamp.isoformat(), "finished_at": stamp.isoformat(),
                         "outcome": "ok", "reason": None, "found": 8, "new": 8, "queued": 0}
        assert archive.windows == [
            (int((stamp - timedelta(days=30)).timestamp()), int(stamp.timestamp()))]
        assert runs(app) == [] and archive.reads == []
        assert app.state.store.article("ka-1")["content"] == ""
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver",
        ) as client:
            status = (await client.get("/api/status")).json()["polling"]
        assert status == {"enabled": True, "interval_minutes": 5, "auto_audit_enabled": False,
                          "auto_audit_max_per_poll": 5, "last": cycle}
        stamp += timedelta(minutes=5)
        assert (await poller.run_cycle())["new"] == 0
        assert archive.windows[-1][0] == int((stamp - timedelta(days=2)).timestamp())
        stamp += timedelta(days=5)
        await poller.run_cycle()
        assert archive.windows[-1][0] == int((stamp - timedelta(days=6)).timestamp())
        stamp += timedelta(days=50)
        await poller.run_cycle()
        assert archive.windows[-1][0] == int((stamp - timedelta(days=30)).timestamp())


async def test_auto_audit_oldest_no_previous_attempt_and_budget(tmp_path):
    async with service(
        tmp_path, auto_audit_enabled=True, auto_audit_max_per_poll=2,
    ) as (app, archive, _):
        store = app.state.store
        store.import_articles(archive.items)
        # Make registration ordering deliberate, including a ref tiebreak.
        with Session(store.engine) as session, session.begin():
            for row in session.scalars(select(ArticleRow)):
                row.created_at = "2026-11-02" if row.ref in {"ka-1", "ka-2"} else "2026-11-01"
        for ref, status, origin in [("ka-3", "failed", "api"), ("ka-4", "cancelled", "article")]:
            await app.state.poller.fill_archive_text(ref, store.article(ref))
            run = store.create_run(workflow_id=app.state.workflow["id"], category="live",
                                   origin=origin, input={}, article_ref=ref)
            if status == "failed":
                store.fail_run(run["id"], "interrupted")
            else:
                store.cancel_run(run["id"])
        cycle = await app.state.poller.run_cycle()
        assert cycle["queued"] == 2
        automatic = [r for r in runs(app) if r.origin == "poll"]
        assert [r.article_ref for r in automatic] == ["ka-5", "ka-6"]
        assert all(r.category == "live" for r in automatic)
        assert store.active_poll_runs() == 2
        assert (await app.state.poller.run_cycle())["queued"] == 0
        assert len(runs(app)) == 4
        # Once one slot finishes, exactly one new attempt is allowed.
        store.cancel_run(automatic[0].id)
        assert (await app.state.poller.run_cycle())["queued"] == 1
        assert [r.article_ref for r in runs(app) if r.origin == "poll"] == ["ka-5", "ka-6", "ka-7"]


async def test_one_listing_is_audited_in_numeric_archive_order(tmp_path):
    # A first poll registers a whole listing at once; "ka-10" must not sort before "ka-9".
    async with service(
        tmp_path, auto_audit_enabled=True, auto_audit_max_per_poll=2,
    ) as (app, archive, _):
        archive.items = [{"article_id": i, "url": f"https://paper.example/fiction-{i}",
                          "title": f"Invented report {i}"} for i in (10, 9, 100)]
        store = app.state.store
        store.import_articles(archive.items)
        with Session(store.engine) as session, session.begin():
            for row in session.scalars(select(ArticleRow)):
                row.created_at = "2026-11-01"
        assert store.unaudited_archive_refs() == ["ka-9", "ka-10", "ka-100"]


async def test_overlapping_cycles_and_manual_audit_race(tmp_path):
    async with service(tmp_path, auto_audit_enabled=True) as (app, archive, _):
        archive.items = archive.items[:1]
        poller = app.state.poller
        filled, release = asyncio.Event(), asyncio.Event()
        original_fill = poller.fill_archive_text

        async def delayed_fill(ref, article):
            await original_fill(ref, article)
            filled.set()
            await release.wait()

        poller.fill_archive_text = delayed_fill
        first = asyncio.create_task(poller.run_cycle())
        await filled.wait()
        second = asyncio.create_task(poller.run_cycle())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver",
        ) as client:
            response = await client.post("/api/articles/ka-1/audit")
            assert response.status_code == 202
        release.set()
        assert (await first)["queued"] == 0
        assert (await second)["queued"] == 0
        assert len(runs(app)) == 1 and runs(app)[0].origin == "article"


@pytest.mark.parametrize("failure,state,reason", [
    ("none", "unreachable", "unreachable"), ("exception", "rejected", "rejected"),
    ("exception", "unchecked", "archive_error"), ("none", "ok", "archive_error"),
])
async def test_archive_failure_is_safe(tmp_path, caplog, failure, state, reason):
    async with service(tmp_path, auto_audit_enabled=True) as (app, archive, _):
        archive.failure, archive.state = failure, state
        cycle = await app.state.poller.run_cycle()
        assert cycle["outcome"] == "skipped" and cycle["reason"] == reason
        assert cycle["found"] == cycle["new"] == cycle["queued"] == 0
        assert app.state.store.articles()["total"] == 0 and runs(app) == []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver",
        ) as client:
            status = (await client.get("/api/status")).json()
        assert status["polling"]["last"] == cycle
        assert "PRIVATE" not in str(status) + caplog.text


async def test_import_in_progress_and_queue_paused_and_missing_text(tmp_path):
    async with service(tmp_path, auto_audit_enabled=True) as (app, archive, _):
        poller = app.state.poller
        async with app.state.article_import_lock:
            cycle = await poller.run_cycle()
        assert cycle["reason"] == "import_in_progress" and archive.windows == []
        app.state.queue_paused = True
        cycle = await poller.run_cycle()
        assert cycle["reason"] == "queue_paused" and cycle["new"] == 8
        assert runs(app) == [] and archive.reads == []
        app.state.queue_paused = False
        archive.empty = True
        assert (await poller.run_cycle())["queued"] == 0
        assert runs(app) == []


async def test_cycle_crash_recovers_on_next_interval(tmp_path, monkeypatch, caplog):
    async with service(tmp_path) as (app, archive, _):
        poller = app.state.poller
        original = app.state.store.import_articles

        def fail(_items):
            raise RuntimeError("PRIVATE database error")

        monkeypatch.setattr(app.state.store, "import_articles", fail)
        delays = []

        async def advance(seconds):
            delays.append(seconds)
            if len(delays) == 2:
                assert poller.last["reason"] == "poll_error"
                monkeypatch.setattr(app.state.store, "import_articles", original)
            if len(delays) == 3:
                raise asyncio.CancelledError

        poller.sleep = advance
        with pytest.raises(asyncio.CancelledError):
            await poller.run()
        assert delays == [1, 300, 300]
        assert poller.last["outcome"] == "ok" and poller.last["new"] == 8
        assert "PRIVATE" not in str(poller.status()) + caplog.text


@pytest.mark.parametrize("case", ["off", "missing", "disabled", "test"])
async def test_no_task_when_off_or_archive_unavailable_or_test_mode(tmp_path, case):
    engine = FakeEngine()
    engine.archive = Archive() if case != "missing" else None
    if case == "disabled":
        engine.archive.enabled = False
    config = Settings(data_dir=tmp_path, mode="test" if case == "test" else "live",
                      poll_interval_minutes=0 if case == "off" else 5,
                      auto_audit_enabled=True)
    app = create_app(config, engine)
    async with app.router.lifespan_context(app):
        assert app.state.tasks == set()
        assert app.state.poller.status() == {
            "enabled": False, "interval_minutes": 0, "auto_audit_enabled": False,
            "auto_audit_max_per_poll": 5, "last": None}


async def test_lifespan_cancels_polling_task_without_startup_wait(tmp_path):
    engine = FakeEngine()
    engine.archive = Archive()
    app = create_app(Settings(data_dir=tmp_path), engine)
    entered = asyncio.Event()

    async def hold(_seconds):
        entered.set()
        await asyncio.Event().wait()

    async with app.router.lifespan_context(app):
        task, = app.state.tasks
        app.state.poller.sleep = hold
        await entered.wait()
        assert not task.done() and engine.archive.windows == []
    assert task.cancelled() and engine.closed


async def test_pause_while_filling_prevents_submission(tmp_path):
    async with service(tmp_path, auto_audit_enabled=True) as (app, archive, _):
        archive.items = archive.items[:1]
        poller = app.state.poller
        original_fill = poller.fill_archive_text

        async def pause_during_fill(ref, article):
            await original_fill(ref, article)
            app.state.queue_paused = True

        poller.fill_archive_text = pause_during_fill
        cycle = await poller.run_cycle()
        assert cycle["reason"] == "queue_paused" and cycle["queued"] == 0
        assert runs(app) == []


async def test_query_excludes_non_archive_and_benchmark_articles(tmp_path):
    async with service(tmp_path) as (app, archive, _):
        store = app.state.store
        store.import_articles(archive.items[:3])
        with Session(store.engine) as session, session.begin():
            session.get(ArticleRow, "ka-1").category = "benchmark"
            # A run-owned ref does not become an archive ref when import attaches an id.
            session.get(ArticleRow, "ka-2").ref = "u-fictional"
        assert store.unaudited_archive_refs() == ["ka-3"]
