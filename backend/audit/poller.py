"""Single-process, read-only archive intake. Public diagnostics are fixed labels only."""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import get_args

from audit.clients.karakutu import ArchiveState


ARCHIVE_FAILURES = frozenset(get_args(ArchiveState)) - {"ok", "unchecked"}


def utc_now():
    return datetime.now(timezone.utc)


class ArchivePoller:
    def __init__(self, settings, state, client, fill_archive_text, submit, *,
                 clock=utc_now, sleep=asyncio.sleep, startup_delay=1):
        self.settings, self.state, self.client = settings, state, client
        self.fill_archive_text, self.submit = fill_archive_text, submit
        self.clock, self.sleep, self.startup_delay = clock, sleep, startup_delay
        self.enabled = (settings.mode != "test" and settings.poll_interval_minutes > 0
                        and client is not None and client.enabled)
        self.last = None
        self.last_success = None
        self.cycle_lock = asyncio.Lock()

    def status(self):
        return {
            "enabled": self.enabled,
            "interval_minutes": self.settings.poll_interval_minutes if self.enabled else 0,
            "auto_audit_enabled": self.enabled and self.settings.auto_audit_enabled,
            "auto_audit_max_per_poll": self.settings.auto_audit_max_per_poll,
            "last": self.last,
        }

    def archive_failure(self):
        state = getattr(self.client, "state", None)
        return state if state in ARCHIVE_FAILURES else "archive_error"

    async def run(self):
        # Yield to startup and allow the first status request before intake starts.
        await self.sleep(self.startup_delay)
        while True:
            await self.run_cycle()
            await self.sleep(self.settings.poll_interval_minutes * 60)

    async def run_cycle(self):
        """Run one cycle without sleeping; clocks and scheduling are injectable."""
        async with self.cycle_lock:
            cycle = {"started_at": self.clock().isoformat(), "finished_at": None,
                     "outcome": "ok", "reason": None, "found": 0, "new": 0, "queued": 0}
            try:
                reason = await self._cycle(cycle)
            except Exception:
                # Do not retain exceptions or log their text, including persistence errors.
                reason = "poll_error"
            if reason:
                cycle.update(outcome="skipped", reason=reason)
            cycle["finished_at"] = self.clock().isoformat()
            self.last = cycle
            return cycle

    async def _cycle(self, cycle):
        if not self.enabled:
            return "disabled"
        lock = self.state.article_import_lock
        if lock.locked():
            return "import_in_progress"
        async with lock:
            end = self.clock()
            start = (self.last_success - timedelta(days=1) if self.last_success
                     else end - timedelta(days=30))
            start = max(end - timedelta(days=30), min(start, end - timedelta(days=2)))
            try:
                items = await self.client.audits_created_between(
                    int(start.timestamp()), int(end.timestamp()))
            except Exception:
                return self.archive_failure()
            if items is None:
                return self.archive_failure()
            cycle["found"] = len(items)
            counts = self.state.store.import_articles(items)
            cycle["new"] = counts["imported"]
            self.last_success = end
        if not self.settings.auto_audit_enabled:
            return None
        if self.state.queue_paused:
            return "queue_paused"
        store = self.state.store
        budget = self.settings.auto_audit_max_per_poll - store.active_poll_runs()
        if budget <= 0:
            return None
        for ref in store.unaudited_archive_refs():
            if cycle["queued"] >= budget:
                break
            if self.state.queue_paused:
                return "queue_paused"
            article = store.article(ref)
            if article is None:
                continue
            await self.fill_archive_text(ref, article)
            async with self.state.article_lock:
                # Manual audits may win while the archive read is in flight. Even a
                # completed/failed/stopped manual run prevents an automatic retry.
                if self.state.queue_paused:
                    return "queue_paused"
                if store.active_poll_runs() >= self.settings.auto_audit_max_per_poll:
                    break
                article = store.article(ref)
                if (article is None or article["category"] != "live"
                        or not article["content"].strip() or store.has_article_run(ref)):
                    continue
                self.submit(None, "live", "poll", article_ref=ref)
                cycle["queued"] += 1
        return None
