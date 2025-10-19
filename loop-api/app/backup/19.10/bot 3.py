# app/bot.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel
from loguru import logger

import os
import psycopg  # psycopg 3.x
from psycopg.rows import dict_row

# Project-local helpers
from app.llm import generate_reply

router = APIRouter(prefix="/api", tags=["bot"])

# ---------- Models ----------

class BotProcessItemPreview(BaseModel):
    recipient_profile_id: str
    text: str


class BotProcessItem(BaseModel):
    human_message_id: str
    previews: List[BotProcessItemPreview] = []


class BotProcessStats(BaseModel):
    scanned: int = 0
    processed: int = 0
    inserted: int = 0
    skipped: int = 0
    dry_run: bool = True
    recipient_ids: List[str] = []  # flattened unique ids for quick console checks


class BotProcessResponse(BaseModel):
    ok: bool = True
    reason: Optional[str] = None
    stats: BotProcessStats
    items: List[BotProcessItem] = []


# ---------- Utility ----------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_conn() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL (or SUPABASE_DB_URL) is not set")
    return psycopg.connect(dsn, row_factory=dict_row)


# ---------- Core SQL helpers (RLS-safe with service creds) ----------

def _select_unprocessed(conn: psycopg.Connection, thread_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """
    Select unprocessed inbox_to_bot messages.
    Using COALESCE(processed,false)=false to be robust.
    """
    if thread_id:
        q = """
            SELECT id, thread_id, created_at, created_by, author_member_id, audience, content
            FROM messages
            WHERE thread_id = %s
              AND audience = 'inbox_to_bot'
              AND COALESCE(processed, FALSE) = FALSE
            ORDER BY created_at ASC
            LIMIT %s
        """
        args: Tuple[Any, ...] = (thread_id, limit)
    else:
        q = """
            SELECT id, thread_id, created_at, created_by, author_member_id, audience, content
            FROM messages
            WHERE audience = 'inbox_to_bot'
              AND COALESCE(processed, FALSE) = FALSE
            ORDER BY created_at ASC
            LIMIT %s
        """
        args = (limit,)

    with conn.cursor() as cur:
        cur.execute(q, args)
        return list(cur.fetchall())


def _loop_id_for_member(conn: psycopg.Connection, author_member_id: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT loop_id FROM members WHERE id = %s",
            (author_member_id,),
        )
        row = cur.fetchone()
        return row["loop_id"] if row else None


def _recipients_for_loop(conn: psycopg.Connection, loop_id: str, exclude_ids: List[str]) -> List[str]:
    placeholders = ", ".join(["%s"] * len(exclude_ids)) if exclude_ids else ""
    if exclude_ids:
        q = f"""
            SELECT profile_id
            FROM members
            WHERE loop_id = %s
              AND profile_id NOT IN ({placeholders})
        """
        args: Tuple[Any, ...] = (loop_id, *exclude_ids)
    else:
        q = """
            SELECT profile_id
            FROM members
            WHERE loop_id = %s
        """
        args = (loop_id,)

    with conn.cursor() as cur:
        cur.execute(q, args)
        return [r["profile_id"] for r in cur.fetchall()]


def _insert_bot_messages_and_mark_processed(
    conn: psycopg.Connection,
    *,
    bot_profile_id: str,
    to_publish: List[Dict[str, Any]],
    source_message_ids: List[str],
) -> Tuple[int, int]:
    """
    Insert rows into messages (audience='bot_to_user') and mark sources processed.
    Returns (inserted_count, processed_count).
    """
    inserted = 0
    processed = 0

    with conn.cursor() as cur:
        # Insert bot_to_user rows
        if to_publish:
            # Build a VALUES list
            values_sql = ", ".join(
                ["(%s, %s, %s, %s, %s, %s)"] * len(to_publish)
            )
            args: List[Any] = []
            for row in to_publish:
                args.extend([
                    row["thread_id"],
                    row["created_at"],
                    bot_profile_id,
                    "bot_to_user",
                    row["recipient_profile_id"],
                    row["content"],
                ])

            cur.execute(
                f"""
                INSERT INTO messages (thread_id, created_at, created_by, audience, recipient_profile_id, content)
                VALUES {values_sql}
                """,
                args,
            )
            inserted = cur.rowcount  # number of rows inserted

        # Mark sources processed
        if source_message_ids:
            placeholders = ", ".join(["%s"] * len(source_message_ids))
            cur.execute(
                f"""
                UPDATE messages
                SET processed = TRUE, processed_at = NOW()
                WHERE id IN ({placeholders})
                """,
                source_message_ids,
            )
            processed = cur.rowcount

    return inserted, processed


# ---------- Route ----------

@router.post("/bot/process", response_model=BotProcessResponse)
def process_bot_messages(
    thread_id: Optional[str] = Query(default=None, description="Thread (conversation) id"),
    limit: int = Query(default=10, ge=1, le=100),
    dry_run: bool = Query(default=True),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """
    Process human inbox_to_bot messages into bot_to_user messages.
    Recipient resolution is done via members(loop_id) derived from author_member_id,
    excluding {author_profile_id, bot_profile_id}.
    All publish operations happen in a single DB transaction.
    """
    if not x_user_id:
        raise HTTPException(status_code=400, detail="missing_bot_profile_id_header")

    scanned = 0
    processed = 0
    inserted = 0
    skipped = 0
    items: List[BotProcessItem] = []
    flat_recipients: List[str] = []

    try:
        with _get_conn() as conn:
            rows = _select_unprocessed(conn, thread_id=thread_id, limit=limit)
            scanned = len(rows)

            if not rows:
                return BotProcessResponse(
                    stats=BotProcessStats(
                        scanned=scanned,
                        processed=processed,
                        inserted=inserted,
                        skipped=skipped,
                        dry_run=dry_run,
                        recipient_ids=[],
                    ),
                    items=[],
                )

            to_publish: List[Dict[str, Any]] = []
            source_ids: List[str] = []

            for r in rows:
                msg_id = r["id"]
                author_profile_id = r["created_by"]
                author_member_id = r.get("author_member_id")
                content = r.get("content") or ""
                thread = r["thread_id"]

                if not author_member_id:
                    logger.warning("message %s missing author_member_id; skipping", msg_id)
                    skipped += 1
                    continue

                loop_id = _loop_id_for_member(conn, author_member_id)
                if not loop_id:
                    logger.warning("message %s could not resolve loop_id; skipping", msg_id)
                    skipped += 1
                    continue

                exclude = [author_profile_id, x_user_id]
                recipients = _recipients_for_loop(conn, loop_id, exclude_ids=exclude)
                flat_recipients.extend(recipients)

                previews: List[BotProcessItemPreview] = []
                for pid in recipients:
                    reply_text = generate_reply(
                        human_text=content,
                        author_profile_id=author_profile_id,
                        recipient_profile_id=pid,
                        thread_id=thread,
                    )
                    previews.append(BotProcessItemPreview(recipient_profile_id=pid, text=reply_text))

                    if not dry_run:
                        to_publish.append(
                            {
                                "thread_id": thread,
                                "created_at": _now_iso(),
                                "recipient_profile_id": pid,
                                "content": reply_text,
                            }
                        )

                items.append(BotProcessItem(human_message_id=msg_id, previews=previews))
                if not dry_run:
                    source_ids.append(msg_id)

            if not dry_run:
                with conn.transaction():
                    ins, proc = _insert_bot_messages_and_mark_processed(
                        conn,
                        bot_profile_id=x_user_id,
                        to_publish=to_publish,
                        source_message_ids=source_ids,
                    )
                    inserted += ins
                    processed += proc

        return BotProcessResponse(
            stats=BotProcessStats(
                scanned=scanned,
                processed=processed,
                inserted=inserted,
                skipped=skipped,
                dry_run=dry_run,
                recipient_ids=list(dict.fromkeys(flat_recipients)),
            ),
            items=items,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("bot.process failed: {}", e)
        raise HTTPException(status_code=500, detail="bot_process_exception")