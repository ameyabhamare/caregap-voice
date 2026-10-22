"""Database helpers."""

from __future__ import annotations

from importlib.resources import files

import psycopg


def schema_sql() -> str:
    return files("caregap.db").joinpath("schema.sql").read_text()


def apply_schema(conn: psycopg.Connection) -> None:
    """Apply the idempotent application schema."""
    conn.execute(schema_sql().encode())
    conn.commit()
