"""The job worker: an asyncio task started in the app lifespan (JOB_CONCURRENCY loops).

One job = the batch flow for ONE article's audit, through the same functions the CLIs use:

  precheck   `GET /audits/by-article/{article_id}` with status (404 = the article has no audit,
             ≠ unreachable); the answer must embed `article.id == article_id` (else fail closed);
             eligibility, nothing-to-do (tags present AND discussions > 0) — read-only; a dry run
             records but does not take the eligibility / nothing-to-do short-circuits
  fetching   `src.archive_flow.intake.build_case`        (body recovery, 400-char floor)
  auditing   `src.archive_flow.runner.run_case`          (CONTENT_LIMIT, per-case timeout, record)
  applying   `src.archive_flow.applier.apply_record`     (template C, live guards, ledger)

Template C, tags and comments are fixed (`COMMENT_TEMPLATE`, `WRITE_TAGS`, `WRITE_COMMENTS`): the
operator's standing choice, not configuration. The CLI keeps its own flags.

Fail-closed write path: the job writes only when `gates.write_blockers` is empty (writes enabled,
real model, real search, catalog loaded, archive client on) and the record shows no archive outage
(`gates.archive_degradation`). Otherwise it is a dry run (blockers) or withheld and
retried (`archive_degraded`).

Retries: unusable audit / archive unreachable / archive degraded → re-queued after
JOB_RETRY_BACKOFF_S until JOB_MAX_ATTEMPTS failures; a failed WRITE retries only the apply phase,
re-using the stored record (the model is never re-run for it), up to JOB_APPLY_MAX_ATTEMPTS.
After JOB_FAIL_STREAK consecutive unusable results the worker stops claiming and polls the model's
health (`runner.wait_for_model`) — the 2026-09-17 burn-through, where a dead model server turned a
whole queue into AUDIT_FAILED, cannot repeat.

Error messages in the job are fixed text per error code (`models.ERROR_MESSAGES`). Logs carry the
exception TYPE and the frame locations (file:line function), never the exception message or source
text (it can hold URLs, hosts, tokens or upstream bodies).
"""
import asyncio
import copy
import json
import logging
import time
import traceback
from pathlib import Path
from typing import Callable, Dict, Optional

from src.api.correlation import CORRELATION_ID
from src.archive_flow import applier, intake, runner
from src.archive_flow.rules import has_any_tag, is_eligible, result_usable
from src.jobs import models as M
from src.jobs.gates import archive_degradation, write_blockers
from src.jobs.store import JobStore

logger = logging.getLogger(__name__)

LEASE_S = 3600.0          # on top of JOB_CASE_TIMEOUT_S; renewed at every phase change
MAX_INTERRUPTIONS = 3     # a job found `running` with nobody processing it this often is failed
IDLE_POLL_S = 1.0
PRUNE_EVERY_S = 3600.0

COMMENT_TEMPLATE = "C"
WRITE_TAGS = True
WRITE_COMMENTS = True


class _Retry(Exception):
    """Internal: end this attempt and re-queue (or fail when attempts are exhausted)."""

    def __init__(self, outcome: str, code: str, *, apply_phase: bool = False, paused: bool = False):
        super().__init__(code)
        self.outcome, self.code, self.apply_phase = outcome, code, apply_phase
        # True when this attempt's failure paused the worker (JOB_FAIL_STREAK): the model is the
        # suspect, not the job, so the attempt is not counted toward JOB_MAX_ATTEMPTS.
        self.paused = paused


class _Done(Exception):
    """Internal: the job reached a terminal outcome."""

    def __init__(self, status: str, outcome: str, code: Optional[str] = None):
        super().__init__(outcome)
        self.status, self.outcome, self.code = status, outcome, code


def default_job_client(rt):
    """The archive client handed to the pipeline for ONE job.

    The batch runner never passed a client, so each stage built a fresh one: the step-4 window
    cache (TTL 30 min) never survived from one case to the next. A fresh client per job keeps that
    exactly (no stale window from an earlier audit — design §4.3, "TTL 0") while SHARING the
    runtime client's auth object, so the cached 15-minute JWT is reused and /authenticate
    (3/min per IP) is not hit once per stage.
    """
    from src.clients.karakutu import KarakutuClient
    base = getattr(rt, "karakutu_client", None)
    if isinstance(base, KarakutuClient):
        return KarakutuClient(base_url=base.base_url, timeout=base.timeout, auth=base.auth)
    return None   # unknown client type (tests): the stages build their own, as the batch did


