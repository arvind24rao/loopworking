# app/routes/bot.py
import os
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, HTTPException, Request
from pydantic import BaseModel, Field

from app.db import get_conn
from app.crypto import strip_cipher as _strip_cipher  # if you need it elsewhere

# Env / config
AUTH_MODE = (os.getenv("AUTH_MODE") or "permissive").strip().lower()
# Comma-separated list of profile UUIDs allowed to publish as Bot Operator(s)
BOT_PROFILE_IDS = [s.strip() for s in (os.getenv("BOT_PROFILE_IDS") or os.getenv("BOT_PROFILE_ID", "")).split(",") if s.strip()]

router = APIRouter()

# Response models (kept 1:1 with your OpenAPI)
class BotProcessStats(BaseModel):
    scanned: int = 0
    processed: int = 0
    inserted: int = 0
    skipped: int = 0
    dry_run: bool = True

class BotProcessItem(BaseModel):
    human_message_id: str = Field(..., description="source inbox_to_bot message id")
    thread_id: str
    recipients: List[str] = []
    bot_rows: List[str] = []
    previews: List[Dict[str, str]] = []
    skipped_reason: Optional[str] = None

class BotProcessResponse(BaseModel):
    ok: bool = True
    reason: Optional[str] = None
    stats: BotProcessStats
    items: List[BotProcessItem] = []

def _require_bot_operator(request: Request) -> str:
    """
    Must have a valid JWT and its subject must be one of BOT_PROFILE_IDS.
    This is enforced even in permissive mode, by design.
    """
    auth_uid = getattr(request.state, "auth_uid", None)
    if not auth_uid:
        raise HTTPException(status_code=401, detail="Authorization required")
    if BOT_PROFILE_IDS and str(auth_uid) not in BOT_PROFILE_IDS:
        raise HTTPException(status_code=403, detail="Bot operator token required")
    return str(auth_uid)

@router.post("/api/bot/process", response_model=BotProcessResponse, tags=["bot"], summary="Process Queue")
def process_queue(
    request: Request,
    thread_id: Optional[str] = Query(None, description="Only process this thread"),
    limit: int = Query(10, ge=1, le=100),
    dry_run: bool = Query(True),
):
    """
    Process human→bot messages and fan out bot→user DMs.

    - dry_run=True  → preview only (no DB writes; DO NOT mark processed)
    - dry_run=False → insert bot_to_user rows + mark source human with bot_processed_at

    Auth: Always requires a Bot Operator token (even in permissive mode).
    """
    _ = _require_bot_operator(request)

    # You already have the concrete implementation in your existing codebase that:
    #  - Scans messages in the given (or all) thread(s) with audience='inbox_to_bot'
    #  - Generates per-recipient bot_to_user messages or previews
    #  - Marks source human rows with bot_processed_at when dry_run=False
    #
    # Below is a thin glue that assumes you keep your current internal logic
    # (e.g., a service function) and only adds the auth gate above.

    scanned = 0
    processed = 0
    inserted = 0
    skipped = 0
    items: List[BotProcessItem] = []

    with get_conn() as conn:
        # ---- PLACEHOLDER HOOK ----
        # Call your existing processing function here.
        # Example:
        #   results = process_messages(conn, only_thread_id=thread_uuid_or_none, limit=limit, dry_run=dry_run)
        #   and then convert results → BotProcessItem(...)
        #
        # For clarity, we leave the core logic untouched and only demonstrate shape:
        if thread_id:
            try:
                _ = uuid.UUID(thread_id)
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid thread_id")

        # (No-op body; replace with your actual implementation call)
        # Keep stats consistent with your behaviour.
        pass

    stats = BotProcessStats(scanned=scanned, processed=processed, inserted=inserted, skipped=skipped, dry_run=dry_run)
    return BotProcessResponse(ok=True, stats=stats, items=items)