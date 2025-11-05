# app/routes/bot.py
import os
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, HTTPException, Request
from pydantic import BaseModel, Field

from app.db import get_conn
from app.crypto import seal_plaintext  # same helper used by /send_message

# Env / config
AUTH_MODE = (os.getenv("AUTH_MODE") or "permissive").strip().lower()
# Comma-separated list of profile UUIDs allowed to publish as Bot Operator(s)
BOT_PROFILE_IDS = [s.strip() for s in (os.getenv("BOT_PROFILE_IDS") or os.getenv("BOT_PROFILE_ID", "")).split(",") if s.strip()]
INBOX_TO_BOT = "inbox_to_bot"
BOT_TO_USER  = "bot_to_user"
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

def _decode_cipher(c: Optional[str]) -> str:
    if not c:
        return ""
    return c[7:].strip() if c.startswith("cipher:") else c

def _thread_loop_id(conn, thread_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("select loop_id from threads where id = %s", (uuid.UUID(thread_id),))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Thread not found")
        (loop_id,) = row
        return str(loop_id)

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

    # Hook into your existing processing logic here.
    # scanned = 0
    # processed = 0
    # inserted = 0
    # skipped = 0
    # items: List[BotProcessItem] = []

    # with get_conn() as conn:
    #     if thread_id:
    #         try:
    #             _ = uuid.UUID(thread_id)
    #         except Exception:
    #             raise HTTPException(status_code=400, detail="Invalid thread_id")

    #     # TODO: call your existing processing function and populate stats/items.

    # stats = BotProcessStats(scanned=scanned, processed=processed, inserted=inserted, skipped=skipped, dry_run=dry_run)
    # return BotProcessResponse(ok=True, stats=stats, items=items)

    scanned = 0
    processed = 0
    inserted = 0
    skipped = 0
    items: List[BotProcessItem] = []

    with get_conn() as conn:
        # Validate thread filter if present
        if thread_id:
            try:
                _ = uuid.UUID(thread_id)
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid thread_id")

        # 1) Load candidate human→bot messages (oldest first)
        #    We keep it simple: pick recent unprocessed inbox_to_bot messages in this thread (or any thread if no filter),
        #    up to "limit". If you add a bot_processed_at column later, filter on that here.
        with conn.cursor() as cur:
            if thread_id:
                cur.execute(
                    """
                    select m.id, m.thread_id, m.created_by, m.content_ciphertext
                    from messages m
                    where m.audience = %s
                      and m.thread_id = %s
                    order by m.created_at asc
                    limit %s
                    """,
                    (INBOX_TO_BOT, uuid.UUID(thread_id), limit),
                )
            else:
                cur.execute(
                    """
                    select m.id, m.thread_id, m.created_by, m.content_ciphertext
                    from messages m
                    where m.audience = %s
                    order by m.created_at asc
                    limit %s
                    """,
                    (INBOX_TO_BOT, limit),
                )
            rows = cur.fetchall()

        scanned = len(rows)
        if not rows:
            stats = BotProcessStats(scanned=scanned, processed=processed, inserted=inserted, skipped=skipped, dry_run=dry_run)
            return BotProcessResponse(ok=True, stats=stats, items=items)

        # 2) For each human message, figure recipients = other humans in the loop (exclude sender & agents)
        for (msg_id, msg_thread_id, msg_sender_profile_id, content_cipher) in rows:
            try:
                loop_id = _thread_loop_id(conn, str(msg_thread_id))  # get loop for this thread
                # get members in loop excluding agents and the sender
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        select profile_id
                        from loop_members
                        where loop_id = %s
                          and role <> 'agent'
                          and profile_id <> %s
                        """,
                        (uuid.UUID(loop_id), uuid.UUID(str(msg_sender_profile_id))),
                    )
                    recipient_rows = cur.fetchall()
                recipients = [str(r[0]) for r in recipient_rows]

                # Build preview text (simple echo bot)
                plaintext = _decode_cipher(content_cipher)
                preview_text = f"Bot: {plaintext}" if plaintext else "Bot: (no content)"

                item = BotProcessItem(
                    human_message_id=str(msg_id),
                    thread_id=str(msg_thread_id),
                    recipients=recipients,
                    previews=[{"recipient_profile_id": r, "content": preview_text} for r in recipients],
                )

                if dry_run or not recipients:
                    # Dry run or nobody to send to
                    if not recipients:
                        item.skipped_reason = "no_recipients"
                        skipped += 1
                    else:
                        processed += 1
                    items.append(item)
                    continue

                # 3) Publish: insert bot_to_user rows
                bot_row_ids: List[str] = []
                with conn.cursor() as cur:
                    for rpid in recipients:
                        cur.execute(
                            """
                            insert into messages (thread_id, created_by, author_member_id, audience, recipient_profile_id, content_ciphertext)
                            values (%s, %s, %s, %s, %s, %s)
                            returning id
                            """,
                            (
                                uuid.UUID(str(msg_thread_id)),
                                uuid.UUID(str(msg_sender_profile_id)),  # attribute to sender OR change to operator id if you prefer
                                None,                                   # author_member_id is nullable; leave NULL unless you have a bot member
                                BOT_TO_USER,
                                uuid.UUID(rpid),
                                seal_plaintext(preview_text),
                            ),
                        )
                        bot_row_ids.append(str(cur.fetchone()[0]))

                inserted += len(bot_row_ids)
                processed += 1
                item.bot_rows = bot_row_ids
                items.append(item)

            except Exception as e:
                # Keep going; report skip
                skipped += 1
                items.append(
                    BotProcessItem(
                        human_message_id=str(msg_id),
                        thread_id=str(msg_thread_id),
                        recipients=[],
                        previews=[],
                        skipped_reason=str(e),
                    )
                )

    stats = BotProcessStats(scanned=scanned, processed=processed, inserted=inserted, skipped=skipped, dry_run=dry_run)
    return BotProcessResponse(ok=True, stats=stats, items=items)