# app/db.py
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg
from psycopg import Connection

# DATABASE_URL examples:
# - postgres://user:pass@host:5432/dbname
# - postgresql://user:pass@host:5432/dbname?sslmode=require
_DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL")

if not _DATABASE_URL:
    print("[app.db] WARNING: DATABASE_URL is not set — DB access will fail.")

@contextmanager
def get_conn(*, autocommit: bool = False) -> Iterator[Connection]:
    """
    Context manager that yields a psycopg v3 connection with default (tuple) rows.
    This matches our route code that unpacks fetchone()/fetchall() by position.
    """
    if not _DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    dsn = _DATABASE_URL
    if "sslmode=" not in dsn:
        if dsn.startswith("postgres://") or dsn.startswith("postgresql://"):
            sep = "&" if "?" in dsn else "?"
            dsn = f"{dsn}{sep}sslmode=require"

    conn = psycopg.connect(dsn, autocommit=autocommit)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass

__all__ = ["get_conn"]