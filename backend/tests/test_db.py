"""All examples are fabricated, with reserved .invalid hosts and invented article text."""
import json
import sqlite3

import pytest
from sqlalchemy import inspect

from audit.db import Store
from audit.provenance import with_prompts


INPUT = {"removed_article": {"url": "https://fabricated.invalid/story", "title": "Invented test title", "content": "Fabricated article for storage tests."}, "similar_articles": []}


def workflow(store, name="wf_test"):
    return store.save_workflow({"id": name, "created_at": "test", "definition": {"steps": [], "settings": {}}})


def prompt_workflow(texts):
    return with_prompts({}, texts)


def create(store, category="benchmark", workflow_id="wf_test"):
    return store.create_run(workflow_id=workflow_id, category=category,
                            origin="benchmark" if category == "benchmark" else "api", input=INPUT)


def test_persist_restart_and_immutable_workflow(tmp_path, database_url):
    url = database_url
    store = Store(url)
    first = workflow(store)
    replacement = {**first, "created_at": "changed", "definition": {"changed": True}}
    assert store.save_workflow(replacement) == first
    done = create(store)
    store.start_run(done["id"])
    store.append_step(done["id"], {"name": "classification", "status": "completed"})
    result = {"response": {"verdict": "INCONCLUSIVE"}, "steps": [{"name": "classification"}], "model": {"name": "test"}, "synthetic": True}
    store.complete_run(done["id"], result)
    queued = create(store)
    running = create(store)
    store.start_run(running["id"])
    store.close()

    reopened = Store(url)
    reopened.interrupt_incomplete()
    assert reopened.workflow("wf_test") == first
    assert reopened.run(done["id"])["status"] == "succeeded"
    assert reopened.run(done["id"])["input"] == INPUT
    for run in (queued, running):
        assert reopened.run(run["id"])["error"]["code"] == "interrupted"
        assert reopened.run(run["id"])["status"] == "failed"
    reopened.close()


def test_filter_pagination_and_reactions_keep_run_workflow(tmp_path, database_url):
    store = Store(database_url)
    workflow(store)
    workflow(store, "wf_other")
    a = create(store)
    b = create(store, "live", "wf_other")
    store.complete_run(a["id"], {"response": {"verdict": "INCONCLUSIVE"}, "steps": [], "synthetic": True})
    store.complete_run(b["id"], {"response": {"verdict": "MOVED"}, "steps": []})
    reaction = store.add_reaction(a["id"], {"reaction": "agree", "reviewer": "Fabricated reviewer", "note": "Invented note", "expected_verdict": None})
    assert reaction["workflow_id"] == a["workflow_id"] == "wf_test"
    assert reaction["run_id"] == a["id"]
    assert store.run(a["id"])["reactions"] == [reaction]
    assert store.run(b["id"])["reactions"] == []
    assert store.runs(category="benchmark")["total"] == 1
    assert store.runs(verdict="MOVED")["items"][0]["id"] == b["id"]
    assert store.runs(workflow_id="wf_test")["total"] == 1
    assert len(store.runs(limit=1)["items"]) == 1
    assert store.runs(offset=2)["items"] == []
    assert store.runs(status="succeeded")["total"] == 2
    assert store.runs(status="queued")["total"] == 0
    assert store.runs(origin="api")["items"][0]["id"] == b["id"]
    assert store.runs(origin="webhook")["total"] == 0
    c = create(store)
    store.complete_run(c["id"], {"response": {"verdict": "INCONCLUSIVE"}, "steps": []})
    queued = create(store)
    filters = {"category": "benchmark", "verdict": "INCONCLUSIVE",
               "workflow_id": "wf_test", "status": "succeeded", "origin": "benchmark"}
    page = store.runs(**filters, limit=1)
    next_page = store.runs(**filters, limit=1, offset=1)
    assert page["total"] == next_page["total"] == 2
    assert len(page["items"]) == len(next_page["items"]) == 1
    assert {page["items"][0]["id"], next_page["items"][0]["id"]} == {a["id"], c["id"]}
    assert store.runs(**filters, offset=2) == {"items": [], "total": 2}
    assert store.runs(status="queued")["items"][0]["id"] == queued["id"]
    for name, value in [("category", "live"), ("verdict", "MOVED"),
                        ("workflow_id", "wf_other"), ("status", "failed"), ("origin", "api")]:
        assert store.runs(**{**filters, name: value}) == {"items": [], "total": 0}
    assert store.add_reaction("missing", {}) is None
    store.close()


