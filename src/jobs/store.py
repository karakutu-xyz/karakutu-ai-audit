"""Durable job state: jobs and the per-audit apply ledger.

`JobStore` is the interface; `SqliteJobStore` (stdlib sqlite3, WAL, one file on a persistent
volume — single replica) is the default and `MemoryJobStore` serves tests (`memory://`).

The store is the source of truth for the queue: a job is claimed by flipping `queued → running`
under the store's lock, and `requeue_running()` at startup puts back whatever a crash or a
shutdown left `running` (the apply phase resumes safely from the ledger plus the live guards).
A `running` job whose lease has expired (`lease_until < now`: nothing is processing it any more)
is claimable again. Each such recovery counts as an interruption (`data["interrupted"]`); a job
interrupted more than `max_interruptions` times is failed as `internal` instead of being re-queued
forever (a job that crashes the process every time it runs).

The ledger entry for an audit is EXACTLY the `*-apply_log.json` entry shape the batch scripts
write (`{"tags": {ok, at, payload}, "comment": {ok, at, template, tags_state, discussion_id,
view_url}}`), so an export can be read by `verify_apply.py` / `retag_results.py`.
"""
import asyncio
import copy
import json
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional

from src.jobs.models import FAILED, FAILED_OUTCOME, QUEUED, RUNNING, SUCCEEDED, Job, error


def _interrupt(job: Job, now: float, max_interruptions: Optional[int]) -> bool:
    """Record one interruption of a `running` job. False (and the job FAILED) past the cap."""
    n = int(job.data.get("interrupted", 0)) + 1
    job.data["interrupted"] = n
    job.lease_until = None
    if max_interruptions is not None and n > max_interruptions:
        job.status, job.phase, job.outcome = FAILED, "done", FAILED_OUTCOME
        job.finished_at, job.error = now, error("internal")
        return False
    return True


def _lease_expired(job: Job, now: float) -> bool:
    return job.status == RUNNING and job.lease_until is not None and job.lease_until < now


class JobStore(ABC):
    @abstractmethod
    async def create_job(self, job: Job) -> Job: ...

    @abstractmethod
    async def get_job(self, job_id: str) -> Optional[Job]: ...

    @abstractmethod
    async def save_job(self, job: Job) -> None: ...

    @abstractmethod
    async def jobs_for(self, article_id: int, limit: int = 20) -> List[Job]:
        """The article's jobs, newest first."""

    @abstractmethod
    async def count_queued(self) -> int: ...

    @abstractmethod
    async def claim_next(self, lease_s: float, now: Optional[float] = None, *,
                         max_interruptions: Optional[int] = None) -> Optional[Job]:
        """Atomically flip the oldest claimable job to running (attempts += 1): a queued job past
        its backoff, else a running job whose lease has expired (an interruption)."""

    @abstractmethod
    async def requeue_running(self, max_interruptions: Optional[int] = None) -> int:
        """Startup recovery: every `running` job back to `queued` (an interruption; past
        `max_interruptions` the job is failed instead). Returns how many were re-queued."""

    @abstractmethod
    async def get_ledger(self, audit_id: int) -> dict: ...

    @abstractmethod
    async def put_ledger(self, audit_id: int, entry: dict) -> None: ...

    @abstractmethod
    async def prune(self, *, jobs_finished_before: float) -> None:
        """Forget finished jobs. The ledger is never pruned."""

    @abstractmethod
    async def check(self) -> bool:
        """True when the store can be written (readiness)."""

    async def close(self) -> None:
        return None

    async def latest_for_article(self, article_id: int) -> Optional[Job]:
        jobs = await self.jobs_for(article_id, limit=1)
        return jobs[0] if jobs else None


# --- in-memory ------------------------------------------------------------------------------

