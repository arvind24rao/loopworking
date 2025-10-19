# app/diagnostics.py
from fastapi import APIRouter
import os
import psycopg
from psycopg.rows import dict_row

router = APIRouter(prefix="/api", tags=["diagnostics"])

def _get_conn():
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    return psycopg.connect(dsn, row_factory=dict_row)

@router.get("/whoami")
def whoami():
    db_user = session_user = None
    try:
        with _get_conn() as conn, conn.cursor() as cur:
            cur.execute("select current_user as u, session_user as s")
            row = cur.fetchone()
            if row:
                db_user = row["u"]
                session_user = row["s"]
    except Exception as e:
        return {"ok": False, "error": str(e), "db_user": db_user, "session_user": session_user}

    return {
        "ok": True,
        "build_id": os.getenv("BUILD_ID", "unknown"),
        "db_user": db_user,
        "session_user": session_user,
        "module": __name__,
    }