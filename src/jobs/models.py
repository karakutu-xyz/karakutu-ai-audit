"""Job record and its vocabulary (status / phase / outcome / error codes).

A job is "run the whole archive flow for this article": look up the article's audit (pre-check)
→ fetch the body → six-step audit → apply under the standing rules. The job record IS the report
(the archive has no callback): `GET /api/v1/jobs/{id}`.

`skipped_*` and `dry_run` are SUCCEEDED outcomes — the service did its job correctly. Everything
else is FAILED, and only a failed job lets a later request for the same article start a new job
(the `--retry-failed` of the batch scripts). `no_audit_for_article` is a failure on purpose: the
archive may open an audit for the article later, and the next request must then run.
"""
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

# --- status ---------------------------------------------------------------------------------
QUEUED, RUNNING, SUCCEEDED, FAILED = "queued", "running", "succeeded", "failed"
STATUSES = (QUEUED, RUNNING, SUCCEEDED, FAILED)
ACTIVE_STATUSES = (QUEUED, RUNNING)

# --- phase ----------------------------------------------------------------------------------
PHASES = ("queued", "precheck", "fetching", "auditing", "applying", "done")

# --- outcome --------------------------------------------------------------------------------
APPLIED = "applied"
DRY_RUN = "dry_run"
SKIPPED_INELIGIBLE = "skipped_ineligible_status"
SKIPPED_THIN_BODY = "skipped_thin_body"
SKIPPED_NO_URL = "skipped_no_url"
SKIPPED_NOTHING_TO_DO = "skipped_nothing_to_do"
NO_AUDIT_FOR_ARTICLE = "no_audit_for_article"
REJECTED_ARTICLE_MISMATCH = "rejected_article_mismatch"
AUDIT_FAILED = "audit_failed"
ARCHIVE_UNAVAILABLE = "archive_unavailable"
ARCHIVE_DEGRADED = "archive_degraded"
APPLY_FAILED = "apply_failed"
FAILED_OUTCOME = "failed"

SUCCESS_OUTCOMES = (APPLIED, DRY_RUN, SKIPPED_INELIGIBLE, SKIPPED_THIN_BODY, SKIPPED_NO_URL,
                    SKIPPED_NOTHING_TO_DO)
FAILURE_OUTCOMES = (NO_AUDIT_FOR_ARTICLE, REJECTED_ARTICLE_MISMATCH, AUDIT_FAILED, ARCHIVE_UNAVAILABLE,
                    ARCHIVE_DEGRADED, APPLY_FAILED, FAILED_OUTCOME)
OUTCOMES = SUCCESS_OUTCOMES + FAILURE_OUTCOMES

# --- error codes (job.error.code); the message is always fixed, safe text -----------------
ERROR_MESSAGES = {
    "case_timeout": "the audit exceeded the per-case time limit",
    "audit_unusable": "the audit did not complete (AUDIT_FAILED/ERROR); no result to apply",
    "model_unavailable": "the model server is not answering its health check",
    "archive_unavailable": "the archive could not be reached",
    "archive_disabled": "the archive client is disabled (no wallet configured)",
    "archive_degraded": "the archive was unreachable during the audit; writes withheld",
    "apply_failed": "a write to the archive did not succeed",
    "no_audit_for_article": "the archive has no audit for this article",
    "article_mismatch": "the archive answered with an audit for a different article",
    "internal": "internal error; see the service logs under this job's correlation id",
}


def error(code: str) -> dict:
    return {"code": code, "message": ERROR_MESSAGES.get(code, ERROR_MESSAGES["internal"])}


def new_job_id() -> str:
    """Time-sortable, unguessable: 12 hex of milliseconds + 12 random hex."""
    return f"{int(time.time() * 1000):012x}{secrets.token_hex(6)}"


@dataclass
class Job:
    id: str
    article_id: int
    audit_id: Optional[int] = None         # the article's audit, set by the worker's lookup
    status: str = QUEUED
    phase: str = "queued"
    outcome: Optional[str] = None
    dry_run: bool = False                  # EFFECTIVE dry run (requested or forced by a gate)
    requested_dry_run: bool = False
    attempts: int = 0                      # claims so far (audit / archive retries)
    apply_attempts: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    run_after: float = 0.0                 # not claimable before this (retry backoff)
    lease_until: Optional[float] = None
    error: Optional[dict] = None           # {"code", "message"} — never raw exception text
    # Everything else the job learned (record, decisions, gates …), JSON-serialisable.
    data: dict = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        return cls(**d)
