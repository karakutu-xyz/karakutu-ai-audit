# Configuration

[`.env.example`](../.env.example) is the explained source for every project setting.
Create local settings with `make env`; it generates the SearXNG secret and writes
`.env` with mode 600. `make env API_TOKEN=1` also generates a bearer token, only
when creating a new file. An existing file is always left unchanged, including its
permissions. Setup prints missing required names and reasons for the selected mode
and both test and live, never values. Use `python3 scripts/setup.py --dir <directory>
--mode test` to prepare a separate directory.

The defaults below describe **unset** variables, which can differ from the local
example: the API defaults to live with model/search disabled; the example retains
live with LM Studio and SearXNG enabled. `make test` selects offline mode regardless
of the file. For a local demo use `RUN_MODE=test make dev`; a file with no RUN_MODE
makes the local runner default to test. Production means live behind a TLS proxy.
Test ignores model/search/archive/live-intake settings. Optional integrations are
not prerequisites for a benchmark; disabling the model yields `AUDIT_FAILED`.

Tooling settings are marked in the purpose column. Make-specific variables are
normally passed as command arguments or exported by the caller; Make does not
source `.env` into the shell. The live-start wrapper reads its literal assignments
only to select and preflight the optional wallet secret. Changing PROJECT also requires matching TEST_PROJECT, VOLUME and
API_IMAGE if exporting the example's explicit values. The smoke suite reads its
SMOKE_* controls from the caller, creates its own temporary clone and assigns its
internal image/directory/token values. It does not source this checkout's `.env`.
OS variables (PATH, TMPDIR), Make built-ins and forced tracing-off/PYTHONPATH
assignments are not project configuration. `MODEL_MAX_TOKENS` is an output count;
`API_TOKEN` is a setup boolean. Neither contains a secret.

