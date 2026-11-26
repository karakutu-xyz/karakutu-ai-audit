# Data, API and local operations

## Storage

SQLite is the default: `data/audit.sqlite3` plus its WAL/SHM files, through SQLAlchemy 2.
Live mode also supports PostgreSQL with psycopg v3 through `DATABASE_URL`; JSON columns
use PostgreSQL `json` there (not `jsonb`), which keeps the saved text verbatim: exact
number formatting, key order and `\u0000` escapes. Test mode and demo seeding remain SQLite-only. The database stores:

| Table | Contents |
| --- | --- |
| `workflows` | Immutable fingerprint and source/configuration/dependency snapshot. |
| `articles` | Stable URL identity, title/source/host, publication/removal dates, content and bounded archive version metadata; category and origin (`run`, `archive`, `demo`). |
| `runs` | Nullable indexed `article_ref` links an article; immutable submitted input and workflow ID; category/origin; execution lifecycle; prediction; step/model/evidence traces; optional expected verdict. |
| `reactions` | Append-only reviewer reaction, note, optional expected verdict, run ID and the same workflow ID. |
| `prompt_versions` | Append-only prompt name/version, exact text, UTF-8 SHA-256, origin (`default` or `api`), author, note and timestamp; unique name/version. |
| `model_assignments` | Append-only step, nullable model override, author, note and timestamp. Newest insertion per step wins. |
| `prompt_activations` | Append-only name/version reference, kind (`seed`, `activate`, `rollback`, `default_update`), author, note and timestamp. The newest activation selects the active version. |

`benchmark` and `live` are separate categories in the same local database. Routes assign the category; a caller-supplied category is rejected. A benchmark may exercise real LM Studio and search without becoming live data. Manual audits use `POST /api/articles/{ref}/audit` (origin `article`), which keeps the article's category. Opt-in archive auto-audits use origin `poll` and category `live`. Historical runs with origin `api` or `webhook` remain readable and filterable. Export does not reclassify data. Karakutu intake uses background polling; manual archive import remains available.

The in-process task queue persists a queued record before starting work, then each
finished step. One process owns it; `MAX_CONCURRENT_RUNS` bounds simultaneous model
workflows. Run one API container and one worker process; do not scale replicas or
add uvicorn workers, because separate processes cannot coordinate this in-memory
queue. On shutdown/restart, incomplete runs become failed with `interrupted`,
preserving completed step evidence. Re-submit explicitly to retry. Operator stops
instead become terminal `cancelled` runs, keeping recorded steps with no verdict
or output. Queue pause is in memory, leaves running work alone, and resets to open
on every restart. Benchmark POSTs create distinct runs; saved-article audits reject
concurrent audits of the same article. An external durable worker is not implemented.

### Archive polling

In live mode with an enabled archive client and `POLL_INTERVAL_MINUTES > 0`,
the lifespan starts one background task after a one-second startup delay. It runs
one cycle at a time and waits the configured interval after each cycle. Shutdown
cancels it with the other tasks. Test mode never creates a poller task.

Each cycle uses the same Unix-second archive audit listing and metadata-only
`import_articles` as manual Import. The first window is 30 days. Later windows
start one day before this process's last successful listing/import, clamped to
2–30 days ending now. Restart resets the window to 30 days. An overlapping manual
import skips the cycle with `import_in_progress`. Polling requires every status and
page to succeed and reach the end before the page cap. Each page must be an object
with an `items` list of objects; malformed pages are incomplete. Manual Import
keeps readable entries from partial or malformed listings. Incomplete polling listings register
nothing, queue nothing and leave the successful window unchanged. They use a fixed
archive failure state or `archive_incomplete`. Listing exceptions use a fixed
archive failure state or `archive_error`; other cycle failures use `poll_error` and
the next cycle still runs. No upstream bodies or exception messages enter status
or logs. Listing counts include invalid/duplicate entries; `new` counts only
newly registered articles. Both polling and manual Import persist metadata in a worker
thread under the shared import lock; candidate queries also run off the event loop.
Imports commit at most 100 listing rows per transaction, releasing the writer lock
and connection between batches so foreground reads and writes can proceed. URL
deduplication spans the whole listing and counts retain their existing meaning.
Earlier committed batches remain saved if a later batch fails; the poll window
advances only after the whole import succeeds. Each batch owns its session.
SQLite permits cross-thread connections;
in-memory SQLite uses one exclusively checked-out pooled connection rather than
sharing an active connection between sessions.
PostgreSQL uses its connection pool and transaction writer lock. Shutdown waits for
an in-flight worker operation before closing the store or releasing the import lock.
Cancellation still exits the poller if that worker fails while shutdown is waiting.

