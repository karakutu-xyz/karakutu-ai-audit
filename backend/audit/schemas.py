"""Bounded public inputs; workflow outputs retain their original domain contract."""
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from audit.record import ARTICLE_URL_MAX_BYTES, AuditResult as AuditResult
from audit.record import RecordErrorCode
from audit.clients.karakutu import ArchiveState
from audit.search.base import SearchState
from audit.domain import Article, AuditRequest, AuditResponse, StageName, Verdict


class TextInput(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def no_nul(cls, value):
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("invalid_text")
        return value


class ArticleInput(Article, TextInput):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=4096)
    title: str = Field(min_length=1, max_length=2000)
    content: str = Field(min_length=1, max_length=250_000)

    @field_validator("url")
    @classmethod
    def web_url(cls, value: str) -> str:
        if len(value.encode("utf-8")) > ARTICLE_URL_MAX_BYTES:
            raise ValueError("invalid_url")
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("invalid_url")
            _ = parsed.port
        except ValueError:
            raise ValueError("invalid_url") from None
        return value


class RunInput(TextInput):
    model_config = ConfigDict(extra="forbid")
    removed_article: ArticleInput
    similar_articles: list[ArticleInput] = Field(default_factory=list, max_length=5)
    label: str | None = Field(default=None, max_length=200)
    expected_verdict: Verdict | None = None

    def audit_request(self) -> AuditRequest:
        return AuditRequest.model_validate(self.model_dump(include={"removed_article", "similar_articles"}))