| Variable | Purpose | Default | Required in test / live / production | Secret | Where to get it |
| --- | --- | --- | --- | --- | --- |
| `RUN_MODE` | Select offline demo (test) or real integrations (live). | API/example: live; run-local: test; Compose selects mode | test/live/production: select mode | no | Choose test or live |
| `DATA_DIR` | Local private storage directory; Compose overrides to /data. | data | test/live/production: default sufficient | no | Local writable directory |
| `DATABASE_URL` | Live SQLite or PostgreSQL/psycopg URL; require SSL for remote PostgreSQL. Test ignores it. | live: DATA_DIR/audit.sqlite3 | test: ignored; live/production: optional override | yes when it holds a password | Local SQLite path or provider connection URL |
| `POSTGRES_PASSWORD` | Optional local PostgreSQL service password; tooling only. | empty (service refuses to start) | test/live: only with optional PostgreSQL Compose file; production: provider credentials | yes | Generate with openssl rand -hex 16 |
| `TEST_POSTGRES_URL` | Test-only admin URL to a throwaway PostgreSQL server; creates/drops one database per test. | empty (temporary SQLite) | manual PostgreSQL pytest: admin URL; make test-postgres: generated | yes | Isolated test server; never production |
| `MAX_CONCURRENT_RUNS` | Concurrent audits inside the single API process (1 to 8). | 1 | test/live/production: default sufficient | no | Operator capacity choice |
| `MODEL_ENABLED` | Enable model calls; test mode always ignores integrations. | false (example enables it) | test: ignored; live/production: true for useful audits | no | Choose true for live |
| `MODEL_BASE_URL` | Local/Tilt OpenAI-compatible inference endpoint. | http://127.0.0.1:1234/v1 | test: ignored; live/production: reachable endpoint | no | LM Studio server; Scaleway console inference endpoint |
| `MODEL_PROVIDER` | Provider profile: lmstudio or openai_compatible. | lmstudio | test: ignored; live/production: choose provider | no | LM Studio or provider profile |
| `MODEL_NAME` | Chat model identifier; auto discovers the first chat model. | auto | test: ignored; live/production: loaded model or auto | no | LM Studio model list or Scaleway model catalog |
| `MODEL_API_KEY` | Provider credential; unauthenticated local servers accept empty with either profile. | lm-studio (example leaves empty) | test: ignored; live/production: only if provider needs authentication (hosted providers do) | yes | Provider console; Scaleway API secret key |
| `MODEL_TIMEOUT` | Model request timeout in seconds (1 to 600). | 90 | test: ignored; live/production: optional | no | Operator latency budget |
| `MODEL_MAX_TOKENS` | Maximum model output tokens (128 to 16384); a limit, not a credential. | 2048 | test: ignored; live/production: optional | no | Operator output budget |
| `MODEL_TEMPERATURE` | Model sampling temperature. | 1.0 | test: ignored; live/production: optional | no | Provider sampling guidance |
| `MODEL_THINKING_CONTROL` | Thinking extension: lmstudio, reasoning_effort or none; set none for hosted. | lmstudio profile: lmstudio; openai_compatible: none | test: ignored; live/production: optional; omit rather than set empty | no | Provider capability; hosted usually none |
| `MODEL_RESPONSE_FORMAT` | JSON mode: json_schema, json_object or auto; hosted usually auto. | lmstudio: json_schema; openai_compatible: auto | test: ignored; live/production: optional; omit rather than set empty | no | Provider capability; hosted usually auto |
| `MODEL_NAME_CLASSIFICATION` | Optional classification model; latest API assignment wins. | inherit MODEL_NAME | test: ignored; live/production: optional | no | LM Studio model list or provider catalog |
| `MODEL_NAME_RELEVANCE` | Optional relevance model; latest API assignment wins. | inherit MODEL_NAME | test: ignored; live/production: optional | no | LM Studio model list or provider catalog |
| `MODEL_NAME_RELATED_REMOVALS` | Optional related removals model; latest API assignment wins. | inherit MODEL_NAME | test: ignored; live/production: optional | no | LM Studio model list or provider catalog |
| `MODEL_NAME_SOURCE` | Optional source model; latest API assignment wins. | inherit MODEL_NAME | test: ignored; live/production: optional | no | LM Studio model list or provider catalog |
| `MODEL_NAME_WEB_SEARCH` | Optional web search model; latest API assignment wins. | inherit MODEL_NAME | test: ignored; live/production: optional | no | LM Studio model list or provider catalog |
| `SEARCH_PROVIDER` | Search backend: searxng or disabled. | disabled (example uses searxng) | test: ignored; live/production: optional search | no | Choose provider |
| `SEARXNG_URL` | Local/Tilt search endpoint; Compose overrides to internal service. | http://127.0.0.1:2202 | test: ignored; live/production: required when searxng | no | Bundled search; match KARAKUTU_SEARCH_PORT locally |
| `SEARXNG_SECRET` | SearXNG signing secret. Tooling only, not read by the API. | empty | test: unused; live/production: required for bundled search | yes | Generated by make env |
| `ARCHIVE_BASE_URL` | Trusted read-only archive API; empty disables archive reads. | empty (disabled) | test: ignored; live/production: optional | no | Your archive operator, e.g. https://archive.example |
| `ARCHIVE_WALLET_PATH` | Host path to your read-auth keyfile outside the repo, for local and Docker use; Docker always uses /run/secrets/archive_wallet inside the API. | empty | test: ignored; live/production: only when archive requires auth | yes | Your own protected keyfile outside the repo |
| `ARCHIVE_WALLET_JSON` | Inline read-auth wallet JSON; prefer a keyfile path; quote JSON in shell. | empty | test: ignored; live/production: alternative to wallet path | yes | Your own wallet; host secret store |
| `API_AUTH_TOKEN` | Bearer token (at least 32 characters); protects all /api data when set. | empty | test/live: remote access or editing; live: ingestion; production: required | yes | make env API_TOKEN=1 or openssl rand -hex 32 |
| `ALLOWED_HOSTS` | Extra comma-separated hosts without schemes/ports; remote hosts need token. | localhost,127.0.0.1,testserver | test/live: remote access; production: public proxy hostname | no | Your hostname, e.g. audit.example |
| `ALLOWED_ORIGINS` | Extra comma-separated browser origins, no trailing slash. | http://localhost and http://127.0.0.1 on 2200/2201 | test/live: non-default origins; production: public TLS origin | no | Your browser origin, e.g. https://audit.example |
| `LIVE_INGESTION_ENABLED` | Enable authenticated live submissions; benchmarks remain separate. | false | test: ignored; live/production: only for live intake; token required | no | Enable deliberately after review |
| `CONTAINER_MODEL_BASE_URL` | Container-visible model URL; overrides MODEL_BASE_URL. Tooling only. | http://host.docker.internal:1234/v1 | test: ignored; live/production: reachable model endpoint | no | Docker host LM Studio or Scaleway console endpoint |
| `KARAKUTU_BIND_ADDRESS` | Compose API/UI publish address; API validates it; local runner stays loopback. | Compose: 127.0.0.1; API: empty | test/live/production: loopback recommended; remote requires token | no | Keep loopback behind TLS proxy |
| `KARAKUTU_API_PORT` | Published API/local runner port. Tooling only, not read by API. | 2200 | test/live/production: optional | no | Free local port |
| `KARAKUTU_WEB_PORT` | Published UI/local runner port; API adds matching loopback origins. | Compose/runner: 2201; API: unset | test/live/production: optional | no | Free local port |
| `KARAKUTU_SEARCH_PORT` | Published loopback search port. Tooling only, not read by API. | 2202 | test: unused; live/production: optional | no | Free local port; update SEARXNG_URL locally too |
| `KARAKUTU_API_PROXY` | Vite API proxy target. Tooling only, not read by API. | Vite: http://127.0.0.1:2200; runner derives API port | test/live: optional local proxy; production: unused by built UI | no | Local API URL |
| `API_TOKEN` | Make env token-generation flag (1 enables). Tooling only. | 0 | test/live/production: optional setup flag | no | Pass make env API_TOKEN=1 |
| `PROJECT` | Make live Compose project name. Tooling only. | karakutu-ai-audit | test/live/production: optional Make override | no | Choose isolated stack name |
| `TEST_PROJECT` | Make test Compose project name. Tooling only. | PROJECT plus -test | test: optional Make override; live/production: unused | no | Choose isolated test name |
| `LIVE_FILES` | Make live Compose file arguments. Tooling only. | -f compose.yaml | test/live/production: optional Make override | no | Your Compose override files |
| `VOLUME` | Make backup/restore data volume. Tooling only. | PROJECT plus _karakutu-audit-data | production: match stack when backing up; otherwise optional | no | Compose volume name |
| `API_IMAGE` | Make backup/restore helper image. Tooling only. | PROJECT plus -karakutu-audit-api | production: match built image for backup; otherwise optional | no | Locally built API image |
| `BACKUP_DIR` | Make backup destination; must exist outside checkout. Tooling only. | unset | production: required for backup; otherwise unused | no | Your protected backup directory |
| `BACKUP_FILE` | Make restore archive path. Tooling only. | unset | production: required for restore; otherwise unused | no | Your selected backup archive |
| `START` | Make restore restarts API when 1; 0 leaves it stopped. Tooling only. | 1 | test/live/production: optional restore flag | no | Choose 0 for rollback |
| `SMOKE_PROJECT` | Smoke run name prefix; random suffix always appended. Tooling only. | karakutu-smoke | test/live/production: unused; smoke: optional | no | Choose isolated smoke prefix |
| `SMOKE_REF` | Commit exercised by Docker smoke suite. Tooling only. | HEAD | test/live/production: unused; smoke: optional | no | Local Git revision |
| `SMOKE_API_PORT` | Smoke API loopback port. Tooling only. | 23100 | test/live/production: unused; smoke: optional | no | Free local port |
| `SMOKE_WEB_PORT` | Smoke UI loopback port. Tooling only. | 23101 | test/live/production: unused; smoke: optional | no | Free local port |
| `SMOKE_SEARCH_PORT` | Smoke search loopback port. Tooling only. | 23102 | test/live/production: unused; smoke: optional | no | Free local port |
| `SMOKE_KEEP_IMAGES` | Keep smoke-built images when 1. Tooling only. | 0 | test/live/production: unused; smoke: optional | no | Choose 1 for inspection |
| `SMOKE_UPGRADE_FROM` | Optional old commit for smoke upgrade test. Tooling only. | empty (skip upgrade) | test/live/production: unused; smoke: optional | no | Local Git revision |
| `SMOKE_API_IMAGE` | Generated smoke API image name; do not set manually. Tooling only. | generated by docker_smoke.sh | test/live/production: unused; smoke override: generated | no | Smoke suite assigns it |
| `SMOKE_WEB_IMAGE` | Generated smoke UI image name; do not set manually. Tooling only. | generated by docker_smoke.sh | test/live/production: unused; smoke override: generated | no | Smoke suite assigns it |
| `SMOKE_DIR` | Generated smoke fixture directory; do not set manually. Tooling only. | generated by docker_smoke.sh | test/live/production: unused; smoke override: generated | no | Smoke suite assigns it |
| `SMOKE_API_TOKEN` | Generated smoke bearer token; do not set manually. Tooling only. | generated by docker_smoke.sh | test/live/production: unused; smoke override: generated | yes | Smoke suite generates it |
| `FAKE_MODEL_PORT` | Offline smoke fake-model port. Tooling only. | 8000 | test/live/production: unused; smoke: optional | no | Smoke helper setting |
| `FAKE_MODEL_SLOW_SECONDS` | Offline fake-model delay; smoke Compose sets 90. Tooling only. | 60 | test/live/production: unused; smoke: optional | no | Smoke helper setting |