def test_sqlite_safety_pragmas_and_foreign_keys(tmp_path):
    path = tmp_path / "test.sqlite3"
    store = Store(f"sqlite:///{path}")
    with store.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    store.close()
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


def test_schema_fresh_and_newer_refused(tmp_path):
    from audit.db import SCHEMA_VERSION
    path = tmp_path / "schema.sqlite3"
    store = Store(f"sqlite:///{path}")
    with store.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(ValueError, match="newer"):
        Store(f"sqlite:///{path}")


def old_database(path):
    # The pre-card schema is created through sqlite3, without current metadata.
    with sqlite3.connect(path) as connection:
        connection.executescript('''
            CREATE TABLE workflows (id VARCHAR(80) PRIMARY KEY, snapshot JSON NOT NULL);
            CREATE TABLE runs (
                id VARCHAR(40) PRIMARY KEY, workflow_id VARCHAR(80) NOT NULL REFERENCES workflows(id),
                category VARCHAR(20) NOT NULL, origin VARCHAR(20) NOT NULL, status VARCHAR(20) NOT NULL,
                created_at VARCHAR(40) NOT NULL, finished_at VARCHAR(40), label VARCHAR(200),
                article_title TEXT NOT NULL, verdict VARCHAR(50), synthetic BOOLEAN NOT NULL,
                input JSON NOT NULL, output JSON, steps JSON NOT NULL, model JSON NOT NULL,
                error JSON, expected_verdict VARCHAR(50));
            CREATE TABLE reactions (
                id VARCHAR(40) PRIMARY KEY, run_id VARCHAR(40) NOT NULL REFERENCES runs(id),
                workflow_id VARCHAR(80) NOT NULL REFERENCES workflows(id), reaction VARCHAR(20) NOT NULL,
                reviewer VARCHAR(120) NOT NULL, note TEXT NOT NULL, expected_verdict VARCHAR(50),
                created_at VARCHAR(40) NOT NULL);
            INSERT INTO workflows VALUES ('wf_old', '{"retained": true}');
        ''')
        connection.execute(
            "INSERT INTO runs (id,workflow_id,category,origin,status,created_at,article_title,synthetic,input,steps,model) "
            "VALUES ('run_old','wf_old','benchmark','benchmark','succeeded','old','Fabricated title',0,?,'[]','{}')",
            (json.dumps(INPUT),))
        connection.execute(
            "INSERT INTO reactions VALUES ('rx_old','run_old','wf_old','agree','Reviewer','Fabricated note',NULL,'old')")


def test_old_schema_upgrades_once_and_column_migration(tmp_path):
    from audit.db import MIGRATIONS, SCHEMA_VERSION
    path = tmp_path / "old.sqlite3"
    old_database(path)
    calls = []

    def add_column(connection):
        calls.append(SCHEMA_VERSION + 1)
        connection.exec_driver_sql("ALTER TABLE workflows ADD COLUMN migration_marker TEXT")
        connection.exec_driver_sql("UPDATE workflows SET migration_marker='preserved'")

    def counted(version, migration):
        def wrapped(connection):
            calls.append(version)
            migration(connection)
        return wrapped

    migrations = [(version, counted(version, migration)) for version, migration in MIGRATIONS]
    migrations.append((SCHEMA_VERSION + 1, add_column))
    for _ in range(2):
        store = Store(f"sqlite:///{path}", migrations=migrations)
        assert store.workflow("wf_old") == {"retained": True}
        assert store.run("run_old")["input"] == INPUT
        assert store.run("run_old")["reactions"][0]["id"] == "rx_old"
        assert {"prompt_versions", "prompt_activations"} <= set(inspect(store.engine).get_table_names())
        with store.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION + 1
            assert connection.exec_driver_sql("SELECT migration_marker FROM workflows").scalar_one() == "preserved"
        store.close()
    assert calls == list(range(1, SCHEMA_VERSION + 2))


def test_failed_migration_rolls_back_ddl_rows_and_version(tmp_path):
    from audit.db import MIGRATIONS, SCHEMA_VERSION
    path = tmp_path / "failure.sqlite3"
    old_database(path)

    def fail(connection):
        connection.exec_driver_sql("ALTER TABLE workflows ADD COLUMN abandoned TEXT")
        connection.exec_driver_sql("UPDATE workflows SET snapshot='{}'")
        raise RuntimeError("fabricated migration failure")

    with pytest.raises(RuntimeError, match="fabricated migration failure"):
        Store(f"sqlite:///{path}", migrations=[*MIGRATIONS, (SCHEMA_VERSION + 1, fail)])
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "abandoned" not in [row[1] for row in connection.execute("PRAGMA table_info(workflows)")]
        assert connection.execute("SELECT snapshot FROM workflows").fetchone()[0] == '{"retained": true}'
    store = Store(f"sqlite:///{path}")
    store.close()


