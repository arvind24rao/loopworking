# app/routes/bot.py
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

# --- Adjust these imports to match your project structure ---
from app.db import get_conn                  # psycopg connection (context manager)
from app.llm import generate_reply           # LLM helper you already use
from app.crypto import seal_plaintext        # encrypts message.content before insert
# ------------------------------------------------------------

router = APIRouter(prefix="/api/bot", tags=["bot"])

INBOX_TO_BOT = "inbox_to_bot"
BOT_TO_USER = "bot_to_user"


# ========================= Helpers ========================= #

def _require_bot_caller(x_user_id: Optional[str]) -> str:
    """Ensure X-User-Id header (bot profile id) is present & a UUID."""
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Missing X-User-Id header")
    try:
        uuid.UUID(x_user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="X-User-Id must be a UUID")
    return x_user_id


def _fetch_unprocessed_human_messages(conn, *, thread_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """
    Fetch human messages queued for the bot (audience='inbox_to_bot') that have not been processed.
    Uses SKIP LOCKED to avoid concurrent workers clashing.
    """
    with conn.cursor() as cur:
        if thread_id:
            cur.execute(
                """
                SELECT id, thread_id, created_by, author_member_id, created_at, content
                FROM messages
                WHERE audience = %s
                  AND thread_id = %s
                  AND bot_processed_at IS NULL
                ORDER BY created_at ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (INBOX_TO_BOT, uuid.UUID(thread_id), limit),
            )
        else:
            cur.execute(
                """
                SELECT id, thread_id, created_by, author_member_id, created_at, content
                FROM messages
                WHERE audience = %s
                  AND bot_processed_at IS NULL
                ORDER BY created_at ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (INBOX_TO_BOT, limit),
            )
        rows = cur.fetchall()
    cols = ["id", "thread_id", "created_by", "author_member_id", "created_at", "content"]
    return [dict(zip(cols, r)) for r in rows]


def _thread_loop_id_and_members(conn, thread_id: str) -> Tuple[str, List[str]]:
    """Return (loop_id, [profile_id, ...]) for the given thread."""
    with conn.cursor() as cur:
        cur.execute("SELECT loop_id FROM threads WHERE id = %s", (uuid.UUID(thread_id),))
        res = cur.fetchone()
        if not res:
            raise HTTPException(status_code=404, detail="Thread not found")
        (loop_id,) = res

        cur.execute("SELECT profile_id FROM loop_members WHERE loop_id = %s", (loop_id,))
        member_rows = cur.fetchall()
    member_ids = [str(r[0]) for r in member_rows]
    return (str(loop_id), member_ids)


def _insert_bot_to_user(conn, *, thread_id: str, bot_profile_id: str, recipient_id: str, content: str) -> str:
    """Insert a bot_to_user message and return its id."""
    new_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO messages (id, thread_id, created_by, audience, recipient_profile_id, content)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                new_id,
                uuid.UUID(thread_id),
                uuid.UUID(bot_profile_id),
                BOT_TO_USER,
                uuid.UUID(recipient_id),
                seal_plaintext(content),
            ),
        )
        (inserted_id,) = cur.fetchone()
    return str(inserted_id)


def _mark_human_processed(conn, human_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE messages SET bot_processed_at = NOW() WHERE id = %s",
            (uuid.UUID(human_id),),
        )


# ========================= Models ========================= #

class ProcessStats(BaseModel):
    scanned: int = 0
    processed: int = 0
    inserted: int = 0
    skipped: int = 0
    dry_run: bool = False


class PreviewItem(BaseModel):
    recipient_profile_id: str
    content: str


class ProcessItemResult(BaseModel):
    human_message_id: str
    thread_id: str
    recipients: List[str] = []
    bot_rows: List[str] = []
    previews: List[PreviewItem] = []
    skipped_reason: Optional[str] = None


class BotProcessResponse(BaseModel):
    ok: bool = True
    reason: Optional[str] = None
    stats: ProcessStats
    items: List[ProcessItemResult] = []


# ========================= Route ========================= #

@router.post("/process", response_model=BotProcessResponse)
def process_queue(
    thread_id: Optional[str] = Query(None, description="Only process this thread"),
    limit: int = Query(20, ge=1, le=200),
    dry_run: bool = Query(False),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    """
    Process human→bot messages and emit per-recipient bot DMs.

    Behaviour:
    - With dry_run=True: DO NOT insert; return `previews` per recipient for the UI.
    - With dry_run=False: insert `bot_to_user` rows and mark the human message as processed.
    """
    bot_profile_id = _require_bot_caller(x_user_id)
    stats = ProcessStats(dry_run=dry_run)
    items: List[ProcessItemResult] = []

    with get_conn() as conn:
        conn.autocommit = False
        humans = _fetch_unprocessed_human_messages(conn, thread_id=thread_id, limit=limit)
        stats.scanned = len(humans)

        for h in humans:
            item = ProcessItemResult(
                human_message_id=str(h["id"]),
                thread_id=str(h["thread_id"]),
            )

            try:
                # --- recipients: everyone in the loop except the author ---
                loop_id, member_ids = _thread_loop_id_and_members(conn, str(h["thread_id"]))
                author_profile_id = str(h["created_by"])
                recipients = [pid for pid in member_ids if pid != author_profile_id]
                item.recipients = recipients

                # --- build candidate bot messages (LLM) per recipient ---
                candidate_msgs: List[Dict[str, str]] = []
                for rid in recipients:
                    try:
                        reply_text = generate_reply(
                            text=str(h["content"] or ""),
                            sender_profile_id=author_profile_id,
                            recipient_profile_id=rid,
                            thread_id=str(h["thread_id"]),
                            loop_id=loop_id,
                        )
                    except Exception as e:
                        # If LLM fails for this human message, mark skipped and continue to next human message
                        item.skipped_reason = f"llm_error: {e}"
                        stats.skipped += 1
                        # Do not mark processed; just append and move on
                        items.append(item)
                        raise  # bubble to outer except to skip publish/commit for this human

                    candidate_msgs.append(
                        {
                            "recipient_profile_id": rid,
                            "content": reply_text,
                        }
                    )

                if dry_run:
                    # --- DRY RUN: return previews (NO INSERTS, NO MARK) ---
                    item.previews = [
                        PreviewItem(recipient_profile_id=m["recipient_profile_id"], content=m["content"])
                        for m in candidate_msgs
                    ]
                    stats.processed += 1
                    items.append(item)
                    # Continue to next human without committing
                    continue

                # --- PUBLISH: insert rows & mark processed ---
                new_ids: List[str] = []
                for m in candidate_msgs:
                    new_id = _insert_bot_to_user(
                        conn,
                        thread_id=str(h["thread_id"]),      # ✅ correct indexing
                        bot_profile_id=bot_profile_id,
                        recipient_id=m["recipient_profile_id"],
                        content=m["content"],
                    )
                    new_ids.append(new_id)

                _mark_human_processed(conn, str(h["id"]))

                stats.processed += 1
                stats.inserted += len(new_ids)
                item.bot_rows = new_ids

                conn.commit()
                items.append(item)

            except HTTPException:
                # Respect explicit HTTP errors
                conn.rollback()
                raise

            except Exception:
                # Any other failure for this human message -> rollback this iteration only, keep overall request 200
                conn.rollback()
                # If we didn't set skipped_reason above, set a generic one
                if not item.skipped_reason:
                    item.skipped_reason = "error: failed to process message"
                stats.skipped += 1
                items.append(item)
                # proceed with next human

    return BotProcessResponse(ok=True, stats=stats, items=items)