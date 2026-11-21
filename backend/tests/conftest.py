"""Isolated storage: offline SQLite by default, throwaway databases for PostgreSQL."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from audit.config import database_backend


def pytest_collection_modifyitems(items):
    if os.environ.get("TEST_POSTGRES_URL"):
        for item in items:
            if "database_url" in item.fixturenames:
                item.add_marker(pytest.mark.postgres)


@pytest.fixture
def database_url(tmp_path):
    admin_url = os.environ.get("TEST_POSTGRES_URL")
    if not admin_url:
        yield f"sqlite:///{tmp_path / 'audit.sqlite3'}"
        return
    url = database_backend(admin_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("TEST_POSTGRES_URL must use postgresql+psycopg", pytrace=False)
    name = "audit_test_" + uuid4().hex
    engine = create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    try:
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
        except Exception:
            pytest.fail("Cannot create isolated PostgreSQL test database", pytrace=False)
        try:
            yield url.set(database=name).render_as_string(hide_password=False)
        finally:
            with engine.connect() as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
    finally:
        engine.dispose()
