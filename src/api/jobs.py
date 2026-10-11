"""`/api/v1`: run the archive flow for one article, and read the job back.

    POST /api/v1/articles/{article_id}/audit[?dry_run=true]    queue the job (or return the existing one)
    GET  /api/v1/jobs/{job_id}[?include=result]                one job
    GET  /api/v1/articles/{article_id}/jobs/latest[?include=result]

Every route needs `Authorization: Bearer <API_AUTH_TOKEN>` (constant-time compare). Fail closed:
with API_AUTH_TOKEN unset there is no job store and each route answers a plain 404 for its own
method (the routes stay mounted, so they are listed in /openapi.json). The order is fixed:
404 (disabled) → 401 (token) → 422 (parameters), so an unauthenticated caller learns nothing.

Intake makes NO archive calls: authenticate, validate, dedupe, enqueue. The worker looks up the
article's audit (`GET /audits/by-article/{id}`) and does the rest. One job per article: a
queued/running job is returned (202, duplicate), the latest succeeded job is returned (200,
duplicate, nothing re-runs), after a failure a new job starts. A dry-run job never blocks a later
real request. Log lines carry ids only, never a header or a token.
"""
import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.api.auth import bearer_matches, unauthorized
from src.api.models import ErrorResponse, Job, JobAccepted
from src.config.settings import get_settings
from src.jobs import models as M
from src.jobs.store import JobStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["jobs"])

PREFIX = "/api/v1"
_ARTICLE_ID = re.compile(r"[0-9]{1,12}")   # ASCII digits only: '²'.isdigit() is True
_ERRORS = {401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}


# --- helpers ----------------------------------------------------------------------------------

def _intake_lock(request: Request) -> asyncio.Lock:
    """One lock per app (created in lifespan) around dedupe + create: single flight per article."""
    lock = getattr(request.app.state, "intake_lock", None)
    if lock is None:
        lock = request.app.state.intake_lock = asyncio.Lock()
    return lock


def _error(status: int, error: str, detail: Optional[str] = None, headers=None) -> JSONResponse:
    return JSONResponse(status_code=status, headers=headers,
                        content=ErrorResponse(error=error, detail=detail, status_code=status).model_dump())


def _gate(request: Request) -> Optional[JSONResponse]:
    """404 while the routes are disabled, 401 without the token; None = go on."""
    s = get_settings()
    if not s.jobs_enabled or getattr(request.app.state, "job_store", None) is None:
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    if not bearer_matches(request, s.API_AUTH_TOKEN):
        logger.warning("jobs: %s %s rejected (unauthorized)", request.method, request.url.path)
        return unauthorized()
    return None


def _article_id(raw: str) -> Optional[int]:
    if not _ARTICLE_ID.fullmatch(raw):
        return None
    value = int(raw)
    return value if value >= 1 else None


def _bool_query(raw: Optional[str]) -> Optional[bool]:
    """`true`/`false` (also 1/0); None for anything else."""
    if raw is None:
        return False
    return {"true": True, "1": True, "false": False, "0": False}.get(raw.strip().lower())


def _iso(ts: Optional[float]) -> Optional[datetime]:
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None


def job_view(job: M.Job, *, include_result: bool = False) -> Job:
    d = job.data or {}
    rec = d.get("record") or {}
    comment = dict(d["comment"]) if d.get("comment") else None
    if comment is not None and not include_result:
        comment.pop("text", None)
    return Job(
        job_id=job.id, article_id=job.article_id, audit_id=job.audit_id, status=job.status,
        phase=job.phase, outcome=job.outcome, dry_run=job.dry_run, requested_dry_run=job.requested_dry_run,
        write_blockers=d.get("write_blockers") or [], attempts=job.attempts,
        apply_attempts=job.apply_attempts, correlation_id=d.get("correlation_id"),
        created_at=_iso(job.created_at), started_at=_iso(job.started_at), finished_at=_iso(job.finished_at),
        verdict=rec.get("verdict") if rec else None, flags=list(rec.get("flags") or []) if rec else [],
        classification=rec.get("classification") if rec else None,
        archive_degradation=d.get("archive_degradation") or [], tags=d.get("tags"), comment=comment,
        error=job.error, config=d.get("config"), result=(rec or None) if include_result else None,
    )


def _accepted(job: M.Job, duplicate: bool) -> JSONResponse:
    body = JobAccepted(job_id=job.id, article_id=job.article_id, status=job.status, dry_run=job.dry_run,
                       duplicate=duplicate, status_url=f"{PREFIX}/jobs/{job.id}")
    return JSONResponse(status_code=202 if job.active else 200, content=body.model_dump(mode="json"))


