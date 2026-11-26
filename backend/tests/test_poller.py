"""Offline polling, bounded automatic work and safe public diagnostics."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import threading

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from audit.api import ARCHIVE_TEXT_RETRY_SECONDS, create_app
from audit.clients.karakutu import KarakutuClient
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
        self.read_failure = False
        self.version = 1

    async def audits_created_between(self, start, end, *, require_complete=False):
        self.windows.append((start, end))
        if self.failure == "exception":
            raise RuntimeError("PRIVATE upstream text must never escape")
        return None if self.failure == "none" else self.items

    async def get_article(self, article_id):
        self.reads.append(article_id)
        if self.read_failure:
            return None
        return ArticleRecord(id=int(article_id),
            url=f"https://paper.example/fiction-{article_id}", versions=[{
                "title": f"Invented report version {self.version}",
                "news_text": "" if self.empty else f"Invented prose version {self.version}.",
                "check_date": self.version, "content_hash": f"fiction-{self.version}"}])


@asynccontextmanager
async def service(tmp_path, *, archive=None, **kwargs):
    archive = archive or Archive()
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
                         "outcome": "ok", "reason": None, "found": 8, "new": 8,
                         "queued": 0, "unreadable": 0}
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
        assert [r.article_ref for r in automatic] == ["ka-1", "ka-2"]
        assert all(r.category == "live" for r in automatic)
        assert store.active_poll_runs() == 2
        assert (await app.state.poller.run_cycle())["queued"] == 0
        assert len(runs(app)) == 4
        # Once one slot finishes, exactly one new attempt is allowed.
        store.cancel_run(automatic[0].id)
        assert (await app.state.poller.run_cycle())["queued"] == 1
        assert [r.article_ref for r in runs(app) if r.origin == "poll"] == ["ka-1", "ka-2", "ka-5"]


async def test_one_listing_is_audited_in_numeric_archive_order(tmp_path):
    async with service(
        tmp_path, auto_audit_enabled=True, auto_audit_max_per_poll=3,
    ) as (app, archive, _):
        archive.items = [{"article_id": i, "url": f"https://paper.example/fiction-{i}",
                          "title": f"Invented report {i}"} for i in (100, 10, 9)]
        assert (await app.state.poller.run_cycle())["queued"] == 3
        assert [r.article_ref for r in runs(app)] == ["ka-9", "ka-10", "ka-100"]
        assert archive.reads == ["9", "10", "100"]


async def test_overlapping_cycles_and_manual_audit_race(tmp_path):
    async with service(tmp_path, auto_audit_enabled=True) as (app, archive, _):
        archive.items = archive.items[:1]
        poller = app.state.poller
        filled, release = asyncio.Event(), asyncio.Event()
        original_fill = poller.fill_archive_text

        async def delayed_fill(ref, article, **kwargs):
            result = await original_fill(ref, article, **kwargs)
            filled.set()
            await release.wait()
            return result

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
    ("exception", "unchecked", "archive_error"), ("none", "ok", "archive_incomplete"),
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
        cycle = await poller.run_cycle()
        assert cycle["queued"] == 0 and cycle["unreadable"] == 8
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

        async def pause_during_fill(ref, article, **kwargs):
            result = await original_fill(ref, article, **kwargs)
            app.state.queue_paused = True
            return result

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


@pytest.mark.parametrize("failed_page", [1, 2])
async def test_incomplete_listing_retries_full_window(tmp_path, failed_page):
    incomplete, requests = True, []

    def handle(request):
        if request.url.path != "/audits":
            return httpx.Response(200, json={"id": 1, "url": "https://paper.example/fiction-1",
                                            "news_text": "Invented prose."})
        params = request.url.params
        status, page = int(params["status"]), int(params["page"])
        requests.append((status, page, int(params["date_from"])))
        if incomplete and status == 0 and page == failed_page:
            return httpx.Response(503)
        ids = range(1, 101) if status == 0 and page == 1 else [101 + status]
        return httpx.Response(200, json={"items": [
            {"id": i, "article": {"id": i, "link": f"https://paper.example/fiction-{i}"}}
            for i in ids]})

    archive = KarakutuClient("https://archive.example")
    await archive.http.aclose()
    archive.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        async with service(tmp_path, archive=archive, auto_audit_enabled=True,
                           auto_audit_max_per_poll=1) as (app, _, _):
            poller = app.state.poller
            stamp = datetime(2026, 11, 20, tzinfo=timezone.utc)
            poller.clock = lambda: stamp
            # Preserve the Import button's existing partial-list contract.
            assert await archive.audits_created_between(0, 1)
            cycle = await poller.run_cycle()
            assert archive.state == "ok"  # Later successful statuses mask the first failure.
            assert cycle["reason"] == "archive_incomplete"
            assert cycle["found"] == cycle["new"] == cycle["queued"] == 0
            assert poller.last_success is None and runs(app) == []
            assert app.state.store.articles()["total"] == 0
            incomplete = False
            stamp += timedelta(minutes=5)
            cycle = await poller.run_cycle()
            assert cycle["new"] == 105 and cycle["queued"] == 1
            assert poller.last_success == stamp
            assert requests[-1][2] == int((stamp - timedelta(days=30)).timestamp())
            # A later incomplete listing must also preserve the previous successful window.
            app.state.store.cancel_run(runs(app)[0].id)
            previous_success = poller.last_success
            incomplete = True
            stamp += timedelta(days=5)
            assert (await poller.run_cycle())["reason"] == "archive_incomplete"
            assert poller.last_success == previous_success
            assert len(runs(app)) == 1
            incomplete = False
            stamp += timedelta(minutes=5)
            await poller.run_cycle()
            assert requests[-1][2] == int((previous_success - timedelta(days=1)).timestamp())
    finally:
        await archive.close()


@pytest.mark.parametrize("cached", [False, True])
async def test_failed_text_read_retries_after_cooldown(tmp_path, cached):
    async with service(tmp_path, auto_audit_enabled=True) as (app, archive, _):
        archive.items = archive.items[:1]
        if cached:
            app.state.store.import_articles(archive.items)
            await app.state.poller.fill_archive_text("ka-1", app.state.store.article("ka-1"))
        archive.reads.clear()
        archive.read_failure = True
        clock = 0
        app.state.article_fill_clock = lambda: clock
        poller = app.state.poller
        cycle = await poller.run_cycle()
        assert cycle["queued"] == 0 and cycle["unreadable"] == 1
        assert runs(app) == [] and archive.reads == ["1"]
        archive.read_failure = False
        archive.version = 2
        clock = ARCHIVE_TEXT_RETRY_SECONDS - 1
        cycle = await poller.run_cycle()
        assert cycle["queued"] == 0 and cycle["unreadable"] == 1
        assert archive.reads == ["1"]
        clock += 1
        cycle = await poller.run_cycle()
        assert cycle["queued"] == 1 and cycle["unreadable"] == 0
        assert archive.reads == ["1", "1"]
        assert runs(app)[0].input["removed_article"]["content"] == "Invented prose version 2."


@pytest.mark.parametrize("entrypoint", ["detail", "audit"])
async def test_manual_reads_retry_after_cooldown(tmp_path, entrypoint):
    async with service(tmp_path) as (app, archive, _):
        app.state.store.import_articles(archive.items[:1])
        clock = 0
        app.state.article_fill_clock = lambda: clock
        archive.read_failure = True
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver",
        ) as client:
            async def read():
                return (await client.get("/api/articles/ka-1") if entrypoint == "detail"
                        else await client.post("/api/articles/ka-1/audit"))

            for _ in range(2):
                response = await read()
                assert response.status_code == (200 if entrypoint == "detail" else 422)
            assert archive.reads == ["1"] and runs(app) == []
            archive.read_failure = False
            clock = ARCHIVE_TEXT_RETRY_SECONDS
            response = await read()
            assert response.status_code == (200 if entrypoint == "detail" else 202)
            clock += ARCHIVE_TEXT_RETRY_SECONDS
            await read()
            assert archive.reads == ["1", "1"]  # Successful lazy reads still happen once.


async def test_auto_audit_refreshes_cached_version_and_preserves_run_inputs(tmp_path):
    async with service(tmp_path, auto_audit_enabled=True) as (app, archive, _):
        archive.items = archive.items[:2]
        store = app.state.store
        store.import_articles(archive.items)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver",
        ) as client:
            for ref in ("ka-1", "ka-2"):
                assert (await client.get(f"/api/articles/{ref}")).json()["content"] == (
                    "Invented prose version 1.")
            # Cached manual audits keep their existing behavior, even after upstream changes.
            archive.version = 2
            assert (await client.post("/api/articles/ka-2/audit")).status_code == 202
        historical = runs(app)[0]
        original_input = historical.input
        assert original_input["removed_article"]["content"] == "Invented prose version 1."
        assert (await app.state.poller.run_cycle())["queued"] == 1
        automatic, = [run for run in runs(app) if run.origin == "poll"]
        assert automatic.input["removed_article"]["content"] == "Invented prose version 2."
        assert automatic.input["removed_article"]["title"] == "Invented report version 2"
        article = store.article("ka-1")
        assert article["snapshot"]["versions"] == [{
            "title": "Invented report version 2", "check_date": 2, "content_hash": "fiction-2"}]
        assert store.run(historical.id)["input"] == original_input
        assert archive.reads == ["1", "2", "1"]
        # Later refreshes of the same article cannot rewrite the automatic run's input.
        archive.version = 3
        await app.state.poller.fill_archive_text("ka-1", article, refresh=True)
        assert store.article("ka-1")["content"] == "Invented prose version 3."
        assert store.run(automatic.id)["input"] == automatic.input


@pytest.mark.parametrize("entrypoint", ["poll", "import"])
@pytest.mark.parametrize("memory", [False, True])
async def test_import_and_candidate_query_run_in_worker_threads(
    tmp_path, database_url, monkeypatch, entrypoint, memory,
):
    async with service(tmp_path, database_url="sqlite:///:memory:" if memory else database_url,
                       auto_audit_enabled=True) as (app, archive, _):
        archive.items = [{"article_id": i, "url": f"https://paper.example/fiction-{i}"}
                         for i in range(1, 2001)]
        main_thread = threading.get_ident()
        store = app.state.store
        original_import, original_query = store.import_articles, store.unaudited_archive_refs
        called = []

        def import_in_worker(items):
            assert threading.get_ident() != main_thread
            assert app.state.article_import_lock.locked()
            called.append("import")
            return original_import(items)

        def query_in_worker():
            assert threading.get_ident() != main_thread
            called.append("query")
            return original_query()

        monkeypatch.setattr(store, "import_articles", import_in_worker)
        monkeypatch.setattr(store, "unaudited_archive_refs", query_in_worker)
        if entrypoint == "poll":
            assert (await app.state.poller.run_cycle())["new"] == 2000
            assert called == ["import", "query"]
        else:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://testserver",
            ) as client:
                response = await client.post("/api/articles/import")
            assert response.status_code == 200 and response.json()["imported"] == 2000
            assert called == ["import"]
        assert store.articles()["total"] == 2000


async def test_cancelled_import_holds_lock_until_worker_finishes(tmp_path, monkeypatch):
    async with service(tmp_path) as (app, archive, _):
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        store = app.state.store
        original = store.import_articles

        def held_import(items):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=5)
            return original(items)

        monkeypatch.setattr(store, "import_articles", held_import)
        task = asyncio.create_task(app.state.poller.run_cycle())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and app.state.article_import_lock.locked()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not app.state.article_import_lock.locked()
        assert store.articles()["total"] == len(archive.items)
        assert (await app.state.poller.run_cycle())["new"] == 0


async def test_listing_page_cap_is_incomplete():
    archive = KarakutuClient("https://archive.example")
    await archive.http.aclose()
    archive.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
        httpx.Response(200, json={"items": [{"article": {"id": i,
            "link": f"https://paper.example/fiction-{i}"}} for i in range(100)]})))
    try:
        assert len(await archive.audits_created_between(0, 1, statuses=(0,))) == 3000
        assert await archive.audits_created_between(
            0, 1, statuses=(0,), require_complete=True) is None
    finally:
        await archive.close()
