# app/routes/bot.py
from __future__ import annotations

import os
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Header, HTTPException
from pydantic import BaseModel, Field

from loguru import logger

# Project-local helpers (present in your repo)
from app.llm import generate_reply
from app.supa import supa  # typed Supabase client
from app.crypto import encrypt_plaintext  # encryption helper, used pre-insert if needed

router = APIRouter(tags=["bot"])  # main.py mounts this with prefix="/api/bot"

# --- Env knobs / IDs ---
BOT_PROFILE_ID = os.getenv("BOT_PROFILE_ID") or os.getenv("BOT_PROFILE_IDS", "").split(",")[0] or ""

# Your known test users (A, B) — can be overridden via env if needed
USER_A = os.getenv("TEST_USER_A", "c9cf9661-346c-4f9d-a549-66137f29d87e")
USER_B = os.getenv("TEST_USER_B", "21520d4c-3c62-46d1-b056-636ca91481a2")

# Keep work small (but still LLM-powered)
RECIPIENT_MAX        = int(os.getenv("RECIPIENT_MAX", "2"))
HISTORY_MAX_MESSAGES = int(os.getenv("HISTORY_MAX_MESSAGES", "6"))

# --- Pydantic response model for symmetry with your existing API shape ---
class BotProcessItem(BaseModel):
    human_message_id: str = Field(..., description="source inbox_to_bot message id")
    thread_id: str
    recipients: List[str] = []
    bot_rows: List[str] = []        # ids of inserted bot_to_user rows
    skipped_reason: Optional[str] = None

class BotProcessStats(BaseModel):
    scanned: int
    processed: int
    inserted: int
    skipped: int
    dry_run: bool

class BotProcessResponse(BaseModel):
    ok: bool = True
    reason: Optional[str] = None
    stats: BotProcessStats
    items: List[BotProcessItem] = []


# ---- Small data helpers (DB access via supabase) ----

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _select_unprocessed(thread_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """
    Fetches inbox_to_bot messages awaiting processing, oldest first.
    If thread_id is supplied, scope to that thread.
    """
    q = supa.table("messages").select("*").eq("audience", "inbox_to_bot").eq("processed", False).order("created_at", asc=True)
    if thread_id:
        q = q.eq("thread_id", thread_id)
    q = q.limit(max(1, int(limit)))
    res = q.execute()
    rows: List[Dict[str, Any]] = res.data or []
    return rows

def _mark_processed(ids: List[str]) -> None:
    if not ids:
        return
    supa.table("messages").update({"processed": True}).in_("id", ids).execute()

def _fetch_recent_history(thread_id: str, limit: int) -> List[Dict[str, Any]]:
    res = supa.table("messages").select("id, audience, created_by, recipient_profile_id, content, created_at") \
             .eq("thread_id", thread_id) \
             .order("created_at", desc=True) \
             .limit(max(1, limit)) \
             .execute()
    rows = res.data or []
    rows.reverse()  # oldest first
    return rows

def _compute_recipients(sender_id: str) -> List[str]:
    # For test purposes, only A and B (minus the sender)
    pool = [USER_A, USER_B]
    recipients = [rid for rid in pool if rid and rid != sender_id]
    if RECIPIENT_MAX > 0 and len(recipients) > RECIPIENT_MAX:
        recipients = recipients[:RECIPIENT_MAX]
    return recipients

def _insert_bot_to_user(thread_id: str, recipient_id: str, content: str) -> str:
    # If your schema needs encryption, uncomment this:
    # content = encrypt_plaintext(content)
    payload = {
        "thread_id": thread_id,
        "audience": "bot_to_user",
        "created_by": BOT_PROFILE_ID or "bot",
        "recipient_profile_id": recipient_id,
        "content": content,
        "created_at": _now_iso(),
    }
    res = supa.table("messages").insert(payload).execute()
    row = (res.data or [{}])[0]
    return row.get("id") or ""


# ---- The processor endpoint ----

@router.post("/process", response_model=BotProcessResponse)
def process(
    thread_id: Optional[str] = Query(None, description="If set, only process this thread"),
    limit: int = Query(1, ge=1, le=50),
    dry_run: bool = Query(False),
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    """
    Scans inbox_to_bot messages and fans out bot_to_user messages using the LLM.
    Keeps workload tiny via RECIPIENT_MAX and HISTORY_MAX_MESSAGES.
    """
    if not BOT_PROFILE_ID:
        raise HTTPException(status_code=400, detail="No BOT_PROFILE_ID configured")
    if x_user_id != BOT_PROFILE_ID:
        raise HTTPException(status_code=403, detail="X-User-Id must match BOT_PROFILE_ID")

    try:
        rows = _select_unprocessed(thread_id, limit)
        scanned = len(rows)
        processed = 0
        inserted = 0
        skipped = 0
        items: List[BotProcessItem] = []

        for row in rows:
            src_id    = row.get("id")
            t_id      = row.get("thread_id")
            sender_id = row.get("created_by") or row.get("sender_id") or ""

            if not (src_id and t_id and sender_id):
                skipped += 1
                items.append(BotProcessItem(
                    human_message_id=src_id or "",
                    thread_id=t_id or (thread_id or ""),
                    recipients=[],
                    bot_rows=[],
                    skipped_reason="missing_fields"
                ))
                continue

            # recipients (A,B minus sender), cap by RECIPIENT_MAX
            recips = _compute_recipients(sender_id)

            # trim history for prompt
            history = _fetch_recent_history(t_id, HISTORY_MAX_MESSAGES)
            # very small prompt: just the latest human text
            latest_text = (row.get("content") or "").strip()
            if not latest_text:
                skipped += 1
                items.append(BotProcessItem(
                    human_message_id=src_id,
                    thread_id=t_id,
                    recipients=recips,
                    bot_rows=[],
                    skipped_reason="empty_text"
                ))
                continue

            # Call LLM ONCE per source message (not per recipient) to keep time bounded
            try:
                reply_text = generate_reply(message_text=latest_text, thread_id=t_id, recipients=recips)
            except Exception as e:
                logger.exception("LLM error on src %s: %s", src_id, e)
                skipped += 1
                items.append(BotProcessItem(
                    human_message_id=src_id,
                    thread_id=t_id,
                    recipients=recips,
                    bot_rows=[],
                    skipped_reason="llm_error"
                ))
                continue

            bot_rows: List[str] = []
            if not dry_run:
                for r in recips:
                    try:
                        new_id = _insert_bot_to_user(t_id, r, reply_text)
                        if new_id:
                            bot_rows.append(new_id)
                            inserted += 1
                    except Exception as e:
                        logger.exception("Insert failed for recipient %s (src %s): %s", r, src_id, e)

            processed += 1
            items.append(BotProcessItem(
                human_message_id=src_id,
                thread_id=t_id,
                recipients=recips,
                bot_rows=bot_rows,
                skipped_reason=None
            ))

            # mark processed AFTER successful handling (or dry_run skip marking)
            if not dry_run:
                try:
                    _mark_processed([src_id])
                except Exception as e:
                    logger.exception("Mark processed failed for %s: %s", src_id, e)

        return BotProcessResponse(
            ok=True,
            reason=None,
            stats=BotProcessStats(
                scanned=scanned,
                processed=processed,
                inserted=inserted,
                skipped=skipped,
                dry_run=dry_run
            ),
            items=items
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("bot.process failed: %s", e)
        raise HTTPException(status_code=500, detail="bot_process_exception")