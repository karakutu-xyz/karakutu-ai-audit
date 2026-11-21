"""Offline coverage of the fictional demo records and their storage guardrails."""
from collections import Counter
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import literal_column, select
from sqlalchemy.orm import Session

from audit.api import create_app
from audit.config import Settings
from audit.db import PromptActivation, PromptVersion, Reaction, Run, Store, Workflow
from audit.demo import CASES, DemoSeedRefused, seed
from audit.demo_engine import DemoEngine
from audit.domain import AuditFlag, Verdict
from audit.engine import NAMES
from audit.provenance import build_base, with_prompts
from audit.schemas import RunDetail, WorkflowSnapshot
from audit.workflow.prompt_registry import DEFAULTS, REGISTRY, validation_codes


TABLE_COUNTS = {"workflows": 3, "runs": 28, "reactions": 8, "articles": 19,
                "prompt_versions": 3, "prompt_activations": 5}


def _url(path):
    return f"sqlite:///{path}"


def _details(store):
    return {row["id"]: store.run(row["id"]) for row in store.runs(limit=100)["items"]}


@pytest.fixture
def seeded(tmp_path):
    url = _url(tmp_path / "demo-test.sqlite3")
    summary = seed(url)
    store = Store(url)
    try:
        yield url, store, summary
    finally:
        store.close()


def test_counts_and_domain_contracts(seeded):
    _, store, summary = seeded
    rows = _details(store)
    assert summary["tables"] == TABLE_COUNTS
    assert summary["statuses"] == {"cancelled": 1, "failed": 4, "queued": 1, "running": 1, "succeeded": 21}
    assert summary["statuses"] == dict(Counter(r["status"] for r in rows.values()))
    assert summary["verdicts"] == dict(Counter(r["verdict"] or "pending" for r in rows.values()))
    assert {r["verdict"] for r in rows.values() if r["verdict"]} == {v.value for v in Verdict}
    assert {r["error"]["code"] for r in rows.values() if r["error"]} == {"audit_failed", "interrupted", "cancelled"}
    with Session(store.engine) as session:
        assert len(session.scalars(select(Workflow)).all()) == 3
        assert len(session.scalars(select(Reaction)).all()) == 8
    for row in rows.values():
        detail = RunDetail.model_validate(row)
        assert detail.label.startswith("DEMO · ")
        assert "fictional demo data" in detail.input.removed_article.content
        assert detail.synthetic == (detail.id == "run_demo_disabled")
        assert detail.model == {"name": f"demo-model-{detail.workflow_id[-1]}", "enabled": not detail.synthetic}
        if detail.finished_at and detail.status not in {"failed", "cancelled"}:
            assert [s.name.value for s in detail.steps] == NAMES
        if detail.output:
            assert [s.output for s in detail.steps] == [s.model_dump(mode="json") for s in detail.output.stages]
        for index, step in enumerate(detail.steps):
            assert step.input["article"] == row["input"]["removed_article"]
            assert step.input["prior_stages"] == [s["output"] for s in row["steps"][:index]]
        if detail.verdict == Verdict.POSSIBLE_CENSORSHIP:
            assert "not a finding of censorship" in detail.output.findings
    for version, accuracy in ((1, 3), (2, 4), (3, 5)):
        suite = [r for r in rows.values() if r["workflow_id"] == f"wf_demo_v{version}" and r["expected_verdict"]]
        assert len(suite) == 6
        assert sum(r["verdict"] == r["expected_verdict"] for r in suite) == accuracy
        snapshot = store.workflow(f"wf_demo_v{version}")
        WorkflowSnapshot.model_validate(snapshot)
        assert snapshot["definition"]["demo_note"] in snapshot["definition"]["sources"]["DEMO_README.txt"]
    for case in CASES:
        assert rows[f"run_demo_{case.slug}_v1"]["input"] == rows[f"run_demo_{case.slug}_v2"]["input"] == rows[f"run_demo_{case.slug}_v3"]["input"]


