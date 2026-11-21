"""Copy an upgraded, quiescent SQLite database into an empty PostgreSQL database.

Only counts are printed. Stop the API and take a backup before invoking this tool.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from sqlalchemy import MetaData, create_engine, func, select
from sqlalchemy.engine import URL

from audit.config import database_backend, has_demo_marker, is_test_database_path
from audit.db import Base, SCHEMA_VERSION, Store, begin_write, get_version, insertion_order


class CopyRefused(ValueError):
    """A value-free operator error."""


def _empty(connection):
    metadata = MetaData()
    metadata.reflect(connection)
    for table in metadata.sorted_tables:
        if table.name == "schema_version":
            continue
        if connection.scalar(select(func.count()).select_from(table)):
            raise CopyRefused("Target database is not empty")


def _digest(connection, table, columns):
    digest = hashlib.sha256()
    count = 0
    # Database locale must not change the canonical row order across backends.
    collation = "BINARY" if connection.dialect.name == "sqlite" else "C"
    query = select(*columns).order_by(
        *(column.collate(collation) for column in table.primary_key))
    for row in connection.execute(query).mappings():
        encoded = json.dumps(dict(row), sort_keys=True, ensure_ascii=True,
                             separators=(",", ":"), allow_nan=False).encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return count, digest.digest()


def copy_database(source_url, target_url, *, dry_run=False, _target_backend="postgresql"):
    source = database_backend(source_url)
    target = database_backend(target_url)
    if source.get_backend_name() != "sqlite" or not source.database or source.database == ":memory:":
        raise CopyRefused("Source must be a SQLite file")
    if target.get_backend_name() != _target_backend:
        raise CopyRefused("Target must use postgresql+psycopg")
    path = Path(source.database).resolve()
    if not path.is_file() or source.query:
        raise CopyRefused("Source must be an existing plain SQLite file")
    if target.get_backend_name() == "sqlite" and Path(target.database).resolve() == path:
        raise CopyRefused("Source and target must differ")
    # A demo or test database never becomes live data, the same rule live mode applies.
    try:
        demo = is_test_database_path(path) or has_demo_marker(path, include_wal=True)
    except ValueError:
        raise CopyRefused("Source could not be inspected for demo data") from None
    if demo:
        raise CopyRefused("Demo or test databases are never copied")
    # Read-only mode includes committed WAL data; never migrate or stamp the source.
    readonly = URL.create("sqlite", database=path.as_uri(), query={"mode": "ro", "uri": "true"})
    source_engine = create_engine(readonly, hide_parameters=True)
    target_engine = None
    store = None
    try:
        with source_engine.connect() as source_connection:
            source_connection.exec_driver_sql("BEGIN")
            if get_version(source_connection) != SCHEMA_VERSION:
                raise CopyRefused("Upgrade the source database before copying")
            tables = Base.metadata.sorted_tables
            counts = {table.name: source_connection.scalar(
                select(func.count()).select_from(table)) for table in tables}
            # Preflight before Store can change the target schema. Dry run never writes it.
            target_options = ({"connect_timeout": 10}
                              if target.get_backend_name() == "postgresql" else {})
            target_engine = create_engine(target, hide_parameters=True, connect_args=target_options)
            with target_engine.connect() as connection:
                _empty(connection)
            if dry_run:
                return counts
            store = Store(target_url)
            with store.engine.begin() as connection:
                begin_write(connection)
                _empty(connection)
                for table in tables:
                    columns = [column for column in table.columns if column.name != "seq"]
                    query = select(*columns)
                    if "seq" in table.c:
                        query = query.order_by(insertion_order(table))
                    else:
                        query = query.order_by(*table.primary_key)
                    rows = source_connection.execute(query).mappings()
                    while batch := rows.fetchmany(500):
                        connection.execute(table.insert(), [dict(row) for row in batch])
                    if _digest(source_connection, table, columns) != _digest(connection, table, columns):
                        raise CopyRefused("Copy verification failed")
            return counts
    finally:
        if store is not None:
            store.close()
        if target_engine is not None:
            target_engine.dispose()
        source_engine.dispose()


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes unrecognized arguments, which could contain a URL.
        self.exit(2, "Invalid arguments; use --help for database copy usage\n")


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("source_sqlite_url")
    parser.add_argument("target_postgres_url")
    parser.add_argument("--dry-run", action="store_true", help="Print counts without changing the target")
    args = parser.parse_args(argv)
    try:
        counts = copy_database(args.source_sqlite_url, args.target_postgres_url, dry_run=args.dry_run)
    except CopyRefused as refused:
        # Refusal reasons are fixed strings without URLs or values.
        print(f"Database copy refused: {refused}", file=sys.stderr)
        return 1
    except Exception:
        # Never print driver exceptions, URLs, row values or argparse input values.
        print("Database copy refused or failed; check connectivity, source version and empty target",
              file=sys.stderr)
        return 1
    for table, count in counts.items():
        prefix = "would copy " if args.dry_run else ""
        print(f"{prefix}{table}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
