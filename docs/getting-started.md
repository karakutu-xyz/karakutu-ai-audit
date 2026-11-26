# Getting started

This guide takes you from a fresh clone to a running workbench: first test mode (demo data, no
model), then live mode (your own model and search), then the development setup. For how the
parts fit together, read [Architecture](architecture.md). Every variable and its source is in
[Configuration](configuration.md).

## Prerequisites

| To run | You need |
| --- | --- |
| Test mode in Docker | Docker with Compose 2.24 or newer and `make` |
| Live mode in Docker | The same, Python 3 and a reachable OpenAI-compatible chat model |
| Local development | Python 3.11, uv and Node 22+; Docker Compose for live search |
| Tilt hot reload | The local tools above, Docker Compose and Tilt |

## From test mode to a real audit

1. **Try test mode.** Clone the repository using the [README quick start](../README.md#quick-start-test-mode-in-5-minutes), then from the checkout run:

   ```sh
   make test
   ```

   Open http://localhost:2201. The **Test mode · demo data** pill marks the
   fictional demo. Its 28 runs cover verdicts and run states; no model, search,
   archive or website is called. Stop it before starting live mode:

   ```sh
   make down
   ```

2. **Load a model.** Load `google/gemma-4-12b-qat` in LM Studio and start its
   server on port 1234. Check the server's model list:

   ```sh
   curl http://127.0.0.1:1234/v1/models
   ```

   The example settings enable the model and set `MODEL_NAME=auto`. At startup,
   `auto` selects the first returned model ID that does not contain `embed` or
   `rerank`; it does not otherwise verify the model type. To pin the model, set
   `MODEL_NAME` to its exact ID from the list, for example
   `google/gemma-4-12b-qat`. Settings can also override models per step.

   Compose uses `CONTAINER_MODEL_BASE_URL`, defaulting to
   `http://host.docker.internal:1234/v1` when empty. LM Studio must accept
   connections from Docker; enable its local network serving if needed. Local
   `make dev` instead uses `MODEL_BASE_URL`, which defaults to
   `http://127.0.0.1:1234/v1`. For another OpenAI-compatible server use
   `MODEL_PROVIDER=openai_compatible`; see [Hosted model providers](#hosted-model-providers).
   After startup, `GET /api/status` reports `model_enabled` and `model_name`;
   this reports configuration, not ongoing model reachability.

3. **Configure and start live mode.** Create settings if needed:

   ```sh
   make env
   ```

   This creates `.env` with a random search secret only if the file does not
   already exist. An existing file is left unchanged. Fill in valid model,
   search and archive settings for your environment and keep the file private.
   With a filled, valid live `.env`, auditing imported articles works locally
   with no `API_AUTH_TOKEN` while the app is only reachable from this machine.

   A token of at least 32 characters is required whenever the app is reachable
   from other machines: a non-loopback `KARAKUTU_BIND_ADDRESS` or
   `ALLOWED_HOSTS` entry makes startup refuse an empty token. Generate one with
   `make env API_TOKEN=1` for a new file, or `openssl rand -hex 32` for an
   existing file, and put it on the `API_AUTH_TOKEN` line.
   Then run:

   ```sh
   make live
   ```

   Open http://localhost:2201. **Live** appears in the top bar. The API is at
   http://localhost:2200; SearXNG is at http://localhost:2202. All bind to
   loopback by default. `make live` rebuilds the images and recreates containers
   with changed `.env` values; `docker compose restart` does not reload them.
   `make logs` follows API logs and `make down` stops the stack.

4. **Enter the UI token if configured.** Skip this step for local use with no
   token. With authentication enabled the UI reports
   “Access requires an API token. Add it in Settings.” Open **Settings**, choose
   **Connection / API token**, paste the value into **API token (optional)**,
   and choose **Apply connection**. To display your local value for copying:

   ```sh
   grep '^API_AUTH_TOKEN=' .env
   ```

   Copy only the value after `=` and never share it. The UI keeps it in memory
   for this tab only; a reload or new tab needs it again. Browser storage would
   outlive the session and be readable by scripts on the page. API calls such
   as `GET /api/status` also need a bearer token when one is configured. Check
   the status there or see **Model**, **Search** and **Archive reads** in Settings.

5. **Import from the archive.** Set `ARCHIVE_BASE_URL` in `.env` to your
   read-only archive endpoint. If it requires wallet authentication, set
   `ARCHIVE_WALLET_PATH` to an absolute private keyfile path outside the checkout.
   `make live` checks that file and adds `compose.wallet.yaml` to mount it
   read-only; `ARCHIVE_WALLET_JSON` is an alternative and takes precedence.
   Archive reads do not write to the archive. Restart with `make live` after
   editing settings. On Linux, both the operator and container UID/group 10001
   must be able to read the wallet; do not make it world-readable. See
   [archive configuration](configuration.md#archive-read-state).

   In `GET /api/status` (with a bearer token when configured),
   `archive_reads_enabled` indicates that an endpoint is configured and
   `archive` reports its state, including `ok` or
   `unchecked`. **Settings** shows **Archive reads**. In **Articles**, set
   **Days** from 1 to 90 (default 30) and choose **Import from archive**. It
   imports articles from archive audits created in that period, initially as
   metadata. The control appears in live mode when archive reads are enabled.

   Intake from Karakutu will be by polling, which is not yet built. Use archive
   import to load saved articles for auditing now.

6. **Audit an article.** Open an imported article and choose **Run first audit**
   (or **Re-run audit**). If a token is configured, enter it under
   Connection settings as in step 4. Its text is loaded from the archive the
   first time it is opened or audited. If the read fails and no content is
   available, the audit returns HTTP 422 with `article_has_no_content`. A failed
   fill is tried once per article and URL per API process; restart after fixing
   archive access to retry. The audit is queued and the page updates as steps finish.

7. **Allow time.** The default `MAX_CONCURRENT_RUNS=1` runs one audit at a
   time in the single API process; its allowed range is 1 to 8. A 12B model on
   local hardware can take a few minutes per audit, depending on hardware and
   evidence.

8. **Read the result.** Review the verdict, findings and evidence on the article
   page. Choose **Open full run** to inspect **Workflow steps**, inputs, exact
   model messages and output, and record a review. A person decides; a missing
   search hit is never evidence of censorship. `GET /api/runs/{run_id}` returns
   the lean audit result with bounded evidence. `GET /api/runs/{run_id}/details`
   returns private review details, including input and step records with model
   calls. The article and full run pages use the details route. See
   [review a saved run](workflow.md#review-a-saved-run).

9. **Check search quality.** Bundled SearXNG uses the image's default engines
   plus mwmbl. Big providers such as Bing and Yandex stay off because queries
   describe removed news; see [Web search engines](workflow.md#web-search-engines).
   `GET /api/status` can report `search: degraded` when a query yields no usable
   hits and reports unresponsive engines. Settings shows **Search** as
   “SearXNG · reachable, but its search engines are not answering (blocked or
   rate-limited)”. Web evidence may be missing, which can leave more results
   `INCONCLUSIVE`. Retry after the engines recover.

10. **Run a second stack if needed.** Give it a distinct project and three
    unused ports, and use the same values when stopping it:

    ```sh
    make live PROJECT=second KARAKUTU_API_PORT=2300 KARAKUTU_WEB_PORT=2301 KARAKUTU_SEARCH_PORT=2302
    make down PROJECT=second
    ```

    Compose names the search container under that project and stores live data
    in `second_karakutu-audit-data`, separate from the default
    `karakutu-ai-audit_karakutu-audit-data`. The test project defaults to the
    live project name plus `-test`. `scripts/run-local.sh` always uses the
    default `karakutu-ai-audit` project for search, so use `make live PROJECT=...`
    for a second stack. Avoid port conflicts with Tilt and local processes.

11. **Troubleshoot.**

    - For unhealthy containers, inspect `docker compose -p <project> ps` and
      `docker logs <container>`. The default project is `karakutu-ai-audit`.
      A checkout created with a restrictive umask such as 077 can make copied
      files unreadable to the API image's non-root user. Clone again with a
      normal umask and move your private files across; do not loosen permissions
      on the old checkout, which also holds `.env` and `data/`:

      ```sh
      (umask 022 && git clone "$(git remote get-url origin)" ../karakutu-ai-audit-fresh)
      ```

      Then move `.env` (keep it mode 600) and any `data/` you need into the new
      checkout.
    - If model discovery fails or no chat model is loaded, check LM Studio,
      its model list and the appropriate model URL in step 2. A disabled model
      shows **Disabled** in Settings and produces `AUDIT_FAILED` runs; calls can
      also fail after startup even when status says it is enabled.
    - If every candidate page from one publisher reads as unreadable with
      `HTTP 403` inside Docker while the pages open normally in a browser, the
      publisher is refusing the API's fetches, not removing its articles. One
      known cause is an old API image: the image is now built on Debian trixie
      because the older bookworm image drew bot challenges from some publishers,
      so run `make live` again to rebuild it. If the 403s persist on the current
      image, the publisher is blocking this fetch environment itself. See
      [Outbound fetches and bot challenges](deploy-docker.md#outbound-fetches-and-bot-challenges).
    - For degraded search, see step 9. For an unreachable search service, check
      SearXNG and its configured URL; a refused request may mean JSON output is off.
    - An article audit 404 means the saved article was not found; refresh the
      Articles list. The token message is resolved in step 4. For HTTP 422
      `article_has_no_content`, fix archive access and retry as in steps 5–6.
    - If `make live` needs `.env`, run `make env`; if `make test` fails on
      `!reset`, update Docker Compose. A port in use may belong to another mode.
    - If `make dev` reports stale web or Python dependencies, run
      `npm --prefix web ci` or `uv sync --frozen` respectively. Prompt or model
      changes returning 403 require an API token. `untrusted_host` (400) means
      the host must be added to `ALLOWED_HOSTS`; non-loopback hosts need a token.
      To reset old demo data, run `make test-reset`, then `make test`.

### Batch benchmarks

Keep benchmark cases in the ignored file `data/benchmark/cases.jsonl`, one JSON object per
line: `removed_article: {url, title, content}`, plus optional `label` and `expected_verdict`.

```sh
uv run python scripts/benchmark.py data/benchmark/cases.jsonl
uv run python scripts/benchmark.py --demo   # two fictional smoke cases
uv run python scripts/benchmark.py --set verdict-quality
```

The script submits through the benchmark route only, waits for the results and writes a report
to `data/reports/`. Set `API_AUTH_TOKEN` in the environment if the API needs a token. Run the
same cases after a workflow change to compare versions by workflow ID.

The Turkish `verdict-quality` set has 15 invented cases: public affairs without
wrongdoing allegations, clear accountability or rights issues, private matters
of influential people, low-interest controls, and archive HTML hiding the prose.
Use a live-mode API with your model for these measurements; test mode gives
synthetic answers. The report measures verdict and relevance per pattern.
Relocation, rewrites, shared footers and unrelated cited sources use offline
page-pair tests in `backend/tests/test_verdict_patterns.py`; the API cannot
supply fake live pages.

Compare saved reports without network access (omit workflow options when each
report contains only one workflow):

```sh
uv run python scripts/benchmark_compare.py data/reports/before.json \
  data/reports/after.json --before-workflow OLD --after-workflow NEW \
  --markdown data/reports/comparison.md
```

The same report can serve both sides with different workflow IDs. Cases match by
label, using the latest run per label. `fixed` means the verdict now matches its
expectation; `broke` means it stopped matching. Cases present on only one side
are counted separately as `added` or `missing`. Metadata comes from the entry
paired with the selected run. If both sides disagree on metadata, the comparison
uses the before side's expectations for the table and totals, warns with the
label, and counts `metadata_conflicts`. The table also shows relevance outcomes
and per-side totals. Markdown is written only when requested.

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

After a pull, `make dev` first checks offline that both installs still match
`web/package-lock.json` and `uv.lock`. If not, it starts nothing and names the command to
run (`npm --prefix web ci` or `uv sync --frozen`).

Ctrl-C stops the children. For live integrations, load a chat model in LM Studio,
check the values setup lists, then run `RUN_MODE=live make dev`. API/UI ports use
KARAKUTU_API_PORT/KARAKUTU_WEB_PORT (2200/2201 by default), and Vite's API proxy
follows the API port. `scripts/run-local.sh --check` validates without starting
anything. See [how commands load settings](configuration.md#how-each-command-loads-env)
for Compose overrides, Tilt, and plain uvicorn.

## Reproduce this state

Follow [the ordered path](#from-test-mode-to-a-real-audit) for Docker test and
live modes. For local processes, follow [Development without Docker](#development-without-docker).
For a fictional live-model exercise, use **Benchmarks** and **New benchmark run**,
then **Use a synthetic example**. It remains a benchmark, not a live article audit.

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
