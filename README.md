# Karakutu Audit Service

A FastAPI service that triages **removed news articles** for the [Karakutu](https://karakutu.xyz)
censorship archive. For one removed article it classifies the text, looks for a relocated copy,
screens it for public interest, checks whether other articles on the same subject vanished too,
probes the source the article credits, and searches the open web for the story. It can also run
as a back-end worker for the archive: one authenticated call with an article id queues the audit,
and the service writes the IPTC classification and one discussion comment back.

The output is a **verdict plus the evidence behind it, never a finding of censorship.** Deciding
that something *was* censored is a human judgement on independent evidence. The strongest thing
this service says is `POSSIBLE_CENSORSHIP`, which means *"a human should look at this one"*.

Licensed under [Apache-2.0](LICENSE). Version 1.0.0; see the [changelog](CHANGELOG.md).

## Contents

- [How it works](#how-it-works) · [The guardrail](#the-guardrail)
- [Quick start](#quick-start) · [Docker Compose](#docker-compose) · [Going live](#going-live)
- [API](#api) · [Documentation](#documentation) · [Development](#development) ·
  [Known limits](#known-limits)

## How it works

Every audit runs six steps in a fixed order. The response always contains six `StageResult`
entries; a step that did not run comes back with `skipped: true` and a reason.

| # | Step | What it does | Can end the audit? |
|---|------|--------------|--------------------|
| 1 | `classification` | IPTC media topics (up to 5, ranked), genre and geographic scope | No: metadata only |
| 2 | `similarity` | Asks the archive's similarity index for candidates, fetches them and **measures** how much of the text they contain | Yes: a measured republication ends it as `MOVED` |
| 3 | `relevance` | Public-interest screen | Yes: out of scope ends it as `LIKELY_LOW_PUBLIC_INTEREST`, unless step 4 finds a pattern |
| 4 | `related_removals` | Did other articles on the same subject disappear from the archive within ±14 days? | No: sets the `RELATED_REMOVALS` flag, which keeps an out-of-scope audit going |
| 5 | `source` | Probes the removed URL, then finds and probes the outlet or agency the text credits | No: a dead cited source sets the `SOURCE_DEAD` flag |
| 6 | `web_search` | Search passes, fetch, judge, measure | Skipped when step 5 found the article live at its own URL |

Plain code then turns the evidence into one verdict:

| Verdict | Meaning |
|---------|---------|
| `MOVED` | Measured republication: a live same-publisher copy contains the original's text |
| `LIKELY_MOVED` | A same-publisher copy with a partial or unmeasurable textual fit |
| `FOUND_ONLINE` | The story is live at another outlet. Not exculpatory: this publisher still removed its copy |
| `NOT_REMOVED` | The removed URL answers with its text today. Descriptive, not exculpatory |
| `POSSIBLE_CENSORSHIP` | A review flag, not a finding: a demonstrably dead copy of the same story, or a relocation that was measurably hollowed out |
| `LIKELY_LOW_PUBLIC_INTEREST` | Step 3 screened it out |
| `INCONCLUSIVE` | Nothing was established, including when the search itself failed |
| `AUDIT_FAILED` | Not a result. The model failed every attempt; the article must be audited again |

Each step's module (`src/workflow/<step>.py`) documents its thresholds and the measurements
behind them, next to the constants.

## The guardrail

The service may raise a flag; it never makes a finding. The code enforces this in five ways:

- **The model cannot choose a verdict.** It reports what pages say and which links are dead;
  one function in plain code picks the verdict from that evidence.
- **Only a dead link (HTTP 404/410) counts as evidence of removal.** A 403 means the bot was
  blocked, an unreachable host is a network failure, and a missing PDF is not a deleted article.
- **Relocation is decided by arithmetic.** A copy that measurably contains the original's text
  cannot have been gutted, whatever the model claims.
- **Every cited URL was actually fetched.** Model-written URLs are matched back to fetched pages
  or dropped.
- **Nothing is written from a failed audit**, and the automatic write path is off until an
  operator turns it on (see [Shadow mode](#shadow-mode)).

## Quick start

Requires **Python 3.11** (the pinned `pydantic-ai` version is exercised only there).

```bash
git clone https://github.com/karakutu-xyz/karakutu-ai-audit.git && cd karakutu-ai-audit
python3.11 -m venv .venv311
.venv311/bin/pip install -r requirements.txt
cp .env.example .env            # optional: the defaults work
.venv311/bin/uvicorn src.api.app:app --reload --port 2200
```

`./run_local.sh` does the same with a `.venv` directory. Open <http://localhost:2200/docs> for the
interactive API.

**With no configuration, the output is synthetic.** The default model is pydantic-ai's
`TestModel` and the default search provider returns three canned `example.com` URLs, so the
service boots and the test suite runs with no secrets and no network. The service logs a
`SYNTHETIC CONFIGURATION` warning at startup while this is the case. For real output, see
[Going live](#going-live).

```bash
curl -sS localhost:2200/health
# {"status":"ok","service":"karakutu-audit"}

curl -sS -X POST localhost:2200/api/audit -H 'Content-Type: application/json' -d '{
  "removed_article": {
    "url": "https://example.com/news/removed-piece",
    "title": "City council votes on transit budget",
    "content": "The city council approved a transit budget after a contentious debate...",
    "published_at": "2026-04-20T09:00:00Z"
  }
}'
```

## Docker Compose

The image runs as an unprivileged user with a read-only root filesystem. The job store lives
on a named volume mounted at `/data`. Ports are bound to loopback only.

```bash
cp .env.example .env                                # then edit it
docker compose up -d --build                        # the service only
docker compose --profile searxng up -d --build      # the service plus a self-hosted SearXNG
```

Two `.env` keys are read by Compose itself, not by the service:

| Key | Purpose |
|-----|---------|
| `KARAKUTU_WALLET_FILE` | Host path of the archive wallet keyfile. Compose mounts it read-only at `/run/secrets/karakutu-wallet.json` and points `KARAKUTU_WALLET_PATH` at it. If it is unset, `/dev/null` is mounted and the archive client stays disabled. |
| `SEARXNG_SECRET` | SearXNG's server secret, required for the `searxng` profile. Generate one with `openssl rand -hex 32`. SearXNG refuses to start without it. |

Inside Compose the service always reaches SearXNG at `http://searxng:8080` (Compose overrides
`SEARXNG_URL`). Compose v2.24 or newer is required for the optional `.env` file.

## Going live

Real audits need all three of the following:

1. **A real model.** Set `MODEL_ENABLED=true`, `MODEL_BASE_URL` and `MODEL_NAME`. Any
   OpenAI-compatible chat-completions server that supports **tool calling** can be used, local
   or hosted. Every call has a `MODEL_REQUEST_TIMEOUT` (180 s), an output cap of
   `MODEL_MAX_TOKENS` (4096) and no client retries. Thinking is switched off per
   `MODEL_THINKING_CONTROL`: the default sends `chat_template_kwargs.enable_thinking=false` and
   `reasoning_effort: "none"`; use `reasoning_effort` for servers that reject the first field, or
   `none` to send neither. The health check is `GET {MODEL_BASE_URL}/models` with
   `MODEL_API_KEY` as a bearer token. `.env.example` has a Scaleway example. **Changing the model
   changes results**; compare against the reference model before you switch.
2. **Real search.** Set `WEB_SEARCH_PROVIDER=searxng` and run SearXNG (the Compose profile
   above). `searxng/settings.yml` must keep `json` in `search.formats`, or every query fails and
   step 6 sees no results.
3. **An archive wallet.** This is an Arweave keyfile that the archive has whitelisted, set with
   `KARAKUTU_WALLET_PATH` (a file) or `KARAKUTU_WALLET_JSON` (the content, from a secret
   manager). Without one, the archive client is disabled: step 2 reports `available: false`, step
   1 falls back to the cached IPTC vocabulary, and nothing can be written.

`.env.lmstudio` is a ready-made profile for a local LM Studio and SearXNG. It is the reference
profile the pipeline is calibrated against (`google/gemma-4-12b-qat`).

**Set `API_AUTH_TOKEN` as soon as any of the three is real.** The service fails closed: with a
real model, a non-stub search provider or a wallet configured and no token, it refuses to boot,
because an open `/api/audit` would spend your model budget and read the archive with your
wallet for anyone who reaches the port. `ALLOW_UNAUTHENTICATED_AUDIT=true` overrides this for
an isolated local run only.

### Shadow mode

With `ARCHIVE_WRITES_ENABLED=false` (the default) every article job is a dry run: it runs the
whole flow, stores the rendered comment in the job, and writes nothing. Even with writes on, a
job is forced to a dry run (and lists why in `write_blockers`) unless the model is real, search
is not the stub, the IPTC catalog is loaded and the wallet is present; and a result produced
while the archive was unreachable is never written. Review dry-run jobs with
`GET /api/v1/jobs/{job_id}?include=result` before turning writes on. What a job writes (IPTC tags,
never over a human's, and one discussion comment, only while the audit has none) is fixed in
code.

## API

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `POST` | `/api/audit` | Bearer token if `API_AUTH_TOKEN` is set | Run the six-step audit on an article you supply; returns the verdict synchronously |
| `POST` | `/api/v1/articles/{article_id}/audit` | Bearer | Queue a job that audits one archive article and writes the result back (`?dry_run=true` writes nothing) |
| `GET` | `/api/v1/jobs/{job_id}` | Bearer | One job's status and outcome (`?include=result` adds the run record and the comment text) |
| `GET` | `/api/v1/articles/{article_id}/jobs/latest` | Bearer | The article's latest job |
| `GET` | `/health` | none | Liveness: touches no model, search backend or archive |
| `GET` | `/ready` | none (details: bearer) | Readiness: checks the store, wallet, archive, model, search, catalog and worker; returns 503 when not ready. Anonymous callers get the status only; the checks and write posture need `Authorization: Bearer <API_AUTH_TOKEN>` |
| `GET` | `/docs` | none | Swagger UI |

"Bearer" is `Authorization: Bearer <API_AUTH_TOKEN>` (at least 32 characters). The `/api/v1`
routes work only while `API_AUTH_TOKEN` is set; otherwise each answers 404 and no job worker runs.
Every
response carries an `X-Correlation-ID` header. The service honours an incoming
`X-Correlation-ID` (up to 64 printable characters).

[`karakutu-audit-api-contract.yml`](karakutu-audit-api-contract.yml) (OpenAPI 3.0.3) is the
source of truth for request and response shapes. A test fails the build if the Pydantic models
drift from it.

On `/api/audit`, `similar_articles` is accepted for backwards compatibility but ignored, because
step 2 asks the archive instead. Any `url`, `title` or `content` field of 2,000,000 characters or
more is refused with 413.

## Documentation

| File | For |
|------|-----|
| [`.env.example`](.env.example) | Every setting, its default and why |
| [`karakutu-audit-api-contract.yml`](karakutu-audit-api-contract.yml) | Request and response shapes (OpenAPI 3.0.3) |
| [AGENTS.md](AGENTS.md) | The hard rules for contributors and coding agents, commands, layout, gotchas |
| [CONTRIBUTING](CONTRIBUTING.md) · [SECURITY](SECURITY.md) · [CODE_OF_CONDUCT](CODE_OF_CONDUCT.md) | Contributors |

## Development

```bash
.venv311/bin/pip install -r requirements-dev.txt
.venv311/bin/python -m pytest -q        # no network, model, wallet or .env needed
.venv311/bin/ruff check .
```

The suite has to stay runnable with no `.env`, no wallet, no model server and no network, and
it contains no real archive data: fixtures are synthetic (see "How the suite is built" in
[CONTRIBUTING.md](CONTRIBUTING.md)). Read CONTRIBUTING.md and [AGENTS.md](AGENTS.md) (the hard
rules) before changing pipeline code. Prompt quality is not asserted in CI; the stage tests are
structural.

## Known limits

- **Run one replica.** The job queue runs in process, and the SQLite store and the per-audit
  write ledger assume a single writer.
- **"Not found" does not raise the review flag.** With current search recall, failing to find a
  story measures the search's misses, not the story's absence.
- **Finding the exact relocated URL is still an open problem.** Search misses some relocations
  that exist.
- **No metrics or tracing.** Logs are text lines with a correlation id; the job worker logs one
  JSON object per event.