## Secrets

MODEL_API_KEY, SEARXNG_SECRET, API_AUTH_TOKEN, ARCHIVE_WALLET_JSON,
ARCHIVE_WALLET_PATH, POSTGRES_PASSWORD, TEST_POSTGRES_URL and password-bearing
DATABASE_URL are secret settings. The wallet path is sensitive metadata and
points to your own keyfile outside the repository; never put a wallet in an image.
SMOKE_API_TOKEN is a temporary secret generated by the isolated smoke suite.

Locally, secrets live in ignored `.env` with mode 600. In production, use the host's
secret store or environment and restrict any materialized env file to mode 600.
Compose reads `.env` at container creation through `env_file`; values are never
baked into images. If injecting values from a host secret manager, use a protected
runtime env file or a Compose override that explicitly forwards the environment
into the API container: exported variables alone are not automatically forwarded
by `env_file`. Compose interpolation inputs such as SEARXNG_SECRET can also come
from the host environment.

Never commit secrets. `.gitignore` excludes `.env` and `.env.*` except
`.env.example`; the data-hygiene gate checks ignored paths and staged content.
The service never logs tokens, prompts, wallets or article data. Setup and the
local runner print names and reasons only. Private run transcripts and databases
remain sensitive, even though they are not application logs.

To rotate API_AUTH_TOKEN, generate a fresh value with `openssl rand -hex 32`,
replace it in the protected runtime configuration, then recreate the API container
with `make live` (or restart a local API process). Update the UI's in-memory
Connection settings token and any authorized clients. The old token stops working
after restart. `make env API_TOKEN=1` never rotates an existing file.

