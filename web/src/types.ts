export type Category = "benchmark" | "live";

export type RunStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface QueueState {
  paused: boolean;
  max_concurrent_runs: number;
  running: string[];
  queued: string[];
}

export interface QueueCancelled {
  cancelled: string[];
}

export type RunOrigin = "benchmark" | "api" | "webhook" | "article" | "poll";

export type ReactionKind = "agree" | "disagree" | "needs_review";

export interface Reaction {
  id: string;
  run_id: string;
  workflow_id: string;
  reaction: ReactionKind;
  reviewer: string;
  note: string;
  expected_verdict?: string | null;
  created_at: string;
}

export interface RunSummary {
  id: string;
  workflow_id: string;
  category: Category;
  origin: RunOrigin;
  status: RunStatus;
  created_at: string;
  finished_at: string | null;
  label: string | null;
  article_title: string;
  verdict: Verdict | null;
  synthetic: boolean;
  reactions: Reaction[];
}

export interface Article {
  url: string;
  title: string;
  content: string;
  published_at?: string;
}

export interface AuditInput {
  removed_article: Article;
  similar_articles: unknown[];
}

export interface Stage {
  stage: string;
  outcome: string;
  confidence: number | null;
  explanation: string;
  skipped: boolean;
  details: unknown;
}

export interface AuditOutput {
  verdict: string;
  findings: unknown;
  classification: unknown;
  flags: string[];
  short_circuited_at: string | null;
  stages: Stage[];
}

export interface Step {
  name: string;
  status: string;
  input: unknown;
  output: unknown;
  model: string | null;
  model_calls: Record<string, unknown>[];
  evidence?: Record<string, unknown>[];
  duration_ms: number | null;
}

export interface Run extends RunSummary {
  article_ref: string | null;
  summary: RunStory | null;
  model: Record<string, unknown>;
  input: AuditInput;
  output: AuditOutput | null;
  steps: Step[];
  error: {
    code: "interrupted" | "audit_failed" | "cancelled";
    message: string;
  } | null;
  expected_verdict: string | null;
}

export interface Workflow {
  id: string;
  created_at: string;
  definition: {
    steps: { name: string; title: string; description: string }[];
    edges: [string, string][];
    settings: Record<string, unknown>;
    sources: Record<string, unknown>;
    dependencies?: Record<string, string>;
    decision?: string;
    format_version?: number;
    models?: {
      provider: string;
      response_format: string;
      default: string;
      steps: Record<string, string>;
    } | null;
    prompts?: Record<string, string>;
  };
}

export type RunMode = "test" | "live";

export type ArchiveState =
  | "disabled" | "unchecked" | "ok" | "wallet_missing" | "wallet_unreadable"
  | "auth_unavailable" | "unreachable" | "rejected";

export type SearchState =
  | "disabled" | "unchecked" | "ok" | "unreachable" | "rejected" | "degraded";

export type PollReason = ArchiveState | "archive_error" | "poll_error"
  | "archive_page_failed" | "archive_page_malformed" | "archive_page_cap"
  | "archive_incomplete" | "import_in_progress" | "queue_paused";

export interface PollCycle {
  started_at: string;
  finished_at: string;
  outcome: "ok" | "skipped";
  reason: PollReason | null;
  found: number;
  new: number;
  queued: number;
  unreadable: number;
}

export interface PollHistoryCycle extends PollCycle {
  updated: number;
}

export interface PollingStatus {
  enabled: boolean;
  interval_minutes: number;
  auto_audit_enabled: boolean;
  auto_audit_max_per_poll: number;
  last: PollCycle | null;
  lookback_days: number;
  history: PollHistoryCycle[];
  last_success_at: string | null;
}

export interface ServiceStatus {
  service: string;
  mode: RunMode;
  model_enabled: boolean;
  model_name: string;
  model_provider: "lmstudio" | "openai_compatible";
  response_format: "json_schema" | "json_object" | "auto";
  search_provider: "disabled" | "searxng";
  search: SearchState;
  archive_reads_enabled: boolean;
  archive: ArchiveState;
  writes_enabled: false;
  prompts_editable: boolean;
  polling: PollingStatus;
}

export interface PromptVersionSummary {
  id: string;
  name: string;
  version: number;
  sha256: string;
  origin: "default" | "api";
  author: string;
  note: string;
  created_at: string;
  active: boolean;
}

export interface PromptVersion extends PromptVersionSummary {
  text: string;
}

export interface PromptActivation {
  id: string;
  name: string;
  version_id: string;
  kind: "seed" | "activate" | "rollback" | "default_update";
  author: string;
  note: string;
  created_at: string;
}