def _job_response(job: Optional[M.Job], include: Optional[str]) -> JSONResponse:
    if job is None:
        return _error(404, "job_not_found")
    return JSONResponse(content=job_view(job, include_result=(include == "result")).model_dump(mode="json"))


# --- routes -----------------------------------------------------------------------------------

@router.post("/articles/{article_id}/audit", status_code=202, response_model=JobAccepted,
             responses={200: {"model": JobAccepted}, **_ERRORS, 422: {"model": ErrorResponse},
                        429: {"model": ErrorResponse}})
async def audit_article(article_id: str, request: Request, dry_run: Optional[str] = None):
    denied = _gate(request)
    if denied:
        return denied
    aid = _article_id(article_id)
    if aid is None:
        return _error(422, "invalid_article_id", "article_id must be a positive integer")
    requested_dry = _bool_query(dry_run)
    if requested_dry is None:
        return _error(422, "invalid_dry_run", "dry_run must be true or false")
    s = get_settings()
    store: JobStore = request.app.state.job_store
    async with _intake_lock(request):
        dry = bool(requested_dry or not s.ARCHIVE_WRITES_ENABLED)
        existing = await store.jobs_for(aid, limit=20)
        for j in existing:
            # A dry-run job never blocks a later real request for the same article.
            if j.active and (not j.dry_run or dry):
                return _accepted(j, duplicate=True)
        finished = [j for j in existing if not j.active and (dry or not j.dry_run)]
        if finished and finished[0].status == M.SUCCEEDED:
            return _accepted(finished[0], duplicate=True)
        if await store.count_queued() >= s.JOB_QUEUE_MAX:
            return _error(429, "queue_full", "the job queue is full; retry later",
                          headers={"Retry-After": str(int(max(1, s.JOB_RETRY_BACKOFF_S)))})
        job = M.Job(id=M.new_job_id(), article_id=aid, dry_run=dry, requested_dry_run=requested_dry)
        if not dry and s.JOB_RESULT_REUSE_S > 0:
            _maybe_reuse(job, existing, s.JOB_RESULT_REUSE_S)
        await store.create_job(job)
    logger.info("jobs: accepted job=%s article=%s dry_run=%s", job.id, aid, job.dry_run)
    worker = getattr(request.app.state, "job_worker", None)
    if worker is not None:
        worker.notify()
    return _accepted(job, duplicate=False)


def _reusable_blockers(data: dict) -> bool:
    """True when the source dry run was held back ONLY by ARCHIVE_WRITES_ENABLED=false (or by
    nothing: a requested dry run). A record made while another publish gate was closed (a
    synthetic model, the stub search, no concept catalog, no archive client) is never reused;
    a job without a recorded blocker list is not either (fail closed)."""
    blockers = data.get("write_blockers")
    return isinstance(blockers, list) and all(b == "writes_disabled" for b in blockers)


def _maybe_reuse(job: M.Job, existing: List[M.Job], max_age_s: float) -> None:
    """A real run after a reviewed dry run may reuse its stored result (JOB_RESULT_REUSE_S > 0).
    The worker still drops it when the archive now returns a different audit."""
    now = time.time()
    for j in existing:
        data = j.data or {}
        rec = data.get("record")
        if (j.dry_run and j.status == M.SUCCEEDED and j.outcome == M.DRY_RUN and rec
                and not data.get("archive_degradation") and _reusable_blockers(data)
                and j.finished_at and now - j.finished_at <= max_age_s):
            job.data["record"] = rec
            job.data["reused_from"] = j.id
            return


@router.get("/jobs/{job_id}", response_model=Job, responses=_ERRORS)
async def get_job(job_id: str, request: Request, include: Optional[str] = None):
    denied = _gate(request)
    if denied:
        return denied
    return _job_response(await request.app.state.job_store.get_job(job_id[:64]), include)


@router.get("/articles/{article_id}/jobs/latest", response_model=Job,
            responses={**_ERRORS, 422: {"model": ErrorResponse}})
async def latest_job(article_id: str, request: Request, include: Optional[str] = None):
    denied = _gate(request)
    if denied:
        return denied
    aid = _article_id(article_id)
    if aid is None:
        return _error(422, "invalid_article_id", "article_id must be a positive integer")
    return _job_response(await request.app.state.job_store.latest_for_article(aid), include)
