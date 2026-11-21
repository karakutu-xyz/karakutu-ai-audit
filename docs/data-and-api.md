# Data, API and local operations

## Storage

SQLite is the default: `data/audit.sqlite3` plus its WAL/SHM files, through SQLAlchemy 2. The database stores:

| Table | Contents |
| --- | --- |
| `workflows` | Immutable fingerprint and source/configuration/dependency snapshot. |
| `articles` | Stable URL identity, title/source/host, publication/removal dates, content and bounded archive version metadata; category and origin (`run`, `archive`, `demo`). |
| `runs` | Nullable indexed `article_ref` links an article; immutable submitted input and workflow ID; category/origin; execution lifecycle; prediction; step/model/evidence traces; optional expected verdict. |
| `reactions` | Append-only reviewer reaction, note, optional expected verdict, run ID and the same workflow ID. |
| `prompt_versions` | Append-only prompt name/version, exact text, UTF-8 SHA-256, origin (`default` or `api`), author, note and timestamp; unique name/version. |
| `model_assignments` | Append-only step, nullable model override, author, note and timestamp. Newest insertion per step wins. |
| `prompt_activations` | Append-only name/version reference, kind (`seed`, `activate`, `rollback`, `default_update`), author, note and timestamp. The newest activation selects the active version. |

`benchmark` and `live` are separate categories in the same local database. Routes assign the category; a caller-supplied category is rejected. A benchmark may exercise real LM Studio and search without becoming live data. Live runs come only from the gated `POST /api/audit` (origin `api`) and `POST /api/webhooks/audit` (origin `webhook`) routes, and from auditing an existing live article with `POST /api/articles/{ref}/audit` (origin `article`), which keeps the article's category. Export does not reclassify data. No production article subscription is configured.

The in-process task queue persists a queued record before starting work, then each
finished step. One process owns it; `MAX_CONCURRENT_RUNS` bounds simultaneous model
workflows. Run one API container and one worker process; do not scale replicas or
add uvicorn workers, because separate processes cannot coordinate this in-memory
queue. On shutdown/restart, incomplete runs become failed with `interrupted`,
preserving completed step evidence. Re-submit explicitly to retry. Operator stops
instead become terminal `cancelled` runs, keeping recorded steps with no verdict
or output. Queue pause is in memory, leaves running work alone, and resets to open
on every restart. This does not promise exactly-once webhook delivery: duplicate
POSTs create distinct runs. An external durable worker and idempotent webhook
delivery are deferred until a live producer exists.

### Schema changes

`db.py` maintains ordered `MIGRATIONS` and SQLite `PRAGMA user_version`. Version 1 marks the original schema; version 2 adds prompt tables; version 3 adds `model_assignments`; version 4 adds `articles`, `runs.article_ref` and the article/activity indexes, then backfills existing runs. Migration 3 was already allocated, so articles use 4 without changing applied migrations. A fresh database creates all tables at the latest version. Existing databases (including unversioned databases at version 0) apply each pending migration and version bump in one transaction, then create any new tables. Failures roll back that migration, including DDL, and leave the last successful version. Databases newer than the code are refused.

For every schema change, add a numbered migration; never edit an applied one. Test upgrades with retained rows, repeat startup, and failure rollback. JSON fields allow evidence formats to evolve without routine column migrations. Back up data before upgrading. Use a consistent SQLite backup (the SQLite backup API, or stop the service first); copying only a live `.sqlite3` file can omit WAL data.

### Article identity and history

`articles.ref` is `ka-<archive id>` for a newly imported archive article, otherwise
`u-<first 16 SHA-256 hex characters of normalized URL>`. Normalization strips outer
whitespace, lowercases scheme/host and drops the fragment; path and query retain their
case and spelling. The normalized URL has a unique index, and non-null `archive_id`
values have a partial unique index. An existing `ka-<id>` row is canonical when that
archive ID is imported again, including when its URL changes. `ref` is String(80);
`source` and `host` are String(200); `published_at`, `removed_at`, `created_at` and
`updated_at` are String(40); `archive_id` is String(80), `language` String(20), and
`category`/`origin` String(20). URL, title and content are text; nullable `snapshot`
is JSON containing version titles, check dates and hashes, never duplicated full text.
Source, publication/removal dates, archive ID and language are nullable.

