"""Single-process, read-only archive intake. Public diagnostics are fixed labels only."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import get_args

from audit.clients.karakutu import ArchiveState


logger = logging.getLogger(__name__)


def enable_cycle_log():
    """Uvicorn configures only its own loggers; give the one-line cycle log a stderr handler."""
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# A failed text read, or an article that cannot be submitted, waits this long before retry.
ARCHIVE_TEXT_RETRY_SECONDS = 10 * 60
ARCHIVE_FAILURES = frozenset(get_args(ArchiveState)) - {"ok", "unchecked"}


def utc_now():
    return datetime.now(timezone.utc)


async def store_in_thread(function, *args):
    """Finish persistence before cancellation releases its lock or closes the store."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        # A worker failure must never replace shutdown cancellation. Keep the
        # connection/lock alive even if cancellation is requested again while draining.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # Retrieve a failed worker's exception without exposing it.
        raise cancelled


def sweep_key(ref):
    archive_id = ref[3:]
    return (len(archive_id), archive_id, ref)


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
        self.history = []
        self.loaded = False
        self.next_wait = settings.poll_interval_minutes * 60
        self.archive_skips = 0
        self.window_days = 0
        # Where the last read-bounded sweep stopped; the next sweep resumes after it so
        # unreadable older articles cannot use up every cycle's reads.
        self.sweep_after = None
        self.cycle_lock = asyncio.Lock()

    def status(self):
        return {
            "enabled": self.enabled,
            "interval_minutes": self.settings.poll_interval_minutes if self.enabled else 0,
            "auto_audit_enabled": self.enabled and self.settings.auto_audit_enabled,
            "auto_audit_max_per_poll": self.settings.auto_audit_max_per_poll,
            "last": self.last,
            "lookback_days": self.settings.poll_lookback_days,
            "history": self.history,
            "last_success_at": self.last_success.isoformat() if self.last_success else None,
        }

    async def load_state(self):
        try:
            saved = await store_in_thread(self.state.store.poll_status)
            self.history = saved["history"]
            self.last_success = (datetime.fromisoformat(saved["last_success_at"])
                                 if saved["last_success_at"] else None)
            self.loaded = True
        except Exception:
            # The cycle will retry and report poll_error, never persistence text.
            return False
        return True

    def archive_failure(self, fallback="archive_error"):
        state = getattr(self.client, "state", None)
        return state if state in ARCHIVE_FAILURES else fallback

    async def run(self):
        # Yield to startup and allow the first status request before intake starts.
        await self.sleep(self.startup_delay)
        while True:
            await self.run_cycle()
            await self.sleep(self.next_wait)

    async def run_cycle(self):
        """Run one cycle without sleeping; clocks and scheduling are injectable."""
        async with self.cycle_lock:
            cycle = {"started_at": self.clock().isoformat(), "finished_at": None,
                     "outcome": "ok", "reason": None, "found": 0, "new": 0,
                     "updated": 0, "queued": 0, "unreadable": 0}
            self.window_days = 0
            try:
                if not self.loaded and not await self.load_state():
                    raise RuntimeError("poll_error")
                reason = await self._cycle(cycle)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not retain exceptions or log their text, including persistence errors.
                reason = "poll_error"
            if reason:
                cycle.update(outcome="skipped", reason=reason)
            cycle["finished_at"] = self.clock().isoformat()
            try:
                await store_in_thread(self.state.store.save_poll_cycle, cycle)
                self.history = [cycle, *self.history][:20]
            except asyncio.CancelledError:
                raise
            except Exception:
                cycle.update(outcome="skipped", reason="poll_error")
            reason = cycle["reason"]
            base = self.settings.poll_interval_minutes * 60
            if cycle["outcome"] == "ok":
                self.archive_skips = 0
                self.next_wait = base
            elif reason in ARCHIVE_FAILURES or (reason or "").startswith("archive_"):
                # Every archive skip doubles the wait, from the first one, up to an hour.
                self.archive_skips += 1
                self.next_wait = min(base * 2 ** min(self.archive_skips, 12), 3600)
            else:
                self.next_wait = base
            logger.info(
                "poll outcome=%s reason=%s found=%d new=%d updated=%d queued=%d window_days=%.3f",
                cycle["outcome"], cycle["reason"], cycle["found"], cycle["new"],
                cycle["updated"], cycle["queued"], self.window_days)
            # Keep the original last-cycle contract; history adds update counts.
            self.last = {key: value for key, value in cycle.items() if key != "updated"}
            return self.last

    async def _cycle(self, cycle):
        if not self.enabled:
            return "disabled"
        lock = self.state.article_import_lock
        if lock.locked():
            return "import_in_progress"
        async with lock:
            end = self.clock()
            oldest = end - timedelta(days=self.settings.poll_lookback_days)
            start = (max(oldest, min(self.last_success - timedelta(days=1),
                                    end - timedelta(days=2)))
                     if self.last_success else oldest)
            self.window_days = (end - start).total_seconds() / 86400
            try:
                items = await self.client.audits_created_between(
                    int(start.timestamp()), int(end.timestamp()), require_complete=True)
            except Exception:
                return self.archive_failure()
            if items is None:
                return (getattr(self.client, "listing_reason", None)
                        or self.archive_failure("archive_page_failed"))
            cycle["found"] = len(items)
            counts = await store_in_thread(self.state.store.import_articles, items)
            cycle["new"] = counts["imported"]
            cycle["updated"] = counts["updated"]
            await store_in_thread(self.state.store.save_poll_position, end.isoformat())
            self.last_success = end
        if not self.settings.auto_audit_enabled:
            return None
        if self.state.queue_paused:
            return "queue_paused"
        store = self.state.store
        budget = self.settings.auto_audit_max_per_poll - store.active_poll_runs()
        if budget <= 0:
            return None
        attempts, reads = 0, 0
        read_limit = self.settings.auto_audit_max_per_poll
        candidates = await store_in_thread(store.unaudited_archive_refs)
        if self.sweep_after is not None:
            # Same order as the store query: archive id in numeric order, then ref.
            after = sweep_key(self.sweep_after)
            candidates = ([ref for ref in candidates if sweep_key(ref) > after]
                          + [ref for ref in candidates if sweep_key(ref) <= after])
        self.sweep_after = None
        last_tried = None
        for ref in candidates:
            if cycle["queued"] >= budget:
                break
            if reads >= read_limit or attempts >= 2 * read_limit:
                self.sweep_after = last_tried
                break
            if self.state.queue_paused:
                return "queue_paused"
            article = store.article(ref)
            if article is None:
                attempts += 1
                last_tried = ref
                continue
            retry_at = self.state.article_fill_attempts.get((ref, article["url"]))
            if retry_at is not None and self.state.article_fill_clock() < retry_at:
                cycle["unreadable"] += 1
                continue
            attempts += 1
            reads += 1
            last_tried = ref
            refreshed = await self.fill_archive_text(ref, article, refresh=True)
            if refreshed is None or not refreshed["content"].strip():
                cycle["unreadable"] += 1
                if getattr(self.client, "state", None) in ARCHIVE_FAILURES:
                    # Stop this cycle, but resume after this candidate next time: one
                    # article the archive refuses must not block every later one.
                    self.sweep_after = ref
                    return self.archive_failure()
                continue
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
                try:
                    self.submit(None, "live", "poll", article_ref=ref)
                except Exception:
                    # Saved data this article cannot be audited with (for example an
                    # unusable date) creates no run. Count it, cool it down like a failed
                    # read and keep going, so it cannot block every later candidate.
                    cycle["unreadable"] += 1
                    self.state.article_fill_attempts[(ref, article["url"])] = (
                        self.state.article_fill_clock() + ARCHIVE_TEXT_RETRY_SECONDS)
                    continue
                cycle["queued"] += 1
        return None