def test_scenario_evidence_and_failure_shapes(seeded):
    _, store, _ = seeded
    censored = store.run("run_demo_reservoir_v3")
    assert set(censored["output"]["flags"]) == {"RELATED_REMOVALS", "SOURCE_DEAD"}
    assert [s["status"] for s in censored["steps"]] == ["completed"] * 6
    related = censored["steps"][3]["output"]["details"]
    assert len(related["members"]) >= 3 and len(related["publishers"]) >= 2
    assert censored["steps"][4]["output"]["details"]["dead_source"]["status"] == 404
    assert censored["steps"][1]["model_calls"] == []  # Similarity never uses a model.
    moved = store.run("run_demo_library_v3")
    assert moved["steps"][1]["output"]["details"]["old_in_new"] == 1.0
    assert moved["output"]["short_circuited_at"] == "similarity"
    assert [s["status"] for s in moved["steps"]][2:] == ["skipped"] * 4
    kept = store.run("run_demo_kept")
    assert kept["verdict"] == "INCONCLUSIVE"
    assert kept["steps"][3]["output"]["details"]["kept_in_scope"] is True
    assert "kept it in the audit" in kept["output"]["findings"]
    assert store.run("run_demo_live_webhook")["output"]["flags"] == [AuditFlag.SOURCE_DEAD.value]
    assert [(store.run(i)["category"], store.run(i)["origin"]) for i in ("run_demo_live_api", "run_demo_live_webhook")] == [("live", "api"), ("live", "webhook")]
    failure = store.run("run_demo_relevance_failure")
    assert [s["status"] for s in failure["steps"]] == ["skipped", "completed", "failed", "skipped", "skipped", "skipped"]
    call = failure["steps"][2]["model_calls"][0]
    assert call["error_type"] == "ValidationError" and call["parsed_output"] is None
    assert call["raw_completion"]["choices"][0]["message"]["content"] == "{invalid demo JSON"
    assert bool(call["request"]["messages"][0]["content"] ==
                store.prompt_version("relevance", 3)["text"])
    web_calls = store.run("run_demo_web_failure")["steps"][-1]["model_calls"]
    assert len(web_calls) == 3 and all(c["error_type"] for c in web_calls)
    assert len({c["request"]["messages"][1]["content"] for c in web_calls}) == 3
    assert [s["status"] for s in store.run("run_demo_disabled")["steps"]] == ["failed"] + ["skipped"] * 5
    for slug in ("running", "stopped", "cancelled"):
        row = store.run(f"run_demo_{slug}")
        assert len(row["steps"]) == 3 and all(s["status"] == "completed" for s in row["steps"])
        assert row["output"] is None
    cancelled = store.run("run_demo_cancelled")
    assert cancelled["status"] == "cancelled" and cancelled["verdict"] is None
    assert cancelled["summary"]["verdict_label"] == "Stopped by a person"
    assert cancelled["error"]["code"] == "cancelled"
    assert store.run("run_demo_running")["finished_at"] is None
    assert store.run("run_demo_queued")["steps"] == []


def test_idempotent(seeded):
    url, store, counts = seeded
    before = _details(store)
    snapshots = [store.workflow(f"wf_demo_v{v}") for v in (1, 2, 3)]
    assert seed(url) == counts
    assert _details(store) == before
    assert [store.workflow(f"wf_demo_v{v}") for v in (1, 2, 3)] == snapshots


def _prompt_rows(store):
    with Session(store.engine) as session:
        return {table: session.connection().exec_driver_sql(
            f"SELECT rowid, * FROM {table} ORDER BY rowid").all()
            for table in ("prompt_versions", "prompt_activations")}


