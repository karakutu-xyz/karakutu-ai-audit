"""Data-copy verification uses invented rows and local databases only by default."""
from decimal import Decimal
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from audit.db import Base, ModelAssignment, PromptActivation, Store, insertion_order
from test_db import create, workflow

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("copy_database", ROOT / "scripts/migrate_sqlite_to_postgres.py")
copy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(copy)


@pytest.fixture
def source_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'source.sqlite3'}"
    store = Store(url)
    workflow(store)
    for name in ("wf-Alpha", "wf_Zeta", "wf_alpha"):
        workflow(store, name)
    run = create(store)
    store.complete_run(run["id"], {"response": {"verdict": "INCONCLUSIVE"}, "steps": []})
    store.add_reaction(run["id"], {"reaction": "agree", "reviewer": "Invented reviewer", "note": "", "expected_verdict": None})
    store.sync_prompt_defaults({"relevance": "Invented first prompt"})
    version = store.create_prompt_version("relevance", "Invented next prompt", "Reviewer", "")
    store.activate_prompt("relevance", version["version"], "activate", "Reviewer", "", lambda texts: {"id": "wf_next"})
    store.assign_model("relevance", "invented-first", "Reviewer", "", lambda: {"id": "wf_first"})
    store.assign_model("relevance", None, "Reviewer", "", lambda: {"id": "wf_last"})
    store.close()
    return url


def backend(database_url):
    return "postgresql" if database_url.startswith("postgresql") else "sqlite"


def test_copy_counts_hashes_selection_and_nonempty_rerun(source_url, database_url):
    counts = copy.copy_database(source_url, database_url, _target_backend=backend(database_url))
    source, target = Store(source_url), Store(database_url)
    try:
        assert set(counts) == set(Base.metadata.tables)
        with source.engine.connect() as left, target.engine.connect() as right:
            for table in Base.metadata.sorted_tables:
                columns = [c for c in table.c if c.name != "seq"]
                assert copy._digest(left, table, columns) == copy._digest(right, table, columns)
                assert copy._digest(right, table, columns)[0] == counts[table.name]
            for model in (PromptActivation, ModelAssignment):
                query = select(model.id).order_by(insertion_order(model))
                assert left.scalars(query).all() == right.scalars(query).all()
        assert source.active_prompts() == target.active_prompts()
        assert source.model_assignments() == target.model_assignments() == {"relevance": None}
        with pytest.raises(copy.CopyRefused, match="not empty"):
            copy.copy_database(source_url, database_url, _target_backend=backend(database_url))
    finally:
        source.close()
        target.close()


def test_dry_run_does_not_create_schema_or_change_source(source_url, database_url):
    source_path = Path(source_url.removeprefix("sqlite:///"))
    before = source_path.read_bytes()
    counts = copy.copy_database(source_url, database_url, dry_run=True,
                                _target_backend=backend(database_url))
    assert counts["runs"] == 1 and counts["model_assignments"] == 2
    engine = copy.create_engine(database_url)
    try:
        assert inspect(engine).get_table_names() == []
    finally:
        engine.dispose()
    assert source_path.read_bytes() == before


def test_copy_failure_rolls_back_all_rows(source_url, database_url, monkeypatch):
    original = copy._digest
    calls = 0

    def corrupted(*args):
        nonlocal calls
        calls += 1
        result = original(*args)
        return (result[0] + 1, result[1]) if calls == 4 else result

    monkeypatch.setattr(copy, "_digest", corrupted)
    with pytest.raises(copy.CopyRefused, match="verification"):
        copy.copy_database(source_url, database_url, _target_backend=backend(database_url))
    engine = copy.create_engine(database_url)
    try:
        with engine.connect() as connection:
            copy._empty(connection)
    finally:
        engine.dispose()


def test_refuses_unrelated_nonempty_table_before_schema_creation(source_url, database_url):
    engine = copy.create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE unrelated (id INTEGER)")
            connection.exec_driver_sql("INSERT INTO unrelated VALUES (1)")
        with pytest.raises(copy.CopyRefused, match="not empty"):
            copy.copy_database(source_url, database_url, _target_backend=backend(database_url))
        assert inspect(engine).get_table_names() == ["unrelated"]
    finally:
        engine.dispose()


def test_cli_failure_is_value_free(capsys, tmp_path):
    value = "postgresql+psycopg://audit:fictional-private-password@db.example.invalid/audit"
    assert copy.main([f"sqlite:///{tmp_path / 'missing.sqlite3'}", value]) == 1
    output = capsys.readouterr()
    assert not output.out and "fictional-private-password" not in output.err


