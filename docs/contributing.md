# Contributing

Thank you for helping. Read [Architecture](architecture.md) and [the workflow guide](workflow.md)
first: most changes touch a rule that the docs explain.

## Ground rules

- **No result is a finding of censorship.** Keep that true in code, prompts, UI text and docs.
  `POSSIBLE_CENSORSHIP` nominates an article for human review; `FOUND_ONLINE`, `MOVED` and
  `NOT_REMOVED` never excuse a removal. A model failure is `AUDIT_FAILED`, never a quiet
  negative result.
- **No real data, ever.** Do not commit real article text, headlines, URLs, people's names,
  personal data, datasets, databases, reports or secrets: not in source, tests, prompts, docs or
  commit messages. Make up test inputs inline and use reserved host names such as
  `news.example` or `site.test`. Private material belongs in the ignored `data/` folder.
- **Models describe, code decides.** Verdicts come from measured evidence in
  [`pipeline.py`](../backend/audit/workflow/pipeline.py). Do not let model output pick a
  verdict.
- **No archive writes.** The archive client is read-only and stays that way.
- **History is immutable.** Never change a saved run's input or a workflow snapshot.

## Set up

You need Python 3.11, uv and Node 22+. Docker is only needed to run the stack.

```sh
uv sync --frozen
npm --prefix web ci
```

To run the app while you work, see [Getting started](getting-started.md): `make test` for the
demo, `tilt up` for hot reload.

## The six checks

Run all six before you open a pull request. They need no network, model or private data.

```sh
uv run pytest -q                                   # backend tests
uv run ruff check .                                # Python lint
npm --prefix web run typecheck                     # TypeScript types
npm --prefix web run build                         # production web build
uv run python scripts/check_data_hygiene.py        # no private data or secrets in Git
uv run python scripts/export_openapi.py --check    # openapi.json matches the API
```

The data-hygiene check reads what Git tracks and stages. Stage new files (`git add`) before
you run it, or it cannot see them. It flags private paths, data file types, home-directory
paths, email addresses, keys, tokens, internal tracker ids (`KRK-` and four digits) and real host
names. Describe changes in plain words instead of tracker ids. It cannot spot real article prose
or real names, so that part is on you.

## Tests

Tests live in [`backend/tests/`](../backend/tests) and run with pytest (async tests run
automatically). They use fabricated inline inputs and fake integrations: no network, model,
archive or real dataset.

| File | Covers |
| --- | --- |
| `test_api.py` | Routes, auth, host and origin guards, body limits, safe errors, prompt and model routes, restarts |
| `test_articles.py` | Article identity, migration 4 and backfill, list filters and detail, first audit and re-run, archive import and fill |
| `test_config.py` | Environment parsing, run modes, allowed hosts and origins, token rules, provider settings |
| `test_db.py` | Storage, filters, migrations and rollback, prompt seeding and activation |
| `test_provenance.py` | What goes into the workflow fingerprint, and that secrets do not |
| `test_workflow_engine.py` | Graph paths, early exits, model call capture, cancellation |
| `test_workflow_verdicts.py` | The deterministic verdict rules |
| `test_workflow_textmatch.py` | Text containment and thresholds |
| `test_workflow_fetch.py`, `test_workflow_ssrf_guard.py` | Guarded page fetching |
| `test_workflow_archive_auth.py` | Archive sign-in and the read-only client |
| `test_model_profiles.py` | Provider profiles, response formats and step models |
| `test_demo_seed.py`, `test_demo_engine.py` | The demo dataset and the offline test-mode engine |
| `test_local_deps.py` | The offline install check `make dev` runs before starting |
| `test_data_hygiene.py`, `test_docker_context.py` | The hygiene check and the Docker build context |
| `test_docker_smoke_tooling.py`, `test_volume_restore.py` | Compose and Make settings for deployment, the smoke suite's fake model, and the validated, reversible volume restore |

Run one file while you work, for example `uv run pytest -q backend/tests/test_workflow_verdicts.py`.
The web app has no unit test runner. Besides typecheck and build, two offline checks cover it;
run them when you change the UI (details in [`web/README.md`](../web/README.md)):