def test_prompt_history_and_exact_workflow_records(seeded):
    url, store, counts = seeded
    assert {name: value["text"] for name, value in store.active_prompts().items()} == DEFAULTS
    expected_models = {
        "classification": "demo-model-small", "relevance": "demo-model-small",
        "related_removals": "demo-model-small", "source": "demo-model-large",
        "web_search": "demo-model-large",
    }
    with Session(store.engine) as session:
        versions = session.scalars(select(PromptVersion).where(
            PromptVersion.id.in_(("pv_demo_relevance_v2", "pv_demo_relevance_v3", "pv_demo_web_search_v2")))).all()
        assert len(versions) == 3
        for row in versions:
            assert not validation_codes(row.name, row.text)
            assert row.text.startswith(DEFAULTS[row.name] + "\n\nDemo wording change (fictional): ")
            assert row.sha256 == hashlib.sha256(row.text.encode("utf-8")).hexdigest()
            assert row.origin == "api" and row.author.startswith("demo-editor-")
            assert "Fictional demo" in row.note
        activations = session.scalars(select(PromptActivation).order_by(
            literal_column("prompt_activations.rowid"))).all()[6:]
        assert [row.id for row in activations] == [
            "pa_demo_relevance_v2", "pa_demo_web_search_v2", "pa_demo_relevance_v3",
            "pa_demo_relevance_default", "pa_demo_web_search_default",
        ]
        assert [row.created_at for row in activations] == sorted(row.created_at for row in activations)

    for version in (1, 2, 3):
        snapshot = store.workflow(f"wf_demo_v{version}")
        expected_prompts = dict(DEFAULTS)
        if version > 1:
            expected_prompts["relevance"] = store.prompt_version("relevance", version)["text"]
        if version == 3:
            expected_prompts["web_search"] = store.prompt_version("web_search", 2)["text"]
        definition = snapshot["definition"]
        assert bool(definition["prompts"] == expected_prompts)
        assert definition["format_version"] == build_base({})["format_version"] == 3
        assert definition["models"] == {"provider": "lmstudio", "response_format": "json_schema",
                                         "default": f"demo-model-{version}", "steps": expected_models}
        for run in _details(store).values():
            if run["workflow_id"] != snapshot["id"]:
                continue
            for step in run["steps"]:
                assert step["model"] == (expected_models[step["name"]] if step["model_calls"] else None)
                for index, call in enumerate(step["model_calls"]):
                    model = expected_models[step["name"]]
                    assert call["request"]["model"] == call["response_model"] == call["raw_completion"]["model"] == model
                    name = ("classification_coarse" if index == 0 else "classification_fine") if step["name"] == "classification" else step["name"]
                    assert bool(call["request"]["messages"][0]["content"] == expected_prompts[name])
    before = _prompt_rows(store)
    assert seed(url) == counts
    assert _prompt_rows(store) == before


def test_seeded_workflows_and_prompt_history_through_api(tmp_path):
    app = create_app(Settings(mode="test", data_dir=tmp_path), DemoEngine(step_delay=0))
    for _ in range(2):
        with TestClient(app) as client:
            current = client.get("/api/workflow")
            assert current.status_code == 200
            expected = with_prompts(app.state.workflow_base, DEFAULTS, app.state.model_defaults)
            assert current.json()["id"] == expected["id"]
            listed = client.get("/api/prompts")
            assert listed.status_code == 200
            assert {item["name"]: item["versions"] for item in listed.json()["items"]} == {
                name: 3 if name == "relevance" else 2 if name == "web_search" else 1 for name in REGISTRY}
            detail = client.get("/api/prompts/relevance")
            assert detail.status_code == 200
            assert detail.json()["active"]["version"] == 1
            assert [row["version"] for row in detail.json()["versions"]] == [3, 2, 1]
            assert detail.json()["activations"][0]["id"] == "pa_demo_relevance_default"
            diff = client.get("/api/prompts/relevance/diff", params={"from": 1, "to": 3})
            assert diff.status_code == 200
            assert "+Demo wording change (fictional):" in diff.json()["diff"]
            for version in (1, 2, 3):
                workflow = client.get(f"/api/workflows/wf_demo_v{version}")
                assert workflow.status_code == 200
                definition = workflow.json()["definition"]
                assert bool(definition["prompts"] == app.state.store.workflow(f"wf_demo_v{version}")["definition"]["prompts"])
                assert definition["models"]["steps"]["source"] == "demo-model-large"
                run = client.get(f"/api/runs/run_demo_reservoir_v{version}")
                assert run.status_code == 200
                assert run.json()["workflow_id"] == workflow.json()["id"]
                assert run.json()["steps"][2]["model"] == "demo-model-small"
                article = client.get(f"/api/articles/{run.json()['article_ref']}")
                assert article.status_code == 200
                history = {row["id"]: row for row in article.json()["runs"]}
                assert history[run.json()["id"]]["workflow_id"] == workflow.json()["id"]


def _real_run(store):
    store.save_workflow({"id": "wf_real"})
    return store.create_run(workflow_id="wf_real", category="benchmark", origin="benchmark",
        input={"removed_article": {"url": "https://source.example/report", "title": "Fictional input", "content": "Fictional demo data."}})


