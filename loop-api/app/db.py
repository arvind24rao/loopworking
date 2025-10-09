# app/db.py
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional
from urllib.parse import urlparse

import psycopg
from psycopg import Connection

_DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL")

def _normalize_dsn(raw: str) -> str:
    if not raw:
        raise RuntimeError("DATABASE_URL is not configured")
    # Must be postgres/postgresql, NOT https
    parsed = urlparse(raw)
    if parsed.scheme not in ("postgres", "postgresql"):
        raise RuntimeError(
            f"Invalid DATABASE_URL scheme '{parsed.scheme}'. "
            "Use the Postgres connection string (not the HTTP Supabase URL)."
        )
    dsn = raw
    if "sslmode=" not in dsn and parsed.scheme in ("postgres", "postgresql"):
        sep = "&" if "?" in dsn else "?"
        dsn = f"{dsn}{sep}sslmode=require"
    return dsn

@contextmanager
def get_conn(*, autocommit: bool = False) -> Iterator[Connection]:
    dsn = _normalize_dsn(_DATABASE_URL or "")
    conn = psycopg.connect(dsn, autocommit=autocommit)  # tuple rows
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass

__all__ = ["get_conn"]