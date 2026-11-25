# karakutu-ai-audit contributor guide

Read README, docs/architecture.md, docs/workflow.md and docs/contributing.md before editing. Do not commit, push, deploy, publish or mutate Karakutu unless explicitly instructed.

## Architecture

- TypeScript/React/Vite in `web/`; FastAPI in `backend/audit/api.py`; SQLAlchemy with SQLite (default) or PostgreSQL/psycopg in `db.py`.
- `engine.py` is the LangGraph composition root, six ordered steps; `workflow/` owns prompts/rules and the editable-prompt registry (byte-exact defaults stay in `prompts.py`); `search/` is the only caller-influenced page-fetch boundary.
- `model.py` is a bounded OpenAI-compatible JSON adapter, not pydantic-ai. Model output never has authority to declare censorship. `workflow/pipeline.py` selects evidence verdicts and assembles low-interest responses; engine handles failed execution exits.
- `provenance.py` fingerprints backend source, prompts, locked dependencies, search configuration and behavior settings. Active DB prompt texts and effective step models join the format-3 definition; submissions capture both before queueing. Model assignments and prompt activations share one lock and atomically save snapshots; latest API model assignments override env (null selects the default). Every run/reaction links its immutable workflow snapshot. Never mutate historical run input or workflow snapshots.
- `config.py` reads environment when constructed; tests inject Settings/engines. One process; no multiprocess workers against the in-process queue.
- `RUN_MODE` (`config.py`) picks `live` (default) or `test`. Test mode swaps in `demo_engine.DemoEngine` (offline, synthetic), forces every integration off, uses only a `*-test.sqlite3` database and seeds it through `audit.demo.seed(database_url)` at startup; `compose.test.yaml` and the `Makefile` run it as a separate Compose project.
- `compose.yaml` runs the non-root API/UI images plus search; `Tiltfile.stack` loads only `compose.search.yaml` and keeps local hot reload. API/UI/search ports: 2200/2201/2202, loopback by default. API/UI Docker binding and allowed hosts/origins are configurable; non-loopback allowed hosts require bearer authentication. All Compose files, including `compose.test.yaml`, are provenance dependencies.

- Storage dialect differences belong in `db.py`: SQLite keeps pragmas, `user_version`
  and rowid ordering; PostgreSQL uses plain `json` (never `jsonb`: saved records, including
  evidence with NUL and exact number formatting, must stay verbatim), `schema_version`,
  identity `seq` ordering and a transaction advisory writer lock. Archive article text is
  NUL-free on both backends. Test mode and demo seeding stay SQLite-only.
  Keep database credentials out of errors and logs. One API process remains required.

## Invariants

Preserve prompts, thresholds and verdict semantics unless intentionally changing behavior; document and benchmark deliberate changes. `POSSIBLE_CENSORSHIP` is not a finding of censorship and requires human review. `FOUND_ONLINE`/`NOT_REMOVED` are never exculpatory. Model failure must be `AUDIT_FAILED`, never an invented negative finding. Only independently verified 404/410 evidence can raise a dead-source flag; archive-sourced dead candidates, 403s, PDFs, private URLs and unreachable sites cannot.

Same-publisher rewrite verdicts require a live page whose requested and final URLs
stay on the publisher. At least 20 own distinctive words require 35% word overlap,
35% of the original headline words and a known publication gap of at most two days.
Shorter originals require 80% headline overlap in both directions within one day.
Their live page must contain at least 80% of the original headline words.
No rewrite may redirect to a different page: a final URL, when present,
must normalize to the requested URL or identify the same article variant.
Candidate headlines use the archive title, falling back to the URL slug only.
Use measured vocabulary, never archive title/URL similarity scores; missing dates
fail closed. In scope, web search must still match the same story. A demoted rewrite
is promoted only when no other same-publisher match qualifies under the existing
relocation rule; missing key facts retain the existing review verdict.
Web search receives only the best rewrite. Each prompt keeps the first budgeted
live pages in their unchanged order and appends that rewrite as at most one extra
page if absent, including on smaller retries.
At the low-interest exit, a partial copy
holding at least 50% wins first, then a qualifying rewrite, then a below-floor note.
Key facts are not compared on that exit; findings ask a person to compare the pages.
Siblings remain reviewer context, and rewrites add no flags.