With `AUTO_AUDIT_ENABLED=true`, the store selects live archive refs with no run of
any status or origin, ordered by numeric archive id (length, then id, then ref).
Archive ids increase as articles are detected, so this queues oldest detections first
even when a newest-first listing gives newer articles earlier local registration times.
The budget is `AUTO_AUDIT_MAX_PER_POLL` minus queued/running `poll` runs, keeping
that backlog at most N. At most N new runs are submitted in one cycle even if
older work finishes during it. Each cycle also allows at most N archive text reads
and 2×N candidate attempts outside cooldown, including failed or empty reads.
Candidates in cooldown consume neither a read nor an attempt, allowing later
candidates to progress. When a sweep stops on the read or attempt limit, the next
cycle resumes after the last candidate it tried and wraps around, so unreadable
older articles cannot use up every cycle's reads. The first failed read leaving the archive client unavailable
or rejected stops the sweep with its fixed failure reason; metadata import counts
and the successful import window remain saved. Queue pause allows metadata registration but skips
automatic submissions (`queue_paused`). Each candidate refreshes the latest archive
version through `fill_archive_text`, including when text is already cached, updating
changed title, text and version references without changing historical run inputs. It
then re-checks the article and absence of any run under the shared article lock
before submission with origin `poll`. A failed refresh or missing text creates no run
and increments `unreadable`, even when stale text is cached. Failed reads share a
ten-minute cooldown per ref/URL, including empty text, then become retryable. Failed,
stopped and interrupted runs are never retried automatically. Manual audits may
still re-run articles. Automatic work uses the current workflow and normal engine,
including real model/search calls when configured; it never writes to the archive.

Registration adds no text storage. Opening or auditing loads only the latest
version's text; successful lazy reads still happen once per ref/URL, while automatic
submissions always require a fresh read. Default archive evidence remains
summaries and hashes, with full bodies only for `RECORD_FULL_EVIDENCE=true`.

`GET /api/status` includes `polling`: `enabled`, `interval_minutes` (0 when off),
`auto_audit_enabled` (false when polling is off), `auto_audit_max_per_poll`, and
`last` (null until a cycle finishes). `last` holds RFC 3339 `started_at` and
`finished_at`, `outcome` (`ok` or `skipped`), `reason` (a fixed label or null),
`found`, `new`, `queued` and `unreadable`. Counts remain visible on a partial failed cycle.
This state stays in memory and resets on restart. Settings displays it; Articles
continues to offer manual selection and Run audit.

### Schema changes

`db.py` maintains ordered `MIGRATIONS`: SQLite stores the version in `PRAGMA user_version`;
PostgreSQL uses a singleton `schema_version` table. Version 1 marks the original schema; version 2 adds prompt tables; version 3 adds `model_assignments`; version 4 adds `articles`, `runs.article_ref` and the article/activity indexes, then backfills existing runs. Migration 3 was already allocated, so articles use 4 without changing applied migrations. Version 5 adds nullable, unused `seq` columns to existing SQLite append-only activation
and assignment tables and widens unbounded archive metadata on PostgreSQL. Fresh
PostgreSQL tables generate `seq` with `Identity()`. A fresh database creates all tables at the latest version. Existing databases (including unversioned databases at version 0) apply each pending migration and version bump in one transaction, then create any new tables. Failures roll back that migration, including transactional DDL on either backend, and leave the last successful version. Databases newer than the code are refused.

Every migration runs under the global transaction writer lock: `begin_write` uses
SQLite `BEGIN IMMEDIATE` or PostgreSQL `pg_advisory_xact_lock` with one fixed application
key. The same lock protects article check-then-insert, import, prompt version numbering,
activation and model assignment/snapshot transactions. It serializes these writers until
commit or rollback. It does not coordinate the in-process execution queue across replicas.
`insertion_order` preserves SQLite rowid ordering and uses PostgreSQL `seq` for activation
and assignment selection, so timestamps and random IDs never select the newest row.

For every schema change, add a numbered migration; never edit an applied one. Test upgrades with retained rows, repeat startup, and failure rollback. JSON fields allow evidence formats to evolve without routine column migrations. Back up data before upgrading. Use a consistent SQLite backup (the SQLite backup API, or stop the service first); copying only a live `.sqlite3` file can omit WAL data.

### Article identity and history

`articles.ref` is `ka-<archive id>` for a newly imported archive article, otherwise
`u-<first 16 SHA-256 hex characters of normalized URL>`. Normalization strips outer
whitespace, lowercases scheme/host and drops the fragment; path and query retain their
case and spelling. The normalized URL has a unique index, and non-null `archive_id`
values have a partial unique index. An existing `ka-<id>` row is canonical when that
archive ID is imported again, including when its URL changes. `ref`, `source`, `host`,
`published_at`, `removed_at`, `archive_id` and `language` are Text because archive input
has no length bounds; `runs.article_ref` is also Text. Version 5 widens these on PostgreSQL;
SQLite already accepts arbitrary lengths and needs no physical width change.
`created_at`/`updated_at` are String(40), and `category`/`origin` String(20).
URL, title and content are Text; nullable `snapshot` is JSON (`json` on PostgreSQL)
containing version titles, check dates and hashes, never duplicated full text.
Source, publication/removal dates, archive ID and language are nullable.

Migration 4 processes runs in batches of 500 in creation order (SQLite insertion
order breaks ties), creates one article per normalized URL, and links runs with batch
updates. It extracts URL/title/publication metadata with SQLAlchemy JSON indexing and reads content only
for the first run of each new article. The first run supplies title, content and category.
The migration also backfills `search_text`, a text column containing NFKC-normalized,
Unicode-casefolded title, source, host and URL, separated by spaces. Article inserts and
updates maintain this value. Historical run inputs and workflow snapshots do
not change. Every submission route creates or selects its article in the same database
transaction as the run insertion. Subsequent runs retain the original article text;
each run still preserves its own submitted input. `runs.article_ref` is a nullable,
indexed plain column; the composite `(article_ref, created_at)` index supports history.

