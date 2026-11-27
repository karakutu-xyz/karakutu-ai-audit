"""Runtime intake controls use only fabricated archive responses and local stores."""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from audit.api import create_app
from audit.config import Settings
from audit.db import Store, get_version
from test_api import FakeEngine
from test_poller import Archive, runs, service


DEFAULTS = {
    "polling_enabled": True,
    "interval_minutes": 5,
    "lookback_days": 7,
    "auto_audit_enabled": False,
    "auto_audit_max_per_poll": 5,
}
OVERRIDES = {
    "polling_enabled": False,
    "interval_minutes": 12,
    "lookback_days": 3,
    "auto_audit_enabled": True,
    "auto_audit_max_per_poll": 2,
}


def connection(app, token=""):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost",
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )


async def test_defaults_persist_all_values_restart_sources_and_resets(tmp_path):
    async with service(tmp_path) as (app, _, _), connection(app) as client:
        response = await client.get("/api/polling/settings")
        assert response.status_code == 200
        assert response.json() == {
            name: {"value": value, "default": value, "source": "env"}
            for name, value in DEFAULTS.items()}
        response = await client.put("/api/polling/settings", json=OVERRIDES)
        assert response.status_code == 200
        assert response.json() == {
            name: {"value": value, "default": DEFAULTS[name], "source": "ui"}
            for name, value in OVERRIDES.items()}
        assert app.state.settings.poll_interval_minutes == 5
    # A new app, poller and store read overrides from the same database.
    async with service(tmp_path) as (app, _, _), connection(app) as client:
        saved = (await client.get("/api/polling/settings")).json()
        assert {name: field["value"] for name, field in saved.items()} == OVERRIDES
        assert all(field["source"] == "ui" for field in saved.values())
        response = await client.post("/api/polling/settings/reset",
                                     json={"names": ["interval_minutes"]})
        assert response.status_code == 200
        assert response.json()["interval_minutes"] == {
            "value": 5, "default": 5, "source": "env"}
        assert response.json()["lookback_days"]["source"] == "ui"
        reset = await client.post("/api/polling/settings/reset", json={"names": []})
        assert reset.status_code == 200
        assert all(field["source"] == "env" for field in reset.json().values())
        assert {name: field["value"] for name, field in reset.json().items()} == DEFAULTS
        # Omitted body and an empty object also reset all.
        for body in (None, {}):
            await client.put("/api/polling/settings", json=OVERRIDES)
            response = await client.post("/api/polling/settings/reset",
                                         **({"json": body} if body is not None else {}))
            assert all(field["source"] == "env" for field in response.json().values())
    async with service(tmp_path) as (app, _, _), connection(app) as client:
        assert all(field["source"] == "env" for field in (
            await client.get("/api/polling/settings")).json().values())


async def test_environment_defaults_and_zero_interval(tmp_path):
    async with service(tmp_path, poll_interval_minutes=0, poll_lookback_days=9,
                       auto_audit_enabled=True, auto_audit_max_per_poll=4) as (app, _, tasks):
        assert len(tasks) == 1  # Dormant, ready for runtime resume.
        async with connection(app) as client:
            settings = (await client.get("/api/polling/settings")).json()
            assert settings["polling_enabled"] == {
                "value": False, "default": False, "source": "env"}
            assert settings["interval_minutes"]["value"] == 5
            assert settings["lookback_days"]["value"] == 9
            assert settings["auto_audit_enabled"]["value"] is True
            assert settings["auto_audit_max_per_poll"]["value"] == 4
            assert all(field["source"] == "env" for field in settings.values())
            await client.put("/api/polling/settings", json={"polling_enabled": True})
            assert app.state.poller.enabled
            status = (await client.get("/api/status")).json()["polling"]
            assert status["interval_minutes"] == 5 and not status["paused"]
            assert status["source"]["polling_enabled"] == "ui"


