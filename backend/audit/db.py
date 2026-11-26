"""SQLAlchemy persistence for immutable workflows and append-only human reactions.

One local service process owns the worker queue. Interrupted runs fail on restart;
no model work is silently replayed. Workflow and input JSON are never updated.
"""
from collections.abc import Callable
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from unicodedata import normalize
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import (
    JSON, Identity, Integer, Index, ForeignKey, and_, bindparam, case, cast, or_, String, Text,
    UniqueConstraint, create_engine, event, func, inspect, literal_column, select, update,
)
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateColumn
from sqlalchemy.sql.expression import ColumnElement

from audit.config import database_backend


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# PostgreSQL `json`, not `jsonb`: it keeps the saved text verbatim (exact number formatting
# and key order) and accepts the \u0000 escape, so captured evidence is stored exactly.
JSON_VALUE = JSON()
WRITER_LOCK = 684219705


def begin_write(connection: Connection):
    # One global writer lock protects check-then-insert paths and snapshot changes.
    # Explicit BEGIN also makes SQLite DDL transactional.
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        connection.exec_driver_sql(f"SELECT pg_advisory_xact_lock({WRITER_LOCK})")


class _InsertionOrder(ColumnElement):
    inherit_cache = False
    type = Integer()

    def __init__(self, table):
        self.table = getattr(table, "__table__", table)


@compiles(_InsertionOrder)
def _sequence_order(element, compiler, **kw):
    table = element.table
    # Migration 4's legacy runs have no append sequence; their PK breaks ties.
    column = table.c.seq if "seq" in table.c else next(iter(table.primary_key))
    return compiler.process(column, **kw)


@compiles(_InsertionOrder, "sqlite")
def _sqlite_order(element, compiler, **kw):
    return compiler.preparer.format_table(element.table) + ".rowid"


def insertion_order(table):
    """Insertion order, independent of timestamps and random IDs."""
    return _InsertionOrder(table)


def get_version(connection: Connection) -> int:
    if connection.dialect.name == "sqlite":
        return connection.exec_driver_sql("PRAGMA user_version").scalar_one()
    if not inspect(connection).has_table("schema_version"):
        return 0
    return connection.exec_driver_sql("SELECT version FROM schema_version").scalar_one()


def set_version(connection: Connection, version: int):
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql(f"PRAGMA user_version={int(version)}")
    else:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(singleton INTEGER PRIMARY KEY CHECK (singleton = 1), version INTEGER NOT NULL)")
        connection.exec_driver_sql("DELETE FROM schema_version")
        connection.exec_driver_sql(
            "INSERT INTO schema_version (singleton, version) VALUES (1, %s)", (version,))


@compiles(CreateColumn, "postgresql")
def _postgres_column(element, compiler, **kw):
    ddl = compiler.visit_create_column(element, **kw)
    # PostgreSQL identity is implicitly NOT NULL and rejects an explicit NULL.
    # SQLite needs the same metadata column nullable because it does not generate seq.
    if element.element.identity is not None and element.element.nullable:
        return ddl.removesuffix(" NULL")
    return ddl


class Base(DeclarativeBase):
    pass


class Workflow(Base):
    __tablename__ = "workflows"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON_VALUE)


