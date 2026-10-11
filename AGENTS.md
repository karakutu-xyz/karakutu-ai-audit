# karakutu-audit: contributor and agent guide

Read this before changing code. It holds the rules that are easy to break and the commands to
check them. [README.md](./README.md) explains the steps, verdicts and API;
[`.env.example`](./.env.example) documents every setting; [CONTRIBUTING.md](./CONTRIBUTING.md)
has setup and pull-request rules. The reasons behind a rule or threshold are recorded next to it
in code (module docstrings and the comments on each constant).

## Service identity

| | |
|---|---|
| Purpose | Audit a news article removed from a censorship archive in six steps and return evidence plus a verdict tier for a human auditor, never a finding of censorship. Optionally run the audit for the archive and write back IPTC tags and one discussion comment. |
| Stack | Python 3.11 (exactly), FastAPI 0.115, pydantic 2.10, **pydantic-ai 0.0.49** |
| Port | `2200` |
| Contract | [`karakutu-audit-api-contract.yml`](./karakutu-audit-api-contract.yml) (OpenAPI 3.0.3), the source of truth |
| Endpoints | `POST /api/audit`, `GET /health`, `GET /ready`, `POST /api/v1/articles/{article_id}/audit`, `GET /api/v1/jobs/{job_id}`, `GET /api/v1/articles/{article_id}/jobs/latest` |
| Persistence | None for `/api/audit`. Article jobs use a SQLite job store and apply ledger in `DATA_DIR`. Single replica only. |
| External | Karakutu Server API (Arweave-wallet JWT auth), an OpenAI-compatible model server, SearXNG |
| License | Apache-2.0 |

Six steps, always reported in this order (`src/workflow/pipeline.py::run_audit`):
`classification → similarity → relevance → related_removals → source → web_search`, then
`_decide`. Verdicts: `NOT_REMOVED | MOVED | LIKELY_MOVED | FOUND_ONLINE | POSSIBLE_CENSORSHIP |
INCONCLUSIVE | LIKELY_LOW_PUBLIC_INTEREST | AUDIT_FAILED`. Flags: `SOURCE_DEAD`,
`RELATED_REMOVALS`.

## Hard rules

1. **Results must not change unless that is the point of the change.** A result is the verdict,
   the stage outputs, the classification, the flags and the comment text. Do not edit
   `src/workflow/prompts.py`, stage logic, thresholds (`src/search/textmatch.py`, constants at the
   top of each `src/workflow/<step>.py`), `pipeline._decide`, `src/archive_flow/rules.py` or the
   rendering in `src/archive_flow/comments.py` as a side effect of another change. Refactors and
   moves are verbatim. `CONTENT_LIMIT`, `MIN_BODY_CHARS`, `ELIGIBLE_STATUSES` and the thresholds
   are code, never environment variables. The golden tests pin this:
   `tests/test_archive_flow.py` hashes the rendered comments for real records
   (`tests/fixtures/golden_records.json`, `golden_comments.json`) under every template and tag
   state, and `tests/test_verdict_tiers.py` pins every verdict tier. If one fails, your change
   altered what a reader sees: revert it, or make it a separate pull request that is labelled as a
   behaviour change, explains the measurement, and regenerates the fixtures.
2. **Contract first.** Change the YAML first, then `src/api/models.py`, then
   `tests/test_contract_alignment.py`, all in one change. Additive changes are preferred. Removing
   or renaming a field is a breaking change.
3. **The censorship guardrail: a flag, never a finding.** No model output type gets a verdict
   field, and `_decide` stays the only place a verdict is chosen, in plain code. Every
   `POSSIBLE_CENSORSHIP` text says "not a finding of censorship" and that a human must decide.
   `FOUND_ONLINE` and `NOT_REMOVED` are never worded as exculpatory. `AUDIT_FAILED` is not a result,
   and `/api/audit` must never answer 500 for a model failure. Only `dead` evidence (404/410 from
   an independent web hit or the cited source) may raise the flag. 403, unreachable hosts, PDFs,
   archive-sourced 404s, variants of the removed URL and SSRF refusals are `unreadable` or
   excluded. Do not loosen this.
4. **pydantic-ai is pinned to 0.0.49.** Use `result_type=`, `system_prompt=` and `result.data`,
   not the newer `output_type` / `instructions` / `result.output`. Do not "modernise" without
   deliberately bumping the pin.