def test_appends_after_existing_api_versions_and_preserves_later_selections(tmp_path):
    url = _url(tmp_path / "existing-test.sqlite3")
    store = Store(url)
    try:
        store.sync_prompt_defaults(DEFAULTS)
        existing = store.create_prompt_version("relevance", DEFAULTS["relevance"] + "\nFictional API edit.",
                                               "invented-editor", "Fictional existing version.")
        # Lookalike prefixes must neither count as demo rows nor be modified.
        with Session(store.engine) as session, session.begin():
            row = session.get(PromptVersion, existing["id"])
            row.id = "pvXdemoYexisting"
            session.flush()
            session.add(PromptActivation(id="paXdemoYexisting", name="relevance", version_id=row.id,
                kind="activate", author="invented-editor", note="Fictional existing activation.",
                created_at="2026-11-19T09:00:00+00:00"))
        before = _prompt_rows(store)
        assert seed(url)["tables"] == TABLE_COUNTS
        assert store.prompt_version("relevance", 2)["id"] == "pvXdemoYexisting"
        assert store.prompt_version("relevance", 3)["id"] == "pv_demo_relevance_v2"
        assert store.prompt_version("relevance", 4)["id"] == "pv_demo_relevance_v3"
        # The operator's selection (after the default sync) stays active once the demo history is added.
        assert store.active_prompts()["relevance"]["version"] == 2
        assert store.active_prompts()["web_search"]["version"] == 1
        for table, rows in before.items():
            assert _prompt_rows(store)[table][:len(rows)] == rows
        store.activate_prompt("relevance", 2, "activate", "invented-editor", "Fictional later choice.",
                              lambda texts: with_prompts(build_base({}), texts))
        before = _prompt_rows(store)
        seed(url)
        assert _prompt_rows(store) == before
        assert store.active_prompts()["relevance"]["version"] == 2
    finally:
        store.close()


def test_upgrades_pre_card_demo_rows_without_touching_non_demo_records(seeded):
    url, store, counts = seeded
    real = _real_run(store)
    reaction = store.add_reaction("run_demo_reservoir_v3", {
        "reaction": "needs_review", "reviewer": "invented-reviewer", "note": "Fictional operator note.",
        "expected_verdict": None,
    })
    existing = store.create_prompt_version("relevance", DEFAULTS["relevance"] + "\nFictional retained edit.",
                                           "invented-editor", "Fictional retained API version.")
    with Session(store.engine) as session, session.begin():
        # Simulate the old seeder: format 1, no demo prompt rows, one model per run,
        # no step.model. Non-demo versions/activations remain exactly as stored.
        session.connection().exec_driver_sql("DELETE FROM prompt_activations WHERE substr(id, 1, 8) = 'pa_demo_'")
        session.connection().exec_driver_sql("DELETE FROM prompt_versions WHERE substr(id, 1, 8) = 'pv_demo_'")
        for version in (1, 2, 3):
            row = session.get(Workflow, f"wf_demo_v{version}")
            definition = dict(row.snapshot["definition"])
            definition.pop("models")
            definition.pop("prompts")
            row.snapshot = {**row.snapshot, "definition": {**definition, "format_version": 1}}
        rows = session.scalars(select(Run).where(Run.id != real["id"])).all()
        for row in rows:
            row.steps = [{key: value for key, value in step.items() if key != "model"} for step in row.steps]
    real_before = store.run(real["id"])
    prompts_before = _prompt_rows(store)
    inputs_before = {key: row["input"] for key, row in _details(store).items()}
    assert seed(url) == counts
    assert store.run(real["id"]) == real_before
    assert store.workflow("wf_real") == {"id": "wf_real"}
    assert next(row for row in store.run("run_demo_reservoir_v3")["reactions"] if row["id"] == reaction["id"]) == reaction
    assert bool(store.prompt_version("relevance", 4)["text"] == existing["text"])
    assert store.prompt_version("relevance", 5)["id"] == "pv_demo_relevance_v2"
    assert store.prompt_version("relevance", 6)["id"] == "pv_demo_relevance_v3"
    for table, rows in prompts_before.items():
        assert _prompt_rows(store)[table][:len(rows)] == rows
    for version in (1, 2, 3):
        definition = store.workflow(f"wf_demo_v{version}")["definition"]
        assert definition["format_version"] == 3
        assert definition["models"]["steps"]["web_search"] == "demo-model-large"
        expected = DEFAULTS["relevance"] if version == 1 else store.prompt_version("relevance", version + 3)["text"]
        assert bool(definition["prompts"]["relevance"] == expected)
    assert {key: row["input"] for key, row in _details(store).items()} == inputs_before
    assert store.run("run_demo_reservoir_v3")["steps"][2]["model"] == "demo-model-small"
    after = _prompt_rows(store)
    assert seed(url) == counts
    assert _prompt_rows(store) == after


