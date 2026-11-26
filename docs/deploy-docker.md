# Deploy with Docker

The hosting provider is not decided yet. Scaleway below is a worked
configuration example, not a hosting commitment. The complete variable table and
loading rules are in [Configuration](configuration.md).

## Server requirements

Use one Linux server with Docker Engine, Compose v2 (2.24.4 or newer), Python 3 and `make`. A modest
starting point is 2 CPUs, 4 GB RAM and 20 GB disk; adjust for traffic and retained data. SQLite
and one API process keep the local footprint modest, and the model runs remotely. Allow room
for Docker images, database growth and backups. Allow outbound HTTPS to the model provider and,
for live search, to the web. Put a TLS reverse proxy on the server.

## Production configuration

From the checkout, run `make env API_TOKEN=1` and edit the protected file. This
creates secrets only for a new file. An existing file is left unchanged; fill any
names setup reports. Production checklist:

- Set RUN_MODE=live, MODEL_ENABLED=true, MODEL_PROVIDER and MODEL_NAME; set
  MODEL_API_KEY if the provider needs authentication (hosted providers do).
  Set a reachable CONTAINER_MODEL_BASE_URL. Set
  MODEL_BASE_URL too if also using local tooling. Hosted profiles normally use
  MODEL_THINKING_CONTROL=none and MODEL_RESPONSE_FORMAT=auto.
- Set API_AUTH_TOKEN (at least 32 characters), ALLOWED_HOSTS to the proxy hostname
  and ALLOWED_ORIGINS to its TLS origin. Retain loopback KARAKUTU_BIND_ADDRESS.
- Set SEARCH_PROVIDER deliberately and generate SEARXNG_SECRET for bundled search.
  Compose overrides SEARXNG_URL to its internal service address.
- Leave LIVE_INGESTION_ENABLED=false unless API/webhook intake is authorized;
  manual article audits do not need it. Set
  ARCHIVE_BASE_URL and your own read wallet only if using archive evidence.
- Preserve the named /data volume, plan backups and run exactly one API process.