class ReactionInput(TextInput):
    model_config = ConfigDict(extra="forbid")
    reaction: Literal["agree", "disagree", "needs_review"]
    reviewer: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=4000)
    expected_verdict: Verdict | None = None

    @field_validator("reviewer")
    @classmethod
    def nonblank_reviewer(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reviewer_required")
        return value.strip()


RunMode = Literal["test", "live"]


class Health(BaseModel):
    status: Literal["ok"]
    mode: RunMode


class ValidationField(BaseModel):
    loc: list[str | int]
    type: str
    code: Literal["invalid_value"]


class SafeError(BaseModel):
    code: str
    reason: ArchiveState | RecordErrorCode | Literal["not_configured"] | None = None
    fields: list[ValidationField] | None = None
    correlation_id: str | None = None
    codes: list[str] | None = None
    run_id: str | None = None


class SafeErrorResponse(BaseModel):
    error: SafeError


PollReason = ArchiveState | Literal[
    "archive_error", "poll_error", "import_in_progress", "queue_paused"]


class PollCycle(BaseModel):
    started_at: str = Field(json_schema_extra={"format": "date-time"})
    finished_at: str = Field(json_schema_extra={"format": "date-time"})
    outcome: Literal["ok", "skipped"]
    reason: PollReason | None
    found: int = Field(ge=0)
    new: int = Field(ge=0)
    queued: int = Field(ge=0)


class PollingStatus(BaseModel):
    enabled: bool
    interval_minutes: int = Field(ge=0, le=1440)
    auto_audit_enabled: bool
    auto_audit_max_per_poll: int = Field(ge=1, le=50)
    last: PollCycle | None


class ServiceStatus(BaseModel):
    service: str
    mode: RunMode
    model_enabled: bool
    model_name: str
    model_provider: Literal["lmstudio", "openai_compatible"]
    response_format: Literal["json_schema", "json_object", "auto"]
    search_provider: Literal["disabled", "searxng"]
    search: SearchState
    archive: ArchiveState
    archive_reads_enabled: bool
    writes_enabled: Literal[False]
    prompts_editable: bool
    polling: PollingStatus


class WorkflowStep(BaseModel):
    name: StageName
    title: str
    description: str


class WorkflowModels(BaseModel):
    provider: Literal["lmstudio", "openai_compatible"]
    response_format: Literal["json_schema", "json_object", "auto"]
    default: str
    steps: dict[str, str]


class WorkflowDefinition(BaseModel):
    models: WorkflowModels | None = None
    prompts: dict[str, str] = Field(default_factory=dict)
    format_version: int
    steps: list[WorkflowStep]
    edges: list[tuple[StageName, StageName]]
    settings: dict[str, JsonValue]
    sources: dict[str, str]
    dependencies: dict[str, str]
    decision: str


class WorkflowSnapshot(BaseModel):
    id: str
    created_at: str = Field(json_schema_extra={"format": "date-time"})
    definition: WorkflowDefinition


class Reaction(BaseModel):
    id: str
    run_id: str
    workflow_id: str
    reaction: Literal["agree", "disagree", "needs_review"]
    reviewer: str
    note: str
    expected_verdict: Verdict | None
    created_at: str = Field(json_schema_extra={"format": "date-time"})


class RunSummary(BaseModel):
    id: str
    workflow_id: str
    category: Literal["benchmark", "live"]
    origin: Literal["benchmark", "api", "webhook", "article", "poll"]
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    created_at: str = Field(json_schema_extra={"format": "date-time"})
    finished_at: str | None
    label: str | None
    article_title: str
    verdict: Verdict | None
    synthetic: bool
    reactions: list[Reaction]


class RunStep(BaseModel):
    """One durable node record, including flexible exact model and evidence I/O."""
    name: StageName
    status: Literal["completed", "failed", "skipped"]
    input: dict[str, JsonValue]
    output: dict[str, JsonValue]
    model: str | None = None
    model_calls: list[dict[str, JsonValue]]
    evidence: list[dict[str, JsonValue]]
    duration_ms: int = Field(ge=0)


class RunError(BaseModel):
    code: Literal["interrupted", "audit_failed", "cancelled"]
    message: str


class StepLine(BaseModel):
    name: StageName
    title: str
    status: Literal["completed", "failed", "skipped", "pending", "running", "stopped"]
    outcome: Literal["pass", "fail", "inconclusive"] | None
    line: str | None


class RunStory(BaseModel):
    verdict: Verdict | None
    verdict_label: str
    headline: str
    sentences: list[str]
    steps: list[StepLine]
    failed_steps: list[str]
    skipped_steps: list[str]
    human_review_note: str | None


ArticleStatus = Literal["not_audited", "in_progress", "audited", "failed", "cancelled"]


class ArticleSummary(BaseModel):
    ref: str
    title: str
    source: str | None
    host: str
    url: str
    published_at: str | None
    removed_at: str | None
    # False only for an archive article whose details (text, removal date) are not loaded yet.
    removal_checked: bool
    category: Literal["live", "benchmark"]
    status: ArticleStatus
    latest_run_id: str | None
    latest_run_status: Literal["queued", "running", "succeeded", "failed", "cancelled"] | None
    latest_verdict: Verdict | None
    latest_run_at: str | None
    run_count: int


class ArticleList(BaseModel):
    items: list[ArticleSummary]
    total: int
    limit: int
    offset: int


class ArticleRun(BaseModel):
    id: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    verdict: Verdict | None
    created_at: str
    finished_at: str | None
    origin: Literal["benchmark", "api", "webhook", "article", "poll"]
    category: Literal["live", "benchmark"]
    workflow_id: str
    label: str | None
    summary: RunStory | None


class ArticleLinks(BaseModel):
    original_url: str
    archive_url: str | None


class ArticleDetail(ArticleSummary):
    content: str
    language: str | None
    archive_id: str | None
    snapshot: dict[str, JsonValue] | None
    links: ArticleLinks
    runs: list[ArticleRun]


class ArticleImported(BaseModel):
    imported: int
    updated: int
    skipped: int


class RunDetail(RunSummary):
    article_ref: str | None
    summary: RunStory | None
    input: AuditRequest
    output: AuditResponse | None
    steps: list[RunStep]
    model: dict[str, JsonValue]
    error: RunError | None
    expected_verdict: Verdict | None


class RunList(BaseModel):
    items: list[RunSummary]
    total: int = Field(ge=0)


class PromptAuthorInput(TextInput):
    model_config = ConfigDict(extra="forbid")
    author: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)

    @field_validator("author")
    @classmethod
    def nonblank_author(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("author_required")
        return value.strip()


class PromptVersionInput(PromptAuthorInput):
    text: str = Field(strict=True, min_length=1, max_length=20_000)


class PromptActivateInput(PromptAuthorInput):
    version: int = Field(strict=True, ge=1)


class PromptVersionSummary(BaseModel):
    id: str
    name: str
    version: int
    sha256: str
    origin: Literal["default", "api"]
    author: str
    note: str
    created_at: str = Field(json_schema_extra={"format": "date-time"})
    active: bool


class PromptVersion(PromptVersionSummary):
    text: str


class PromptActivation(BaseModel):
    id: str
    name: str
    version_id: str
    kind: Literal["seed", "activate", "rollback", "default_update"]
    author: str
    note: str
    created_at: str = Field(json_schema_extra={"format": "date-time"})


class PromptListItem(BaseModel):
    name: str
    step: StageName
    active: PromptVersionSummary
    versions: int


class PromptList(BaseModel):
    items: list[PromptListItem]


class PromptDetail(BaseModel):
    name: str
    step: StageName
    required_tokens: list[str]
    active: PromptVersion
    versions: list[PromptVersionSummary]
    activations: list[PromptActivation]


class PromptActivated(BaseModel):
    name: str
    active: PromptVersionSummary
    workflow_id: str


class PromptDiff(BaseModel):
    name: str
    from_version: int
    to_version: int
    diff: str


class ModelStepInput(PromptAuthorInput):
    step: str = Field(strict=True, min_length=1, max_length=80)
    model: str | None = Field(max_length=200)

    @field_validator("model")
    @classmethod
    def nonblank_model(cls, value):
        if value is not None and (not value.strip() or value != value.strip() or "\x00" in value):
            raise ValueError("invalid_model")
        return value


class ModelStepValue(BaseModel):
    override: str | None
    effective: str
    source: Literal["config", "api"]


class ModelList(BaseModel):
    available: list[str]
    error: Literal["models_unavailable"] | None
    default: str
    steps: dict[str, ModelStepValue]


class ModelAssigned(BaseModel):
    step: str
    override: str | None
    effective: str
    source: Literal["api"]
    workflow_id: str


class QueueState(BaseModel):
    paused: bool
    max_concurrent_runs: int
    running: list[str]
    queued: list[str]


class QueueCancelled(BaseModel):
    cancelled: list[str]
