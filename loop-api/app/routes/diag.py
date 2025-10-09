from fastapi import APIRouter
from app.db import get_conn

router = APIRouter(prefix="/health")

@router.get("/db")
def health_db():
    try:
        with get_conn(autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("select now()")
                (now_val,) = cur.fetchone()
        return {"ok": True, "now": str(now_val)}
    except Exception as e:
        return {"ok": False, "error": str(e)}