Keep secrets in the host's secret store/environment; if materializing `.env`, use
mode 600 and never commit it or bake it into images. Compose's `env_file` reads it
at runtime. Host secret-manager environment injection requires an explicit Compose
forwarding override or protected env file; see [Secrets](configuration.md#secrets).
For token rotation, replace API_AUTH_TOKEN, recreate the API and update clients.

For archive read authentication, materialize a wallet from the host's secret
store as a protected read-only file, outside the checkout. The secret provider
is undecided. Set the host `ARCHIVE_WALLET_PATH` for `make live`; its optional
`compose.wallet.yaml` mounts the file at `/run/secrets/archive_wallet`, which is
always the API's container path. A production orchestrator should supply the
same read-only container path. Never place wallet content in an image or an env
file in the repository. On Linux, keep the operator as owner and grant read access
to the container's group 10001 (`chgrp 10001`, mode 640); see [Docker archive reads](getting-started.md#from-test-mode-to-a-real-audit).
Check `/api/status`'s `archive` field and Settings after recreation. The compatible
`archive_reads_enabled` boolean only says reads are configured.

See [Configuration](configuration.md) for the full variable table and Compose overrides.

For a Scaleway example, provision one instance with persistent disk, Docker and a
TLS reverse proxy. In the Scaleway console choose your project
`<your-scaleway-project-id>`, create an inference API key and obtain its
OpenAI-compatible endpoint and chat model name. Store credentials in Scaleway
Secret Manager and materialize them through the host's runtime secret handling.
The project identifier belongs to provider provisioning; the API does not read a
Scaleway-specific project variable. Set only the inference variables it consumes:

```dotenv
MODEL_ENABLED=true
MODEL_PROVIDER=openai_compatible
MODEL_BASE_URL=https://inference.example/v1
CONTAINER_MODEL_BASE_URL=https://inference.example/v1
MODEL_NAME='<your-chat-model>'
MODEL_API_KEY='<your-scaleway-api-key>'
MODEL_THINKING_CONTROL=none
MODEL_RESPONSE_FORMAT=auto
```

A non-loopback `ALLOWED_HOSTS` entry or `KARAKUTU_BIND_ADDRESS` without a token stops the API
at startup. A configured token shorter than 32 characters also stops startup. After changing
`.env`, recreate containers with `make live`. Do not use a plain `docker compose … up -d`:
it omits `compose.wallet.yaml`, so a configured archive wallet would no longer be mounted
and archive reads would lose authentication. To call Compose directly, go through the same
wrapper, for example `python3 scripts/compose_live.py -p karakutu-ai-audit -f compose.yaml --
up -d`. `docker compose restart` does not apply changed environment values.

## TLS reverse proxy

Keep the default loopback bind. Configure DNS and a TLS reverse proxy on the same server to
forward to the UI port. The UI forwards `/api` to the API itself. Preserve the original Host
header, set `ALLOWED_HOSTS=audit.example.org` and
`ALLOWED_ORIGINS=https://audit.example.org`, and configure a strong `API_AUTH_TOKEN`.
Clients send `Authorization: Bearer <token>`; in the UI, enter the token in Connection settings.
Do not expose API port 2200 or SearXNG port 2202 publicly.

For example, a Caddy site configuration can be:

```caddy
audit.example.org {
    reverse_proxy 127.0.0.1:2201
}
```

Caddy passes the Host header and obtains a certificate for the name by default. Replace the
placeholder name with your own.

## Start, stop and status

```sh
make live
docker compose -p karakutu-ai-audit ps
curl -fsS http://127.0.0.1:2200/health
curl -fsS -o /dev/null http://127.0.0.1:2201/
make down
```

`make live` builds and starts the stack with Compose `--wait`, which waits until every service
is healthy. The API, the UI and SearXNG each have a Docker healthcheck (`docker compose ps` shows
the state); `/health` on the API also reports its mode.
Stopping the stack preserves the named volume. Stopping during a run marks it interrupted on
next startup; submit a new run to retry.

## Logs

```sh
make logs
```

This follows the API log. The application does not log article data, prompts or tokens. Stored
runs and workflow snapshots can contain sensitive content, so protect the database and backups.

## Backup and restore

These targets support SQLite only and refuse a PostgreSQL `DATABASE_URL` before stopping
the API. PostgreSQL backups and restores are the managed provider’s job; configure retention,
point-in-time recovery and restore drills with that provider. Preserve any local private
files separately.

Run these from the checkout. Both stop the API first and start it again afterwards, so SQLite is
never copied or replaced while it is being written; the whole volume, WAL sidecar files
included, is covered. They use the live stack's own API image, so nothing is downloaded. Replace
`<backup-dir>` with an existing, protected directory outside the checkout.

```sh
make backup BACKUP_DIR=<backup-dir>
```

This writes `karakutu-audit-data-<UTC timestamp>.tar.gz` into that directory and prints its path.

```sh
make restore BACKUP_FILE=<backup-dir>/karakutu-audit-data-<UTC timestamp>.tar.gz
```

Restore replaces **all** current volume contents with the backup and loses changes made since
then. It first unpacks the archive into a scratch directory and runs SQLite's integrity check on
the database in it; only a backup that passes replaces the data. The current contents are moved
aside, not deleted, and put back if copying fails. A refused or failed restore leaves the data as
it was and starts the API again on it (`scripts/volume_restore.py`).

Keep copies of the backup off the server. The targets assume the default Compose project name
(`karakutu-ai-audit`); the smoke suite runs both against its own stack.

## Upgrade and rollback

Take a backup (`make backup`) before upgrading. Record the current Git revision and
workflow ID, then update the checkout and rebuild:

```sh
git rev-parse HEAD
git pull
# Or check out a chosen release tag instead of pulling.
make live
curl -fsS http://127.0.0.1:2200/health
```

Check the current workflow version in the UI or query `GET /api/workflow` with the bearer token;
its `id` identifies the current snapshot. Startup applies pending migrations for the configured backend. Migrations
are forward-only, and older code refuses a database with a newer schema.

To roll back, restore first with the current checkout, whose `make restore` and image hold the
restore tooling, and leave the API stopped. Then switch to the previous revision and start it on
the restored data:

```sh
make restore BACKUP_FILE=<pre-upgrade backup> START=0
git checkout <previous revision or tag>
make live
```

Do not start the newer version on the restored data in between: it would migrate the schema
forward again, and the older code refuses a newer schema. Check health and the workflow again.
Restoring discards data written since the backup.

## Outbound fetches and bot challenges

The API image is built on Debian trixie (`python:3.11-slim-trixie`). Some publishers behind
Cloudflare answer the older bookworm image's OpenSSL 3.0 TLS handshake with a bot challenge
(HTTP 403, `cf-mitigated: challenge`) while serving the same request from a newer OpenSSL. Every page
of such an outlet then reads as unreadable inside the container only, and audits lose that
evidence. Keep the base image current. When a run shows a publisher's candidates as unreadable,
the reason on each candidate says why (for example `HTTP 403 (blocked/erroring, not gone)` or a
connection error class); a whole outlet with the same 403 points at the fetch environment, not
at the articles.

## One API process

The run queue is in process. Do not scale the API service or add worker processes: they cannot
coordinate the queue. `MAX_CONCURRENT_RUNS` controls parallel audits inside the one process.

## Verify a deployment build

```sh
make smoke
```

`make smoke` also calls `make test-postgres`, which checks the current checkout against
a throwaway PostgreSQL 17 container on a free loopback port. Its random password stays
in the script environment, its database is isolated per test, and an exit trap removes
the container. This pass needs the local uv test environment and the PostgreSQL image.

The Docker smoke suite clones committed `HEAD` into a temporary directory and builds without cache;
uncommitted changes are not tested. It exercises test mode, live mode with an offline fake
model, access controls, persistence, image hygiene and Compose configuration. Builds need image
and package registries; the live stage runs on an internal Docker network with no route out, so
no real model, search, archive or web page is reached. A run takes a few minutes. Every
project, volume and image it creates carries a random run ID, it refuses to start if those
names exist, and it removes only what it created.

Set `SMOKE_REF` to test another commit (its Makefile must support the `PROJECT` and
`TEST_PROJECT` overrides), `SMOKE_PROJECT` for the name prefix,
`SMOKE_API_PORT`, `SMOKE_WEB_PORT` and `SMOKE_SEARCH_PORT` for ports,
`SMOKE_UPGRADE_FROM` to test migration from an older commit, or `SMOKE_KEEP_IMAGES=1` to retain
built images. Defaults and details are in [`scripts/docker_smoke.sh`](../scripts/docker_smoke.sh).

## PostgreSQL storage

The database provider is undecided. Use any compatible managed PostgreSQL server and
set the live `DATABASE_URL` through protected runtime configuration, for example:

```text
postgresql+psycopg://audit:<password>@db.example.invalid:5432/audit?sslmode=require
```

Require SSL remotely. Prefer `sslmode=verify-full` with the provider's `sslrootcert`
when certificate verification is available. Percent-encode password punctuation in the
URL. Treat the whole URL as a secret; never paste effective Compose configuration into
logs or tickets. Recreate the API after changing it. The database role needs DDL rights
for startup migrations. PostgreSQL still requires exactly one API process.

For an optional local PostgreSQL service, set a generated `POSTGRES_PASSWORD` in the
protected `.env`, then run:

```sh
docker compose -p karakutu-ai-audit -f compose.yaml -f compose.postgres.yaml up -d --wait db
```

The service publishes only `127.0.0.1:5432`, retains a separate named volume, and is
unused by default `make test` and `make live`. Point container `DATABASE_URL` at
`postgresql+psycopg://audit:<password>@db:5432/audit` (local development only), set
`LIVE_FILES='-f compose.yaml -f compose.postgres.yaml'`, then use `make live`.
For a local host process use `127.0.0.1` instead of `db`. The local container does not
configure TLS; the remote SSL requirement still applies to managed servers.

### Move existing SQLite data

1. Stop intake and the API. Take a consistent SQLite backup and retain it for rollback.
   Upgrade the SQLite file with the current application first (schema version 5).
2. Provision an empty target database. Do not start the API against it yet. Supply source
   and target URLs from protected shell variables; avoid putting credentials in history.
3. Preview counts, then copy with the same URLs:

   ```sh
   uv run python scripts/migrate_sqlite_to_postgres.py "$source_url" "$target_url" --dry-run
   uv run python scripts/migrate_sqlite_to_postgres.py "$source_url" "$target_url"
   ```

   The script opens the source read-only, creates target schema through `Store`, copies in
   foreign-key order in one transaction, and verifies every table's row count and an
   order-independent SHA-256 (a hash of the sorted per-row hashes). JSON columns move as
   their exact stored text and are compared verbatim, so even a reordered key or a
   reformatted number fails verification; SQL NULL stays distinct from JSON null.
   Activation/assignment rows copy in SQLite
   insertion order so generated PostgreSQL `seq` preserves the active selections. Generated
   sequences are excluded from logical row hashes. Output contains table counts only.
   Dry run reads counts and checks target emptiness without creating its schema. Any row in
   any target application or unrelated table refuses the operation; the singleton migration
   version record is metadata. Failed copies roll back all copied rows, leaving empty schema.
   A successful copy cannot be rerun into the now populated target. If old rows hold NUL
   characters in text columns, which PostgreSQL text cannot store, the copy refuses and
   names each table, column and row count; rerun with `--strip-nul` to remove them from
   those text columns only. JSON columns keep NUL exactly. A test database or one
   holding demo data is never copied, and live startup on any backend refuses a database
   that holds demo data.
4. Set the protected `DATABASE_URL`, recreate the API, check health and saved history, and
   resume intake. Keep the SQLite backup until the move and provider backups are verified.
   Restoring the original configuration/file is a rollback only before accepting new writes;
   after that, reconcile new records before switching back.

Run `make test-postgres` to exercise store, API, article and copy tests against the throwaway
server. Default `uv run pytest -q` remains offline on SQLite.