No archive mutation methods. Optional wallet authentication is for reads only. Live category runs come only from the enabled/authenticated API or webhook routes, or from auditing an existing live article (`POST /api/articles/{ref}/audit`, origin `article`, behind the same live gate in live mode); benchmarks remain benchmarks even when using a real model. No remote tracing. Never log article data, raw exceptions, prompts, auth tokens or wallets. Exact transcripts belong only in ignored local storage and authenticated review responses. The audit record (`docs/audit-record-schema.md`, `backend/audit/record.py`) is the only shape meant to leave the service: it carries article identity, verdict and bounded summaries/evidence, with a workflow pointer; article text, pages, prompts and model calls stay behind the explicit run details route; change it through the schema and its version.

All article data, test corpora, generated reports, exports and databases are Git-ignored. No recorded news articles, real article URLs or datasets in tests/docs/source. The operator endpoint in README and .env.example is the narrow configuration exception enforced by the hygiene check. Generate fabricated test inputs inline. Keep human documentation in tracked docs/, generated outputs in ignored data/reports/. The data-hygiene check must remain passing.

## Workflow

Every new environment variable must have an explained assignment in `.env.example`
and a row in `docs/configuration.md`; `test_env_example.py` enforces static coverage.
Keep setup/run tests isolated in temporary directories and never overwrite operator settings.

Read relevant files and preserve unrelated work. Prefer small domain changes. Update the nearest doc and CHANGELOG.md for meaningful changes. Maintain `openapi.yaml` with `scripts/export_openapi.py`; response models and TypeScript types must agree.

Run focused tests, then `uv run pytest -q`, `uv run ruff check .`, `npm --prefix web run typecheck`, `npm --prefix web run build`, `uv run python scripts/check_data_hygiene.py`, and `uv run python scripts/export_openapi.py --check`. Unit tests must not need network or a live model. Optional live smoke uses generated fictional data via the benchmark endpoint. Report exact checks and remaining limitations.

## UI conventions

- `web/src/App.tsx` composes providers, shell and route pages. `api/client.ts` is
  the sole HTTP boundary; `api/hooks.ts` owns reads, cancellation and polling.
  Reusable UI belongs in `components/`, route composition in `pages/`, retained
  review/trace/workflow behavior in `features/`, and forms in `dialogs/`.
- Declare all colours and shared visual tokens in `styles/tokens.css`; area CSS
  consumes tokens. Keep self-hosted fonts, visible focus and reduced motion.
  Run `npm --prefix web run check:contrast` after changing colour pairs and
  `npm --prefix web run check:ui` for UI behavior checks. Keep source/scripts
  near 80 columns, never over 100 except unavoidable strings or URLs.
- Keep addresses in `router.tsx`: Articles and article detail, Runs and run detail,
  Benchmarks, Settings, root replacement to Articles, and a readable unknown route.
  Ledger filters/paging use the query string. Article search/status/verdict/category
  filter on the server (default All, which omits category); all run filters apply on
  the server. Each ledger remembers its own service epoch for paging.
- `VerdictChip` owns verdict labels/tones. Each verdict area has one authored
  disclaimer: “This is not a finding of censorship; a person decides.” Do not
  repeat it in shell or review UI. Article stories use `human_review_note` when
  present instead of adding a second disclaimer. Exact saved records remain unmodified.
  Story footnotes project recognized stored step fields only; a failed original
  probe or dead cited source never establishes that the original URL is dead.
- `format.ts` owns durations and ledger label de-duplication. Article titles are
  row links; row click handlers preserve native control and modified clicks.
  Same-path reloads retain content with `refreshing` only within the same token
  and service epoch; an uncached path or any token/epoch change clears data before
  rendering.
  Ledger responses stay in provider memory per query within that epoch; returning
  queries render cached rows before revalidation and history scroll restoration.
  Epoch changes reset paging; out-of-range offsets replace to the last valid page.
  Retained data shows refresh errors with retry. Reset drafts explicitly on
  service epochs without remounting read views.
- Expanded steps show exact input/output JSON directly, in two columns from
  1000px and stacked below. Use labelled chevron buttons with `aria-expanded`
  and `aria-controls`; keep evidence and model calls in disclosures.