@pytest.mark.parametrize("body", [
    {"interval_minutes": 0}, {"interval_minutes": 1441}, {"interval_minutes": 1.5},
    {"lookback_days": 0}, {"lookback_days": 31}, {"lookback_days": "7"},
    {"auto_audit_max_per_poll": 0}, {"auto_audit_max_per_poll": 51},
    {"auto_audit_max_per_poll": True}, {"polling_enabled": 1},
    {"polling_enabled": "false"}, {"auto_audit_enabled": "yes"},
    {"auto_audit_enabled": []}, {"unknown": True},
    *({name: None} for name in DEFAULTS),
])
async def test_invalid_partial_updates_are_rejected_atomically(tmp_path, body):
    async with service(tmp_path) as (app, _, _), connection(app) as client:
        response = await client.put("/api/polling/settings", json=body)
        assert response.status_code == 422
        assert app.state.store.poll_overrides() == {}
        assert app.state.poller.controls.effective.interval_minutes == 5


@pytest.mark.parametrize("name,low,high", [
    ("interval_minutes", 1, 1440), ("lookback_days", 1, 30),
    ("auto_audit_max_per_poll", 1, 50),
])
async def test_range_endpoints_and_partial_updates(tmp_path, name, low, high):
    async with service(tmp_path) as (app, _, _), connection(app) as client:
        for value in (low, high):
            response = await client.put("/api/polling/settings", json={name: value})
            assert response.status_code == 200
            assert response.json()[name]["value"] == value
            assert response.json()[name]["source"] == "ui"
        assert len(app.state.store.poll_overrides()) == 1
        for body in ({"names": ["unknown"]}, {"unknown": []}, {"names": None}):
            assert (await client.post("/api/polling/settings/reset", json=body)).status_code == 422