```sh
npm --prefix web run check:ui         # routes, navigation, ledger filters, run detail, data hooks
npm --prefix web run check:contrast   # declared text and state colour pairs at 4.5:1 or more
```

The contrast check reads a hand-kept list of colour pairs; add a pair to it when you introduce one.

## Change a prompt

The six default prompts are constants in
[`workflow/prompts.py`](../backend/audit/workflow/prompts.py). The registry in
[`workflow/prompt_registry.py`](../backend/audit/workflow/prompt_registry.py#L14) names each one
and lists the tokens it must keep (output field names and the `GUARDRAIL` section).

- To experiment, create and activate a prompt version through the API. No restart is needed.
- To change a default, edit the constant. At the next start a text that was never stored before
  becomes a new version, and it becomes active unless a version made through the API is active
  (that one stays). Going back to an earlier text does not reactivate it: activate that version
  or roll back through the API.

Either way the workflow fingerprint changes. A prompt change changes verdicts, so run a
benchmark before and after and say what moved. Prompt examples must be made up. The full
procedure is in [Change a prompt or rule](workflow.md#change-a-prompt-or-rule).

## Add a workflow step

Steps are wired in several places. Follow an existing step such as `relevance` and change:

1. A new module in [`backend/audit/workflow/`](../backend/audit/workflow) with a `run`
   function that returns its evidence.
2. The step name: `StageName` in [`domain.py`](../backend/audit/domain.py#L42), `NAMES` and the
   `_execute` branch in [`engine.py`](../backend/audit/engine.py#L259), `ORDER` in
   [`pipeline.py`](../backend/audit/workflow/pipeline.py#L27), and `STEPS` in
   [`provenance.py`](../backend/audit/provenance.py).
3. If the step calls a model: its prompt in `prompts.py` and `prompt_registry.py`, its agent in
   [`agents.py`](../backend/audit/workflow/agents.py), its name in `MODEL_STEPS` in
   [`config.py`](../backend/audit/config.py#L175) plus a `model_name_<step>` field on `Settings`
   (read from `MODEL_NAME_<STEP>`), the commented variable in `.env.example`, and a case in
   `test_config.py` and `test_model_profiles.py`.
4. If it changes the verdict: the rules in `pipeline.py`, with tests in
   `test_workflow_verdicts.py`.
5. Test mode: the offline engine in [`demo_engine.py`](../backend/audit/demo_engine.py) and
   the seeded dataset in [`demo.py`](../backend/audit/demo.py), which builds every step of every
   demo run.
6. Tests in `test_workflow_engine.py` and `test_demo_seed.py`, and the step table in [the workflow guide](workflow.md).

The UI draws steps from the run data, so it usually needs no change.

## Change the API or the database

- API: response models live in [`schemas.py`](../backend/audit/schemas.py). After a change,
  run `uv run python scripts/export_openapi.py` to update [`openapi.json`](../openapi.json) and
  update [`web/src/types.ts`](../web/src/types.ts) to match. Update the route table in
  [the data and API guide](data-and-api.md#api).
- Database: add a numbered migration in [`db.py`](../backend/audit/db.py); never edit one that
  has shipped. Test an upgrade with existing rows. See
  [Schema changes](data-and-api.md#schema-changes).

## Docs and the changelog

- Every change a user or operator would notice gets one bullet under `## Unreleased` in
  [`CHANGELOG.md`](../CHANGELOG.md): what changed and why it matters, in one or two sentences.
- When you change behaviour, a rule, a setting or a route, update the doc that describes it in
  the same change. Docs live in `docs/`; generated reports never do.
- Some code links in the docs point at a line (`#L123`). If you move or rename a linked symbol,
  fix its link.
- [`AGENTS.md`](../AGENTS.md) holds the same rules in short form for coding agents.

## Pull requests

Keep changes small and focused. In the description, say what changed, why, and which checks you
ran. Mention any verdict change and the benchmark you used to check it.
