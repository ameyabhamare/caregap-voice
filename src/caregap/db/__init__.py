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


def main() -> int:
    """``python -m caregap.db``: apply the schema to DATABASE_URL (after a schema change on
    an existing volume; a new volume gets it from the Postgres init script)."""
    from caregap.config import get_settings

    with psycopg.connect(get_settings().database_url) as conn:
        apply_schema(conn)
    print("[db] schema applied")
    return 0