def test_refuses_non_test_database_without_changes(tmp_path):
    path = tmp_path / "records.sqlite3"
    store = Store(_url(path))
    try:
        _real_run(store)
    finally:
        store.close()
    # A closed WAL-mode database without sidecars: the case where a read could create them.
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = list(connection.iterdump())
    connection.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    files = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert sorted(files) == ["records.sqlite3"]
    with pytest.raises(DemoSeedRefused, match="Not a test database"):
        seed(_url(path))
    # Refusal writes nothing at all: same files, same bytes, no WAL/SHM sidecars.
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == files
    assert list(sqlite3.connect(path).iterdump()) == before
    assert sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_marker_needs_literal_demo_prefix(tmp_path):
    path = tmp_path / "records.sqlite3"
    store = Store(_url(path))
    try:
        store.save_workflow({"id": "wfXdemoYreal"})  # would match LIKE 'wf_demo_%'
        with pytest.raises(DemoSeedRefused, match="Not a test database"):
            seed(_url(path))
    finally:
        store.close()


def test_opens_the_checked_path_not_a_dotdot_alias(tmp_path):
    live = tmp_path / "live" / "records.sqlite3"
    store = Store(_url(live))
    try:
        _real_run(store)
        before = list(sqlite3.connect(live).iterdump())
        nested = tmp_path / "data" / "demo" / "nested"
        nested.mkdir(parents=True)
        (tmp_path / "live" / "link").symlink_to(nested)
        # Resolving follows the link first: the check sees data/demo/records.sqlite3, and the
        # seeder must open exactly that file, not live/records.sqlite3 by lexical `..`.
        seed(_url(tmp_path / "live" / "link" / ".." / "records.sqlite3"))
        assert list(sqlite3.connect(live).iterdump()) == before
        assert (tmp_path / "data" / "demo" / "records.sqlite3").is_file()
    finally:
        store.close()


def test_marker_deleted_in_wal_is_not_trusted(tmp_path):
    path = tmp_path / "records.sqlite3"
    seed(_url(tmp_path / "seeded-test.sqlite3"))
    with sqlite3.connect(tmp_path / "seeded-test.sqlite3") as source, sqlite3.connect(path) as target:
        source.backup(target)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # marker is in the main file
        connection.execute("DELETE FROM reactions")
        connection.execute("DELETE FROM runs")
        connection.execute("DELETE FROM workflows")
        connection.commit()  # deletion lives only in the WAL while this connection is open
        assert Path(f"{path}-wal").stat().st_size > 0
        with pytest.raises(DemoSeedRefused, match="Not a test database"):
            seed(_url(path))
    finally:
        connection.close()


def test_marker_with_hot_rollback_journal_is_not_trusted(tmp_path):
    path = tmp_path / "records.sqlite3"
    seed(_url(tmp_path / "seeded-test.sqlite3"))
    with sqlite3.connect(tmp_path / "seeded-test.sqlite3") as source, sqlite3.connect(path) as target:
        source.backup(target)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
    # Stand-in for a crash mid-transaction: a non-empty journal beside a marked main file.
    Path(f"{path}-journal").write_bytes(b"\0" * 512)
    files = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(DemoSeedRefused, match="Not a test database"):
        seed(_url(path))
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == files


def test_refuses_resolved_path_that_reparses_to_another_file(tmp_path):
    live = tmp_path / "records.sqlite3"
    store = Store(_url(live))
    try:
        _real_run(store)
        before = list(sqlite3.connect(live).iterdump())
        # The target name passes the filename rule, but as a URL `?` starts a query string.
        alias = tmp_path / "alias-test.sqlite3"
        alias.symlink_to(tmp_path / "records.sqlite3?ignored-test.sqlite3")
        with pytest.raises(DemoSeedRefused, match="unambiguously"):
            seed(_url(alias))
        assert list(sqlite3.connect(live).iterdump()) == before
    finally:
        store.close()