async def test_poll_now_idle_paused_running_and_history(tmp_path):
    async with service(tmp_path, auto_audit_enabled=True) as (app, archive, _):
        async with connection(app) as client:
            await client.put("/api/polling/settings", json={"polling_enabled": False})
            entered, release = asyncio.Event(), asyncio.Event()
            original = archive.audits_created_between

            async def held(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original(*args, **kwargs)

            archive.audits_created_between = held
            request = asyncio.create_task(client.post("/api/polling/run"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                status = (await client.get("/api/status")).json()["polling"]
                assert status["paused"] and status["running"]
                assert status["next_poll_at"] is None and not status["enabled"]
                response = await client.post("/api/polling/run")
                assert response.status_code == 409
                assert response.json() == {"error": {"code": "poll_running"}}
            finally:
                release.set()
            response = await request
            assert response.status_code == 200
            cycle = response.json()
            assert cycle["outcome"] == "ok" and cycle["new"] == 8
            assert cycle["queued"] == 0 and cycle["updated"] == 0
            status = (await client.get("/api/status")).json()["polling"]
            assert status["paused"] and not status["running"] and not status["enabled"]
            assert status["history"][0] == cycle
            assert status["last"] == {
                key: value for key, value in cycle.items() if key != "updated"}
            assert archive.reads == [] and runs(app) == []


@pytest.mark.parametrize("setting", ["polling_enabled", "auto_audit_enabled"])
@pytest.mark.parametrize("point", ["listing", "fill"])
async def test_pause_or_disable_auto_audit_during_cycle_prevents_queueing(tmp_path, setting, point):
    async with service(tmp_path, auto_audit_enabled=True) as (app, archive, _):
        poller = app.state.poller
        entered, release = asyncio.Event(), asyncio.Event()
        original = (archive.audits_created_between if point == "listing"
                    else poller.fill_archive_text)

        async def held(*args, **kwargs):
            value = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return value

        if point == "listing":
            archive.audits_created_between = held
        else:
            poller.fill_archive_text = held
        cycle = asyncio.create_task(poller.run_cycle())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            async with connection(app) as client:
                assert (await client.put("/api/polling/settings",
                                         json={setting: False})).status_code == 200
        finally:
            release.set()
        result = await cycle
        assert result["new"] == 8 and result["queued"] == 0
        assert app.state.store.articles()["total"] == 8 and runs(app) == []


async def test_effective_auto_audit_and_batch_and_lookback(tmp_path):
    async with service(tmp_path) as (app, archive, _), connection(app) as client:
        await client.put("/api/polling/settings", json={
            "lookback_days": 2, "auto_audit_enabled": True, "auto_audit_max_per_poll": 2})
        response = await client.post("/api/polling/run")
        assert response.json()["queued"] == 2
        assert archive.windows[0][1] - archive.windows[0][0] == 2 * 86400
        assert len(archive.reads) == 2
        await client.put("/api/polling/settings", json={"auto_audit_enabled": False})
        assert (await client.post("/api/polling/run")).json()["queued"] == 0
        assert len(archive.reads) == 2 and len(runs(app)) == 2


@pytest.mark.parametrize("token", ["", "invented-token-" * 3])
async def test_controls_use_normal_access_guards(tmp_path, token):
    async with service(tmp_path, api_auth_token=token) as (app, _, _):
        endpoints = [("GET", "/api/polling/settings", None),
                     ("PUT", "/api/polling/settings", {"polling_enabled": False}),
                     ("POST", "/api/polling/settings/reset", {}),
                     ("POST", "/api/polling/run", None)]
        async with connection(app) as client:
            for method, path, body in endpoints:
                response = await client.request(method, path, **({"json": body} if body else {}))
                assert response.status_code == (401 if token else 200)
        async with connection(app, token) as client:
            for method, path, body in endpoints:
                if method == "GET":
                    continue
                response = await client.request(method, path,
                    headers={"Origin": "https://untrusted.example"}, json=body or {})
                assert response.status_code == 403
            response = await client.put("/api/polling/settings",
                                        json={"interval_minutes": 15})
            assert response.status_code == 200


@pytest.mark.parametrize("mode", ["test", "disabled"])
async def test_unavailable_polling_never_starts_even_after_ui_enable(tmp_path, mode):
    engine = FakeEngine()
    archive = Archive()
    archive.enabled = mode != "disabled"
    engine.archive = archive
    app = create_app(Settings(data_dir=tmp_path, mode="test" if mode == "test" else "live"),
                     engine)
    async with app.router.lifespan_context(app), connection(app) as client:
        assert not app.state.tasks
        response = await client.put("/api/polling/settings", json={"polling_enabled": True})
        assert response.status_code == 200 and response.json()["polling_enabled"]["source"] == "ui"
        status = (await client.get("/api/status")).json()["polling"]
        assert not status["enabled"] and not status["running"]
        reason = "test_mode" if mode == "test" else "archive_disabled"
        assert status["reason"] == reason and status["next_poll_at"] is None
        response = await client.post("/api/polling/run")
        assert response.status_code == 409 and response.json()["error"]["code"] == reason
        assert not app.state.tasks
        assert (await app.state.poller.run_cycle())["reason"] == "disabled"
        assert archive.windows == [] and archive.reads == []


class ControlledTimer:
    def __init__(self):
        self.calls = asyncio.Queue()

    async def __call__(self, seconds):
        timer = asyncio.get_running_loop().create_future()
        self.calls.put_nowait((seconds, timer))
        await timer

    async def next(self, expected):
        seconds, timer = await asyncio.wait_for(self.calls.get(), 2)
        assert seconds == expected
        return timer


@pytest.mark.parametrize("backoff", [False, True])
async def test_pause_interrupts_wait_resume_wakes_interval_reschedules(tmp_path, backoff):
    async with service(tmp_path) as (app, archive, _), connection(app) as client:
        poller = app.state.poller
        stamp = datetime(2026, 11, 20, tzinfo=timezone.utc)
        poller.clock = lambda: stamp
        timer = ControlledTimer()
        poller.sleep = timer
        archive.failure = "none" if backoff else None
        task = asyncio.create_task(poller.run())
        try:
            startup = await timer.next(1)
            startup.set_result(None)
            wait = await timer.next(600 if backoff else 300)
            assert len(archive.windows) == 1
            response = await client.put("/api/polling/settings", json={"polling_enabled": False})
            assert response.status_code == 200
            # Drain scheduling, with no wall-clock sleeps or synthetic archive calls.
            for _ in range(10):
                await asyncio.sleep(0)
            assert wait.cancelled() and len(archive.windows) == 1
            assert poller.next_poll_at is None and not task.done()
            await client.put("/api/polling/settings", json={"polling_enabled": True})
            resumed = await timer.next(300)
            assert poller.next_poll_at == stamp + timedelta(minutes=5)
            stamp += timedelta(seconds=25)
            await client.put("/api/polling/settings", json={"interval_minutes": 11})
            changed = await timer.next(660)
            assert resumed.cancelled()
            assert poller.next_poll_at == stamp + timedelta(minutes=11)
            assert len(archive.windows) == 1
            archive.failure = None
            changed.set_result(None)
            await timer.next(660)
            assert len(archive.windows) == 2 and poller.archive_skips == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert poller.next_poll_at is None


async def test_manual_poll_backoff_resets_only_after_success(tmp_path):
    async with service(tmp_path) as (app, archive, _), connection(app) as client:
        poller = app.state.poller
        await client.put("/api/polling/settings", json={"polling_enabled": False})
        archive.failure = "none"
        for expected in (600, 1200):
            assert (await client.post("/api/polling/run")).json()["outcome"] == "skipped"
            assert poller.next_wait == expected
        archive.failure = None
        assert (await client.post("/api/polling/run")).json()["outcome"] == "ok"
        assert poller.next_wait == 300 and poller.archive_skips == 0
        assert not poller.enabled


def test_migration_seven_preserves_sync_state_and_articles(tmp_path):
    path = tmp_path / "upgrade.sqlite3"
    url = f"sqlite:///{path}"
    store = Store(url)
    store.import_articles([{"article_id": 1, "url": "https://paper.example/fiction"}])
    store.save_poll_position("2026-11-20T00:00:00+00:00")
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE poll_setting_overrides")
        connection.execute("PRAGMA user_version=6")
    for _ in range(2):
        store = Store(url)
        with store.engine.connect() as connection:
            assert get_version(connection) == 7
        assert store.article("ka-1") is not None
        assert store.poll_status()["last_success_at"] == "2026-11-20T00:00:00+00:00"
        assert store.poll_overrides() == {}
        store.close()


def test_poll_override_store_roundtrip(database_url):
    store = Store(database_url)
    try:
        store.update_poll_overrides(OVERRIDES)
        assert store.poll_overrides() == OVERRIDES
        store.update_poll_overrides({}, reset=["interval_minutes"])
        assert "interval_minutes" not in store.poll_overrides()
        store.update_poll_overrides({}, reset=[])
        assert store.poll_overrides() == {}
    finally:
        store.close()


@pytest.mark.parametrize("name", DEFAULTS)
async def test_reset_one_keeps_every_other_override(tmp_path, name):
    async with service(tmp_path) as (app, _, _), connection(app) as client:
        await client.put("/api/polling/settings", json=OVERRIDES)
        response = await client.post("/api/polling/settings/reset", json={"names": [name]})
        assert response.status_code == 200
        for field, value in response.json().items():
            assert value == {
                "value": DEFAULTS[field] if field == name else OVERRIDES[field],
                "default": DEFAULTS[field], "source": "env" if field == name else "ui"}


async def test_dormant_environment_disabled_task_resumes(tmp_path):
    async with service(tmp_path, poll_interval_minutes=0) as (app, archive, _):
        poller = app.state.poller
        poller.clock = lambda: datetime(2026, 11, 20, tzinfo=timezone.utc)
        timer = ControlledTimer()
        poller.sleep = timer
        task = asyncio.create_task(poller.run())
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            assert timer.calls.empty() and archive.windows == [] and not task.done()
            async with connection(app) as client:
                await client.put("/api/polling/settings", json={"polling_enabled": True})
            wait = await timer.next(300)
            assert archive.windows == []
            wait.set_result(None)
            await timer.next(300)
            assert len(archive.windows) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_manual_cycle_does_not_strand_scheduler(tmp_path):
    async with service(tmp_path) as (app, archive, _):
        poller = app.state.poller
        poller.clock = lambda: datetime(2026, 11, 20, tzinfo=timezone.utc)
        timer = ControlledTimer()
        poller.sleep = timer
        entered = asyncio.Event()

        async def held(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        archive.audits_created_between = held
        task = asyncio.create_task(poller.run())
        manual = None
        try:
            startup = await timer.next(1)
            async with connection(app) as client:
                manual = asyncio.create_task(client.post("/api/polling/run"))
                await asyncio.wait_for(entered.wait(), 2)
                startup.set_result(None)
                for _ in range(10):
                    await asyncio.sleep(0)
                manual.cancel()
                await asyncio.gather(manual, return_exceptions=True)
            await timer.next(300)
            assert not poller.status()["running"]
        finally:
            if manual:
                manual.cancel()
            task.cancel()
            await asyncio.gather(task, *([manual] if manual else []), return_exceptions=True)
