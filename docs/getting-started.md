# Getting started

This guide takes you from a fresh clone to a running workbench: first test mode (demo data, no
model), then live mode (your own model and search), then the development setup. For how the
parts fit together, read [Architecture](architecture.md). Every variable and its source is in
[Configuration](configuration.md).

## Prerequisites

| To run | You need |
| --- | --- |
| Test mode in Docker | Docker with Compose 2.24 or newer, and `make` |
| Live mode in Docker | The same, plus an OpenAI-compatible chat model the containers can reach |
| Local development | Python 3.11, uv, Node 22+; Docker Compose for live bundled search |
| Tilt hot reload | The local tools above, Docker Compose and Tilt |

The model can be a local LM Studio server (the default settings expect
one on port 1234) or a hosted OpenAI-compatible provider.

Every mode serves the same addresses, all bound to loopback by default:

| Surface | Address |
| --- | --- |
| Review UI | http://localhost:2201 |
| API and its interactive docs | http://localhost:2200/docs |
| SearXNG (live mode and Tilt only) | http://localhost:2202 |
| Tilt dashboard (Tilt only) | http://localhost:10350 |

Because the ports are shared, run only one of `make test`, `make live`, `make dev` and
`tilt up` at a time.
`make test` and `make live` stop each other; stop Tilt or local `make dev` yourself
before starting another mode.

## Test mode: demo data, no model

Test mode runs the full UI and API against a fictional demo dataset. It calls no model, search
engine, archive or website, and needs no `.env`.

```sh
make test        # build and start; prints the address when ready
make down        # stop
make test-reset  # delete the test database; the next `make test` seeds it again
```

Open http://localhost:2201. The **Test mode · demo data** pill in the top bar shows you are in
test mode. The demo has 28 runs across three workflow versions: every verdict, a
queued, a running, an interrupted, a stopped (cancelled by a person) and several failed
runs, plus reviewer reactions. You can submit
new benchmarks too. The offline demo engine answers them with fabricated, deterministic steps;
they are demo output, not audits.

Without `make`, the same start command is:

```sh
docker compose -p karakutu-ai-audit-test -f compose.yaml -f compose.test.yaml up --build -d --wait karakutu-audit-api karakutu-audit-web
```

