# app/routes/bot.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import os
import uuid
import psycopg  # psycopg 3.x
from psycopg.rows import dict_row
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

# Use the real LLM hook you edited (no local stub)
from app.llm import generate_reply

# This router shape matches your openapi.json path: /api/bot/process
router = APIRouter(prefix="/api/bot", tags=["bot"])

# -------------------------- Models (shape-compatible) --------------------------

class BotProcessItem(BaseModel):
    human_message_id: str = Field(..., description="source inbox_to_bot message id")
    thread_id: str
    recipients: List[str] = []
    bot_rows: List[str] = []        # ids of inserted bot_to_user rows
    previews: List[Dict[str, str]] = []  # [{recipient_profile_id, content}]
    skipped_reason: Optional[str] = None

class BotProcessStats(BaseModel):
    scanned: int = 0
    processed: int = 0    # number of human rows marked processed (only in publish)
    inserted: int = 0     # number of bot_to_user rows inserted
    skipped: int = 0
    dry_run: bool = True

class BotProcessResponse(BaseModel):
    ok: bool = True
    reason: Optional[str] = None
    stats: BotProcessStats
    items: List[BotProcessItem] = []

# ------------------------------ DB utilities ----------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _conn() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL (or SUPABASE_DB_URL) is not set")
    return psycopg.connect(dsn, row_factory=dict_row)

# ------------------------------ SQL helpers -----------------------------------

def _fetch_unprocessed_human_messages(
    conn: psycopg.Connection, *, thread_id: Optional[str], limit: int
) -> List[Dict[str, Any]]:
    """
    Human→bot queue rows are those with audience='inbox_to_bot' and bot_processed_at IS NULL.
    We do not touch 'processed' here to avoid drift with older paths.
    """
    sql = """
      SELECT id, thread_id, created_by, author_member_id
      FROM messages
      WHERE audience = 'inbox_to_bot'
        AND bot_processed_at IS NULL
    """
    args: List[Any] = []
    if thread_id:
        sql += " AND thread_id = %s"
        args.append(thread_id)
    sql += " ORDER BY created_at ASC LIMIT %s"
    args.append(limit)

    with conn.cursor() as cur:
        cur.execute(sql, tuple(args))
        return list(cur.fetchall())