## Demo data (test mode)

Seed a test database with `uv run python scripts/seed_demo.py --db data/audit-test.sqlite3`,
or pass `--database-url sqlite:///data/audit-test.sqlite3`. The Python entry point is
`audit.demo.seed(database_url)`; test mode calls it on every start. It performs no
network/model calls and prints counts only. Exit 0 means seeded (or already seeded).
Demo data stays SQLite and test-mode only: live startup refuses any database, SQLite or
PostgreSQL, that holds a demo workflow, and the SQLite-to-PostgreSQL copy refuses a test
or demo source.

Only a test database is accepted: a file named `*-test.sqlite3`, a path under `data/demo/`,
or a database that already holds the demo workflows (`wf_demo_*`). The resolved path counts,
so a test-named symlink to `data/audit.sqlite3` is refused. The marker is read without writing and is trusted only while no non-empty `-wal` or `-journal` sits beside the file. Anything else, and SQLite URI
options, are refused before a byte is written (non-zero exit). Seeding restores the rows with
fixed demo IDs (`wf_demo_*`, `run_demo_*`, `rx_demo_*`, `pv_demo_*`, `pa_demo_*`;
articles use `origin=demo`) and timestamps, so reseeding is
idempotent; other rows in the test database (the API's own workflow, runs submitted in test
mode, reactions added to demo runs) are never touched.

**Startup order:** the API lifespan calls `store.interrupt_incomplete()`, marking queued and
running records failed with `interrupted`. Test mode seeds **after** that line, so every start restores the demo
queued and running rows.

Prompt defaults are synced before demo seeding, including when using the standalone
script. Three append-only demo versions extend the source texts with short, clearly
fictional paragraphs: relevance v2 and v3, and web search v2. Their IDs start with
`pv_demo_`; origin is `api`, with invented editor names and fictional-change notes.
Version numbers follow any versions already present, so these v2/v3 labels describe
the story rather than guaranteed database numbers. Five `pa_demo_` activations, in
insertion order, select relevance v2, web search v2, relevance v3, and then restore
the selection that was active after the standard default sync. Dates span
October/November 2026. Seeding first runs that default sync, the same one every API
startup runs (an updated source default replaces an active default exactly as on a
normal restart); the demo history then never changes which prompt version is active.
On a fresh test database the active set therefore equals the source defaults and the
current workflow fingerprint is unchanged by the demo history; on an existing test
database an operator's selection stays active. The demo activations are appended
all-or-nothing, only when none exists yet, so reseeding (even over a partly removed
demo history) adds no activation.

The three demo snapshots use the current format 3 definition, with exact prompt
texts: `wf_demo_v1` has all source defaults, `wf_demo_v2` uses relevance v2, and
`wf_demo_v3` uses relevance v3 plus web search v2. Their IDs are fixed fictional
labels, not content fingerprints. Each snapshot records `demo-model-small` for
classification, relevance and related removals, and `demo-model-large` for source
and web search. Step model names and simulated call records agree with that mapping;
similarity and any step without a model call have `model: null`. No current model
assignments are seeded or changed. Upgrading an older seeded database refreshes
only demo-owned workflow snapshots and runs in place and appends the missing demo
prompt history; non-demo snapshots, versions, activations and run inputs are retained.

The Python dataset contains 28 clearly marked fictional runs, three workflow snapshots,
eight invented reviewer reactions and 19 articles. Runs of the same case across workflows
share an article. Three articles have fictional November 2026 publication/removal dates,
outlet names and content but no runs, allowing first-audit testing. All demo articles
include source and removal metadata; counting identifies them by `origin=demo`.
`GET /api/articles?status=not_audited` lists the three never-audited articles on a fresh
test-mode startup. `POST /api/articles/{ref}/audit` starts a first audit or re-run
through the offline `DemoEngine`, including demo articles in the `live` category.
In both run modes, article audits follow the normal token rules: they need no token
unless one is configured; with a configured token, missing/wrong bearer credentials return 401. Existing
queued/running demo examples still return 409 `run_in_progress` until completed,
interrupted or cancelled. Every new run keeps its own history and captured workflow/prompts/models.
Articles without a scripted case select an existing demo scenario deterministically
from their normalized URL-derived ref; all output is explicitly simulated. No model,
search, archive or Karakutu call occurs. Archive import returns 409
`archive_unavailable` in test mode, and lazy archive enrichment never runs.
On upgrade, reseeding repairs origin/source/removal/publication metadata only for
rows whose ref matches the demo URL ref and whose linked runs all have the literal
`run_demo_` prefix, or rows with a demo URL and no runs. Rows with non-demo runs keep
their metadata. Six labelled benchmark cases repeat across versions with
3/6, 4/6 and 5/6 matching expectations; the latest ribbon-column miss intentionally remains
for review. Coverage includes all eight verdicts, both red flags, measured and partial moves,
independent copies, a live original, low interest, a related-removal override, invalid model
output/retries, model-disabled failure, interrupted and person-stopped work, running/queued records, and API and
webhook live-category examples. Stored snapshots contain small fabricated source excerpts
for version comparison. All article text and evidence are invented; reserved hosts cannot
represent real sources. `synthetic` retains its production meaning of model disabled.