Without Docker, set `RUN_MODE=test` for the API process (see [Development without
Docker](#development-without-docker)). The database is then `data/demo/audit-test.sqlite3`.

## Live mode: your model and search

1. Create your settings once. This copies [`.env.example`](../.env.example) to an ignored
   `.env`, fills in a random SearXNG secret and creates `data/`:

   ```sh
   make env
   ```

   Without Python, copy `.env.example` to `.env` yourself and set `SEARXNG_SECRET` to a long
   random string.

2. Point it at a model. With LM Studio, load a chat model and start its server on port 1234;
   the defaults (`MODEL_NAME=auto`) pick the first chat model it reports. For a hosted
   provider, see [Hosted model providers](#hosted-model-providers).

3. Start everything (API, UI and SearXNG):

   ```sh
   make live
   make logs   # follow the API log
   make down   # stop
   ```

   The backend code, the Compose files and the SearXNG settings are built into the images, and
   `make live` and `make test` rebuild them on every start. If you start Compose another way
   after changing those files, add `--build`. `.env` values are read when the container is
   created: after editing `.env`, run `make live` again, which recreates the API container with
   the new values. `docker compose restart` keeps the old ones. Under Tilt or a local API
   process, restarting the process is enough.

The **Live** pill in the top bar shows you are in live mode. Runs and workflow snapshots live in the
Docker volume `karakutu-ai-audit_karakutu-audit-data`.
`docker compose -p karakutu-ai-audit down --volumes` deletes them for good.

Archive reads are off until you set `ARCHIVE_BASE_URL`. Without them, the similarity and
related-removals steps have no archive evidence and classification has no topic catalog; the
audit still runs. Live ingestion (`POST /api/audit` and the webhook) stays off until you set
`LIVE_INGESTION_ENABLED=true` and an `API_AUTH_TOKEN` of at least 32 characters.

A fresh live database has no articles. To list removed articles from the archive, import them
(`days` is 1 to 90):

```sh
curl -X POST 'http://localhost:2200/api/articles/import?days=30'
```

With `API_AUTH_TOKEN` set, every API call needs the token, and the interactive docs at
http://localhost:2200/docs cannot send it, so add the header (the variable holds your token):

```sh
curl -X POST 'http://localhost:2200/api/articles/import?days=30' \
  -H "Authorization: Bearer $API_AUTH_TOKEN"
```

Import only reads the archive and stores titles and links locally; an
article's text and removal date are read when you first open it. Auditing an imported article
(**Run first audit** or **Re-run audit**) is a live run, so it needs the same
`LIVE_INGESTION_ENABLED=true` and `API_AUTH_TOKEN` as live ingestion; without them, use
benchmarks.

If the archive needs read authentication, `ARCHIVE_WALLET_PATH` must be a path the API process
can open. `make live` does not mount files from your machine into the container, so a host
keyfile path makes every archive read fail and import answers `archive_unavailable`. Use
`RUN_MODE=live make dev` for archive reads with a keyfile.

### Your first review

1. Open **Benchmarks** and choose **New benchmark run**. Paste an article URL, title and text,
   or use **Use a synthetic example** for a made-up input. Add a label and an expected verdict
   if you like. The run opens on its own page, `/runs/<id>`.
2. Watch the six steps fill in. Open a step in **Workflow steps** to see its exact input and
   output JSON, its search and fetch evidence, and the exact model requests and responses.
3. Read the verdict and its findings, then record **Agree**, **Disagree** or **Needs review**
   with your name and a note. A reaction is appended to the run; it never changes the result.
4. Open **Saved workflow** to see the exact code, settings and prompts that produced the run.

Every screen has its own address: **Runs** (`/runs`) lists benchmark and live runs in one
ledger, with filters kept in the address so you can bookmark them; **Settings** (`/settings`)
holds the API token, the service details and the current workflow snapshot. The **Articles**
pages list demo, benchmark and imported live articles with audit history.

More on reviewing in [the workflow guide](workflow.md#review-a-saved-run).

### Batch benchmarks

Keep benchmark cases in the ignored file `data/benchmark/cases.jsonl`, one JSON object per
line: `removed_article: {url, title, content}`, plus optional `label` and `expected_verdict`.

```sh
uv run python scripts/benchmark.py data/benchmark/cases.jsonl
uv run python scripts/benchmark.py --demo   # two fictional smoke cases against your real model
```

The script submits through the benchmark route only, waits for the results and writes a report
to `data/reports/`. Set `API_AUTH_TOKEN` in the environment if the API needs a token. Run the
same cases after a workflow change to compare versions by workflow ID.

## Development with Tilt

Tilt gives hot reload: it runs SearXNG in Docker and the API and UI as local processes.

```sh
make env
uv sync --frozen
npm --prefix web ci
tilt up
```

Tilt restarts the API when backend code or `.env` changes. The Tilt dashboard is at
http://localhost:10350. `RUN_MODE=test tilt up` starts the API in test mode instead.

A parent Tiltfile can load [`Tiltfile.stack`](../Tiltfile.stack) and call
`karakutu_ai_audit_stack(<path to this checkout>)`.

### Development without Docker

The API and UI run locally; only bundled live search uses Docker. Install dependencies once:

```sh
uv sync --frozen
npm --prefix web ci
make env
RUN_MODE=test make dev
```

Ctrl-C stops the children. For live integrations, load a chat model in LM Studio,
check the values setup lists, then run `RUN_MODE=live make dev`. API/UI ports use
KARAKUTU_API_PORT/KARAKUTU_WEB_PORT (2200/2201 by default), and Vite's API proxy
follows the API port. `scripts/run-local.sh --check` validates without starting
anything. See [how commands load settings](configuration.md#how-each-command-loads-env)
for Compose overrides, Tilt, and plain uvicorn.

## Reproduce this state

From a fresh clone, use Docker Compose 2.24.4+, make, Python 3.11 and uv, and Node
22+. Live mode additionally needs a reachable LM Studio chat server or hosted
provider. Install local dependencies with `uv sync --frozen` and
`npm --prefix web ci`, then create settings with `make env`. No model or credentials
are required for the offline demo.

1. Run `make test` for the Docker demo. `GET http://127.0.0.1:2200/health` should
   report mode `test`. Open http://localhost:2201/articles, select **Not audited**,
   open a demo article and choose **Run first audit**. Its saved steps are synthetic.
2. Stop Docker with `make down`. Run `RUN_MODE=test make dev` for the same demo
   behavior using local API/UI processes. Verify health and Articles again; local
   demo storage is separate from the Compose volume. Ctrl-C stops local processes.
   Alternatively set RUN_MODE=test in `.env`, then use `make dev`.
3. For live mode, prepare LM Studio:
   - Download a chat model supported by your local server.
   - In the Developer tab, start the server on port 1234 (the default) and turn on
     just-in-time model loading, so the model loads on the first request.
   - `curl http://127.0.0.1:1234/v1/models` should list the model.
   - In `.env`, keep `MODEL_BASE_URL=http://127.0.0.1:1234/v1` and `MODEL_NAME=auto`.
     `auto` picks the first chat model the server lists; to pin one, set
     `MODEL_NAME` to its exact identifier from that list. Startup fails with "No chat
     model is loaded" if the list is empty.
   - Before `make live` (Docker), check from the host that
     `curl http://127.0.0.1:1234/v1/models` lists the model. Containers reach the
     host at `http://host.docker.internal:1234/v1`, the default when
     `CONTAINER_MODEL_BASE_URL` is empty. Turn on LM Studio's "Serve on Local
     Network" so containers can connect, or set `CONTAINER_MODEL_BASE_URL` to an
     address the container can reach.
   - Fill any other names `make env` reported as unset. Keep archive and live
     ingestion disabled unless you need them.
4. Run `make live`, or `RUN_MODE=live make dev` for local processes. Check `/health`
   for `live` and `/api/status` for model/search/archive/live capabilities. If a
   token is configured, enter it in Connection settings before API reads.
   If the API container exits or keeps restarting, run `make logs` and look for
   "Model discovery failed" or "No chat model is loaded". Check the loaded model,
   turn on LM Studio's "Serve on Local Network", or set `CONTAINER_MODEL_BASE_URL`
   to an address the container can reach. `/api/status` reports configuration
   capabilities after startup; it does not report model reachability.
5. Open **Benchmarks**, choose **New benchmark run**, then **Use a synthetic
   example**. Submit that fictional benchmark and inspect its steps. It uses the
   live model while staying in the benchmark category. No archive writes occur.

The complete settings reference is [Configuration](configuration.md); the
production checklist is in [Docker deployment](deploy-docker.md).

## Settings

[Configuration](configuration.md) and [`.env.example`](../.env.example) explain all
variables, defaults and requirements. With no file the API defaults to live with
model and search disabled; the example enables them for local live use. Test mode
ignores all integration settings. Compose always selects test/live explicitly.

### Hosted model providers

Any OpenAI-compatible Chat Completions endpoint works, for example Scaleway Generative APIs.
Change only `.env`:

```sh
MODEL_ENABLED=true
MODEL_PROVIDER=openai_compatible
MODEL_BASE_URL=https://inference.example/v1
CONTAINER_MODEL_BASE_URL=https://inference.example/v1
MODEL_NAME='<provider-chat-model>'
MODEL_API_KEY='<your-provider-key>'
MODEL_THINKING_CONTROL=none
MODEL_RESPONSE_FORMAT=auto
```

Set `MODEL_RESPONSE_FORMAT=auto` (also the hosted profile's default when omitted):
it asks for a JSON schema first and falls
back to plain JSON mode once if the provider rejects that. The details are in
[the workflow guide](workflow.md#provider-profiles-and-step-models).

### Remote access

For a single-server setup, see [Docker deployment](deploy-docker.md).

By default everything listens on loopback only. To serve the workbench to other machines, put
it behind a TLS reverse proxy and set `KARAKUTU_BIND_ADDRESS`, `ALLOWED_HOSTS`,
`ALLOWED_ORIGINS` and a strong `API_AUTH_TOKEN`. The rules are in
[Privacy and access](data-and-api.md#privacy-and-access). Run exactly one API process; the run
queue lives in memory.

## Common problems

**The API will not start: "No chat model is loaded".** `MODEL_NAME=auto` found a model server
with no chat model loaded. Load a chat model in LM Studio, or set `MODEL_NAME` to a model the
server has. In Docker the API keeps restarting until this is fixed; `make logs` or
`docker compose logs karakutu-audit-api` shows the message.

**The API will not start: "Model discovery failed".** The model server did not answer. Check
`MODEL_BASE_URL` (Tilt and local) or `CONTAINER_MODEL_BASE_URL` (Docker). A model server on
your machine must accept connections from Docker, not only from `127.0.0.1`.

**Every run ends `AUDIT_FAILED`.** Read the run's findings. "Model is disabled; the audit did not
run" means there is no `.env` or `MODEL_ENABLED=false`; the first step then reads "Stage did not
complete" and the other five are skipped. For other failures, open the failed step: it keeps the
model's raw response, so you can see what went wrong.

**`make live` says "live mode needs .env".** Run `make env` or copy
`.env.example` to `.env`.

**`make test` fails on `!reset`.** Your Docker Compose is older than 2.24. Update it.

**A port is already in use.** Another mode or Tilt is still running. Run `make down` and stop
Tilt.

**The UI says "Access requires an API token".** `API_AUTH_TOKEN` is set. Enter the token in
**Settings**; the UI keeps it in memory only.

**Prompt or model changes return 403.** They always need `API_AUTH_TOKEN`, even on loopback.

**The browser gets `untrusted_host` (400).** You opened the UI under a host name that is not in
`ALLOWED_HOSTS`. Add it (a non-loopback host also needs `API_AUTH_TOKEN`).

**Test mode shows old demo data.** `make test-reset`, then `make test`.
