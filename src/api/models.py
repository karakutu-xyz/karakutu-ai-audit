# src/api/models.py
from datetime import datetime
from enum import Enum
from typing import Optional, List

from pydantic import BaseModel, Field, field_validator


class Verdict(str, Enum):
    """Audit outcome.

    None of these is a finding of censorship. `POSSIBLE_CENSORSHIP` is a triage flag that
    nominates an article for human review — the determination that something *was* censored
    is a human call on independent evidence, and this service has no authority to make it.
    """

    LIKELY_LOW_PUBLIC_INTEREST = "LIKELY_LOW_PUBLIC_INTEREST"
    # The audit premise did not hold at run time: the "removed" article answered 2xx at
    # its own URL with its text measurably intact (stage 3's liveness probe). It may have
    # been RESTORED after the archive recorded the removal, the removal may be
    # intermittent or geo-dependent, or the archive's status may be stale — a human should
    # re-check and close or re-queue. NOT a finding that nothing was ever removed, and
    # never exculpatory.
    NOT_REMOVED = "NOT_REMOVED"
    # MEASURED republication: the removed article's text is arithmetically contained in a
    # live same-publisher copy (old_in_new >= MOVED_THRESHOLD, src/search/textmatch.py).
    # Still says NOTHING about why the original URL was removed — never surface it to a
    # human as exculpatory.
    MOVED = "MOVED"
    # A same-publisher live copy with a PARTIAL measured textual fit, or a fit that could
    # not be measured. A human should check whether the differences matter.
    LIKELY_MOVED = "LIKELY_MOVED"
    FOUND_ONLINE = "FOUND_ONLINE"
    POSSIBLE_CENSORSHIP = "POSSIBLE_CENSORSHIP"
    INCONCLUSIVE = "INCONCLUSIVE"
    # The audit could not be completed (the analysis model failed). NOT a result: it means
    # this article was never actually audited and must be re-run. Kept distinct from
    # INCONCLUSIVE on purpose — a crashed audit must never be filed as "we found nothing".
    AUDIT_FAILED = "AUDIT_FAILED"


class StageName(str, Enum):
    """The six steps, in pipeline order (AuditResponse.stages is always this order)."""
    CLASSIFICATION = "classification"   # IPTC media topics + genre + geographic scope
    SIMILARITY = "similarity"           # find similar link (archive similarity index)
    RELEVANCE = "relevance"             # out of scope?
    RELATED_REMOVALS = "related_removals"  # did other articles on the same subject vanish too?
    SOURCE = "source"                   # find the source of the article; is it live?
    WEB_SEARCH = "web_search"           # reachable elsewhere / on the publisher's site?


class GeographicScope(str, Enum):
    """Reach of the STORY. Mirrors the archive's `GeographicScope` minus `platform_specific`,
    which describes the reach of a censorship action, not a property of an article."""
    LOCAL = "local"
    REGIONAL = "regional"
    NATIONAL = "national"
    GLOBAL_SCOPE = "global_scope"


class AuditFlag(str, Enum):
    """Machine-readable red flags. A flag is a pointer for a human, never a finding."""
    # The outlet/agency the article itself credits as its source answers HTTP 404/410 —
    # the origin copy of the story is gone too.
    SOURCE_DEAD = "SOURCE_DEAD"
    # Other articles on the SAME subject or event (same company, case, person, incident)
    # disappeared from the archive within ±14 days of this one — a pattern the article's
    # own topic would not show. Keeps an otherwise out-of-scope article in the audit.
    # A pointer for a human: related removals can also be a publisher pruning a section.
    RELATED_REMOVALS = "RELATED_REMOVALS"


class StageOutcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class ConceptTag(BaseModel):
    notation: str = Field(..., description="IPTC notation, e.g. medtop:20000093 or genre:Current")
    label: str


class Classification(BaseModel):
    """Step 1 output. Tags are ranked (most central first); every notation was validated
    against the catalog pulled from the Karakutu API — the model cannot invent one."""
    subject_tags: List[ConceptTag] = Field(default_factory=list, max_length=5)
    genre: Optional[ConceptTag] = None
    geographic_scope: Optional[GeographicScope] = None
    gist: str = ""


class Article(BaseModel):
    url: str = Field(..., description="Canonical URL of the article")
    title: str
    content: str
    published_at: Optional[datetime] = None