class MemoryJobStore(JobStore):
    """Tests and throwaway runs. Same semantics, nothing survives the process."""

    def __init__(self):
        self.jobs: Dict[str, dict] = {}
        self.ledger: Dict[int, dict] = {}

    async def create_job(self, job: Job) -> Job:
        self.jobs[job.id] = copy.deepcopy(job.to_dict())
        return job

    async def get_job(self, job_id: str) -> Optional[Job]:
        d = self.jobs.get(job_id)
        return Job.from_dict(copy.deepcopy(d)) if d else None

    async def save_job(self, job: Job) -> None:
        self.jobs[job.id] = copy.deepcopy(job.to_dict())

    async def jobs_for(self, article_id, limit=20) -> List[Job]:
        rows = [d for d in self.jobs.values() if d["article_id"] == article_id]
        rows.sort(key=lambda d: (d["created_at"], d["id"]), reverse=True)
        return [Job.from_dict(copy.deepcopy(d)) for d in rows[:limit]]

    async def count_queued(self) -> int:
        return sum(1 for d in self.jobs.values() if d["status"] == QUEUED)

    async def claim_next(self, lease_s: float, now: Optional[float] = None, *,
                         max_interruptions: Optional[int] = None) -> Optional[Job]:
        now = time.time() if now is None else now
        while True:
            ready = sorted((d for d in self.jobs.values() if d["status"] == QUEUED and d["run_after"] <= now),
                           key=lambda d: (d["created_at"], d["id"]))
            if ready:
                break
            stale = sorted((d for d in self.jobs.values() if _lease_expired(Job.from_dict(copy.deepcopy(d)), now)),
                           key=lambda d: (d["created_at"], d["id"]))
            if not stale:
                return None
            job = Job.from_dict(copy.deepcopy(stale[0]))
            claimable = _interrupt(job, now, max_interruptions)
            self.jobs[job.id] = copy.deepcopy(job.to_dict())
            if claimable:
                ready = [self.jobs[job.id]]
                break
        d = ready[0]
        d.update(status=RUNNING, lease_until=now + lease_s, attempts=d["attempts"] + 1)
        if d["started_at"] is None:
            d["started_at"] = now
        return Job.from_dict(copy.deepcopy(d))

    async def requeue_running(self, max_interruptions: Optional[int] = None) -> int:
        n, now = 0, time.time()
        for jid, d in list(self.jobs.items()):
            if d["status"] == RUNNING:
                job = Job.from_dict(copy.deepcopy(d))
                if _interrupt(job, now, max_interruptions):
                    job.status, job.run_after = QUEUED, 0.0
                    n += 1
                self.jobs[jid] = copy.deepcopy(job.to_dict())
        return n

    async def get_ledger(self, audit_id: int) -> dict:
        return copy.deepcopy(self.ledger.get(int(audit_id), {}))

    async def put_ledger(self, audit_id: int, entry: dict) -> None:
        self.ledger[int(audit_id)] = copy.deepcopy(entry)

    async def prune(self, *, jobs_finished_before: float) -> None:
        self.jobs = {k: d for k, d in self.jobs.items()
                     if not (d["status"] in (SUCCEEDED, FAILED) and (d["finished_at"] or 0) < jobs_finished_before)}

    async def check(self) -> bool:
        return True


# --- sqlite ---------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    audit_id INTEGER,
    article_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    finished_at REAL,
    run_after REAL NOT NULL DEFAULT 0,
    job_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_article ON jobs (article_id, created_at);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, run_after, created_at);
