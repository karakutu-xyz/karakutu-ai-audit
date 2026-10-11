# Contributing

Thanks for helping. This service produces evidence that human auditors act on, so the rules
below are stricter than usual. Before changing pipeline code, read [AGENTS.md](./AGENTS.md)
(the hard rules, commands and layout). Every setting is documented in
[`.env.example`](./.env.example).

## Setup

Python **3.11** exactly (`requires-python = "==3.11.*"`).

```bash
python3.11 -m venv .venv311
.venv311/bin/pip install -r requirements-dev.txt   # runtime + pytest/ruff
cp .env.example .env                                # optional; defaults need no config
.venv311/bin/uvicorn src.api.app:app --reload --port 2200
```

The defaults are synthetic (`TestModel` and the `stub` search provider), so the service and
the whole test suite run with no `.env`, no wallet keyfile, no LM Studio and no network.

## Tests and lint

```bash
.venv311/bin/python -m pytest -q
.venv311/bin/ruff check .
```

Both must be green before a pull request is reviewed; CI runs the same commands plus a
secret scan and a Docker build that boots the image and checks `/health`.

- Mock at `src.workflow.pipeline.<stage>_stage.run`, not at the `Agent` level.
- Settings are a class-level singleton evaluated at import: use `monkeypatch.setattr` on it,
  not `monkeypatch.setenv` (to test parsing itself, reload the module as
  `tests/test_settings.py::reload_settings` does).
- `pydantic-ai` is pinned to `0.0.49`. Use `result_type=` / `system_prompt=` /
  `result.data`. Do not "modernise" to the newer API without bumping the pin deliberately.
- A test that needs a network, a wallet or a real model is wrong; stub it.

## Rule 1: results must not change unless that is the point of the change

An audit's results are its verdict, its stage outputs, its classification, its flags and the
text of the discussion comment it posts. They are calibrated against real archive data, and
human reviewers compare them across batches. Therefore:

- Do not edit prompts (`src/workflow/prompts.py`), thresholds, `pipeline._decide`, the
  classification rules or the comment templates (`src/archive_flow/comments.py`) as a side
  effect of another change. Refactors and moves are **verbatim**.
- Result-bearing constants (`CONTENT_LIMIT`, `MIN_BODY_CHARS`, `ELIGIBLE_STATUSES`, every
  threshold) are code, not configuration. Do not turn them into environment variables.
- The golden tests pin this. `tests/test_archive_flow.py` hashes the rendered comment for
  real records (`tests/fixtures/golden_records.json`) under every template and tag state
  (`tests/fixtures/golden_comments.json`). If a golden test fails, your change altered what
  a reader sees: revert it, or make it a separate, explicitly-labelled behaviour change.
- A deliberate behaviour change needs its own pull request that says so in the title, explains
  the measurement behind it, and regenerates the fixtures in the same change.

## Rule 2: contract first

`karakutu-audit-api-contract.yml` (OpenAPI 3.0.3) is the source of truth for every HTTP shape.

- Change the contract **first**, then the Pydantic models, then
  `tests/test_contract_alignment.py`, all in the same pull request.
- Additive changes (new optional fields, new endpoints) are preferred. Removing or renaming a
  field is a breaking change and needs a major version.

## The censorship guardrail

The service may raise a **flag**, never a **finding**. A model must not be able to author a
verdict, and no wording may assert that an article was censored. Read rules 1 and 3 in
[AGENTS.md](./AGENTS.md) before touching the evidence rules, the verdict tiers or the comment
templates.

## Pull requests

- One logical change per pull request, with a description of what changed and how you
  verified it (commands and their results).
- Update `CHANGELOG.md` under an "Unreleased" heading (add it above the latest release if it
  is not there yet).
- Update the nearest doc in the same change: the contract for shapes, `.env.example` for a
  setting, the comment on the constant for a rule or threshold (with the measurement behind
  it), README for anything users see, AGENTS.md for a rule.
- Never commit secrets, wallet keyfiles, `.env` files, archive data dumps or campaign output
  (`runs/`, `private/`). See [SECURITY.md](./SECURITY.md).

By contributing you agree that your contributions are licensed under the Apache License 2.0
(see [LICENSE](./LICENSE)), and you agree to follow the
[code of conduct](./CODE_OF_CONDUCT.md).