def test_cli_argument_errors_do_not_echo_urls(capsys):
    private = "postgresql+psycopg://audit:fictional-private-password@db.example.invalid/audit"
    with pytest.raises(SystemExit) as error:
        copy.main(["source", "target", private])
    assert error.value.code == 2
    assert "fictional-private-password" not in capsys.readouterr().err


@pytest.mark.parametrize("name, demo", [("source-test.sqlite3", False), ("source.sqlite3", True)])
def test_refuses_demo_or_test_source(tmp_path, database_url, capsys, name, demo):
    """A demo or test database never becomes live data in the target."""
    url = f"sqlite:///{tmp_path / name}"
    store = Store(url)
    workflow(store, "wf_demo_v1" if demo else "wf_invented")
    store.close()
    with pytest.raises(copy.CopyRefused, match="Demo or test databases"):
        copy.copy_database(url, database_url, _target_backend=backend(database_url))
    assert copy.main([url, "postgresql+psycopg://audit:fictional@db.example.invalid/audit"]) == 1
    assert "Demo or test databases are never copied" in capsys.readouterr().err


def test_hash_treats_equal_json_numbers_alike():
    """PostgreSQL JSONB rewrites 1e20 as 100000000000000000000 and -0.0 as 0."""
    enc = copy._encode
    assert enc({"n": 1e20}) == enc({"n": 100000000000000000000})
    assert enc([1.5, 2.0, -0.0, 0]) == enc([1.50, 2, 0, 0])
    assert enc(2.5e-7) == enc(Decimal("0.00000025"))


def test_hash_never_confuses_unequal_values():
    enc = copy._encode
    assert enc(10**29) != enc(10**29 + 1)
    assert enc(1) != enc({"$number": "1"}) != enc("1")
    assert enc(True) != enc(1) and enc(None) != enc("n")
    assert enc(0.1) != enc(0.2)
    assert enc({"a": 1, "b": 2}) == enc({"b": 2, "a": 1})
    with pytest.raises(copy.CopyRefused):
        enc(float("nan"))


def test_copy_verifies_rows_with_exponent_numbers(tmp_path, database_url):
    url = f"sqlite:///{tmp_path / 'numbers.sqlite3'}"
    store = Store(url)
    workflow(store)
    run = create(store)
    store.complete_run(run["id"], {"response": {"verdict": "INCONCLUSIVE", "score": 1e20,
                                                "zero": -0.0, "big": 10**29 + 1},
                                   "steps": [{"name": "fictional", "value": 2.5e-7}]})
    store.close()
    counts = copy.copy_database(url, database_url, _target_backend=backend(database_url))
    assert counts["runs"] == 1


def test_copy_keeps_json_numbers_exact(tmp_path, database_url):
    """Numbers that a float cannot hold survive the copy and its verification exactly."""
    import json as json_module
    from sqlalchemy import Text, cast, text
    from audit.db import Run
    url = f"sqlite:///{tmp_path / 'exact.sqlite3'}"
    store = Store(url)
    workflow(store)
    run = create(store)
    store.close()
    raw = '{"verdict":"INCONCLUSIVE","a":0.10000000000000001,"b":1e-400,"c":9007199254740993}'
    from sqlalchemy import create_engine
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("UPDATE runs SET output = :raw WHERE id = :id"),
                           {"raw": raw, "id": run["id"]})
    engine.dispose()
    copy.copy_database(url, database_url, _target_backend=backend(database_url))
    target = Store(database_url)
    try:
        with target.engine.connect() as connection:
            stored = connection.execute(select(cast(Run.output, Text)).where(
                Run.id == run["id"])).scalar_one()
    finally:
        target.close()
    exact = json_module.loads(stored, parse_float=Decimal)
    assert exact["a"] == Decimal("0.10000000000000001")
    assert exact["b"] == Decimal("1e-400")
    assert exact["c"] == 9007199254740993


def test_hash_distinguishes_sql_null_from_json_null(tmp_path):
    from sqlalchemy import create_engine, text
    url = f"sqlite:///{tmp_path / 'nulls.sqlite3'}"
    store = Store(url)
    workflow(store)
    run = create(store)
    store.close()
    engine = create_engine(url)
    from audit.db import Run
    columns = [column for column in Run.__table__.columns if column.name != "seq"]
    digests = []
    for raw in ("null", None):
        with engine.begin() as connection:
            connection.execute(text("UPDATE runs SET output = :raw WHERE id = :id"),
                               {"raw": raw, "id": run["id"]})
        with engine.connect() as connection:
            digests.append(copy._digest(connection, Run.__table__, columns))
    engine.dispose()
    assert digests[0] != digests[1]