CREATE TABLE IF NOT EXISTS apply_ledger (
    audit_id INTEGER PRIMARY KEY,
    entry_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


class SqliteJobStore(JobStore):
    """One sqlite file (WAL). Calls run in a worker thread under one lock — the service is a
    single process with a handful of writes per job, so contention is not a concern."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    @classmethod
    def from_url(cls, url: str) -> "SqliteJobStore":
        return cls(url[len("sqlite:///"):])

    async def _run(self, fn, *args):
        def locked():
            with self._lock:
                return fn(*args)
        return await asyncio.to_thread(locked)

    # sync bodies ------------------------------------------------------------------------
    def _upsert(self, job: Job) -> None:
        self._conn.execute(
            "INSERT INTO jobs (id, audit_id, article_id, status, created_at, finished_at, run_after, job_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET audit_id=excluded.audit_id, "
            "article_id=excluded.article_id, status=excluded.status, finished_at=excluded.finished_at, "
            "run_after=excluded.run_after, job_json=excluded.job_json",
            (job.id, job.audit_id, job.article_id, job.status, job.created_at, job.finished_at,
             job.run_after, json.dumps(job.to_dict(), ensure_ascii=False)))

    @staticmethod
    def _job(row) -> Job:
        return Job.from_dict(json.loads(row[0]))

    def _get(self, job_id):
        row = self._conn.execute("SELECT job_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def _jobs_for(self, article_id, limit):
        rows = self._conn.execute("SELECT job_json FROM jobs WHERE article_id = ? "
                                  "ORDER BY created_at DESC, id DESC LIMIT ?", (article_id, limit))
        return [self._job(r) for r in rows.fetchall()]

    def _claim(self, lease_s, now, max_interruptions=None):
        row = self._conn.execute("SELECT job_json FROM jobs WHERE status = ? AND run_after <= ? "
                                 "ORDER BY created_at, id LIMIT 1", (QUEUED, now)).fetchone()
        job = self._job(row) if row else None
        if job is None:
            # No queued job: reclaim a running one whose lease expired (nothing is processing it).
            rows = self._conn.execute("SELECT job_json FROM jobs WHERE status = ? ORDER BY created_at, id",
                                      (RUNNING,)).fetchall()
            for r in rows:
                cand = self._job(r)
                if not _lease_expired(cand, now):
                    continue
                if _interrupt(cand, now, max_interruptions):
                    job = cand
                    break
                self._upsert(cand)   # failed past the cap
            if job is None:
                return None
        job.status, job.lease_until, job.attempts = RUNNING, now + lease_s, job.attempts + 1
        if job.started_at is None:
            job.started_at = now
        self._upsert(job)
        return job

    def _requeue(self, max_interruptions=None):
        rows = self._conn.execute("SELECT job_json FROM jobs WHERE status = ?", (RUNNING,)).fetchall()
        n, now = 0, time.time()
        for r in rows:
            job = self._job(r)
            if _interrupt(job, now, max_interruptions):
                job.status, job.run_after = QUEUED, 0.0
                n += 1
            self._upsert(job)
        return n

    def _prune(self, jobs_finished_before):
        self._conn.execute("DELETE FROM jobs WHERE status IN (?, ?) AND finished_at IS NOT NULL "
                           "AND finished_at < ?", (SUCCEEDED, FAILED, jobs_finished_before))

    def _check(self):
        self._conn.execute("CREATE TABLE IF NOT EXISTS _ready_probe (n INTEGER)")
        self._conn.execute("DELETE FROM _ready_probe")
        self._conn.execute("INSERT INTO _ready_probe (n) VALUES (1)")
        return True

    # async interface ----------------------------------------------------------------------
    async def create_job(self, job: Job) -> Job:
        await self._run(self._upsert, job)
        return job

    async def get_job(self, job_id):
        return await self._run(self._get, job_id)

    async def save_job(self, job: Job) -> None:
        await self._run(self._upsert, job)

    async def jobs_for(self, article_id, limit=20):
        return await self._run(self._jobs_for, article_id, limit)

    async def count_queued(self) -> int:
        return await self._run(lambda: self._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = ?", (QUEUED,)).fetchone()[0])

    async def claim_next(self, lease_s, now=None, *, max_interruptions=None):
        return await self._run(self._claim, lease_s, time.time() if now is None else now,
                               max_interruptions)

    async def requeue_running(self, max_interruptions=None) -> int:
        return await self._run(self._requeue, max_interruptions)

    async def get_ledger(self, audit_id):
        row = await self._run(lambda: self._conn.execute(
            "SELECT entry_json FROM apply_ledger WHERE audit_id = ?", (int(audit_id),)).fetchone())
        return json.loads(row[0]) if row else {}

    async def put_ledger(self, audit_id, entry):
        payload = json.dumps(entry, ensure_ascii=False)
        await self._run(lambda: self._conn.execute(
            "INSERT INTO apply_ledger (audit_id, entry_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(audit_id) DO UPDATE SET entry_json=excluded.entry_json, updated_at=excluded.updated_at",
            (int(audit_id), payload, time.time())))

    async def prune(self, *, jobs_finished_before):
        await self._run(self._prune, jobs_finished_before)

    async def check(self) -> bool:
        try:
            return bool(await self._run(self._check))
        except Exception:  # noqa: BLE001 — readiness answers "no", it never raises
            return False

    async def close(self) -> None:
        await self._run(self._conn.close)


def open_store(url: str) -> JobStore:
    if url == "memory://":
        return MemoryJobStore()
    if url.startswith("sqlite:///"):
        return SqliteJobStore.from_url(url)
    raise ValueError("JOB_STORE_URL must be sqlite:///<path> or memory://")