## API

The generated [OpenAPI contract](../openapi.yaml) (YAML; the running API still serves it as `/openapi.json`) documents validation schemas and endpoints; regenerate with `uv run python scripts/export_openapi.py`. The TypeScript client is kept aligned through the same documented route shapes and integrated browser checks.

| Method / path | Purpose |
| --- | --- |
| `GET /health` | Minimal local liveness and the run `mode` (`test` or `live`). |
| `GET /api/status` | Run `mode` and model/search/read/live posture; `search_provider` identifies configuration; `search` is the safe current search state; `archive` is the safe current archive state and `archive_reads_enabled` remains the configuration boolean; `writes_enabled` is always false for archive writes; `prompts_editable` reports whether a token is configured; `model_provider` and `response_format` identify the inference profile. |
| `GET /api/models` | Provider chat-model names (`available`), safe `error` code or null, resolved `default`, and per-step `override` (nullable), `effective` name and `source` (`config` or `api`). |
| `POST /api/models/steps` | Assign `{step, model: <name or null>, author, note?}` for new runs; return step/override/effective/source and `workflow_id`. |
| `GET /api/prompts` | List the six prompt names, stages, active summaries and version counts. |
| `GET /api/prompts/{name}` | Active text, required tokens, versions and activation history (newest first). |
| `GET /api/prompts/{name}/versions/{version}` | Read one immutable version with text. |
| `POST /api/prompts/{name}/versions` | Create a version with `{text, author, note?}`; 201, without activation. |
| `POST /api/prompts/{name}/activate` | Activate `{version, author, note?}`; return active summary and workflow ID. Already active is a no-op. |
| `POST /api/prompts/{name}/rollback` | With `{author, note?}`, restore the previously active distinct version and return its workflow ID. |
| `GET /api/prompts/{name}/diff?from=<v>&to=<v>` | Unified diff preserving line endings. |
| `GET /api/workflow` | Current immutable workflow snapshot. |
| `GET /api/workflows/{id}` | Snapshot used by a historical run. |
| `POST /api/benchmarks/runs` | Submit a benchmark, return 202 and its lean queued `AuditResult`. |
| `GET /api/articles` | Paginated articles with latest audit status/verdict, run count and activity. |
| `GET /api/articles/{ref}` | Stored content, archive metadata/links and newest-first run history with computed summaries; unknown refs return 404. |
| `POST /api/articles/{ref}/audit` | First audit or re-run; 202 `AuditResult`, origin `article`; preserves every previous run. |
| `POST /api/articles/import?days=30` | Read archive audit listings and upsert local article metadata; 200 `{imported, updated, skipped}`. |
| `GET /api/runs` | Filter/paginate by category, origin, status, verdict and workflow ID. |
| `GET /api/runs/{id}` | Read the lean `AuditResult`; full review data requires `/details`. |
| `GET /api/runs/{id}/details` | Read full `RunDetail`, including input, output, steps, evidence and model calls. |
| `POST /api/runs/{id}/cancel` | Stop a queued/running audit; return 200 `AuditResult`. |
| `GET /api/queue` | Queue pause flag, concurrency limit and oldest-first running/queued run IDs. |
| `POST /api/queue/pause` | Hold queued and new submissions; running audits continue. Return `QueueState`. |
| `POST /api/queue/resume` | Release queued work; return `QueueState`. |
| `POST /api/queue/cancel?include_running=false` | Stop queued audits; optionally include running audits. Return `{cancelled: [run IDs]}`. |
| `GET /api/runs/{id}/export` | Download the full local review record, including exact transcripts. |
| `POST /api/runs/{id}/reactions` | Append `agree`, `disagree` or `needs_review`, reviewer and note. |

Run submission accepts `removed_article: {url, title, content, published_at?}`, `similar_articles` (up to five), optional `label`, and optional `expected_verdict`. Poll `GET /api/runs/{id}` using the returned `run.id` until `run.status` is `succeeded`, `failed` or `cancelled`. `AUDIT_FAILED` is a failed execution, not a substantive finding.

### Run and queue controls

Runs move from `queued` to `running`, then `succeeded` or `failed`. An operator
can stop either active state with `POST /api/runs/{id}/cancel`. Unknown IDs return
404; an already terminal run returns 409 `{"error":{"code":"run_not_active"}}`.
If completion wins the race, the real result remains saved and cancellation returns
that 409. Successful stops retain all steps already recorded, set `finished_at`,
clear verdict/output and return error code `cancelled` with the message:
"A person stopped this audit before it finished. Submit a new run to retry."
Cancelled is a lifecycle status, never a verdict or a finding. Terminal records
cannot be overwritten by late starts, completion or failure writes.

`QueueState` contains `paused`, `max_concurrent_runs`, `running` and `queued`.
The ID lists come from saved statuses, oldest first, including active demo rows.
Pause prevents queued runs from taking a slot, accepts new submissions as queued
and lets running runs finish. Resume reopens the gate. Pause is process-local and
resets on restart; existing incomplete runs still become `failed` / `interrupted`.
Bulk cancellation considers the active IDs at request time and reports only runs
successfully stopped; completed results are preserved. Queued demo seed rows can
also be stopped even though they have no in-process task (demo reseeding restores
them on restart). These routes use the existing bearer and browser-origin guards.

