"""Single-process, read-only archive intake. Public diagnostics are fixed labels only."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import get_args

from audit.clients.karakutu import ArchiveState
from audit.poll_controls import PollControls


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
        self.controls = PollControls(settings, state.store)
        self.controls_lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.next_poll_at = None
        self.schedule_revision = 0
        self.last = None
        self.last_success = None
        self.history = []
        self.loaded = False
        self.next_wait = self.controls.effective.interval_minutes * 60
        self.archive_skips = 0
        self.window_days = 0
        # Where the last read-bounded sweep stopped; the next sweep resumes after it so
        # unreadable older articles cannot use up every cycle's reads.
        self.sweep_after = None
        self.cycle_lock = asyncio.Lock()

    @property
    def unavailable_reason(self):
        if self.settings.mode == "test":
            return "test_mode"
        if self.client is None or not self.client.enabled:
            return "archive_disabled"
        return None

    @property
    def can_run(self):
        return self.unavailable_reason is None

    @property
    def enabled(self):
        return self.can_run and self.controls.effective.polling_enabled

    async def update_controls(self, values, reset=None):
        async with self.controls_lock:
            previous = self.controls.effective
            await store_in_thread(self.state.store.update_poll_overrides, values, reset)
            self.controls.apply(values, reset)
            current = self.controls.effective
            if (previous.polling_enabled != current.polling_enabled
                    or previous.interval_minutes != current.interval_minutes):
                self.schedule_revision += 1
                self.next_wait = current.interval_minutes * 60
                self.schedule(self.next_wait)
                self.wake.set()
            return self.controls.describe()

    def schedule(self, seconds):
        self.next_poll_at = (self.clock() + timedelta(seconds=seconds)
                             if self.enabled else None)

    def status(self):
        values = self.controls.effective
        return {
            "enabled": self.enabled,
            "polling_enabled": values.polling_enabled,
            "paused": not values.polling_enabled,
            "running": self.cycle_lock.locked(),
            "reason": self.unavailable_reason,
            "next_poll_at": self.next_poll_at.isoformat() if self.next_poll_at else None,
            "source": {name: field["source"] for name, field in self.controls.describe().items()},
            "interval_minutes": values.interval_minutes if self.enabled else 0,
            "auto_audit_enabled": self.enabled and values.auto_audit_enabled,
            "auto_audit_max_per_poll": values.auto_audit_max_per_poll,
            "last": self.last,
            "lookback_days": values.lookback_days,
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

    async def wait(self, seconds):
        """Race the injectable timer against control changes; drain both on shutdown."""
        timer = asyncio.create_task(self.sleep(seconds))
        changed = asyncio.create_task(self.wake.wait())
        try:
            done, _ = await asyncio.wait({timer, changed}, return_when=asyncio.FIRST_COMPLETED)
            if timer in done:
                await timer
            return changed in done
        finally:
            for task in (timer, changed):
                task.cancel()
            await asyncio.gather(timer, changed, return_exceptions=True)

    async def run(self):
        # A dormant task allows an environment-disabled schedule to resume at runtime.
        if not self.can_run:
            return
        self.schedule(self.startup_delay)
        try:
            while True:
                self.wake.clear()
                if not self.enabled:
                    self.next_poll_at = None
                    await self.wake.wait()
                    continue
                if self.next_poll_at is None:
                    self.schedule(self.next_wait)
                seconds = max(0, (self.next_poll_at - self.clock()).total_seconds())
                if await self.wait(seconds) or self.wake.is_set():
                    continue
                if self.cycle_lock.locked():
                    await self.wake.wait()
                    continue
                if self.enabled:
                    await self.run_cycle()
        finally:
            self.next_poll_at = None

    async def run_cycle(self, *, manual=False):
        """Run one cycle; API callers check the lock before requesting a manual cycle."""
        async with self.cycle_lock:
            try:
                revision = self.schedule_revision
                values = self.controls.effective
                self.next_poll_at = None
                cycle = {"started_at": self.clock().isoformat(), "finished_at": None,
                         "outcome": "ok", "reason": None, "found": 0, "new": 0,
                         "updated": 0, "queued": 0, "unreadable": 0}
                self.window_days = 0
                try:
                    if not self.loaded and not await self.load_state():
                        raise RuntimeError("poll_error")
                    reason = await self._cycle(cycle, values, manual=manual)
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
                base = self.controls.effective.interval_minutes * 60
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
                if revision == self.schedule_revision:
                    self.schedule(self.next_wait)
                return cycle if manual else self.last
            finally:
                if manual:
                    self.wake.set()

    @property
    def auto_queue_enabled(self):
        return self.enabled and self.controls.effective.auto_audit_enabled

    async def _cycle(self, cycle, values, *, manual=False):
        if not self.can_run or (not manual and not self.enabled):
            return "disabled"
        lock = self.state.article_import_lock
        if lock.locked():
            return "import_in_progress"
        async with lock:
            end = self.clock()
            oldest = end - timedelta(days=values.lookback_days)
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
        if not self.auto_queue_enabled:
            return None
        if self.state.queue_paused:
            return "queue_paused"
        store = self.state.store
        budget = values.auto_audit_max_per_poll - store.active_poll_runs()
        if budget <= 0:
            return None
        attempts, reads = 0, 0
        read_limit = values.auto_audit_max_per_poll
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
            if not self.auto_queue_enabled:
                return None
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
                if not self.auto_queue_enabled:
                    return None
                if self.state.queue_paused:
                    return "queue_paused"
                if store.active_poll_runs() >= values.auto_audit_max_per_poll:
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
