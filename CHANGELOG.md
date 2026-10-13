# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed (behaviour change: classification output)
- Step 1 (IPTC classification) now follows an archive reviewer's feedback on the 2026-10-01
  batch. The coarse call sees up to 10,000 characters of the article (`COARSE_ARTICLE_CHARS`,
  was 1,500) and the fine call up to 5,500 (`FINE_ARTICLE_CHARS`, was 800). Both stay within the
  8,192-token context. The prompts give the main subject and each separate section a topic,
  wherever it appears. The old rule that a topic must cover "roughly a fifth" of the article is
  gone. The prompts also say where subjects hidden by the short definitions are filed, and they
  correct three mislabels: LGBTİ+ people are not an ethnic minority, protests are not press
  freedom, and defendants who are free are not political prisoners.
- A new code rule drops the vague umbrellas people, values, social problem and social condition
  when a list holds a more specific topic. Drops are recorded in `details.vague_dropped`.
- Both prompts now tell the model to answer only through the `final_result` function. With the
  old wording, gemma wrote "field: value" text on a third of the calls, and each one cost a
  hidden retry.

### Fixed
- A list field the model sends as a JSON-encoded string (`"[\"medtop:…\"]"`) is now decoded.
  Before, the field failed validation, and when the retry did the same, a correct answer was
  lost and the case had no classification.

Measured with local gemma-4-12b on 40 held-out gold cases, two runs: must-have recall rose from
0.50 to 0.58–0.60, tags the reviewer rejected fell from 5 to 1–2, and vague tags fell from 10 to
2. On 22 untouched cases from other outlets, recall rose from 0.61 to 0.68–0.73, with no rejected
tags. Step 1 still takes a median 40 s on the reference machine. It now sends about 3,000 more
input tokens per article, which matters for hosted inference costs. Rendered comments for
existing records are unchanged (golden tests).

## [1.0.0] - 2026-10-11

First public release. Audit results (verdicts, stage outputs, classification, flags and
comment text) are unchanged from the recorded 2026-10-01 batch runs; golden tests pin this. The
one exception is failure text: an `AUDIT_FAILED` finding now names the exception type only.

### Added
- Any OpenAI-compatible model server, local or hosted: neutral `MODEL_*` settings
  (`MODEL_ENABLED`, `MODEL_BASE_URL`, `MODEL_NAME`, `MODEL_API_KEY`, `MODEL_HEALTH_TIMEOUT`,
  `MODEL_REQUEST_TIMEOUT`, `MODEL_MAX_TOKENS`) with the earlier `LMSTUDIO_*` names read as
  aliases; `MODEL_THINKING_CONTROL` (`lmstudio` | `reasoning_effort` | `none`) for servers that
  reject `chat_template_kwargs`; optional `MODEL_TEMPERATURE` (unset = the server default, as
  before); the health check sends the API key as a bearer token. The LM Studio request body is
  unchanged. A Scaleway example profile is in `.env.example`.
- Every run record and article job records the requested model, its call settings and the
  model ids that actually answered (`model`); nothing rendered reads it.
- `POST /api/v1/articles/{article_id}/audit` (no body, optional `?dry_run=true`) runs the whole
  archive flow for one article: the worker looks up the article's audit with the archive's exact
  `GET /audits/by-article/{id}` and acts only when the answer embeds the same article id. One job
  per article (a queued/running or succeeded job is returned, a failed one is replaced); 429 when
  the queue is full. Bearer-authenticated with `API_AUTH_TOKEN` (at least 32 characters, or the
  service refuses to boot); with the token unset the routes answer 404 and no worker runs.
- Job status endpoints `GET /api/v1/jobs/{job_id}` and
  `GET /api/v1/articles/{article_id}/jobs/latest`, and a readiness probe `GET /ready`.
- In-process job queue and worker with a SQLite job store (single replica), per-case
  timeout, retries, a model-health pause, restart recovery, and a per-audit apply ledger. Job
  log lines carry the job id as their correlation id.