export interface PromptListItem {
  name: string;
  step: string;
  active: PromptVersionSummary;
  versions: number;
}

export interface PromptList {
  items: PromptListItem[];
}

export interface PromptDetail {
  name: string;
  step: string;
  required_tokens: string[];
  active: PromptVersion;
  versions: PromptVersionSummary[];
  activations: PromptActivation[];
}

export interface PromptActivated {
  name: string;
  active: PromptVersionSummary;
  workflow_id: string;
}

export interface PromptDiff {
  name: string;
  from_version: number;
  to_version: number;
  diff: string;
}

const verdictValues = [
  "NOT_REMOVED",
  "MOVED",
  "LIKELY_MOVED",
  "FOUND_ONLINE",
  "POSSIBLE_CENSORSHIP",
  "INCONCLUSIVE",
  "LIKELY_LOW_PUBLIC_INTEREST",
  "AUDIT_FAILED",
] as const;

export type ArticleStatus =
  | "not_audited"
  | "in_progress"
  | "audited"
  | "failed"
  | "cancelled";
export type Verdict = (typeof verdictValues)[number];
export const VERDICTS: readonly string[] = verdictValues;
export type StageName =
  | "classification"
  | "similarity"
  | "relevance"
  | "related_removals"
  | "source"
  | "web_search";

export interface StepLine {
  name: StageName;
  title: string;
  status:
    | "completed"
    | "failed"
    | "skipped"
    | "pending"
    | "running"
    | "stopped";
  outcome: "pass" | "fail" | "inconclusive" | null;
  line: string | null;
}

export interface RunStory {
  verdict: Verdict | null;
  verdict_label: string;
  headline: string;
  sentences: string[];
  steps: StepLine[];
  failed_steps: string[];
  skipped_steps: string[];
  human_review_note: string | null;
}

export interface ArticleSummary {
  ref: string;
  title: string;
  source: string | null;
  host: string;
  url: string;
  published_at: string | null;
  removed_at: string | null;
  /** False only for an archive article whose details are not loaded yet. */
  removal_checked: boolean;
  category: Category;
  status: ArticleStatus;
  latest_run_id: string | null;
  latest_run_status: RunStatus | null;
  latest_verdict: Verdict | null;
  latest_run_at: string | null;
  run_count: number;
}

export interface ArticleImported {
  imported: number;
  updated: number;
  skipped: number;
}

export interface ArticleList {
  items: ArticleSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface ArticleRun {
  id: string;
  status: RunStatus;
  verdict: Verdict | null;
  created_at: string;
  finished_at: string | null;
  origin: RunOrigin;
  category: Category;
  workflow_id: string;
  label: string | null;
  summary: RunStory | null;
}

export interface ArticleLinks {
  original_url: string;
  archive_url: string | null;
}

export interface ArticleDetail extends ArticleSummary {
  content: string;
  language: string | null;
  archive_id: string | null;
  snapshot: Record<string, unknown> | null;
  links: ArticleLinks;
  runs: ArticleRun[];
}

export type AuditFlag = "SOURCE_DEAD" | "RELATED_REMOVALS";
export type ResultLineage =
  | { kind: "initial" }
  | { kind: "re_audit"; previous_record_id: string }
  | { kind: "correction"; previous_record_id: string;
      reason: "record_error" | "redaction" };

export interface ResultClassification {
  subject_tags: { notation: string; label: string }[];
  genre: { notation: string; label: string } | null;
  geographic_scope: "local" | "regional" | "national" | "global_scope" | null;
  gist: string;
}

export interface ResultEvidence {
  relocated_to: string | null;
  old_in_new: number | null;
  new_in_old: number | null;
  covers_key_facts: boolean | null;
  original_url_status: string | null;
  live_copies: string[];
  dead_copies: { url: string; reason: string }[];
  related_same_publisher: string[];
  search_queries: number | null;
  search_queries_failed: number | null;
}

export interface AuditResult {
  schema: "karakutu-ai-audit/audit-result";
  schema_version: "2.0";
  record_id: string | null;
  lineage: ResultLineage | null;
  article: { ref: string | null; archive_id: string | null;
    url: string; content_sha256: string };
  run: { id: string; origin: RunOrigin; category: Category; status: RunStatus;
    created_at: string; finished_at: string | null; workflow_id: string };
  verdict: Verdict | null;
  flags: AuditFlag[];
  findings: string | null;
  classification: ResultClassification | null;
  stages: { stage: StageName; outcome: "pass" | "fail" | "inconclusive";
    summary: string }[];
  evidence: ResultEvidence | null;
  error: { code: "audit_failed"; message: string } | null;
  notice: string;
}