class JobWorker:
    def __init__(self, store: JobStore, rt, settings, *,
                 job_client_factory: Callable = default_job_client,
                 run_case=None, build_case=None, apply_record=None, wait_for_model=None,
                 idle_poll_s: float = IDLE_POLL_S):
        self.store, self.rt, self.settings = store, rt, settings
        self.job_client_factory = job_client_factory
        # Resolved at call time from the modules so tests can patch them there.
        self._run_case = run_case
        self._build_case = build_case
        self._apply_record = apply_record
        self._wait_for_model = wait_for_model
        self.idle_poll_s = idle_poll_s
        self.fail_streak = 0
        self.paused = False
        self.model_unavailable = False
        self._stopping = False
        self._wake = asyncio.Event()
        self._tasks: list = []
        self._audit_locks: Dict[int, asyncio.Lock] = {}
        self._last_prune = 0.0

    # --- lifecycle --------------------------------------------------------------------------

    async def start(self) -> None:
        n = await self.store.requeue_running(MAX_INTERRUPTIONS)
        if n:
            logger.info(json.dumps({"event": "jobs_requeued_on_start", "count": n}))
        # Logs + stage timings per case, keyed by a ContextVar. set_level=False: the API process's
        # logging configuration is left as it is (the batch CLI lowers the levels itself).
        runner.install_instrumentation(set_level=False)
        self._stopping = False
        for i in range(max(1, int(self.settings.JOB_CONCURRENCY))):
            self._tasks.append(asyncio.create_task(self._loop(), name=f"job-worker-{i}"))

    async def stop(self, grace_s: float) -> None:
        """Stop claiming; let the current job finish for up to `grace_s`, then cancel it (it stays
        `running` in the store and is re-queued at the next start)."""
        self._stopping = True
        self._wake.set()
        if not self._tasks:
            return
        _, pending = await asyncio.wait(self._tasks, timeout=grace_s)
        for t in pending:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def notify(self) -> None:
        self._wake.set()

    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._tasks)

    # --- loop -------------------------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                await self._maybe_prune()
                if self.paused:
                    await self._wait_for_model_health()
                    continue
                job = await self.store.claim_next(self.lease_s, max_interruptions=MAX_INTERRUPTIONS)
                if job is None:
                    await self._idle()
                    continue
                await self.process(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the loop must survive anything
                logger.error("job worker loop error: %s at %s", type(exc).__name__, _frames(exc))
                await self._idle()

    async def _idle(self) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self.idle_poll_s)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _maybe_prune(self) -> None:
        now = time.time()
        if now - self._last_prune < PRUNE_EVERY_S:
            return
        self._last_prune = now
        await self.store.prune(jobs_finished_before=now - self.settings.JOB_RETENTION_DAYS * 86400)

    async def _wait_for_model_health(self) -> None:
        wait = self._wait_for_model or runner.wait_for_model
        ok = await wait(self.rt.provider, every=self.settings.JOB_HEALTH_POLL_S,
                        max_wait=self.settings.JOB_HEALTH_WAIT_MAX_S, say=logger.info)
        if ok:
            self.paused = self.model_unavailable = False
            self.fail_streak = 0
            logger.info(json.dumps({"event": "worker_resumed"}))
        else:
            # Stay paused (nothing is claimed, nothing burns through as AUDIT_FAILED); /ready
            # reports model=fail. The next loop iteration keeps polling.
            self.model_unavailable = True

    # --- one job ----------------------------------------------------------------------------

    async def process(self, job: M.Job) -> M.Job:
        token = CORRELATION_ID.set(job.id)
        job.data.setdefault("correlation_id", job.id)
        try:
            try:
                await self._run(job)
            except _Done as d:
                self._finish(job, d.status, d.outcome, d.code)
            except _Retry as r:
                self._retry(job, r)
            except asyncio.CancelledError:
                raise   # shutdown: the job stays `running`; requeue_running() picks it up
            except Exception as exc:  # noqa: BLE001
                logger.error("job %s failed: %s at %s", job.id, type(exc).__name__, _frames(exc))
                self._finish(job, M.FAILED, M.FAILED_OUTCOME, "internal")
            await self.store.save_job(job)
            self._log(job, "finished" if job.status in (M.SUCCEEDED, M.FAILED) else "requeued")
            return job
        finally:
            CORRELATION_ID.reset(token)

    def _log(self, job: M.Job, event: str) -> None:
        logger.info(json.dumps({
            "event": event, "job_id": job.id, "article_id": job.article_id, "audit_id": job.audit_id,
            "correlation_id": job.id, "phase": job.phase, "status": job.status, "outcome": job.outcome,
            "dry_run": job.dry_run, "attempts": job.attempts,
            "elapsed_s": round(time.time() - (job.started_at or time.time()), 2)}))

    @property
    def lease_s(self) -> float:
        """How long a claimed job may go without a phase change before it counts as abandoned:
        longer than the slowest phase (the audit, bounded by JOB_CASE_TIMEOUT_S)."""
        return float(self.settings.JOB_CASE_TIMEOUT_S) + LEASE_S

    async def _phase(self, job: M.Job, phase: str) -> None:
        job.phase = phase
        job.lease_until = time.time() + self.lease_s
        await self.store.save_job(job)
        self._log(job, "phase")

    def _finish(self, job: M.Job, status: str, outcome: str, code: Optional[str] = None) -> None:
        job.status, job.outcome, job.phase = status, outcome, "done"
        job.finished_at, job.lease_until = time.time(), None
        job.error = M.error(code) if code else None

    def _retry(self, job: M.Job, r: _Retry) -> None:
        s = self.settings
        if r.apply_phase:
            job.apply_attempts += 1
            more = job.apply_attempts < s.JOB_APPLY_MAX_ATTEMPTS
        elif r.paused:
            more = True   # re-queued uncounted; the worker polls the model's health first
        else:
            fails = int(job.data.get("failures", 0)) + 1
            job.data["failures"] = fails
            more = fails < s.JOB_MAX_ATTEMPTS
        if more:
            job.status, job.phase, job.outcome = M.QUEUED, "queued", None
            job.run_after, job.lease_until = time.time() + s.JOB_RETRY_BACKOFF_S, None
            job.error = M.error(r.code)
            job.data["last_retry"] = {"outcome": r.outcome, "code": r.code, "at": time.time()}
        else:
            self._finish(job, M.FAILED, r.outcome, r.code)

    async def _run(self, job: M.Job) -> None:
        s, rt = self.settings, self.rt
        client = rt.karakutu_client
        blockers = write_blockers(s, rt)
        job.data["write_blockers"] = blockers
        job.dry_run = bool(job.requested_dry_run or blockers)
        job.data["config"] = {"template": COMMENT_TEMPLATE, "tags": WRITE_TAGS,
                              "comments": WRITE_COMMENTS, "content_limit": runner.CONTENT_LIMIT,
                              "case_timeout": s.JOB_CASE_TIMEOUT_S, "min_body_chars": intake.MIN_BODY_CHARS,
                              "model": _model_name(rt), "search": s.WEB_SEARCH_PROVIDER}

        # -- precheck: the article's audit ----------------------------------------------------
        await self._phase(job, "precheck")
        status, audit = await client.get_audit_by_article(job.article_id)
        if status is None:
            if (audit or {}).get("error") == "disabled":
                raise _Done(M.FAILED, M.ARCHIVE_UNAVAILABLE, "archive_disabled")
            raise _Retry(M.ARCHIVE_UNAVAILABLE, "archive_unavailable")
        if status == 404:
            raise _Done(M.FAILED, M.NO_AUDIT_FOR_ARTICLE, "no_audit_for_article")
        if status != 200 or not isinstance(audit, dict):
            raise _Retry(M.ARCHIVE_UNAVAILABLE, "archive_unavailable")
        art_id, aid = (audit.get("article") or {}).get("id"), audit.get("id")
        if not (_is_id(art_id) and art_id == job.article_id and _is_id(aid)):
            # Fail closed: never act on an audit the archive did not tie to THIS article.
            job.data["precheck"] = {"audit_article_id": art_id if _is_id(art_id) else None}
            raise _Done(M.FAILED, M.REJECTED_ARTICLE_MISMATCH, "article_mismatch")
        job.audit_id = aid
        discussions = await client.count_discussions(aid)
        ledger = await self.store.get_ledger(aid)
        eligible = is_eligible(audit)
        tags_done = has_any_tag(audit) or bool((ledger.get("tags") or {}).get("ok")) or not WRITE_TAGS
        comment_done = ((discussions is not None and discussions > 0)
                        or bool((ledger.get("comment") or {}).get("ok")) or not WRITE_COMMENTS)
        nothing = tags_done and comment_done
        job.data["precheck"] = {"status": audit.get("status"), "eligible": eligible,
                                "existing_tags": has_any_tag(audit), "discussions": discussions,
                                "nothing_to_do": nothing}
        if not eligible and not job.dry_run:
            raise _Done(M.SUCCEEDED, M.SKIPPED_INELIGIBLE)
        if nothing and not job.dry_run:
            raise _Done(M.SUCCEEDED, M.SKIPPED_NOTHING_TO_DO)

        # -- fetching + auditing (skipped when a usable, non-degraded record is stored) -------
        record = job.data.get("record")
        reusable = (record is not None and result_usable(record).write
                    and not job.data.get("record_degraded") and _record_audit(record) == aid)
        if record is not None and not reusable and job.data.get("reused_from"):
            # A reused dry-run record (JOB_RESULT_REUSE_S) that belongs to another audit than the
            # one the archive returned now, or that is not usable: drop it and run the audit.
            job.data.pop("record", None)
            job.data.pop("reused_from", None)
        if not reusable:
            await self._phase(job, "fetching")
            build = self._build_case or intake.build_case
            case = await build(client, audit=audit, discussions=discussions)
            if isinstance(case, intake.Skip):
                job.data["fetch"] = {"skip": case.reason, "body_chars": case.body_chars}
                if case.reason == "no_url":
                    raise _Done(M.SUCCEEDED, M.SKIPPED_NO_URL)
                if case.body_chars == 0:
                    # get_article_with_body collapses "unreachable" into "no record": ask again
                    # with the status kept, so an outage is retried instead of skipped for good.
                    url = (audit.get("article") or {}).get("link")
                    st, _ = await client.get_with_status("/articles", {"url": url})
                    if st is None:
                        raise _Retry(M.ARCHIVE_UNAVAILABLE, "archive_unavailable")
                raise _Done(M.SUCCEEDED, M.SKIPPED_THIN_BODY)
            job.data["fetch"] = {"content_chars": case.get("content_chars")}

            await self._phase(job, "auditing")
            run = self._run_case or runner.run_case
            record = await run(case, rt, timeout=s.JOB_CASE_TIMEOUT_S,
                               karakutu_client=self.job_client_factory(rt), instrument=False)
            job.data["record"] = record
            job.data["model"] = record.get("model")   # requested + served model ids (additive)
            job.data.pop("record_degraded", None)
            usable = result_usable(record)
            if not usable.write:
                self.fail_streak += 1
                paused_now = False
                if self.fail_streak >= s.JOB_FAIL_STREAK and not self.paused:
                    self.paused = paused_now = True
                    logger.warning(json.dumps({"event": "worker_paused", "fail_streak": self.fail_streak}))
                timed_out = str(record.get("error") or "").startswith("case timeout")
                raise _Retry(M.AUDIT_FAILED, "case_timeout" if timed_out else "audit_unusable",
                             paused=paused_now)
            self.fail_streak = 0
        job.data["verdict"] = record.get("verdict")

        degraded = archive_degradation(record)
        job.data["archive_degradation"] = degraded
        if degraded and not job.dry_run:
            job.data["record_degraded"] = True
            raise _Retry(M.ARCHIVE_DEGRADED, "archive_degraded")

        # -- applying -------------------------------------------------------------------------
        await self._phase(job, "applying")
        lock = self._audit_locks.setdefault(aid, asyncio.Lock())
        async with lock:
            await self._apply(job, aid, record)

    async def _apply(self, job: M.Job, aid: int, record: dict) -> None:
        s, client = self.settings, self.rt.karakutu_client
        ledger = await self.store.get_ledger(aid)

        async def persist():
            await self.store.put_ledger(aid, ledger)

        apply = self._apply_record or applier.apply_record
        # Real run: while the tag write is still being retried, the comment is HELD. Posted now it
        # would say "PROPOSED ONLY, not written" and be contradicted when the retry lands; on the
        # last apply attempt it is posted with whatever wording the actual outcome allows.
        last = job.apply_attempts + 1 >= s.JOB_APPLY_MAX_ATTEMPTS
        out = await apply(client, record, ledger_entry=ledger, persist=persist,
                          template=COMMENT_TEMPLATE, do_tags=WRITE_TAGS,
                          do_comments=WRITE_COMMENTS, dry_run=job.dry_run,
                          pacing_s=s.ARCHIVE_WRITE_PACING_S, frontend_url=s.KARAKUTU_FRONTEND_URL,
                          hold_comment_on_tag_failure=not last)
        job.data["apply"] = {"stage": out.stage, "reason": out.reason,
                             "reconciled_pending": out.reconciled_pending}
        t, c = out.tags, out.comment
        held = bool(getattr(c, "held", False))
        job.data["tags"] = {
            "requested": t.requested, "decision": t.decision.action if t.decision else None,
            "reason": t.decision.reason if t.decision else None, "payload": t.payload,
            "attempted": t.attempted, "written": t.written}
        job.data["comment"] = {
            "requested": c.requested, "decision": c.decision.action if c.decision else None,
            "reason": (c.decision.reason if c.decision else
                       "held until the tag write lands (apply retried)" if held else None),
            "tags_state": c.tags_state,
            "chars": c.chars, "discussions": c.discussions, "attempted": c.attempted,
            "posted": c.posted, "discussion_id": c.discussion_id,
            "view_url": c.view_url or (applier.view_url(aid, record["url"], s.KARAKUTU_FRONTEND_URL)
                                       if c.text else None),
            "text": c.text}
        if out.stage == "missing":
            st, _ = await client.get_with_status(f"/audits/{aid}")
            if st == 404:
                raise _Done(M.FAILED, M.NO_AUDIT_FOR_ARTICLE, "no_audit_for_article")
            raise _Retry(M.ARCHIVE_UNAVAILABLE, "archive_unavailable", apply_phase=True)
        if out.stage == "ineligible":
            raise _Done(M.SUCCEEDED, M.SKIPPED_INELIGIBLE)
        if out.stage != "processed":   # "unusable" cannot happen here (checked before apply)
            raise _Done(M.FAILED, M.FAILED_OUTCOME, "internal")
        if job.dry_run:
            raise _Done(M.SUCCEEDED, M.DRY_RUN)
        if (t.attempted and not t.written) or (c.attempted and not c.posted):
            raise _Retry(M.APPLY_FAILED, "apply_failed", apply_phase=True)
        if (c.requested and not held and c.decision is not None and not c.decision.write
                and c.discussions is None
                and not (ledger.get("comment") or {}).get("ok")):
            # The discussion count could not be read: the comment was skipped for now, not for
            # good (an unknown count is never zero). Retry the apply phase instead of finishing.
            raise _Retry(M.ARCHIVE_UNAVAILABLE, "archive_unavailable", apply_phase=True)
        if t.written or c.posted:
            raise _Done(M.SUCCEEDED, M.APPLIED)
        raise _Done(M.SUCCEEDED, M.SKIPPED_NOTHING_TO_DO)


def _frames(exc: BaseException) -> str:
    """Where it failed — file:line and function per frame, never the message or source text."""
    return " <- ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}"
                       for f in reversed(traceback.extract_tb(exc.__traceback__)))


def _is_id(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _record_audit(record: dict) -> Optional[int]:
    try:
        return int(record.get("audit_id"))
    except (TypeError, ValueError):
        return None


def _model_name(rt) -> Optional[str]:
    m = getattr(rt, "model", None)
    name = getattr(m, "model_name", None) or getattr(m, "name", None)
    return str(name) if name else type(m).__name__ if m is not None else None


def copy_record(record: dict) -> dict:
    return copy.deepcopy(record)