Migration 4 processes runs in batches of 500 in creation order (SQLite insertion
order breaks ties), creates one article per normalized URL, and links runs with batch
updates. It extracts URL/title/publication metadata in SQLite and reads content only
for the first run of each new article. The first run supplies title, content and category.
The migration also backfills `search_text`, a text column containing NFKC-normalized,
Unicode-casefolded title, source, host and URL, separated by spaces. Article inserts and
updates maintain this value. Historical run inputs and workflow snapshots do
not change. Every submission route creates or selects its article in the same SQLite
transaction as the run insertion. Subsequent runs retain the original article text;
each run still preserves its own submitted input. `runs.article_ref` is a nullable,
indexed plain column; the composite `(article_ref, created_at)` index supports history.

## Demo data (test mode)

Seed a test database with `uv run python scripts/seed_demo.py --db data/audit-test.sqlite3`,
or pass `--database-url sqlite:///data/audit-test.sqlite3`. The Python entry point is
`audit.demo.seed(database_url)`; test mode calls it on every start. It performs no
network/model calls and prints counts only. Exit 0 means seeded (or already seeded).

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
through the offline `DemoEngine`, including demo articles in the `live` category while
live ingestion is disabled. Like benchmark submissions, it needs no token unless one is
configured; with a configured token, missing/wrong bearer credentials return 401. Existing
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

The generated [OpenAPI contract](../openapi.json) documents validation schemas and endpoints; regenerate with `uv run python scripts/export_openapi.py`. The TypeScript client is kept aligned through the same documented route shapes and integrated browser checks.

| Method / path | Purpose |
| --- | --- |
| `GET /health` | Minimal local liveness and the run `mode` (`test` or `live`). |
| `GET /api/status` | Run `mode` and model/search/read/live posture; `writes_enabled` is always false for archive writes; `prompts_editable` reports whether a token is configured; `model_provider` and `response_format` identify the inference profile. |
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
| `POST /api/benchmarks/runs` | Submit a benchmark, return 202 and its queued run. |
| `POST /api/audit` | Submit a **live** run; disabled by default. |
| `POST /api/webhooks/audit` | Submit a **live** webhook run; disabled by default. |
| `GET /api/articles` | Paginated articles with latest audit status/verdict, run count and activity. |
| `GET /api/articles/{ref}` | Stored content, archive metadata/links and newest-first run history with computed summaries; unknown refs return 404. |
| `POST /api/articles/{ref}/audit` | First audit or re-run; 202 `RunDetail`, origin `article`; preserves every previous run. |
| `POST /api/articles/import?days=30` | Read archive audit listings and upsert local article metadata; 200 `{imported, updated, skipped}`. |
| `GET /api/runs` | Filter/paginate by category, origin, status, verdict and workflow ID. |
| `GET /api/runs/{id}` | Complete run with stage and model/evidence records. |
| `POST /api/runs/{id}/cancel` | Stop a queued/running audit; return 200 `RunDetail`. |
| `GET /api/queue` | Queue pause flag, concurrency limit and oldest-first running/queued run IDs. |
| `POST /api/queue/pause` | Hold queued and new submissions; running audits continue. Return `QueueState`. |
| `POST /api/queue/resume` | Release queued work; return `QueueState`. |
| `POST /api/queue/cancel?include_running=false` | Stop queued audits; optionally include running audits. Return `{cancelled: [run IDs]}`. |
| `GET /api/runs/{id}/export` | Download the same local review record. |
| `POST /api/runs/{id}/reactions` | Append `agree`, `disagree` or `needs_review`, reviewer and note. |