def _loop_id_for_member(conn: psycopg.Connection, member_id: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT loop_id FROM members WHERE id = %s", (member_id,))
        row = cur.fetchone()
        return row["loop_id"] if row else None

def _bot_member_id_for_loop(conn: psycopg.Connection, loop_id: str, bot_profile_id: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM members WHERE loop_id = %s AND profile_id = %s",
            (loop_id, bot_profile_id),
        )
        row = cur.fetchone()
        return row["id"] if row else None

def _recipients_for_loop(conn: psycopg.Connection, loop_id: str, exclude_profile_ids: List[str]) -> List[str]:
    """
    Resolve recipient profile_ids as all members of the loop minus the exclude set.
    Exclude MUST include the human author and the bot profile id.
    """
    if exclude_profile_ids:
        ph = ", ".join(["%s"] * len(exclude_profile_ids))
        sql = f"""
          SELECT profile_id
          FROM members
          WHERE loop_id = %s
            AND profile_id NOT IN ({ph})
        """
        args: List[Any] = [loop_id, *exclude_profile_ids]
    else:
        sql = "SELECT profile_id FROM members WHERE loop_id = %s"
        args = [loop_id]

    with conn.cursor() as cur:
        cur.execute(sql, tuple(args))
        return [r["profile_id"] for r in cur.fetchall()]

def _insert_bot_to_user(
    conn: psycopg.Connection,
    *,
    thread_id: str,
    bot_profile_id: str,
    bot_member_id: str,
    recipient_id: str,
    content_ciphertext: str,  # storing plaintext for now; swap to real encryption later
) -> str:
    """
    Insert a bot_to_user row that satisfies NOT NULLs in your schema:
      - thread_id, created_by, author_member_id (bot), role, channel, visibility, audience, recipient_profile_id, content_ciphertext
    Returns new message id.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO messages
              (thread_id, created_at, created_by, author_member_id,
               role, channel, visibility, audience, recipient_profile_id, content_ciphertext)
            VALUES
              (%s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                thread_id,
                bot_profile_id,
                bot_member_id,
                "bot",
                "outbox",
                "private",
                "bot_to_user",
                recipient_id,
                content_ciphertext,
            ),
        )
        row = cur.fetchone()
        return str(row["id"])

def _mark_human_processed(conn: psycopg.Connection, human_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE messages SET bot_processed_at = NOW() WHERE id = %s",
            (human_id,),
        )

# ------------------------------ Route handler ---------------------------------

@router.post("/process", response_model=BotProcessResponse)
def process_queue(
    thread_id: Optional[str] = Query(None, description="Only process this thread"),
    limit: int = Query(10, ge=1, le=100),
    dry_run: bool = Query(True),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),  # bot profile id
):
    """
    Process human→bot messages and emit per-recipient bot DMs.

    - dry_run=True: preview only (no DB writes, do NOT mark processed)
    - dry_run=False: insert bot_to_user rows AND mark human messages with bot_processed_at
    """
    bot_profile_id = (x_user_id or "").strip()
    try:
        uuid.UUID(bot_profile_id)
    except Exception:
        raise HTTPException(status_code=400, detail="missing_or_invalid_X-User-Id (bot profile id)")

    stats = BotProcessStats(dry_run=bool(dry_run))
    items: List[BotProcessItem] = []

    try:
        with _conn() as conn:
            humans = _fetch_unprocessed_human_messages(conn, thread_id=thread_id, limit=limit)
            stats.scanned = len(humans)

            if not humans:
                return BotProcessResponse(ok=True, reason=None, stats=stats, items=[])

            for h in humans:
                src_id: str = str(h["id"])
                t_id: str = str(h["thread_id"])
                author_profile_id: str = str(h["created_by"])
                author_member_id: Optional[str] = h.get("author_member_id")

                item = BotProcessItem(human_message_id=src_id, thread_id=t_id)

                # Guard: need author_member_id to resolve loop
                if not author_member_id:
                    stats.skipped += 1
                    item.skipped_reason = "missing author_member_id"
                    items.append(item)
                    continue

                # Resolve loop_id and the bot's member_id in that loop
                loop_id = _loop_id_for_member(conn, author_member_id)
                if not loop_id:
                    stats.skipped += 1
                    item.skipped_reason = "missing loop_id"
                    items.append(item)
                    continue

                bot_member_id = _bot_member_id_for_loop(conn, loop_id, bot_profile_id)
                if not bot_member_id:
                    stats.skipped += 1
                    item.skipped_reason = "bot not a member of loop"
                    items.append(item)
                    continue

                # Compute recipients (exclude author + bot)
                recipients = _recipients_for_loop(conn, loop_id, exclude_profile_ids=[author_profile_id, bot_profile_id])
                item.recipients = recipients[:]  # echo in response

                # Build previews (and publish rows if not dry run)
                new_ids: List[str] = []
                previews: List[Dict[str, str]] = []

                for pid in recipients:
                    # NOTE: we don't fetch plaintext human text here (your table stores ciphertext);
                    # your LLM can be context-free or pull from other sources if needed.
                    reply_text = generate_reply(
                        human_text="",
                        author_profile_id=author_profile_id,
                        recipient_profile_id=pid,
                        thread_id=t_id,
                    )
                    previews.append({"recipient_profile_id": pid, "content": reply_text})

                    if not dry_run:
                        new_id = _insert_bot_to_user(
                            conn,
                            thread_id=t_id,
                            bot_profile_id=bot_profile_id,
                            bot_member_id=bot_member_id,
                            recipient_id=pid,
                            content_ciphertext=reply_text,  # TODO: replace with ciphertext when encryptor is wired
                        )
                        new_ids.append(new_id)

                # On publish, mark the human row processed only after successful inserts
                if not dry_run:
                    _mark_human_processed(conn, src_id)
                    stats.processed += 1
                    stats.inserted += len(new_ids)
                    item.bot_rows = new_ids
                else:
                    # dry run: never mutate state
                    item.previews = previews

                items.append(item)

        return BotProcessResponse(ok=True, reason=None, stats=stats, items=items)

    except HTTPException:
        raise
    except Exception as e:
        # Surface the real reason to help us correct quickly
        return BotProcessResponse(
            ok=False,
            reason=f"bot_process_exception: {e}",
            stats=stats,
            items=items,
        )