class ArticleRow(Base):
    __tablename__ = "articles"
    __table_args__ = (
        Index("uq_articles_archive_id", "archive_id", unique=True,
              sqlite_where=literal_column("archive_id IS NOT NULL"),
              postgresql_where=literal_column("archive_id IS NOT NULL")),
        # URL identity. SQLite indexes the full text, as before. A PostgreSQL B-tree entry
        # is limited to about a third of a page, so a long (allowed) URL would fail there:
        # uniqueness uses md5(url) and equality lookups a hash index, neither size-limited.
        Index("ix_articles_url", "url", unique=True).ddl_if(dialect="sqlite"),
        Index("uq_articles_url_md5", func.md5(literal_column("url")),
              unique=True).ddl_if(dialect="postgresql"),
        Index("ix_articles_url_hash", "url", postgresql_using="hash").ddl_if(dialect="postgresql"),
    )
    ref: Mapped[str] = mapped_column(Text, primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    search_text: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    host: Mapped[str] = mapped_column(Text)
    published_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    removed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    archive_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    snapshot: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    category: Mapped[str] = mapped_column(String(20))
    origin: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[str] = mapped_column(String(40))


def article_search_text(title: str, source: str | None, host: str, url: str) -> str:
    return normalize("NFKC", " ".join((title, source or "", host, url))).casefold()


@event.listens_for(ArticleRow, "before_insert")
@event.listens_for(ArticleRow, "before_update")
def _refresh_article_search(_mapper, _connection, row):
    row.search_text = article_search_text(row.title, row.source, row.host, row.url)


LEGACY_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def readable_removed_at(value):
    """Older versions stored archive removal dates as Unix seconds, written with `str()` of
    whatever the archive sent ("1700000000", "1700000000.0", "1e+30"). Present them as ISO
    like new rows, on every backend, without a migration; an unusable number becomes unknown
    and anything that is not a plain number (an ISO date) is returned unchanged."""
    if isinstance(value, str) and LEGACY_NUMBER.fullmatch(value.strip()):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    return value


def without_nul(value):
    """PostgreSQL text columns cannot hold NUL characters; SQLite can. Archive article data
    is unvalidated upstream input written into text columns, so drop NUL there (also nested
    in the article snapshot) before a write, keeping both backends' stored values identical.
    JSON columns (runs, workflows) are PostgreSQL `json`, which stores NUL as \u0000 exactly;
    submitted input is rejected at the schema."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {without_nul(key): without_nul(item) for key, item in value.items()}
    if isinstance(value, list):
        return [without_nul(item) for item in value]
    return value


def normalized_url(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def article_values(article: dict, category: str, origin="run", created_at=None) -> dict:
    url = normalized_url(article["url"])
    stamp = created_at or now()
    return {"ref": "u-" + hashlib.sha256(url.encode()).hexdigest()[:16],
            "url": url, "title": article["title"], "content": article.get("content") or "",
            "host": urlsplit(url).hostname or "", "published_at": article.get("published_at"),
            "search_text": article_search_text(article["title"], None, urlsplit(url).hostname or "", url),
            "category": category, "origin": origin, "created_at": stamp, "updated_at": stamp}


class ArticleStoreError(ValueError):
    def __init__(self, code, run_id=None):
        self.code, self.run_id = code, run_id
        super().__init__(code)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_article_activity", "article_ref", "created_at"),)
    article_ref: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), index=True)
    category: Mapped[str] = mapped_column(String(20), index=True)
    origin: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[str] = mapped_column(String(40), index=True)
    finished_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    article_title: Mapped[str] = mapped_column(Text)
    verdict: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    synthetic: Mapped[bool]
    input: Mapped[dict] = mapped_column(JSON_VALUE)
    output: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    steps: Mapped[list] = mapped_column(JSON_VALUE)
    model: Mapped[dict] = mapped_column(JSON_VALUE)
    error: Mapped[dict | None] = mapped_column(JSON_VALUE, nullable=True)
    expected_verdict: Mapped[str | None] = mapped_column(String(50), nullable=True)


class Reaction(Base):
    __tablename__ = "reactions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"))
    reaction: Mapped[str] = mapped_column(String(20))
    reviewer: Mapped[str] = mapped_column(String(120))
    note: Mapped[str] = mapped_column(Text)
    expected_verdict: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40))


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("name", "version"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    version: Mapped[int]
    text: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(String(20))
    author: Mapped[str] = mapped_column(String(120))
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40))


class PromptActivation(Base):
    seq: Mapped[int | None] = mapped_column(Integer, Identity(), nullable=True)
    __tablename__ = "prompt_activations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("prompt_versions.id"))
    kind: Mapped[str] = mapped_column(String(20))
    author: Mapped[str] = mapped_column(String(120))
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40))


class ModelAssignment(Base):
    seq: Mapped[int | None] = mapped_column(Integer, Identity(), nullable=True)
    __tablename__ = "model_assignments"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    step: Mapped[str] = mapped_column(String(80), index=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    author: Mapped[str] = mapped_column(String(120))
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40))


def _baseline(connection: Connection):
    """Version 1: the original workflows, runs and reactions schema."""


def _prompt_tables(connection: Connection):
    PromptVersion.__table__.create(connection, checkfirst=True)
    PromptActivation.__table__.create(connection, checkfirst=True)


def _model_assignments(connection: Connection):
    ModelAssignment.__table__.create(connection, checkfirst=True)


def _articles(connection: Connection):
    ArticleRow.__table__.create(connection, checkfirst=True)
    if "article_ref" not in {column["name"] for column in inspect(connection).get_columns("runs")}:
        connection.exec_driver_sql("ALTER TABLE runs ADD COLUMN article_ref VARCHAR(80)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_runs_article_ref ON runs (article_ref)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_runs_article_activity ON runs (article_ref, created_at)")
    # Keyset over metadata only: repeated runs never load their full article text.
    rowid = insertion_order(Run)
    fields = [Run.input["removed_article"][key].as_string().label(key)
              for key in ("url", "title", "published_at")]
    cursor = None
    while True:
        query = select(rowid.label("rowid"), Run.id, Run.category, Run.created_at, *fields).where(
            Run.article_ref.is_(None))
        if cursor is not None:
            stamp, last_rowid = cursor
            query = query.where(or_(Run.created_at > stamp,
                (Run.created_at == stamp) & (rowid > last_rowid)))
        rows = connection.execute(query.order_by(Run.created_at, rowid).limit(500)).mappings().all()
        if not rows:
            break
        # Preserve the earliest run for each normalized URL within this batch.
        first = {}
        for row in rows:
            first.setdefault(normalized_url(row["url"]), row)
        refs = dict(connection.execute(select(ArticleRow.url, ArticleRow.ref).where(
            ArticleRow.url.in_(first))).all())
        missing = {url: row for url, row in first.items() if url not in refs}
        if missing:
            contents = dict(connection.execute(select(Run.id,
                Run.input["removed_article"]["content"].as_string()).where(
                    Run.id.in_([row["id"] for row in missing.values()]))).all())
            values = []
            for url, row in missing.items():
                article = article_values({"url": url, "title": row["title"],
                    "published_at": row["published_at"], "content": contents[row["id"]]},
                    row["category"], created_at=row["created_at"])
                values.append(article)
                refs[url] = article["ref"]
            connection.execute(ArticleRow.__table__.insert(), values)
        connection.execute(update(Run.__table__).where(Run.id == bindparam("run_id")).values(
            article_ref=bindparam("ref")),
            [{"run_id": row["id"], "ref": refs[normalized_url(row["url"])]} for row in rows])
        cursor = (rows[-1]["created_at"], rows[-1]["rowid"])


# Unbounded archive metadata has no API length contract. Text preserves SQLite behavior.
WIDENED_COLUMNS = {
    "articles": ("ref", "source", "host", "published_at", "removed_at",
                 "archive_id", "language"),
    "runs": ("article_ref",),
}


def _portable_storage(connection: Connection):
    for table in (PromptActivation.__table__, ModelAssignment.__table__):
        columns = {column["name"] for column in inspect(connection).get_columns(table.name)}
        if "seq" not in columns:
            suffix = ("INTEGER" if connection.dialect.name == "sqlite" else
                      "INTEGER GENERATED BY DEFAULT AS IDENTITY")
            connection.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN seq {suffix}")
    if connection.dialect.name == "postgresql":
        for table, columns in WIDENED_COLUMNS.items():
            for column in columns:
                connection.exec_driver_sql(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TEXT")


MIGRATIONS: list[tuple[int, Callable[[Connection], None]]] = [
    (1, _baseline), (2, _prompt_tables), (3, _model_assignments), (4, _articles),
    (5, _portable_storage),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]
# Insertion order defines "newest", even when timestamps tie or clocks change.
ACTIVATION_ORDER = insertion_order(PromptActivation).desc()


class PromptStoreError(ValueError):
    """A safe domain error code, without stored or submitted values."""


ERRORS = {
    "interrupted": "The service stopped before this audit completed. Submit a new run to retry.",
    "audit_failed": "The audit could not be completed. Submit a new run to retry.",
    "cancelled": ("A person stopped this audit before it finished. "
                  "Submit a new run to retry."),
}


class Store:
    def __init__(self, database_url: str, *,
                 migrations: list[tuple[int, Callable[[Connection], None]]] | None = None):
        url = database_backend(database_url)
        if url.get_backend_name() == "sqlite":
            memory = not url.database or url.database == ":memory:"
            if not memory:
                Path(url.database).parent.mkdir(parents=True, exist_ok=True)
            options: dict[str, Any] = {
                "connect_args": {"check_same_thread": False, "timeout": 5}}
            if memory:
                options["poolclass"] = StaticPool
        else:
            options = {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 5,
                       "pool_recycle": 1800, "connect_args": {"connect_timeout": 10}}
        self.engine = create_engine(url, hide_parameters=True, **options)

        if url.get_backend_name() == "sqlite":
            @event.listens_for(self.engine, "connect")
            def sqlite_options(connection, _record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()

        migrations = MIGRATIONS if migrations is None else migrations
        schema_version = migrations[-1][0]
        try:
            with self.engine.connect() as connection:
                version = get_version(connection)
                if version > schema_version:
                    raise ValueError("Database schema is newer than this code supports")
                fresh = not inspect(connection).get_table_names()
                connection.rollback()
                if fresh:
                    with connection.begin():
                        begin_write(connection)
                        Base.metadata.create_all(connection)
                        set_version(connection, schema_version)
                else:
                    for target, migrate in migrations:
                        if target > version:
                            with connection.begin():
                                # Explicit BEGIN also makes SQLite DDL transactional.
                                begin_write(connection)
                                migrate(connection)
                                set_version(connection, target)
                    Base.metadata.create_all(connection)
                    connection.commit()
        except DBAPIError as error:
            self.engine.dispose()
            if error.connection_invalidated or not getattr(error, "statement", None):
                backend = url.get_backend_name()
                raise RuntimeError(
                    f"Database unreachable ({backend} at {url.host or 'local'}/{url.database or ''})"
                ) from None
            raise
        except Exception:
            self.engine.dispose()
            raise

    def has_demo_data(self) -> bool:
        """True when the database holds a demo workflow (`wf_demo_*`), on any backend.

        Live mode must never serve demo data. The SQLite file check in config runs before
        this, but cannot inspect a server database, so startup asks the store as well."""
        with self.engine.connect() as connection:
            return connection.execute(select(Workflow.id).where(
                func.substr(Workflow.id, 1, 8) == "wf_demo_").limit(1)).first() is not None

    def close(self):
        self.engine.dispose()

    def interrupt_incomplete(self):
        with Session(self.engine) as session, session.begin():
            session.execute(update(Run).where(Run.status.in_(["queued", "running"])).values(
                status="failed", finished_at=now(), error={"code": "interrupted", "message": ERRORS["interrupted"]}))

    def save_workflow(self, snapshot: dict) -> dict:
        with Session(self.engine) as session, session.begin():
            stored = session.get(Workflow, snapshot["id"])
            if stored is None:
                session.add(Workflow(id=snapshot["id"], snapshot=snapshot))
                return snapshot
            return stored.snapshot

    def workflow(self, workflow_id: str) -> dict | None:
        with Session(self.engine) as session:
            row = session.get(Workflow, workflow_id)
            return row.snapshot if row else None

    def create_run(self, *, workflow_id: str, category: str, origin: str, input: dict,
                   label: str | None = None, expected_verdict: str | None = None,
                   synthetic: bool = False, article_ref: str | None = None) -> dict:
        if category not in {"benchmark", "live"} or origin not in {"benchmark", "api", "webhook", "article", "poll"}:
            raise ValueError("invalid run origin")
        run_id = "run_" + uuid4().hex
        with Session(self.engine) as session, session.begin():
            begin_write(session.connection())
            if article_ref is not None:
                article = session.get(ArticleRow, article_ref)
                if article is None:
                    raise ArticleStoreError("not_found")
                active = session.scalar(select(Run.id).where(Run.article_ref == article_ref,
                    Run.status.in_(["queued", "running"])).order_by(Run.created_at.desc(), Run.id.desc()).limit(1))
                if active:
                    raise ArticleStoreError("run_in_progress", active)
                if not article.content.strip():
                    raise ArticleStoreError("article_has_no_content")
                category = article.category
                input = {"removed_article": {key: getattr(article, key) for key in
                         ("url", "title", "content", "published_at")}, "similar_articles": []}
                from audit.domain import AuditRequest
                AuditRequest.model_validate(input)
                if category == "benchmark":
                    latest = session.scalar(select(Run).where(Run.article_ref == article_ref,
                        Run.category == "benchmark").order_by(Run.created_at.desc(), Run.id.desc()).limit(1))
                    label, expected_verdict = (latest.label, latest.expected_verdict) if latest else (None, None)
            else:
                values = article_values(input["removed_article"], category)
                article = session.scalar(select(ArticleRow).where(ArticleRow.url == values["url"]))
                if article is None:
                    article = ArticleRow(**values)
                    session.add(article)
                article_ref = article.ref
            article.updated_at = now()
            session.add(Run(id=run_id, article_ref=article_ref, workflow_id=workflow_id, category=category, origin=origin,
                            status="queued", created_at=now(), input=input, steps=[], model={},
                            label=label, article_title=input["removed_article"]["title"],
                            expected_verdict=expected_verdict, synthetic=synthetic))
        return self.run(run_id)

    def start_run(self, run_id: str):
        return self._update(run_id, statuses=("queued",), status="running")

    def append_step(self, run_id: str, step: dict):
        with Session(self.engine) as session, session.begin():
            row = session.get(Run, run_id)
            if row is not None and row.status in {"queued", "running"}:
                row.steps = [*row.steps, step]

    def complete_run(self, run_id: str, result: dict):
        response = result["response"]
        verdict = response.get("verdict")
        failed = verdict == "AUDIT_FAILED"
        self._update(run_id, status="failed" if failed else "succeeded", finished_at=now(),
                     output=response, verdict=verdict, steps=result.get("steps", []),
                     model=result.get("model", {}), synthetic=result.get("synthetic", False),
                     error={"code": "audit_failed", "message": ERRORS["audit_failed"]} if failed else None)

    def fail_run(self, run_id: str, code: str = "audit_failed"):
        if code not in ERRORS:
            code = "audit_failed"
        self._update(run_id, status="failed", finished_at=now(), error={"code": code, "message": ERRORS[code]})

    def cancel_run(self, run_id: str) -> bool:
        return self._update(run_id, status="cancelled", finished_at=now(),
                            verdict=None, output=None,
                            error={"code": "cancelled", "message": ERRORS["cancelled"]})

    def _update(self, run_id: str, *, statuses=("queued", "running"), **values) -> bool:
        with Session(self.engine) as session, session.begin():
            result = session.execute(update(Run).where(
                Run.id == run_id, Run.status.in_(statuses)).values(**values))
            return result.rowcount > 0

    def active_runs(self) -> dict[str, list[str]]:
        result = {"running": [], "queued": []}
        with Session(self.engine) as session:
            rows = session.execute(select(Run.id, Run.status).where(
                Run.status.in_(result)).order_by(Run.created_at, Run.id))
            for run_id, status in rows:
                result[status].append(run_id)
        return result

    @staticmethod
    def _reaction(row: Reaction) -> dict:
        return {name: getattr(row, name) for name in ["id", "run_id", "workflow_id", "reaction", "reviewer", "note", "expected_verdict", "created_at"]}

    def _summary(self, session: Session, row: Run) -> dict:
        result = {name: getattr(row, name) for name in ["id", "workflow_id", "category", "origin", "status", "created_at", "finished_at", "label", "article_title", "verdict", "synthetic"]}
        reactions = session.scalars(select(Reaction).where(Reaction.run_id == row.id).order_by(Reaction.created_at, Reaction.id)).all()
        result["reactions"] = [self._reaction(item) for item in reactions]
        return result

    def run(self, run_id: str) -> dict | None:
        with Session(self.engine) as session:
            row = session.get(Run, run_id)
            if row is None:
                return None
            result = {**self._summary(session, row), **{name: getattr(row, name) for name in ["article_ref", "input", "output", "steps", "model", "error", "expected_verdict"]}}
            result["article_archive_id"] = session.scalar(
                select(ArticleRow.archive_id).where(ArticleRow.ref == row.article_ref)
            ) if row.article_ref else None
            from audit.run_summary import summarize_run
            result["summary"] = summarize_run(result)
            return result

    def previous_record_run_id(self, run: dict) -> str | None:
        """Lineage needs only one ID; never load historical payloads or review data."""
        if (run["category"] != "live" or not run.get("article_ref")
                or run["status"] not in {"succeeded", "failed"} or not run.get("finished_at")):
            return None
        with Session(self.engine) as session:
            return session.scalar(select(Run.id).where(
                Run.article_ref == run["article_ref"], Run.category == "live",
                Run.status.in_(["succeeded", "failed"]), Run.finished_at.is_not(None),
                Run.created_at < run["created_at"],
                Run.id.not_like("run_demo_%"),
                or_(and_(Run.output.is_not(None), cast(Run.output, Text) != "null"),
                    Run.error["code"].as_string() == "audit_failed"),
            ).order_by(Run.created_at.desc(), Run.id.desc()).limit(1))

    def runs(self, *, category: str | None = None, limit: int = 50, offset: int = 0,
             verdict: str | None = None, workflow_id: str | None = None,
             status: str | None = None, origin: str | None = None) -> dict:
        conditions = []
        for name, value in [("category", category), ("verdict", verdict),
                            ("workflow_id", workflow_id), ("status", status), ("origin", origin)]:
            if value is not None:
                conditions.append(getattr(Run, name) == value)
        with Session(self.engine) as session:
            total = session.scalar(select(func.count()).select_from(Run).where(*conditions))
            rows = session.scalars(select(Run).where(*conditions).order_by(Run.created_at.desc(), Run.id).limit(limit).offset(offset)).all()
            return {"items": [self._summary(session, row) for row in rows], "total": total}

    @staticmethod
    def _article_query():
        # Select only list metadata, never input/output/steps JSON. Resolve ties by ID.
        latest = select(Run.article_ref, Run.id.label("latest_run_id"),
            Run.status.label("latest_run_status"), Run.verdict.label("latest_verdict"),
            Run.created_at.label("latest_run_at"),
            func.count().over(partition_by=Run.article_ref).label("run_count"),
            func.row_number().over(partition_by=Run.article_ref,
                order_by=(Run.created_at.desc(), Run.id.desc())).label("position")
        ).where(Run.article_ref.is_not(None)).subquery()
        status = case((latest.c.latest_run_id.is_(None), "not_audited"),
                      (latest.c.latest_run_status.in_(["queued", "running"]), "in_progress"),
                      (latest.c.latest_run_status == "succeeded", "audited"),
                      (latest.c.latest_run_status == "cancelled", "cancelled"), else_="failed").label("status")
        fields = [getattr(ArticleRow, key) for key in ("ref", "title", "source", "host", "url",
                  "published_at", "removed_at", "category")]
        # The archive's removal date arrives with the article's details, loaded when it is first
        # opened. Until then an empty removed_at means "not checked yet", not "no removal found".
        # A completed load always stores a snapshot (even for empty text); a failed or cleared
        # one leaves SQL NULL or JSON null, which both backends render as text 'null'.
        # Only archive-owned ("ka-") articles ever load archive details on open (the detail
        # endpoint fills nothing else), so any other article counts as checked.
        checked = case((ArticleRow.ref.not_like("ka-%"), True),
                       else_=and_(ArticleRow.snapshot.is_not(None),
                                  cast(ArticleRow.snapshot, Text) != "null")).label("removal_checked")
        return select(*fields, checked, status, latest.c.latest_run_id, latest.c.latest_run_status,
                      latest.c.latest_verdict, latest.c.latest_run_at,
                      func.coalesce(latest.c.run_count, 0).label("run_count"),
                      func.coalesce(latest.c.latest_run_at, ArticleRow.created_at).label("activity")
        ).outerjoin(latest, (latest.c.article_ref == ArticleRow.ref) & (latest.c.position == 1))

    def articles(self, *, status=None, verdict=None, category=None, q=None, limit=50, offset=0):
        query = self._article_query().subquery()
        conditions = []
        for key, value in (("status", status), ("latest_verdict", verdict), ("category", category)):
            if value is not None:
                conditions.append(query.c[key] == value)
        if q:
            needle = normalize("NFKC", q).casefold()
            needle = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(query.c.ref.in_(select(ArticleRow.ref).where(
                ArticleRow.search_text.like(f"%{needle}%", escape="\\"))))
        with Session(self.engine) as session:
            total = session.scalar(select(func.count()).select_from(query).where(*conditions))
            rows = session.execute(select(query).where(*conditions).order_by(
                query.c.activity.desc(), query.c.ref).limit(limit).offset(offset)).mappings()
            items = [{key: value for key, value in row.items() if key != "activity"} for row in rows]
            for item in items:
                item["removed_at"] = readable_removed_at(item["removed_at"])
            return {"items": items, "total": total, "limit": limit, "offset": offset}

    def article(self, ref: str) -> dict | None:
        from audit.run_summary import summarize_run
        with Session(self.engine) as session:
            article = session.get(ArticleRow, ref)
            if article is None:
                return None
            summary = dict(session.execute(self._article_query().where(ArticleRow.ref == ref)).mappings().one())
            summary.pop("activity")
            summary["removed_at"] = readable_removed_at(summary["removed_at"])
            runs = session.scalars(select(Run).where(Run.article_ref == ref)
                                   .order_by(Run.created_at.desc(), Run.id.desc())).all()
            history = []
            for row in runs:
                values = {key: getattr(row, key) for key in ("id", "status", "verdict", "created_at",
                          "finished_at", "origin", "category", "workflow_id", "label")}
                values["summary"] = summarize_run({**values, "steps": row.steps, "output": row.output})
                history.append(values)
            return {**summary, **{key: getattr(article, key) for key in
                    ("content", "language", "archive_id", "snapshot")}, "runs": history}

    def has_article_run(self, ref: str) -> bool:
        """Any attempt is terminal for automatic intake, regardless of origin/status."""
        with Session(self.engine) as session:
            return session.scalar(select(Run.id).where(Run.article_ref == ref).limit(1)) is not None

    def active_poll_runs(self) -> int:
        with Session(self.engine) as session:
            return session.scalar(select(func.count()).select_from(Run).where(
                Run.origin == "poll", Run.status.in_(["queued", "running"])))

    def unaudited_archive_refs(self) -> list[str]:
        # Registration time is the existing import timestamp; no article text is loaded. One
        # import registers a whole listing at once, so ties fall back to the archive id in
        # numeric order (shorter digit strings first), which follows the archive's own order.
        with Session(self.engine) as session:
            return list(session.scalars(select(ArticleRow.ref).where(
                ArticleRow.category == "live", ArticleRow.archive_id.is_not(None),
                ArticleRow.ref.startswith("ka-"),
                ~select(Run.id).where(Run.article_ref == ArticleRow.ref).exists(),
            ).order_by(ArticleRow.created_at, func.length(ArticleRow.archive_id),
                       ArticleRow.archive_id, ArticleRow.ref)))

    def import_articles(self, items: list[dict]) -> dict:
        counts = {"imported": 0, "updated": 0, "skipped": 0}
        with Session(self.engine) as session, session.begin():
            begin_write(session.connection())
            seen = set()
            for item in items:
                item = without_nul(item) if isinstance(item, dict) else item
                archive_id = item.get("article_id")
                url = item.get("url") or ""
                try:
                    parts = urlsplit(url.strip())
                    valid = (archive_id is not None and parts.hostname and
                             parts.scheme.lower() in {"http", "https"} and not parts.username and not parts.password)
                    _ = parts.port
                except ValueError:
                    valid = False
                if not valid:
                    counts["skipped"] += 1
                    continue
                url = normalized_url(url)
                if url in seen:
                    counts["skipped"] += 1
                    continue
                seen.add(url)
                ref = "ka-" + str(archive_id)
                url_row = session.scalar(select(ArticleRow).where(ArticleRow.url == url))
                row = session.get(ArticleRow, ref)
                if row is None:
                    row = session.scalar(select(ArticleRow).where(ArticleRow.archive_id == str(archive_id)))
                if row is not None and not row.ref.startswith("ka-") and row.url != url:
                    # A run-owned URL identity keeps its original article fields.
                    counts["skipped"] += 1
                    continue
                if url_row is not None and url_row.archive_id not in {None, str(archive_id)}:
                    # A different archive identity cannot be merged by URL alone.
                    counts["skipped"] += 1
                    continue
                merged = False
                if row is not None and url_row is not None and row.ref != url_row.ref:
                    if not url_row.ref.startswith("u-"):
                        counts["skipped"] += 1
                        continue
                    session.execute(update(Run).where(Run.article_ref == url_row.ref).values(article_ref=row.ref))
                    session.delete(url_row)
                    # Release the unique URL before assigning it to the canonical identity.
                    session.flush()
                    merged = True
                if row is None:
                    row = url_row
                paper = item.get("newspaper") or None
                source = paper.get("display_name") if isinstance(paper, dict) else paper
                if row is None:
                    values = article_values({"url": url, "title": item.get("title") or "", "content": "",
                                             "published_at": item.get("publish_date")}, "live", "archive")
                    row = ArticleRow(**{**values, "ref": ref, "archive_id": str(archive_id), "source": source})
                    session.add(row)
                    session.flush()
                    counts["imported"] += 1
                else:
                    values = {"archive_id": str(archive_id), "source": source}
                    if row.origin == "archive" or row.ref.startswith("ka-"):
                        values.update(url=url, host=parts.hostname, title=item.get("title") or row.title,
                                      published_at=item.get("publish_date") or row.published_at)
                    if row.url != url:
                        # Enrichment belongs to the old URL and must be fetched again.
                        values.update(content="", snapshot=None, language=None, removed_at=None)
                    changed = merged or any(getattr(row, key) != value for key, value in values.items())
                    for key, value in values.items():
                        setattr(row, key, value)
                    if changed:
                        row.updated_at = now()
                    counts["updated" if changed else "skipped"] += 1
                    session.flush()
        return counts

    def fill_article(self, ref: str, record, *, expected_url: str, expected_archive_id: str | None) -> bool:
        """Store archive text only if the article still has the identity it was fetched for.

        The fetch happens outside this transaction; a concurrent import may have moved the
        article to another URL (clearing its enrichment) meanwhile. A stale response must
        never attach one page's text to another URL, so it is dropped here.
        """
        from audit.clients.karakutu import first_dead_check_date
        with Session(self.engine) as session, session.begin():
            row = session.get(ArticleRow, ref)
            if row is None or row.content:
                return False
            if row.url != expected_url or row.archive_id != expected_archive_id:
                return False
            if str(record.id) != (expected_archive_id or ref[3:]):
                return False
            versions = sorted(record.versions, key=lambda v: v.check_date or 0)
            row.content = without_nul(versions[-1].news_text if versions else record.news_text)
            row.language = without_nul(record.language_code)
            row.snapshot = without_nul({"versions": [{"title": v.title, "check_date": v.check_date,
                                         "content_hash": v.content_hash} for v in versions]})
            # Archive check dates are Unix seconds; store ISO, like published_at. Checks are
            # unvalidated upstream data: an unusable date leaves the removal date unknown
            # instead of discarding the article text.
            try:
                dead = first_dead_check_date(record.checks)
                row.removed_at = (datetime.fromtimestamp(int(dead), timezone.utc).isoformat()
                                  if dead is not None else None)
            except (TypeError, ValueError, OverflowError, OSError):
                row.removed_at = None
            row.updated_at = now()
            return True

    def add_reaction(self, run_id: str, values: dict) -> dict | None:
        with Session(self.engine) as session, session.begin():
            run = session.get(Run, run_id)
            if run is None:
                return None
            row = Reaction(id="rx_" + uuid4().hex, run_id=run_id, workflow_id=run.workflow_id,
                           created_at=now(), **values)
            session.add(row)
            session.flush()
            return self._reaction(row)

    @staticmethod
    def _active(session: Session, name: str) -> PromptVersion | None:
        activation = session.scalars(select(PromptActivation).where(PromptActivation.name == name)
                                     .order_by(ACTIVATION_ORDER).limit(1)).first()
        return session.get(PromptVersion, activation.version_id) if activation else None

    @staticmethod
    def _version(row: PromptVersion, active_id: str | None, *, text=True) -> dict:
        fields = ["id", "name", "version", "sha256", "origin", "author", "note", "created_at"]
        if text:
            fields.append("text")
        return {**{key: getattr(row, key) for key in fields}, "active": row.id == active_id}

    @staticmethod
    def _activate(session, row, kind, author, note):
        session.add(PromptActivation(id="pa_" + uuid4().hex, name=row.name, version_id=row.id,
                                     kind=kind, author=author, note=note, created_at=now()))

    @staticmethod
    def _insert_version(session, name, text, origin, author, note):
        number = session.scalar(select(func.max(PromptVersion.version)).where(PromptVersion.name == name)) or 0
        row = PromptVersion(id="pv_" + uuid4().hex, name=name, version=number + 1, text=text,
                            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), origin=origin,
                            author=author, note=note, created_at=now())
        session.add(row)
        session.flush()
        return row

    def sync_prompt_defaults(self, defaults: dict[str, str]):
        with Session(self.engine) as session, session.begin():
            begin_write(session.connection())
            for name, text in defaults.items():
                existing = session.scalars(select(PromptVersion).where(PromptVersion.name == name)).all()
                if any(row.origin == "default" and row.text == text for row in existing):
                    continue
                active = self._active(session, name)
                row = self._insert_version(session, name, text, "default", "system", "Seeded from source defaults")
                if not existing or (active and active.origin == "default"):
                    self._activate(session, row, "seed" if not existing else "default_update", "system", "Seeded from source defaults")

    def prompt_versions(self, name: str) -> list[dict]:
        with Session(self.engine) as session:
            active = self._active(session, name)
            rows = session.scalars(select(PromptVersion).where(PromptVersion.name == name).order_by(PromptVersion.version.desc())).all()
            return [self._version(row, active.id if active else None, text=False) for row in rows]

    def prompt_version(self, name: str, version: int) -> dict | None:
        with Session(self.engine) as session:
            active = self._active(session, name)
            row = session.scalar(select(PromptVersion).where(PromptVersion.name == name, PromptVersion.version == version))
            return self._version(row, active.id if active else None) if row else None

    def prompt_activations(self, name: str) -> list[dict]:
        with Session(self.engine) as session:
            rows = session.scalars(select(PromptActivation).where(PromptActivation.name == name)
                                   .order_by(ACTIVATION_ORDER)).all()
            return [{key: getattr(row, key) for key in ("id", "name", "version_id", "kind", "author", "note", "created_at")} for row in rows]

    def active_prompts(self) -> dict[str, dict]:
        with Session(self.engine) as session:
            names = session.scalars(select(PromptVersion.name).distinct()).all()
            result = {}
            for name in names:
                row = self._active(session, name)
                if row:
                    result[name] = {"version_id": row.id, "version": row.version, "sha256": row.sha256, "text": row.text}
            return result

    def create_prompt_version(self, name: str, text: str, author: str, note: str) -> dict:
        with Session(self.engine) as session, session.begin():
            begin_write(session.connection())
            if session.scalar(select(PromptVersion.id).where(PromptVersion.name == name, PromptVersion.text == text)):
                raise PromptStoreError("duplicate_version")
            row = self._insert_version(session, name, text, "api", author, note)
            active = self._active(session, name)
            return self._version(row, active.id if active else None)

    def activate_prompt(self, name: str, version: int | None, kind: str, author: str, note: str,
                        build_snapshot: Callable[[dict[str, str]], dict]) -> tuple[dict, dict]:
        with Session(self.engine) as session, session.begin():
            begin_write(session.connection())
            active = self._active(session, name)
            if kind == "rollback":
                previous = session.scalars(select(PromptActivation).where(
                    PromptActivation.name == name, PromptActivation.version_id != active.id)
                    .order_by(ACTIVATION_ORDER).limit(1)).first()
                if previous is None:
                    raise PromptStoreError("no_previous_version")
                row = session.get(PromptVersion, previous.version_id)
            else:
                row = session.scalar(select(PromptVersion).where(PromptVersion.name == name, PromptVersion.version == version))
                if row is None:
                    raise PromptStoreError("not_found")
            names = session.scalars(select(PromptVersion.name).distinct()).all()
            texts = {}
            for prompt_name in names:
                current = self._active(session, prompt_name)
                if current:
                    texts[prompt_name] = current.text
            texts[name] = row.text
            snapshot = build_snapshot(texts)
            stored = session.get(Workflow, snapshot["id"])
            if stored is None:
                session.add(Workflow(id=snapshot["id"], snapshot=snapshot))
            else:
                snapshot = stored.snapshot
            if active is None or row.id != active.id:
                self._activate(session, row, kind, author, note)
            return self._version(row, row.id, text=False), snapshot

    def model_assignments(self) -> dict[str, str | None]:
        with Session(self.engine) as session:
            rows = session.scalars(select(ModelAssignment).order_by(insertion_order(ModelAssignment))).all()
            return {row.step: row.model for row in rows}

    def assign_model(self, step: str, model: str | None, author: str, note: str,
                     build_snapshot: Callable[[], dict]) -> dict:
        """Commit the append-only assignment and its workflow snapshot together."""
        with Session(self.engine) as session, session.begin():
            begin_write(session.connection())
            session.add(ModelAssignment(id="ma_" + uuid4().hex, step=step, model=model,
                                        author=author, note=note, created_at=now()))
            snapshot = build_snapshot()
            stored = session.get(Workflow, snapshot["id"])
            if stored is None:
                session.add(Workflow(id=snapshot["id"], snapshot=snapshot))
            else:
                snapshot = stored.snapshot
            return snapshot