Run submission accepts `removed_article: {url, title, content, published_at?}`, `similar_articles` (up to five), optional `label`, and optional `expected_verdict`. Poll the returned run ID until status is `succeeded`, `failed` or `cancelled`. `AUDIT_FAILED` is a failed execution, not a substantive finding.

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
serializes article submissions; `BEGIN IMMEDIATE` checks for **any** queued/running
run and inserts atomically. A conflict returns 409
`{"error":{"code":"run_in_progress","run_id":"<existing id>"}}`.
Missing content returns 422 `{"error":{"code":"article_has_no_content"}}`;
unknown articles return 404. All article routes pass through `RequestGuards`:
configured tokens are required (missing/wrong gives 401), writes reject untrusted
browser origins (403). In live mode, local benchmark writes remain available without
a token; live-category article audits additionally require enabled live ingestion
and a token, otherwise 404. Test-mode article audits accept either category, need a
token only when one is configured, and use the offline engine, as described above.
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
the audit-failed story. Every substantive verdict says: "A person decides; this is
not a finding of censorship." Failed/in-progress stories have no review note.
Cancelled runs have verdict label "Stopped by a person" and headline "A person
stopped this audit before it finished. This is not a result." Their verdict and
human-review note are null, with no flag sentences. Reached steps retain their
recorded status, except a failed step with `output.details.error_type` equal to
`CancelledError`: its story status is `stopped`, with neutral UI label "Stopped",
and it is omitted from `failed_steps`. Exact stored step records remain unchanged.
Unreached steps are `pending`. Test-mode cancellation interrupts the demo step
delay and keeps already-emitted steps; the seed includes one cancelled example.
The run-list metadata schema is named `RunSummary`; its existing fields are unchanged.

### Read-only archive import and detail enrichment

`POST /api/articles/import` accepts a `days` query parameter (default 30, range 1–90).
It calls the archive's paginated `audits_created_between` listing for that date window
and imports URL/title/publisher display name/publication date and archive ID as live
metadata, with origin `archive`. It does not fetch full article text or start audits.
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
`{"error":{"code":"archive_unavailable"}}`. When no archive identity exists yet,
import retains an existing normalized URL's ref, category, title and content when
that article originated from a run/demo, adding archive/source metadata without
breaking run links.

Opening an empty `ka-*` article requests the archive article once per ref/URL pair
per service lifetime with checks and versions expanded. A changed URL permits a fresh
attempt during the same service lifetime. It stores the latest version's `news_text` (or
top-level text when no versions exist), language, version title/check date/hash
metadata and the first dead-check date after the last 200, using the shared archive
check logic. The archive's timestamp representation is retained as a string for
`removed_at`. Failed reads return the saved article with empty content and no upstream
error text. Concurrent detail reads share a lock and one attempt; a service restart
permits another attempt for still-empty articles. All archive calls are GET-only;
local import/enrichment never mutates the archive.

This is an intentionally changed API: the old synchronous `/api/audit` response and `/api/v1/articles/...` publishing jobs are archived. New `/api/audit` is asynchronous, persisted and live-gated. Consumers must explicitly migrate; no silent compatibility facade publishes to Karakutu.

A finished live run serializes to the versioned [audit record](audit-record-schema.md) that will be posted to karakutu (`backend/audit/record.py`). Benchmark, unfinished and interrupted runs never serialize. No route posts records yet; `GET /api/runs/{id}/export` remains the local review record, with reactions and exact transcripts.

The editable names are `relevance`, `classification_coarse`, `classification_fine`, `source`, `web_search`, and `related_removals`. Prompt text must be a valid UTF-8 string, nonblank, at most 20,000 characters, without NUL, and retain the registry's required output tokens and `GUARDRAIL` section marker (or `NONE` for related removals). Author is 1–120 characters; note is at most 2,000. Validation returns 422 with safe codes, including `missing_token:<token>`; it never echoes prompt text. Duplicate text for the same name returns 409 `duplicate_version`; activate the existing version instead. Rollback without a previous distinct version returns 409 `no_previous_version`. Unknown names/versions return 404. Prompt summaries omit text; individual versions and the current/historical workflow snapshots contain exact text.

### Model assignments

Supported steps are `classification`, `relevance`, `related_removals`, `source`, and `web_search`. Similarity makes no model call. Unknown steps (including similarity) return 422 `unknown_step`; names missing from an available provider catalog return 422 `unknown_model`. Provider discovery excludes IDs containing `embed` or `rerank`; a failed discovery returns an empty list and `models_unavailable`, never exception text. When discovery is unavailable, names are accepted. Synthetic/model-disabled mode makes no discovery call, returns an empty list without an error and accepts configured names. Clearing an override uses JSON null; blank names are invalid. Author is required and nonblank (1–120 characters); note is optional (up to 2,000); model names are at most 200 characters.

At startup the newest persisted assignment for a step takes precedence over `MODEL_NAME_<STEP>`; without an assignment, the environment applies. An API null clears the override and selects the resolved default, overriding any environment step value across restarts. `source` remains `api` after a clear. Assignments are append-only, ordered by SQLite insertion order, and atomically committed with the new workflow snapshot under the prompt activation lock. Each submission captures its workflow before queueing; already queued/running runs retain their earlier mapping. Prompt activation preserves current model assignments, and model assignment preserves current prompts. Model keys never enter public configurations or fingerprints.

