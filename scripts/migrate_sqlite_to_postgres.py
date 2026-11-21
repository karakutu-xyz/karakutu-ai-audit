"""Copy an upgraded, quiescent SQLite database into an empty PostgreSQL database.

Only counts are printed. Stop the API and take a backup before invoking this tool.
"""
import argparse
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sys

from sqlalchemy import JSON, MetaData, String, Text, bindparam, cast, create_engine, func, select
from sqlalchemy.dialects.postgresql import JSON as PG_JSON
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


def _number(value) -> str:
    """Exact canonical text for a JSON number: digits and exponent, no rounding.

    Equal numbers must encode equally and unequal ones never, whatever the textual form
    (`1e20` and `100000000000000000000`, `-0.0` and `0`)."""
    if isinstance(value, float) and not math.isfinite(value):
        raise CopyRefused("Non-finite number in stored data")
    number = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    sign, digits, exponent = number.as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if digits == [0]:
        return "0"
    return ("-" if sign else "") + "".join(map(str, digits)) + "e" + str(exponent)


class _SqlNull:
    """SQL NULL in a JSON column: a different value from the JSON literal `null`."""


SQL_NULL = _SqlNull()


def _encode(value) -> str:
    """Type-tagged canonical text: no value of one type can encode like another."""
    if value is SQL_NULL:
        return "N"
    if value is None:
        return "n"
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, str):
        return "s" + json.dumps(value, ensure_ascii=True)
    if isinstance(value, (int, float, Decimal)):
        return "d" + _number(value)
    if isinstance(value, dict):
        items = sorted((json.dumps(str(key), ensure_ascii=True), _encode(item))
                       for key, item in value.items())
        return "{" + ",".join(f"{key}:{item}" for key, item in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    raise CopyRefused("Unsupported stored value type")


def _is_json(column) -> bool:
    return isinstance(column.type, JSON)


def _raw(columns):
    """Select JSON columns as their stored text, so no value passes through a float."""
    return [cast(column, Text).label(column.name) if _is_json(column) else column
            for column in columns]


def _reject_constant(_name):
    raise CopyRefused("Non-finite number in stored data")


def _exact(row: dict, columns) -> dict:
    """Decode JSON text with exact numbers (Decimal), for hashing only."""
    values = dict(row)
    for column in columns:
        text = values[column.name]
        if not _is_json(column):
            continue
        if text is None:
            values[column.name] = SQL_NULL
        else:
            values[column.name] = json.loads(text, parse_float=Decimal,
                                             parse_constant=_reject_constant)
    return values


def _insert(table, columns, dialect: str):
    """Insert JSON columns from their raw text: PostgreSQL `json` keeps it verbatim."""
    values = {}
    for column in columns:
        param = bindparam("p_" + column.name, type_=Text if _is_json(column) else column.type)
        values[column.name] = cast(param, PG_JSON) if _is_json(column) and dialect == "postgresql" else param
    return table.insert().values(values)


def _text_columns(columns):
    """Plain text columns; PostgreSQL text cannot hold NUL (JSON keeps it as \u0000)."""
    return [column for column in columns if isinstance(column.type, String) and not _is_json(column)]


def _nul_report(connection, tables):
    """(table, column, rows) for every source text column holding NUL. Names only."""
    report = []
    for table in tables:
        for column in _text_columns(table.columns):
            rows = connection.scalar(select(func.count()).select_from(table).where(
                func.instr(column, func.char(0)) > 0))
            if rows:
                report.append((table.name, column.name, rows))
    return report


def _strip(row: dict, text_names: set[str]) -> dict:
    return {key: value.replace("\x00", "") if key in text_names and isinstance(value, str) else value
            for key, value in row.items()}


def _digest(connection, table, columns, *, strip_nul=False):
    digest = hashlib.sha256()
    count = 0
    # Database locale must not change the canonical row order across backends.
    collation = "BINARY" if connection.dialect.name == "sqlite" else "C"
    query = select(*_raw(columns)).order_by(
        *(column.collate(collation) for column in table.primary_key))
    text_names = {column.name for column in _text_columns(columns)} if strip_nul else set()
    for row in connection.execute(query).mappings():
        encoded = _encode(_exact(_strip(dict(row), text_names), columns)).encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return count, digest.digest()


def copy_database(source_url, target_url, *, dry_run=False, strip_nul=False, nul_report=None,
                  _target_backend="postgresql"):
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
            # Old SQLite rows may hold NUL in text columns, which PostgreSQL text rejects.
            # Refuse by default, naming only tables, columns and counts; --strip-nul removes them.
            found = _nul_report(source_connection, tables)
            if nul_report is not None:
                nul_report.extend(found)
            if found and not strip_nul:
                where = ", ".join(f"{table}.{column} ({rows} rows)" for table, column, rows in found)
                raise CopyRefused(f"NUL characters in source text columns: {where}; "
                                  "rerun with --strip-nul to remove them")
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
                    query = select(*_raw(columns))
                    if "seq" in table.c:
                        query = query.order_by(insertion_order(table))
                    else:
                        query = query.order_by(*table.primary_key)
                    rows = source_connection.execute(query).mappings()
                    insert = _insert(table, columns, connection.dialect.name)
                    text_names = ({column.name for column in _text_columns(columns)}
                                  if strip_nul else set())
                    while batch := rows.fetchmany(500):
                        connection.execute(insert, [
                            {"p_" + key: value for key, value in _strip(dict(row), text_names).items()}
                            for row in batch])
                    source_digest = _digest(source_connection, table, columns, strip_nul=strip_nul)
                    if source_digest != _digest(connection, table, columns):
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
    parser.add_argument("--strip-nul", action="store_true",
                        help="Remove NUL characters from source text columns (JSON keeps them)")
    args = parser.parse_args(argv)
    try:
        found = []
        counts = copy_database(args.source_sqlite_url, args.target_postgres_url, dry_run=args.dry_run,
                               strip_nul=args.strip_nul, nul_report=found)
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
    for table, column, rows in found:
        verb = "would strip" if args.dry_run else "stripped"
        print(f"{verb} NUL characters in {table}.{column}: {rows} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
