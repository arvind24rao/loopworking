from fastapi import APIRouter
import os
from urllib.parse import urlparse

router = APIRouter(prefix="/health")

@router.get("/dbinfo")
def dbinfo():
    raw = os.getenv("DATABASE_URL", "")
    parsed = urlparse(raw) if raw else None
    host = parsed.hostname if parsed else None
    scheme = parsed.scheme if parsed else None
    # mask password if present
    safe = raw
    if "@" in safe and "://" in safe:
        try:
            prefix, rest = safe.split("://", 1)
            userpass, hostrest = rest.split("@", 1)
            if ":" in userpass:
                user, _pwd = userpass.split(":", 1)
                safe = f"{prefix}://{user}:***@{hostrest}"
        except Exception:
            pass
    return {
        "ok": bool(raw),
        "scheme": scheme,
        "host": host,
        "dsn_preview": safe,
    }