- Fail-closed publish gates: writes are off by default (`ARCHIVE_WRITES_ENABLED=false`, shadow
  mode: every job is a dry run that stores the rendered comment), and a job is also forced to a
  dry run without a real model, real search, the IPTC catalog or a wallet. A result produced
  while the archive was unreachable is withheld and the audit re-run. What a job writes (template
  C, IPTC tags, one comment) is fixed in code; `ARCHIVE_WRITES_ENABLED` is the only write switch.
- `src/archive_flow/`: the batch scripts' intake, runner, standing apply rules, comment
  templates and applier, moved so the CLIs and the article jobs share one implementation (rules and
  templates verbatim; golden tests pin the rendered comments); `src/runtime.py` wires both.
- Write-ahead tag mark and read-after-write confirmation when applying tags. In article jobs, the
  comment is held while a failed tag write is retried, so a comment never says "not written"
  about tags that a retry then writes.
- Job robustness: an unreadable discussion count is retried, not turned into a final outcome; a
  running job whose lease has expired is reclaimed, and a
  job interrupted more than three times is failed instead of re-queued forever; the attempt that
  pauses the worker on a model fail streak is not counted against the job.
- `JOB_RESULT_REUSE_S` (off by default) reuses only dry runs made with the real configuration
  (their sole write blocker was `writes_disabled`), and only for the audit the archive returns now.
- Optional bearer token for `POST /api/audit` (`API_AUTH_TOKEN`), and a 413 for any article
  field of 2,000,000 characters or more.
- Configuration is validated at startup (`Settings.validate`); invalid settings stop the boot
  with a list of problems that never echoes a secret.
- `KARAKUTU_WALLET_JSON` to supply the wallet keyfile as content from a secret manager.
- LICENSE (Apache-2.0), NOTICE, SECURITY.md, CONTRIBUTING.md, CODE_OF_CONDUCT.md and AGENTS.md
  (contributor and agent rules); `.env.example` documents every setting.

### Changed
- CORS is off unless `CORS_ALLOW_ORIGINS` is set (it was open to every origin), and it never
  applies to `/api/v1`.
- `SEARXNG_ENGINES` defaults to `google,bing,duckduckgo`, the profile the recorded batch runs
  used, so the image and the batch tools search the same engines.
- The `scripts/open_audit` batch CLIs are thin wrappers over `src/archive_flow/` with the
  same flags, files and output.
- The archive's related-removals window is cached only within one audit, so every audit reads
  a fresh window (the batch runs' behaviour).
- Docker: unprivileged user, `/data` volume for the job store, `--proxy-headers` and a
  graceful-shutdown timeout. Compose binds ports to loopback, runs read-only, mounts the
  wallet read-only, and pins SearXNG by digest behind an optional `searxng` profile with its
  secret taken from the environment and the limiter on.
- CI: actions pinned by commit SHA, compose validation, a hardened container boot smoke test
  and an advisory dependency audit.
- Run artifacts, archive data samples and one-off scripts moved out of the published
  tree.

### Security
- The SSRF guard resolves DNS and vets every connection (including redirect hops), and
  refuses non-canonical IPv4 forms and internal host suffixes.
- Wallet parsing and authentication errors never carry key material or raw exception text;
  stage failures are logged by exception type only.
- One constant-time bearer check (`src/api/auth.py`) for `/api/v1`, `/api/audit` and the
  detailed `/ready`. Path article ids accept ASCII digits only; the order is 404 (disabled), 401,
  then 422, so an unauthenticated caller learns nothing about the parameters.
- `/ready` answers anonymous callers with `{"status"}` only (same 200/503); the checks,
  `writes_enabled` and `write_blockers` need `Authorization: Bearer <API_AUTH_TOKEN>` and are
  never served while `API_AUTH_TOKEN` is unset.
- `API_AUTH_TOKEN` must be at least 32 characters when set (the message never echoes it).
- The `POST /api/audit` 422 body names fields and messages, never the submitted values.
- `/ready` is evaluated at most once every 5 seconds, so polling it cannot multiply store writes
  and upstream probes.