## How each command loads `.env`

- **`make env`:** runs `scripts/setup.py`; no application starts. The optional
  `--dir`/`--path` targets another directory. It creates `data/` as before.
- **`make test` / `make live`:** Compose reads root `.env` for `${...}` interpolation
  and the API's optional `env_file`. Test requires no file and overrides RUN_MODE
  to test. Live requires the file and overrides RUN_MODE to live. Compose also
  sets DATA_DIR=/data, forwards DATABASE_URL (empty selects SQLite), sets internal SEARXNG_URL, and maps
  CONTAINER_MODEL_BASE_URL to MODEL_BASE_URL. Recreate after edits; a container
  restart retains its old environment. Make's own overrides come from its caller.
  Live starts (including backup/restore restarts and storage inspection) first run
  `scripts/compose_live.py`, which uses the local runner's literal `.env` grammar
  and lets the caller's `ARCHIVE_WALLET_PATH` win, even when empty. A non-empty
  wallet path selects `-f compose.wallet.yaml` after `LIVE_FILES`; the source file
  must exist before live startup. The wrapper never reads or prints its content.
  It requires Python 3, without third-party packages; test mode still needs only
  Docker and Make.
- **Tilt:** Compose loads `.env` for the search service. The local API calls
  `load_dotenv(override=False)`, so real environment variables win. Tilt restarts
  the API after `.env` changes. Its API/UI ports remain fixed at 2200/2201; use
  `make dev` for configurable local ports. Tilt always runs bundled search.