def test_refuses_default_live_path_before_creation(tmp_path):
    path = tmp_path / "data" / "audit.sqlite3"
    with pytest.raises(DemoSeedRefused, match="Not a test database"):
        seed(_url(path))
    assert not path.parent.exists()
    # A test-named symlink to the live file resolves to the live name and is refused.
    path.parent.mkdir()
    path.touch()
    alias = tmp_path / "alias-test.sqlite3"
    alias.symlink_to(path)
    with pytest.raises(DemoSeedRefused, match="Not a test database"):
        seed(_url(alias))
    assert path.stat().st_size == 0


def test_accepts_demo_dir_and_marked_database(tmp_path):
    under_demo = tmp_path / "data" / "demo" / "audit.sqlite3"
    assert seed(_url(under_demo))["tables"]["runs"] == 28
    # Any database that already carries the demo workflows is accepted, whatever its name.
    renamed = tmp_path / "copy.sqlite3"
    with sqlite3.connect(under_demo) as source, sqlite3.connect(renamed) as target:
        source.backup(target)
    assert seed(_url(renamed))["tables"]["runs"] == 28


def test_keeps_non_demo_rows_and_restores_pending_after_startup(tmp_path):
    url = _url(tmp_path / "audit-test.sqlite3")
    store = Store(url)
    try:
        real = _real_run(store)
        store.save_workflow({"id": "wfXdemoYreal"})  # must not count as a demo row
        summary = seed(url)
        assert summary["tables"] == TABLE_COUNTS
        # API startup marks queued/running rows interrupted; reseeding restores the demo ones.
        store.interrupt_incomplete()
        assert store.run("run_demo_running")["status"] == "failed"
        assert seed(url) == summary
        assert store.run("run_demo_running")["status"] == "running"
        assert store.run("run_demo_queued")["status"] == "queued"
        kept = store.run(real["id"])
        assert kept["status"] == "failed" and kept["error"]["code"] == "interrupted"
        assert store.workflow("wf_real") == {"id": "wf_real"}
    finally:
        store.close()


def test_memory_database_allowed():
    assert seed("sqlite:///:memory:")["tables"]["runs"] == 28


def test_cli(tmp_path):
    root = Path(__file__).resolve().parents[2]
    command = [sys.executable, str(root / "scripts" / "seed_demo.py")]
    path = tmp_path / "cli-test.sqlite3"
    good = subprocess.run([*command, "--db", str(path)], cwd=tmp_path, capture_output=True, text=True)
    assert good.returncode == 0, good.stderr
    assert "runs=28" in good.stdout and "workflows=3" in good.stdout
    assert "fictional demo data" not in good.stdout and "Fictional council" not in good.stdout
    again = subprocess.run([*command, "--database-url", _url(path)], capture_output=True, text=True)
    assert again.returncode == 0 and again.stdout == good.stdout
    bad = subprocess.run([*command, "--db", str(tmp_path / "data" / "audit.sqlite3")], capture_output=True, text=True)
    assert bad.returncode != 0 and "refused" in bad.stderr
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("removed", [
    ("pa_demo_web_search_v2", "pa_demo_relevance_v3", "pa_demo_relevance_default", "pa_demo_web_search_default"),
    ("pa_demo_relevance_v2",),
    ("pa_demo_relevance_default",),
])
def test_reseeding_a_partial_demo_history_never_changes_the_active_prompts(tmp_path, removed):
    """Seeding never changes the active selection, even over a tampered demo history."""
    url = _url(tmp_path / "partial-test.sqlite3")
    seed(url)
    store = Store(url)
    try:
        with Session(store.engine) as session, session.begin():
            marks = ", ".join(f"'{key}'" for key in removed)
            session.connection().exec_driver_sql(f"DELETE FROM prompt_activations WHERE id IN ({marks})")
        before = store.active_prompts()
        rows = _prompt_rows(store)
        seed(url)
        assert store.active_prompts() == before
        assert _prompt_rows(store) == rows
    finally:
        store.close()


def test_operator_activation_of_a_demo_version_survives_reseed(tmp_path):
    url = _url(tmp_path / "operator-demo-test.sqlite3")
    seed(url)
    store = Store(url)
    try:
        version = store.prompt_version("relevance", 3)
        assert version["id"] == "pv_demo_relevance_v3"
        store.activate_prompt("relevance", 3, "activate", "invented-editor", "Fictional deliberate choice.",
                              lambda texts: with_prompts(build_base({}), texts))
        seed(url)
        assert store.active_prompts()["relevance"]["version_id"] == "pv_demo_relevance_v3"
    finally:
        store.close()
