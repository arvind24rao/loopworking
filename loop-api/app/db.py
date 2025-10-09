# app/db.py
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

# DATABASE_URL examples:
# - postgres://user:pass@host:5432/dbname
# - postgresql://user:pass@host:5432/dbname?sslmode=require
_DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL")

if not _DATABASE_URL:
    # Keep import-time errors explicit; the app can still boot if routes don't hit DB,
    # but we want a loud signal in logs when DB URL is missing.
    print("[app.db] WARNING: DATABASE_URL is not set — DB access will fail.")


@contextmanager
def get_conn(*, row_factory=dict_row, autocommit: bool = False) -> Iterator[Connection]:
    """
    Context manager that yields a psycopg v3 connection.

    Usage:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("select 1")
                print(cur.fetchone())

    Notes:
    - Uses row_factory=dict_row by default for ergonomic column access.
    - Caller controls transactions; set autocommit=True for simple reads.
    """
    if not _DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    # Connect with sslmode=require unless already specified in the URL.
    # (Render + Supabase typically expect SSL.)
    dsn = _DATABASE_URL
    if "sslmode=" not in dsn:
        # Add sslmode=require only for postgres/postgresql URLs (no-op otherwise)
        if dsn.startswith("postgres://") or dsn.startswith("postgresql://"):
            sep = "&" if "?" in dsn else "?"
            dsn = f"{dsn}{sep}sslmode=require"

    conn = psycopg.connect(dsn, row_factory=row_factory, autocommit=autocommit)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


__all__ = ["get_conn"]