- **`make dev` / `scripts/run-local.sh`:** parses `./.env` as literal assignments
  and exports NUL-separated records without shell evaluation. It requires the file
  and accepts only the setting names documented in `.env.example` (upper-case;
  shell variables such as `PATH` or `OPTIND` are refused). Values must not contain
  `$` or a backslash, quoted or not, so Compose, python-dotenv and the runner read
  every value the same way. Like `load_dotenv(override=False)`, a variable the
  caller already set, even to an empty value, wins over the file: for example
  `RUN_MODE=test make dev`, `RUN_MODE=live make dev` or
  `KARAKUTU_API_PORT=3300 make dev`. It checks required names offline, starts bundled
  search only for live + searxng, and runs one loopback API and Vite. Ctrl-C
  terminates its children and stops bundled search; do not share those services
  with another mode. Vite's proxy defaults to the selected local API port.
  `scripts/run-local.sh --check --env-file <path>` only validates and prints a
  start plan; it does not run Docker, a model, the API or UI. Relative storage
  paths at runtime resolve from the checkout. Use plain `KEY=value` assignments
  and full-line comments. Keys must match `[A-Za-z_][A-Za-z0-9_]*`; quote values
  containing spaces, JSON or shell punctuation with single or double quotes.
  The runner strips the surrounding quotes and performs no expansion, command
  substitution or backslash escape processing. Embedded matching quotes and
  multiline values are rejected. Avoid interpolation syntax when sharing the file
  with Compose/python-dotenv, which support interpolation.
  Invalid lines fail with only their line number and key, never their value.
  To customize search ports also update SEARXNG_URL locally.
- **Plain uvicorn:** `uv run uvicorn audit.api:app --host 127.0.0.1 --port 2200
  --no-access-log` calls Settings.from_env, which uses
  `load_dotenv(override=False)`; real environment variables win. Start from the
  checkout so the intended file is found. Uvicorn's bind/port are command flags,
  not API settings. Run only one worker. Benchmark tooling also loads root
  `.env` with `override=False`.

## Variable inventory maintenance

The offline test in [`test_env_example.py`](../backend/tests/test_env_example.py)
collects Python env reads with AST, expands the MODEL_STEPS family, follows the
config helper names, and checks Compose interpolation, Make inputs, shell defaults,
Tilt reads and Vite process.env. It rejects missing/unused keys and unexplained
secrets. New env settings must be documented in both the example and this table.

## Full variable inventory: name → where read

This inventory covers the project inputs below. Names grouped on a line share the same
read locations; helper calls and the step-model family are resolved statically.
`api.py` consumes Settings, with no direct environment reads. Tilt delegates
loading to local Settings and Compose. Setup reads the template and the selected
target file; `env_requirements.py` checks their named values and the runner's
exported environment without printing them.

- `API_IMAGE`, `API_TOKEN`, `BACKUP_DIR`, `BACKUP_FILE`, `LIVE_FILES`, `PROJECT`,
  `START`, `TEST_PROJECT`, `VOLUME` → Makefile.
- `KARAKUTU_WEB_PORT` → Makefile, backend/audit/config.py, compose.yaml,
  scripts/run-local.sh.
- `ALLOWED_HOSTS`, `ALLOWED_ORIGINS`, `ARCHIVE_BASE_URL`, `ARCHIVE_WALLET_JSON`,
  `ARCHIVE_WALLET_PATH`, `DATABASE_URL`, `DATA_DIR`, `LIVE_INGESTION_ENABLED`,
  `MAX_CONCURRENT_RUNS`, `MODEL_API_KEY`, `MODEL_BASE_URL`, `MODEL_ENABLED`,
  `MODEL_MAX_TOKENS`, `MODEL_NAME`, `MODEL_NAME_CLASSIFICATION`,
  `MODEL_NAME_RELATED_REMOVALS`, `MODEL_NAME_RELEVANCE`, `MODEL_NAME_SOURCE`,
  `MODEL_NAME_WEB_SEARCH`, `MODEL_PROVIDER`, `MODEL_RESPONSE_FORMAT`,
  `MODEL_TEMPERATURE`, `MODEL_THINKING_CONTROL`, `MODEL_TIMEOUT`, `SEARXNG_URL`
  → backend/audit/config.py.
- `KARAKUTU_BIND_ADDRESS` → backend/audit/config.py, compose.yaml.
- `API_AUTH_TOKEN` → backend/audit/config.py, scripts/benchmark.py.
- `RUN_MODE`, `SEARCH_PROVIDER` → backend/audit/config.py, scripts/run-local.sh.
- `KARAKUTU_SEARCH_PORT`, `SEARXNG_SECRET` → compose.search.yaml.
- `DATABASE_URL` → compose.yaml and backend/audit/config.py.
- `ARCHIVE_WALLET_PATH` → scripts/compose_live.py and compose.wallet.yaml;
  backend/audit/config.py reads the fixed container path under Docker.