The Runs page shows queue counts, Pause/Resume and a confirmed Stop queued action.
Run detail and active ledger rows share the reusable confirmed Stop audit button. Cancelled
runs show a neutral status and no verdict chip; unrecorded steps remain pending.
The shell indicator links to the Runs controls and shows counts, Queue paused or
Idle on every page. Queue reads poll every 1.5 seconds while queued/running or
paused, and every 30 seconds while idle, when the tab is visible. Successful
controls refresh queue reads immediately. Token/service epoch changes clear old
queue data and restart reads; Refresh also reloads the queue.

### Article reads and audit submissions

`GET /api/articles` accepts `status`, `verdict`, `category` (`live` or `benchmark`,
omit for both), `q` (at most 200 characters), `limit` (1–200, default 50) and `offset`
(nonnegative). Search normalizes `q` with NFKC and Unicode casefolding, then matches
a literal substring of the stored `search_text` over title, source, host and URL.
Non-ASCII case variants match consistently; percent, underscore and backslash are
escaped as literal characters. Results sort by newest
run creation time, otherwise article creation time; refs break ties. `total` counts
all filtered matches before pagination. A SQL window query selects the latest run
and counts runs without reading their input/output/step JSON. Run ID breaks ties
between equal creation timestamps.

Statuses refer to the latest run: `not_audited` means no runs, `in_progress` means
queued/running, `audited` means succeeded, `failed` means failed, and `cancelled`
means stopped by an operator. Verdict filters also match only that latest run. `AUDIT_FAILED` is a failed execution, never a finding.
`ArticleSummary` includes ref/title/source/host/URL, publication/removal dates,
category/status, latest run ID/status/verdict/time and run count. `ArticleDetail` adds
content/language/archive ID/snapshot, `links.original_url`, nullable `links.archive_url`
(always null until a public archive page base is configured), and runs newest first.
Archive connection URLs are never exposed as reader links. Each history entry
contains ID/status/verdict/timestamps/origin/category/workflow ID/label and summary.

The article audit route uses the saved URL/title/content/publication date, no similar
articles, the article's category, and origin `article`. Benchmark re-runs inherit label
and expected verdict from the latest benchmark run. Submission uses the same current
workflow/prompt/model capture and task bookkeeping as other run routes. An app lock
serializes article submissions; the database writer lock checks for **any** queued/running
run and inserts atomically. A conflict returns 409
`{"error":{"code":"run_in_progress","run_id":"<existing id>"}}`.
Missing content returns 422 `{"error":{"code":"article_has_no_content"}}`;
unknown articles return 404. All article routes pass through `RequestGuards`:
configured tokens are required (missing/wrong gives 401), writes reject untrusted
browser origins (403). Article audits in either category follow these normal token
rules in both run modes. Local article audits and benchmark submissions need no token
when none is configured; non-loopback binds or allowed hosts require a token at startup.
Test-mode article audits use the offline engine, as described above.
Lazy archive fill stores text only if the article still has the URL and archive id it
was fetched for; a response that arrives after a concurrent import moved the article is
dropped, and the next detail read fetches again.

Both run detail/export and article run history compute `summary` on read, never
persist it. `RunStory` contains verdict, fixed verdict label/headline, up to three
short factual flag sentences, six step lines in engine order, failed/skipped titles
and a human-review note. Long findings are available separately and are never pasted
into the summary. Step lines retain stored explanations (at most 500 characters,
including an ellipsis when truncated), stored skip reasons, outcomes and execution
status; a running audit marks the next unrecorded step running and subsequent steps
pending. Queued runs have pending steps. Failed executions without a verdict receive
the audit-failed story. Every substantive verdict says: "This is not a finding of
censorship; a person decides." Failed/in-progress stories have no review note.
Cancelled runs have verdict label "Stopped by a person" and headline "A person
stopped this audit before it finished. This is not a result." Their verdict and
human-review note are null, with no flag sentences. Reached steps retain their
recorded status, except a failed step with `output.details.error_type` equal to
`CancelledError`: its story status is `stopped`, with neutral UI label "Stopped",
and it is omitted from `failed_steps`. Exact stored step records remain unchanged.
Unreached steps are `pending`. Test-mode cancellation interrupts the demo step
delay and keeps already-emitted steps; the seed includes one cancelled example.
The run-list metadata schema is named `RunSummary`; its existing fields are unchanged.

### Private step records

