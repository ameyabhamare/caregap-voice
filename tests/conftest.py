from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest

from caregap.cost import InMemoryLedgerStore, LedgerStore, PostgresLedgerStore
from caregap.db import apply_schema

# Integration tests use their own database so they never touch dev data.
TEST_DB = "caregap_test"


def _admin_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_ADMIN_URL", "postgresql://caregap:caregap@localhost:5432/postgres"
    )


@pytest.fixture(scope="session")
def pg_url() -> str:
    admin = _admin_url()
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)
            ).fetchone()
            if not exists:
                conn.execute(f"CREATE DATABASE {TEST_DB}".encode())
    except psycopg.OperationalError as exc:
        if os.environ.get("CAREGAP_REQUIRE_SERVICES") == "1":
            raise
        pytest.skip(f"Postgres not reachable ({exc}); run `make up` first")
    url = admin.rsplit("/", 1)[0] + f"/{TEST_DB}"
    with psycopg.connect(url) as conn:
        apply_schema(conn)
    return url


@pytest.fixture
def pg_store(pg_url: str) -> Iterator[PostgresLedgerStore]:
    with psycopg.connect(pg_url) as conn:
        conn.execute("TRUNCATE cost_ledger RESTART IDENTITY")
    yield PostgresLedgerStore(pg_url)


@pytest.fixture(
    params=["memory", pytest.param("postgres", marks=pytest.mark.integration)],
)
def store(request: pytest.FixtureRequest) -> LedgerStore:
    """Every ledger contract test runs against both store implementations."""
    if request.param == "memory":
        return InMemoryLedgerStore()
    pg: PostgresLedgerStore = request.getfixturevalue("pg_store")
    return pg