- `POSTGRES_PASSWORD` → compose.postgres.yaml.
- `TEST_POSTGRES_URL` → backend/tests/conftest.py (test-only inventory exception).
- `CONTAINER_MODEL_BASE_URL` → compose.yaml.
- `KARAKUTU_API_PORT` → compose.yaml, scripts/run-local.sh.
- `SMOKE_API_PORT`, `SMOKE_KEEP_IMAGES`, `SMOKE_PROJECT`, `SMOKE_REF`,
  `SMOKE_SEARCH_PORT`, `SMOKE_UPGRADE_FROM`, `SMOKE_WEB_PORT`
  → scripts/docker_smoke.sh.
- `KARAKUTU_API_PROXY` → scripts/run-local.sh, web/vite.config.ts.
- `SMOKE_API_IMAGE`, `SMOKE_API_TOKEN`, `SMOKE_DIR`, `SMOKE_WEB_IMAGE`
  → scripts/smoke/compose.smoke.yaml.
- `FAKE_MODEL_PORT`, `FAKE_MODEL_SLOW_SECONDS` → scripts/smoke/fake_model.py.

## Database URLs

SQLite remains the default: `sqlite:///data/audit.sqlite3`. Live mode also accepts
`postgresql+psycopg://audit:<password>@db.example.invalid:5432/audit?sslmode=require`.
Percent-encode password punctuation in URLs. Remote PostgreSQL must use SSL;
`sslmode=verify-full&sslrootcert=<certificate-path>` also verifies the server identity.
The hosting provider is undecided. Keep the URL in protected runtime configuration,
never logs, shell history or shared Compose configuration output.

PostgreSQL uses a pool of five connections with five overflow connections, pre-ping,
30-minute recycling and a ten-second connection timeout. Startup connection errors
name only the backend, host and database. Test mode always uses its own SQLite file;
the demo seeder refuses PostgreSQL. PostgreSQL does not enable multiple API processes.

`TEST_POSTGRES_URL` is a test-only admin URL supplied by `make test-postgres`, not an
application setting; leave it empty in operator `.env`. The fixture creates and drops
one database per test. The default pytest run uses temporary SQLite files offline.

## Archive read state

`ARCHIVE_WALLET_PATH` names a host file outside the repository. `make live`
mounts it read-only through `compose.wallet.yaml`; the API environment contains
only `/run/secrets/archive_wallet`. Production can supply the same container path
from the host secret store. Inline `ARCHIVE_WALLET_JSON` retains precedence.
On Linux the file must be readable by container UID 10001; see
[the ownership note](getting-started.md#docker-live-with-archive-reads).

`GET /api/status` returns `archive`, a fixed enum containing no paths, wallet
content, tokens, URLs or upstream messages. `archive_reads_enabled` remains a
configuration boolean for compatibility, even if reads fail. Settings shows the
state in plain words. The state is process-local, describing the latest completed
archive call; it is not a readiness gate or a periodic health probe.

| State | Meaning |
| --- | --- |
| `disabled` | No archive base URL (also always in test mode). |
| `unchecked` | Configured with no call outcome yet; offline wallet validation passed if configured. |
| `ok` | The latest archive GET returned successful JSON. |
| `wallet_missing` | The configured wallet file does not exist. |
| `wallet_unreadable` | Wallet cannot be opened or parsed into a private RSA key, including invalid inline JSON. |
| `auth_unavailable` | Wallet loaded, but obtaining or refreshing the read token failed for another reason (for example an unexpected sign-in response). |
| `unreachable` | Network failure, 5xx, invalid JSON or another unsuccessful HTTP response, including during sign-in. |
| `rejected` | Archive GET returned 401/403 after the refresh/retry, refused it while refresh was cooling down, or sign-in returned 401/403 (for example a wallet that is not allowed). |

Wallet failures are checked offline at client creation and retried on calls.
Valid keys and read tokens retain their existing process-local caches; restart to
load rotated key material. Startup catalog calls, imports, workflow reads and
silent lazy enrichment all update the same state. A later successful call changes
a failure to `ok`. A sign-in that cannot reach the archive (network error or 5xx)
is `unreachable` and a refused sign-in (401/403) is `rejected`, so the state names
the actual cause. Public unauthenticated
401/403 responses also use `rejected`. Import errors use `not_configured` instead
of `disabled`, retaining HTTP 409 and the `archive_unavailable` code.