5. **Settings are an import-time singleton.** `src/config/settings.py` evaluates class attributes
   when it is imported, so `monkeypatch.setenv` does nothing. In tests use `monkeypatch.setattr` on
   the singleton (job tests: `tests/_service_support.py::service_settings`); to test parsing
   itself, reload the module as `tests/test_settings.py::reload_settings` does. Library code calls `get_settings()` inside
   functions, not at module import. `workflow/` and `archive_flow/` read no settings; callers pass
   values in.
6. **Tests patch the stage modules**, never `Agent`s:
   `src.workflow.pipeline.{classification,similarity,relevance,related_removals,source,web_search}_stage.run`.
   `tests/conftest.py` stubs `src.api.app.load_catalog` (autouse). A test that needs a network, a
   wallet or a real model is wrong: stub it. The comment-template timestamp is patched at
   `src.archive_flow.comments._stamp`, not at the script shim.
7. **No raw exception text in responses, job records or logs we write.** The 500 body carries only
   the correlation id. Stage failures log `type(exc).__name__` (frames only at DEBUG). Job errors
   are `{code, fixed message}` from `src/jobs/models.py::ERROR_MESSAGES`. 401 and 422 bodies name
   codes and field locations, never submitted values. Never log a payload, an `Authorization`
   header, a token or wallet material. Every bearer check goes through
   `src/api/auth.py::bearer_matches` (constant time). The `/api/v1` routes answer 404 (disabled)
   → 401 → 422, in that order. `/ready` shows anonymous callers `{"status"}` only; the checks
   and write posture need the `API_AUTH_TOKEN` bearer token.
8. **SSRF guard on every outbound fetch.** Third-party pages are fetched only through
   `src/search/fetch.py`. It checks `urls.is_public_web_url` on the string and on every redirect,
   and its guarded network backend resolves DNS and connects only to public addresses. A refusal
   is `unreadable` and can never become `dead`. Do not add a raw `httpx` call to a caller-influenced
   URL anywhere else.
9. **Publishing fails closed.** The worker writes only when `jobs/gates.write_blockers` is empty
   (writes enabled, real model, search not `stub`, catalog loaded, wallet present) and
   `gates.archive_degradation(record)` is empty. Otherwise the job is a dry run or is withheld.
   With `API_AUTH_TOKEN` unset the article routes answer 404 and no worker starts; a token under
   32 characters refuses to boot. The worker acts only on the audit whose embedded `article.id`
   equals the requested article id (`GET /audits/by-article/{id}`). Template C, tags and
   comments are code constants in `jobs/worker.py`, not settings. The standing write rules
   (statuses 0/2/3, never overwrite a human's tags, comment only with zero discussions, no writes
   from `AUDIT_FAILED`/`ERROR`) live in `src/archive_flow/rules.py` and have no override flags.
10. **One archive client per process.** It is built in the lifespan (`src/runtime.py`) and stored
    on `app.state.karakutu_client`, never inside a stage. Article jobs get a per-job client that
    shares its auth object (`jobs.worker.default_job_client`). `/authenticate` allows 3 calls per
    minute per IP, and minting per request silently drops the step 2 relocation signal.
11. **One implementation for the CLI and the article jobs.** `scripts/open_audit/{run_open_cases,
    fetch_listed_cases,apply_results}.py` are thin CLIs over `src/archive_flow/`.
    `apply_rules.py` and `comment_templates.py` there are re-export shims. Change behaviour in
    `src/`, not in the scripts.

## Commands

```bash
python3.11 -m venv .venv311 && .venv311/bin/pip install -r requirements-dev.txt
.venv311/bin/python -m pytest -q          # whole suite; no network, wallet or model needed
.venv311/bin/ruff check .                 # lint (config in pyproject.toml)
.venv311/bin/uvicorn src.api.app:app --reload --port 2200    # local server, synthetic defaults
docker compose up --build                 # app only (synthetic search)
docker compose --profile searxng up -d --build   # app + SearXNG: needs SEARXNG_SECRET in .env
                                          # and WEB_SEARCH_PROVIDER=searxng for real search
```

With the defaults (`MODEL_ENABLED=false`, `WEB_SEARCH_PROVIDER=stub`) every audit is
**synthetic**: TestModel answers and three `example.com` hits. Startup logs say so. For real
output use the reference profile `.env.lmstudio` (local LM Studio + SearXNG).