def test_prompt_seeding_default_updates_and_api_precedence(tmp_path, database_url):
    import hashlib
    from audit.db import PromptStoreError
    from audit.workflow.prompt_registry import DEFAULTS
    url = database_url
    store = Store(url)
    store.sync_prompt_defaults(DEFAULTS)
    store.sync_prompt_defaults(DEFAULTS)
    assert {name: row["text"] for name, row in store.active_prompts().items()} == DEFAULTS
    for name, text in DEFAULTS.items():
        assert len(store.prompt_versions(name)) == len(store.prompt_activations(name)) == 1
        row = store.prompt_version(name, 1)
        assert row["text"] == text
        assert row["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert row["origin"] == "default" and row["author"] == "system"
        assert row["note"] == "Seeded from source defaults"
        assert store.prompt_activations(name)[0]["kind"] == "seed"
    name = "relevance"
    updated = {**DEFAULTS, name: DEFAULTS[name] + "\nFabricated source revision"}
    store.sync_prompt_defaults(updated)
    assert store.active_prompts()[name]["version"] == 2
    assert store.prompt_activations(name)[0]["kind"] == "default_update"
    api = store.create_prompt_version(name, DEFAULTS[name] + "\nFabricated API revision", "tester", "")
    assert store.active_prompts()[name]["version"] == 2
    store.activate_prompt(name, api["version"], "activate", "tester", "", prompt_workflow)
    updated[name] += "\nAnother source revision"
    store.sync_prompt_defaults(updated)
    assert len(store.prompt_versions(name)) == 4
    assert store.active_prompts()[name]["version"] == api["version"]
    before = store.prompt_activations(name)
    store.activate_prompt(name, api["version"], "activate", "tester", "", prompt_workflow)
    assert store.prompt_activations(name) == before
    with pytest.raises(PromptStoreError, match="duplicate_version"):
        store.create_prompt_version(name, api["text"], "tester", "")
    assert len(store.prompt_versions(name)) == 4
    store.close()
    store = Store(url)
    store.sync_prompt_defaults(updated)
    assert store.active_prompts()[name]["version"] == api["version"]
    assert len(store.prompt_versions(name)) == 4
    store.close()


def test_default_registry_matches_source_constants_exactly():
    from audit.workflow import prompts
    from audit.workflow.prompt_registry import DEFAULTS
    assert DEFAULTS == {
        "relevance": prompts.RELEVANCE_PROMPT,
        "classification_coarse": prompts.CLASSIFICATION_COARSE_PROMPT,
        "classification_fine": prompts.CLASSIFICATION_FINE_PROMPT,
        "source": prompts.SOURCE_PROMPT,
        "web_search": prompts.WEB_SEARCH_PROMPT,
        "related_removals": prompts.RELATED_REMOVALS_PROMPT,
    }


@pytest.mark.parametrize("kind", ["activate", "rollback"])
def test_prompt_activation_workflow_insert_failure_rolls_back(tmp_path, kind, database_url):
    from sqlalchemy import event
    from audit.db import Workflow
    from audit.workflow.prompt_registry import DEFAULTS
    store = Store(database_url)
    try:
        store.sync_prompt_defaults(DEFAULTS)
        version = store.create_prompt_version("relevance", DEFAULTS["relevance"] + "\nFabricated atomic edit", "Reviewer", "")
        if kind == "rollback":
            store.activate_prompt("relevance", version["version"], "activate", "Reviewer", "", prompt_workflow)
        active_before = store.active_prompts()
        history_before = store.prompt_activations("relevance")
        target = version["version"] if kind == "activate" else None

        def snapshot(texts):
            return {**prompt_workflow(texts), "id": "wf_fabricated_atomic"}

        def fail_insert(_mapper, _connection, _target):
            raise RuntimeError("Fabricated workflow insert failure")

        event.listen(Workflow, "before_insert", fail_insert)
        try:
            with pytest.raises(RuntimeError, match="Fabricated workflow insert failure"):
                store.activate_prompt("relevance", target, kind, "Reviewer", "", snapshot)
        finally:
            event.remove(Workflow, "before_insert", fail_insert)
        assert store.active_prompts() == active_before
        assert store.prompt_activations("relevance") == history_before
        assert store.workflow("wf_fabricated_atomic") is None
        active, saved = store.activate_prompt("relevance", target, kind, "Reviewer", "", snapshot)
        assert active["active"] and "text" not in active
        assert active["version"] == (2 if kind == "activate" else 1)
        assert saved == store.workflow(saved["id"])
        assert saved["definition"]["prompts"] == {name: row["text"] for name, row in store.active_prompts().items()}
        history = store.prompt_activations("relevance")
        repeated, existing = store.activate_prompt("relevance", active["version"], "activate", "Reviewer", "", snapshot)
        assert repeated == active and existing == saved
        assert store.prompt_activations("relevance") == history
    finally:
        store.close()


def test_activation_order_uses_append_order_when_timestamps_tie(monkeypatch, database_url):
    import audit.db as db
    from audit.workflow.prompt_registry import DEFAULTS
    monkeypatch.setattr(db, 'now', lambda: 'fabricated-identical-timestamp')
    store = Store(database_url)
    try:
        store.sync_prompt_defaults(DEFAULTS)
        created = store.create_prompt_version('relevance', DEFAULTS['relevance'] + '\nFabricated change', 'Reviewer', '')
        store.activate_prompt('relevance', created['version'], 'activate', 'Reviewer', '', prompt_workflow)
        assert store.active_prompts()['relevance']['version'] == 2
        assert store.prompt_activations('relevance')[0]['kind'] == 'activate'
        store.activate_prompt('relevance', None, 'rollback', 'Reviewer', '', prompt_workflow)
        assert store.active_prompts()['relevance']['version'] == 1
        assert store.prompt_activations('relevance')[0]['kind'] == 'rollback'
    finally:
        store.close()


def test_version_two_migrates_assignments_without_changing_existing_data(tmp_path):
    from audit.db import MIGRATIONS, SCHEMA_VERSION
    path = tmp_path / 'version-two.sqlite3'
    old_database(path)
    store = Store(f'sqlite:///{path}', migrations=MIGRATIONS[:2])
    store.sync_prompt_defaults({'relevance': 'Invented retained prompt'})
    prompts = store.active_prompts()
    store.close()
    # Current metadata creates all tables even with a custom migration list; reproduce v2.
    with sqlite3.connect(path) as connection:
        connection.execute('DROP TABLE model_assignments')
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 2
    for _ in range(2):
        store = Store(f'sqlite:///{path}')
        assert store.workflow('wf_old') == {'retained': True}
        assert store.run('run_old')['input'] == INPUT
        assert store.run('run_old')['reactions'][0]['id'] == 'rx_old'
        assert store.active_prompts() == prompts
        assert store.model_assignments() == {}
        with store.engine.connect() as connection:
            assert connection.exec_driver_sql('PRAGMA user_version').scalar_one() == SCHEMA_VERSION == 7
        store.close()


@pytest.mark.parametrize("running", [False, True])
def test_cancel_is_terminal_and_keeps_recorded_steps(tmp_path, running, database_url):
    store = Store(database_url)
    workflow(store)
    run_id = create(store)["id"]
    if running:
        assert store.start_run(run_id)
        assert not store.start_run(run_id)
        store.append_step(run_id, {"name": "classification", "status": "completed"})
    assert store.cancel_run(run_id)
    before = store.run(run_id)
    assert not store.cancel_run(run_id)
    assert not store.start_run(run_id)
    store.complete_run(run_id, {"response": {"verdict": "INCONCLUSIVE"}, "steps": []})
    store.fail_run(run_id)
    store.fail_run(run_id, "interrupted")
    store.append_step(run_id, {"name": "similarity", "status": "completed"})
    store.interrupt_incomplete()
    assert store.run(run_id) == before
    assert before["status"] == "cancelled"
    assert before["verdict"] is None
    assert before["output"] is None
    assert len(before["steps"]) == int(running)
    store.close()


@pytest.mark.parametrize("failed", [False, True])
def test_terminal_result_cannot_be_overwritten(tmp_path, failed, database_url):
    store = Store(database_url)
    workflow(store)
    run_id = create(store)["id"]
    if failed:
        store.fail_run(run_id)
    else:
        store.complete_run(run_id, {"response": {"verdict": "INCONCLUSIVE"}})
    before = store.run(run_id)
    assert not store.cancel_run(run_id)
    assert not store.start_run(run_id)
    store.fail_run(run_id, "interrupted")
    store.complete_run(run_id, {"response": {"verdict": "MOVED"}})
    assert store.run(run_id) == before
    store.close()


def test_record_lineage_selects_only_latest_eligible_id(database_url):
    from sqlalchemy import event

    store = Store(database_url)
    workflow(store)
    try:
        def saved(day, *, category="live", state="succeeded", error=None, output=None):
            row = store.create_run(workflow_id="wf_test", category=category,
                                   origin="api", input=INPUT)
            store._update(row["id"], created_at=f"2026-11-{day:02}T00:00:00+00:00",
                          finished_at=f"2026-11-{day:02}T00:01:00+00:00", status=state,
                          output=output, error=error)
            return store.run(row["id"])

        first = saved(1, output={"verdict": "INCONCLUSIVE"})
        previous = saved(2, state="failed", error={"code": "audit_failed", "message": "Fixed failure."})
        saved(3, category="benchmark", output={"verdict": "INCONCLUSIVE"})
        saved(4, state="failed", error={"code": "interrupted", "message": "Fixed interruption."})
        saved(5, state="cancelled")
        saved(6)  # No stored result.
        current = saved(7, output={"verdict": "INCONCLUSIVE"})
        saved(8, output={"verdict": "INCONCLUSIVE"})  # A later run is never an ancestor.
        saved(7, output={"verdict": "INCONCLUSIVE"})  # Equal creation times are not earlier.
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(store.engine, "before_cursor_execute", capture)
        assert store.previous_record_run_id(current) == previous["id"]
        assert len(statements) == 1
        projection = statements[0].split("FROM", 1)[0].strip()
        assert projection == "SELECT runs.id"
        event.remove(store.engine, "before_cursor_execute", capture)
        assert store.previous_record_run_id(first) is None
        assert store.previous_record_run_id({**current, "article_ref": None}) is None
        assert store.previous_record_run_id({**current, "category": "benchmark"}) is None
    finally:
        store.close()


def test_sync_migration_preserves_existing_articles(tmp_path):
    from audit.db import ArticleRow, get_version
    from sqlalchemy.orm import Session
    path = tmp_path / "old-sync.sqlite3"
    store = Store(f"sqlite:///{path}")
    store.import_articles([{"article_id": 1, "url": "https://paper.example/fiction",
                            "title": "Invented retained report"}])
    store.close()
    # Reproduce the schema shipped before sync persistence, including existing rows.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE poll_state")
        connection.execute("DROP TABLE poll_cycles")
        connection.execute("ALTER TABLE articles DROP COLUMN detected_at")
        connection.execute("PRAGMA user_version=5")
    for _ in range(2):
        store = Store(f"sqlite:///{path}")
        with store.engine.connect() as connection:
            assert get_version(connection) == 7
        assert store.article("ka-1")["title"] == "Invented retained report"
        assert store.poll_status() == {"history": [], "last_success_at": None}
        with Session(store.engine) as session:
            assert session.get(ArticleRow, "ka-1").detected_at is None
        store.close()


def test_article_order_uses_detection_then_registration_not_runs(database_url):
    from audit.db import ArticleRow
    from sqlalchemy.orm import Session
    store = Store(database_url)
    workflow(store)
    # Deliberately import newest first: registration order must not reverse detection.
    store.import_articles([
        {"article_id": 3, "url": "https://paper.example/new", "created_at": 1795132800},
        {"article_id": 1, "url": "https://paper.example/old",
         "created_at": "2026-11-18T01:00:00+01:00"},
        {"article_id": 2, "url": "https://paper.example/fallback"},
    ])
    with Session(store.engine) as session, session.begin():
        session.get(ArticleRow, "ka-2").created_at = "2026-11-19T00:00:00+00:00"
        session.get(ArticleRow, "ka-1").content = "Invented prose for a manual audit."
    store.create_run(workflow_id="wf_test", category="live", origin="article",
                     input=INPUT, article_ref="ka-1")
    assert [row["ref"] for row in store.articles()["items"]] == ["ka-3", "ka-2", "ka-1"]
    # Reimport fills old rows without a detection date and counts the metadata update.
    assert store.import_articles([{"article_id": 2, "url": "https://paper.example/fallback",
                                   "created_at": "2026-11-21T00:00:00Z"}])["updated"] == 1
    assert store.articles()["items"][0]["ref"] == "ka-2"
    store.close()