Run details and export retain permissive step input/evidence JSON, accepting both
historical cumulative records and new lean inputs by name. Article-reading inputs
refer to `run.input.removed_article`, with URL, title, publication date, character
count and content SHA-256. Classification stores a catalog count/byte/hash summary;
other steps record only their own dependencies, never full earlier stage objects.
Skipped inputs carry the reason. See [the step contract](workflow.md#exact-review-record).

By default archive outputs are byte/hash summaries plus item counts or article
identity and text length/hash. Fetch events retain requested URLs and page metadata,
text lengths/hashes and dead/unreadable reasons. Search events remain unchanged.
`RECORD_FULL_EVIDENCE=true` restores full private archive/fetch evidence bodies;
inputs stay lean. Exact model requests/messages, parsed output and usage are always
retained. The review panel shows Step input beside exact system/user messages.
Article text stays in the run request and exact model prompts, without extra copies
in default step inputs, evidence or details. No database migration or response
schema change is needed; historical runs keep their saved shape. The portable audit
record and computed run story continue reading output fields, not step input state.

### Search status and evidence

Status `search` is one of `disabled`, `unchecked`, `ok`, `degraded`, `unreachable`, or
`rejected`, with no URL or upstream error text. Test mode always reports
`disabled` offline. Live status probes `/healthz` with a two-second bound and a
30-second cache. Any HTTP answer proves reachability but cannot clear degradation
or rejection learned from a real search query. Settings explains unreachable search in terms
of the configured SEARXNG_URL and rejected search in terms of JSON output.
`degraded` means a query returned no usable hits and reported unresponsive engines; only engine names are retained, never upstream reasons.

New web-search evidence and step `details` include `search_queries` (web queries
attempted), `search_queries_failed`, `web_hits`, `archive_candidates`, and
`cited_source_leads`. The three candidate counts partition the merged,
de-duplicated, capped list; an archive candidate independently returned by web
search retains its existing upgrade to web evidence. `hits_searched` remains
the merged total for saved-run and audit-record compatibility. Historical records
stay unchanged; absent new counts are not inferred as web hits in the UI.
Demo counts describe simulated queries, never real network calls.

Step and verdict explanations prefix total failure with "Web search failed: all
3 search queries failed, so the web was not searched. Only archive candidates (2)
and cited-source leads (0) were available." Partial failure says "1 of 3 search
queries failed." Page counts distinguish archive candidates, cited-source leads
and search hits. When the web-search model then fails as well, the step stays failed
and the audit `AUDIT_FAILED`, but the step keeps these counts and both explanations
still carry the search note. A search answer without a `results` list counts as a
failed query. These descriptions do not change verdicts, thresholds or prompts.

### Read-only archive import and detail enrichment

`POST /api/articles/import` accepts a `days` query parameter (default 30, range 1–90).
It calls the archive's paginated `audits_created_between` listing for that date window
and imports URL/title/publisher display name/publication date and archive ID as live
metadata, with origin `archive`. It retains its existing partial-list behavior when
some archive pages succeed. It does not fetch full article text or start audits.
Changed URLs/hosts are refreshed for an existing `ka-<id>` row. Import resolves both
the archive identity and normalized URL in one transaction. An existing `ka-<id>`
row takes priority: if a URL-derived row already holds its new URL, that row's runs
move to the canonical archive ref and the URL-derived row is deleted. Run inputs
and workflow snapshots remain unchanged. A URL change clears stored content,
snapshot, language and removal date for fresh lazy enrichment. Conflicts with a
different archive ID are skipped; archive IDs cannot be shared by two rows. If an
archive ID was first attached to a run/demo URL-derived row, importing that ID at a
different URL is skipped to preserve the run-owned article fields.
Duplicate, invalid or unchanged items count as skipped; changed metadata counts as
updated. Archive disabled/unavailable returns 409
`{"error":{"code":"archive_unavailable","reason":"not_configured"}}` when disabled.
Otherwise `reason` is the latest `archive` state from `/api/status`:
`wallet_missing`, `wallet_unreadable`, `auth_unavailable`, `unreachable` or `rejected`.
The success and error code contracts are unchanged. Status also permits `unchecked`
before a call and `ok` after successful JSON. These fixed labels contain no source
metadata or upstream text; see [state semantics](configuration.md#archive-read-state).
Startup catalog reads and silent lazy fill update the same state without blocking
startup or showing enrichment errors to readers. When no archive identity exists yet,
import retains an existing normalized URL's ref, category, title and content when
that article originated from a run/demo, adding archive/source metadata without
breaking run links.

Opening an empty `ka-*` article requests the archive article with checks and versions
expanded. A successful lazy read happens once per ref/URL pair per service lifetime;
a failed read becomes retryable after a ten-minute cooldown. A changed URL permits a fresh
attempt during the same service lifetime. It stores the latest version's `news_text` (or
top-level text when no versions exist), language, version title/check date/hash
metadata and the first dead-check date after the last 200, using the shared archive
check logic. The archive's timestamp representation is retained as a string for
`removed_at`. Failed reads return the saved article with empty content and no upstream
error text. Concurrent detail reads share a lock and the attempt cache. Manual Run
audit keeps its missing-text behavior with the same retry cooldown; only automatic
audits explicitly refresh already-cached text. A service restart clears the cache.
All archive calls are GET-only;
local import/enrichment never mutates the archive.

Audit runs are asynchronous and persisted. Start live runs from saved articles or
submit fabricated cases through the benchmark route. Push intake and article publishing
jobs are removed; no route writes back to Karakutu. Background polling registers
metadata and optionally queues first audits (see [Archive polling](#archive-polling)).

All audit-submission POSTs, benchmark submission and individual cancellation return the
lean `AuditResult` defined in [the v2 result contract](audit-record-schema.md).
`GET /api/runs/{id}` returns the same shape: article identity and content hash, run
metadata with a workflow pointer, nullable verdict, bounded findings, classification,
stage summaries and typed evidence. It has no article text, input, steps, prompts or
model calls. Submission returns 202 with queued metadata under `run`, `record_id: null`,
`verdict: null` and `stages: []`. Clients navigate using `run.id`.

Every result route can instead return **409** with
`{"error": {"code": "result_withheld", "reason": "<fixed refusal code>", "run_id": "<id>"}}`.
The shared builder scrubs and scans full projected text before truncation, using the
configured API token, model key, database password and wallet material (the wallet is
resolved like archive sign-in on every check, so a keyfile added after start is covered). Credentials or secrets that survive
scrubbing cause `credential_detected` or `secret_detected`; oversized stored URLs,
identities or serialized results cause `too_large`. An invalid projection causes
`invalid_result`. These responses contain no stored prose. Withholding affects only
the response: a submission still creates and queues its run, and cancellation still
cancels it, so a caller must not resubmit: follow `run_id` instead (the review UI opens the
run or refreshes and shows no error). Full data remains at `/api/runs/{id}/details`. Submitted article URLs over
8,192 UTF-8 bytes fail validation with the existing 422 error shape before creating a run.

A finished live run with a stored result can serialize to the v2 record for karakutu.
Benchmark, queued, running, cancelled and interrupted runs produce no record; their API
result has `record_id: null`. Finished API records use initial lineage or point to the
latest earlier finished live result for the same article. No route posts records yet.
Full `RunDetail` review data is available only through `GET /api/runs/{id}/details` and
the explicit `GET /api/runs/{id}/export` download. The review UI polls `/details`.
Authentication, rate limits and unknown-ID 404 behavior are unchanged.

The editable names are `relevance`, `classification_coarse`, `classification_fine`, `source`, `web_search`, and `related_removals`. Prompt text must be a valid UTF-8 string, nonblank, at most 20,000 characters, without NUL, and retain the registry's required output tokens and `GUARDRAIL` section marker (or `NONE` for related removals). Author is 1–120 characters; note is at most 2,000. Validation returns 422 with safe codes, including `missing_token:<token>`; it never echoes prompt text. Duplicate text for the same name returns 409 `duplicate_version`; activate the existing version instead. Rollback without a previous distinct version returns 409 `no_previous_version`. Unknown names/versions return 404. Prompt summaries omit text; individual versions and the current/historical workflow snapshots contain exact text.

### Model assignments

Supported steps are `classification`, `relevance`, `related_removals`, `source`, and `web_search`. Similarity makes no model call. Unknown steps (including similarity) return 422 `unknown_step`; names missing from an available provider catalog return 422 `unknown_model`. Provider discovery excludes IDs containing `embed` or `rerank`; a failed discovery returns an empty list and `models_unavailable`, never exception text. When discovery is unavailable, names are accepted. Synthetic/model-disabled mode makes no discovery call, returns an empty list without an error and accepts configured names. Clearing an override uses JSON null; blank names are invalid. Author is required and nonblank (1–120 characters); note is optional (up to 2,000); model names are at most 200 characters.

At startup the newest persisted assignment for a step takes precedence over `MODEL_NAME_<STEP>`; without an assignment, the environment applies. An API null clears the override and selects the resolved default, overriding any environment step value across restarts. `source` remains `api` after a clear. Assignments are append-only, ordered by insertion (SQLite rowid or PostgreSQL seq), and atomically committed with the new workflow snapshot under the prompt activation lock. Each submission captures its workflow before queueing; already queued/running runs retain their earlier mapping. Prompt activation preserves current model assignments, and model assignment preserves current prompts. Model keys never enter public configurations or fingerprints.

Definitions now use `format_version` 3; unchanged setups receive a new workflow ID on restart. Effective model mapping, provider and response mode affect the ID; assignment metadata and credentials do not. Historical workflows, runs and inputs stay immutable.

## Privacy and access

Every `/api` call requires the bearer token when `API_AUTH_TOKEN` is configured.
Auditing a saved article follows these normal token rules. Startup refuses a
configured token shorter than 32 characters. On the default loopback workstation, article
audits and the benchmark/review UI are available without a token. Non-loopback
`KARAKUTU_BIND_ADDRESS` or `ALLOWED_HOSTS` entries require a token at startup;
Tilt binds only to loopback.

`ALLOWED_HOSTS` and `ALLOWED_ORIGINS` add comma-separated entries to the loopback defaults. Whitespace around entries is trimmed and empty items are dropped. Hosts always include `localhost`, `127.0.0.1` and the existing `testserver` test host. Origins always include `http://localhost:2200`, `http://127.0.0.1:2200`, `http://localhost:2201` and `http://127.0.0.1:2201`. Empty environment values preserve those defaults exactly.

Hosts are normalized to lowercase and must be plain hostnames or IP literals (unbracketed for IPv6; request hosts are compared after the same IP canonicalization); wildcards, schemes, ports, paths, userinfo, whitespace, empty labels and non-canonical numeric IPv4 spellings (leading zeros, hexadecimal, abbreviated, or a numeric last label) are rejected. Origins normalize scheme/host case, write ports as numbers and drop the scheme default (`:80` for HTTP, `:443` for HTTPS) as browsers do, and must be exactly `http://host[:port]` or `https://host[:port]` (bracket IPv6 hosts; they are stored in the browser's hexadecimal form, so `[::ffff:192.0.2.1]` becomes `[::ffff:c000:201]`). Wildcards, `null`, userinfo, paths including a trailing slash, query strings and fragments are rejected. Invalid entries stop startup without echoing their values. Any non-loopback host requires `API_AUTH_TOKEN`; startup fails with `Non-loopback ALLOWED_HOSTS require API_AUTH_TOKEN` if it is absent. Token length validation still requires at least 32 characters. Origins permit mutation requests; they do not enable cross-origin browser reads or change authentication.

Docker reads `.env` at runtime, stores default SQLite under `/data` in a named volume
(or connects to PostgreSQL through `DATABASE_URL`), and overrides local search/model paths for the container network. `KARAKUTU_BIND_ADDRESS` controls published API/UI interfaces (default `127.0.0.1`), independently of the allowed Host list. `CONTAINER_MODEL_BASE_URL` selects the container's model endpoint; `MODEL_BASE_URL` remains the local/Tilt setting. Preserve Host through reverse proxies so the backend can validate the browser's actual host. Remote use should terminate TLS before the API/UI. SearXNG remains loopback-only on port 2202.

Prompt POST routes always require a configured `API_AUTH_TOKEN` and matching bearer token, including on a loopback service with other reads open. With no configured token they return 403 `prompt_writes_require_token` before parsing the body; a missing/wrong bearer token when configured returns 401. Model assignment POSTs use the same guard; without a configured token they return 403 `model_writes_require_token`, and missing/wrong bearer tokens return 401. The same browser-origin check applies. Prompt edits change verdict semantics: benchmark before relying on a new version. `writes_enabled: false` continues to describe archive writes only.

The UI holds an optional token in memory, never local storage. Mutations reject unexpected browser origins; Host validation rejects rebinding hosts. API responses use `Cache-Control: no-store`. Validation and unexpected errors omit submitted values and raw exception text. Requests have a 2 MiB cap and bounded fields. Page fetching checks URL, redirects, DNS and connect addresses; only public HTTP(S) destinations pass. Trusted configured model/search/archive endpoints are operator configuration, never caller-selected fetch targets.

Optional archive reads use `ARCHIVE_BASE_URL` and, if required, `ARCHIVE_WALLET_PATH` or `ARCHIVE_WALLET_JSON`. Keep keyfiles outside the repository. Authentication only obtains a cached read token; the client offers no tagging, commenting or decision mutation methods. Archive connectivity is not required to start a local benchmark, but catalog-based classification and archive removal signals require access.

## Git hygiene

Use ignored paths:

```text
data/benchmark/  private benchmark inputs and labels
data/live/       optional ingestion inputs/exports
data/reports/    batch summaries and detailed exports
data/audit.sqlite3  all stored runs, reactions and workflow snapshots
```

The ignore rules also cover `reports/`, `exports/`, `fixtures/`, `datasets/`, `private/`, `runs/`, `.env*`, wallet files, database sidecars, JSON/JSONL/CSV/TSV/Parquet files. Only explicit configuration manifests and the audit-record schema are allowed JSON files. Documentation stays tracked under `docs/`; generated reports do not belong there. Tests generate fabricated inputs inline. `scripts/check_data_hygiene.py` catches tracked/staged private paths, including forced Git additions. It also scans the staged content of every file for home-directory paths, email addresses, private keys, keyfile members and common API tokens, URLs on real hosts, and bare real hostnames in prose, comments and string literals. Reserved names pass: `.example`/`.invalid`/`.test`/`.localhost`, `example.<tld>`, internal suffixes such as `.local`/`.internal`, single-label and numeric hosts, and IP literals. Outlet domains are reference data and may appear only in the lookup tables named in the script (`workflow/source.py`, `search/urls.py`, `workflow/web_search.py`); `LICENSE` and lockfiles are exempt from the host rules only. The required pinned uv build-image reference is allowed only as the exact backend Dockerfile `FROM` directive; comments, URLs and other image references remain scanned. Likewise, the project's public repository URL is allowed only as the exact `git clone` line of the README quick start. The patterns are heuristics: they cannot detect real article prose or real names, so never paste either into source, prompts, tests or docs.

### String length contract

The storage width audit retains bounded columns where all writers fit: generated
workflow IDs use at most 67 of 80 characters; generated run/reaction/prompt/activation/
assignment IDs fit 40; registered prompt/step names fit 80; SHA-256 uses exactly 64.
Enums (category, origin, status, reaction and activation kind) fit 20, and verdicts fit 50.
API labels and model names are bounded to 200; reviewers and authors to 120. Internal
UTC timestamps fit 40. Prompt texts, notes, article URLs/titles/content and search text
are Text. Unbounded archive metadata uses Text as listed above. No allowed API input
is truncated. Submitted text fields reject NUL characters before database access with
the existing 422 codes-only validation envelope; submitted values are never echoed.
Archive article data is upstream text written into text columns, which PostgreSQL cannot
hold with NUL characters, so NUL is removed from it (and from its snapshot) before the
write, on both backends. Run and workflow records, including captured evidence and model
output, are JSON and stored exactly, NUL included.