Prompt quality is not asserted in CI. Under `TestModel` the stage tests are structural only.
Iterate on prompts by hand against a live model, and treat any prompt edit as a behaviour change
(rule 1).

## File layout

```
src/
  api/          app.py (lifespan, guards, /api/audit, /health, /ready), jobs.py (/api/v1 article
                routes), auth.py (bearer check), readiness.py, models.py (contract models), correlation.py
  jobs/         worker.py (JobWorker, by-article lookup), store.py (SQLite/memory JobStore), models.py
                (Job, outcomes, error codes), gates.py (write blockers, archive degradation)
  archive_flow/ intake.py (build_case, MIN_BODY_CHARS), runner.py (run_case, CONTENT_LIMIT),
                applier.py (apply_record, ledger), rules.py (standing write rules), comments.py (A–D)
  runtime.py    build_runtime(settings): provider → model → search → agents → archive client → catalog
  workflow/     pipeline.py (run_audit, _decide), classification.py, similarity.py, relevance.py,
                related_removals.py, source.py, web_search.py, prompts.py, agents.py
  search/       fetch.py (fetch gate, probe, SSRF backend), urls.py, textmatch.py, searxng.py,
                duckduckgo.py, stub.py, base.py, _common.py
  clients/      karakutu.py (archive API), auth.py (wallet → JWT), concepts.py (IPTC catalog), models.py
  providers/    local.py, lmstudio.py (bounded OpenAI-compatible model), served.py (served model ids), base.py
  config/       settings.py
scripts/open_audit/   operator CLIs (fetch cases, run, apply, verify, reports); dry-run by default
tests/                pytest suite; fixtures/ holds the golden records and comment hashes
searxng/              SearXNG config (keep `formats: [html, json]`)
```

`private/`, `runs/`, `docs/` (maintainers' local notes), `CLAUDE.md`, `.env*` (except
`.env.example` and `.env.lmstudio`) and `arweave-keyfile*.json` are gitignored. Never commit
them, and never link to them from tracked files. Keep the wallet keyfile **outside** the working tree (the reference profile reads
`${HOME}/.karakutu-audit/arweave-keyfile.json`; deployments use `KARAKUTU_WALLET_JSON` or a mounted
secret).

## Gotchas

- **`PLACEHOLDER_RELEVANCE_PROMPT` / `PLACEHOLDER_WEB_SEARCH_PROMPT`** are real prompts. The
  prefix is a misnomer, but the names are load-bearing imports. Do not rename them.
- **There is no `lmstudio` provider key.** Any OpenAI-compatible server (LM Studio, a hosted
  API) is a mode of `PROVIDER_TYPE=local` (`MODEL_ENABLED=true`). `openai` is imported lazily
  inside that branch. The legacy `LMSTUDIO_*` env names are aliases; settings attributes are
  `MODEL_*` only.
- **Hosted servers.** `MODEL_THINKING_CONTROL=reasoning_effort` for servers that reject
  `chat_template_kwargs` (Scaleway); pin `MODEL_TEMPERATURE`, since hosted defaults differ from
  the LM Studio preset (1.0). The record's `model.served` shows what actually answered.
- **`ddgs` is imported at module top** in `src/search/duckduckgo.py`, so `import src.search`
  needs it installed even with the `stub` provider.
- **The double-hash wallet signature.** The archive verifies `sha256(challenge)` passed as data
  with SHA-256 named again, so the client signs `SHA-256(challenge)`. Signing the raw challenge
  gives a silent 401 (`test_signature_matches_the_backends_double_hash_verification`).
- **SearXNG JSON API.** If search returns nothing, check `formats: [html, json]` in
  `searxng/settings.yml` first, then `SEARXNG_URL` (inside Compose use `http://searxng:8080`).
- **Classification suddenly takes 180 s per call?** Check `MODEL_THINKING_CONTROL` is not `none`
  and that the server honours `reasoning_effort: "none"` (`src/providers/lmstudio.py`).
- **`apply_results.py --template` defaults to `A`**; article jobs always use `C`.

## When you change something

- Update the nearest doc in the same change: the contract for shapes, `.env.example` (and the
  `Settings` comment) for a setting, the comment on the constant for a rule or threshold (with its
  measurement), README for anything a user or integrator sees, and this file for a rule.
- Add an entry under "Unreleased" in `CHANGELOG.md`.
- Report exactly what you ran (`pytest -q` count, `ruff check .`) in the pull request.