class AuditRequest(BaseModel):
    removed_article: Article
    similar_articles: List[Article] = Field(default_factory=list, max_length=5)

    @field_validator("similar_articles", mode="before")
    @classmethod
    def _none_to_empty(cls, v):
        return [] if v is None else v


class StageResult(BaseModel):
    stage: StageName
    outcome: StageOutcome
    # No ge/le bounds: pass-through any float the stage emitted.
    confidence: Optional[float] = None
    explanation: str
    skipped: bool = False
    # Free-form, per-stage structured facts for reporting (keys documented in each
    # src/workflow/<step>.py). Consumers that need guarantees use the typed fields on AuditResponse.
    details: Optional[dict] = None


class AuditResponse(BaseModel):
    verdict: Verdict
    stages: List[StageResult]
    short_circuited_at: Optional[StageName] = None
    findings: str
    classification: Optional[Classification] = None
    flags: List[AuditFlag] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    service: str


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
    status_code: int


# --- article jobs (/api/v1) ----------------------------------------------------------------

class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobPhase(str, Enum):
    QUEUED = "queued"
    PRECHECK = "precheck"
    FETCHING = "fetching"
    AUDITING = "auditing"
    APPLYING = "applying"
    DONE = "done"


class JobOutcome(str, Enum):
    APPLIED = "applied"
    DRY_RUN = "dry_run"
    SKIPPED_INELIGIBLE_STATUS = "skipped_ineligible_status"
    SKIPPED_THIN_BODY = "skipped_thin_body"
    SKIPPED_NO_URL = "skipped_no_url"
    SKIPPED_NOTHING_TO_DO = "skipped_nothing_to_do"
    NO_AUDIT_FOR_ARTICLE = "no_audit_for_article"
    REJECTED_ARTICLE_MISMATCH = "rejected_article_mismatch"
    AUDIT_FAILED = "audit_failed"
    ARCHIVE_UNAVAILABLE = "archive_unavailable"
    ARCHIVE_DEGRADED = "archive_degraded"
    APPLY_FAILED = "apply_failed"
    FAILED = "failed"


class TagsState(str, Enum):
    SET = "set"
    PROPOSED_EXISTING = "proposed_existing"
    PROPOSED = "proposed"


class JobAccepted(BaseModel):
    job_id: str
    article_id: int
    status: JobStatus
    dry_run: bool
    duplicate: bool
    status_url: str


class JobError(BaseModel):
    code: str
    message: str


class TagsDecision(BaseModel):
    requested: bool = False
    decision: Optional[str] = None      # write | skip
    reason: Optional[str] = None
    payload: Optional[dict] = None
    attempted: bool = False
    written: bool = False


class CommentDecision(BaseModel):
    requested: bool = False
    decision: Optional[str] = None      # write | skip
    reason: Optional[str] = None
    tags_state: Optional[TagsState] = None
    chars: int = 0
    discussions: Optional[int] = None
    attempted: bool = False
    posted: bool = False
    discussion_id: Optional[int] = None
    view_url: Optional[str] = None
    text: Optional[str] = None          # only with ?include=result


class Job(BaseModel):
    job_id: str
    article_id: int
    audit_id: Optional[int] = None      # the article's audit, once the worker has looked it up
    status: JobStatus
    phase: JobPhase
    outcome: Optional[JobOutcome] = None
    dry_run: bool
    requested_dry_run: bool = False
    write_blockers: List[str] = Field(default_factory=list)
    attempts: int = 0
    apply_attempts: int = 0
    correlation_id: Optional[str] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    verdict: Optional[str] = None
    flags: List[str] = Field(default_factory=list)
    classification: Optional[dict] = None
    archive_degradation: List[str] = Field(default_factory=list)
    tags: Optional[TagsDecision] = None
    comment: Optional[CommentDecision] = None
    error: Optional[JobError] = None
    config: Optional[dict] = None
    result: Optional[dict] = None       # the stored run record, only with ?include=result


class ReadinessResponse(BaseModel):
    # Anonymous callers get `status` only; checks/writes_enabled/write_blockers are served only to
    # a caller presenting API_AUTH_TOKEN as a bearer token (src/api/app.py, ready()).
    status: str                         # ready | not_ready
    checks: Optional[dict] = None
    writes_enabled: Optional[bool] = None
    write_blockers: Optional[List[str]] = None