Definitions now use `format_version` 3; unchanged setups receive a new workflow ID on restart. Effective model mapping, provider and response mode affect the ID; assignment metadata and credentials do not. Historical workflows, runs and inputs stay immutable.

## Privacy and access

All routes with article/run data require the bearer token when `API_AUTH_TOKEN` is configured. Live submission additionally requires `LIVE_INGESTION_ENABLED=true`; startup refuses enabled live ingestion without a token, or a configured token shorter than 32 characters. A disabled live route returns 404 before auth or body validation. On the default loopback workstation, the benchmark/review UI is available without a token. Non-loopback allowed hosts require a token at startup; Tilt binds only to loopback.

`ALLOWED_HOSTS` and `ALLOWED_ORIGINS` add comma-separated entries to the loopback defaults. Whitespace around entries is trimmed and empty items are dropped. Hosts always include `localhost`, `127.0.0.1` and the existing `testserver` test host. Origins always include `http://localhost:2200`, `http://127.0.0.1:2200`, `http://localhost:2201` and `http://127.0.0.1:2201`. Empty environment values preserve those defaults exactly.

Hosts are normalized to lowercase and must be plain hostnames or IP literals (unbracketed for IPv6; request hosts are compared after the same IP canonicalization); wildcards, schemes, ports, paths, userinfo, whitespace, empty labels and non-canonical numeric IPv4 spellings (leading zeros, hexadecimal, abbreviated, or a numeric last label) are rejected. Origins normalize scheme/host case, write ports as numbers and drop the scheme default (`:80` for HTTP, `:443` for HTTPS) as browsers do, and must be exactly `http://host[:port]` or `https://host[:port]` (bracket IPv6 hosts; they are stored in the browser's hexadecimal form, so `[::ffff:192.0.2.1]` becomes `[::ffff:c000:201]`). Wildcards, `null`, userinfo, paths including a trailing slash, query strings and fragments are rejected. Invalid entries stop startup without echoing their values. Any non-loopback host requires `API_AUTH_TOKEN`; startup fails with `Non-loopback ALLOWED_HOSTS require API_AUTH_TOKEN` if it is absent. Token length validation still requires at least 32 characters. Origins permit mutation requests; they do not enable cross-origin browser reads or change authentication.

Docker reads `.env` at runtime, stores SQLite under `/data` in a named volume, and overrides local search/model paths for the container network. `KARAKUTU_BIND_ADDRESS` controls published API/UI interfaces (default `127.0.0.1`), independently of the allowed Host list. `CONTAINER_MODEL_BASE_URL` selects the container's model endpoint; `MODEL_BASE_URL` remains the local/Tilt setting. Preserve Host through reverse proxies so the backend can validate the browser's actual host. Remote use should terminate TLS before the API/UI. SearXNG remains loopback-only on port 2202.

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

The ignore rules also cover `reports/`, `exports/`, `fixtures/`, `datasets/`, `private/`, `runs/`, `.env*`, wallet files, database sidecars, JSON/JSONL/CSV/TSV/Parquet files. Only explicit configuration manifests and `openapi.json` are allowed JSON files. Documentation stays tracked under `docs/`; generated reports do not belong there. Tests generate fabricated inputs inline. `scripts/check_data_hygiene.py` catches tracked/staged private paths, including forced Git additions. It also scans the staged content of every file for home-directory paths, email addresses, private keys, keyfile members and common API tokens, URLs on real hosts, and bare real hostnames in prose, comments and string literals. Reserved names pass: `.example`/`.invalid`/`.test`/`.localhost`, `example.<tld>`, internal suffixes such as `.local`/`.internal`, single-label and numeric hosts, and IP literals. Outlet domains are reference data and may appear only in the lookup tables named in the script (`workflow/source.py`, `search/urls.py`, `workflow/web_search.py`); `LICENSE` and lockfiles are exempt from the host rules only. The required pinned uv build-image reference is allowed only as the exact backend Dockerfile `FROM` directive; comments, URLs and other image references remain scanned. Likewise, the project's public repository URL is allowed only as the exact `git clone` line of the README quick start. The patterns are heuristics: they cannot detect real article prose or real names, so never paste either into source, prompts, tests or docs.
