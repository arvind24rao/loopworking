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

# Use your real LLM response
from app.llm import generate_reply

# Path = /api/bot/process (as in openapi.json)
router = APIRouter(prefix="/api/bot", tags=["bot"])

# =============================== Models ===============================

class BotProcessItem(BaseModel):
    human_message_id: str = Field(..., description="source inbox_to_bot message id")
    thread_id: str
    recipients: List[str] = []
    bot_rows: List[str] = []               # ids of inserted bot_to_user rows (publish only)
    previews: List[Dict[str, str]] = []    # shown only in dry_run
    skipped_reason: Optional[str] = None

class BotProcessStats(BaseModel):
    scanned: int = 0
    processed: int = 0        # human rows marked processed (publish only)
    inserted: int = 0         # bot_to_user rows inserted
    skipped: int = 0
    dry_run: bool = True

class BotProcessResponse(BaseModel):
    ok: bool = True
    reason: Optional[str] = None
    stats: BotProcessStats
    items: List[BotProcessItem] = []

# =============================== DB utils =============================

def _conn() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL (or SUPABASE_DB_URL) is not set")
    return psycopg.connect(dsn, row_factory=dict_row)

def _require_bot_id(x_user_id: Optional[str]) -> str:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Missing X-User-Id (bot profile id required)")
    try:
        uuid.UUID(str(x_user_id))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid X-User-Id (must be UUID)")
    return str(x_user_id)

# =============================== Helpers ==============================

def _decode_ciphertext(c: Optional[str]) -> str:
    """
    Align with feed.py: content is currently plaintext; if legacy prefix 'cipher:' exists, strip it.
    """
    if not c:
        return ""
    return c[7:].strip() if c.startswith("cipher:") else c

def _now_singapore() -> tuple[str, str, str]:
    """
    Produce (current_date, current_time, timezone_str) for the model prompt.
    Format: date 'DD Month YYYY' (zero-padded day is acceptable), time 'HH:MM' 24h.
    """
    try:
        from zoneinfo import ZoneInfo  # py3.9+
        tz = ZoneInfo("Asia/Singapore")
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz)
    current_date = now.strftime("%d %B %Y")
    current_time = now.strftime("%H:%M")
    return current_date, current_time, "Asia/Singapore"

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

def _profile_handle(conn: psycopg.Connection, profile_id: str) -> Optional[str]:
    """
    Resolve a user's handle for nicer author attribution.
    Returns None if not found.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT handle FROM profiles WHERE id = %s", (profile_id,))
        row = cur.fetchone()
        handle = row["handle"] if row else None
        if handle:
            handle = handle.strip()
        return handle or None

def _author_label(conn: psycopg.Connection, profile_id: str) -> str:
    """
    Prefer @handle; else 'User <uuid-prefix>'.
    """
    handle = _profile_handle(conn, profile_id)
    if handle:
        # Avoid duplicating '@' if already present
        return f"@{handle}" if not handle.startswith("@") else handle
    return f"User {profile_id[:8]}"

def _recipients_for_loop(conn: psycopg.Connection, loop_id: str, exclude_profile_ids: List[str]) -> List[str]:
    """
    recipients = members.profile_id in loop MINUS exclude set
    (exclude must include author_profile_id and bot_profile_id)
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

# =============================== SQL helpers ==========================

def _fetch_unprocessed_humans(
    conn: psycopg.Connection, *, thread_id: Optional[str], limit: int
) -> List[Dict[str, Any]]:
    """
    Queue = audience='inbox_to_bot' AND bot_processed_at IS NULL
    (Intentionally not using messages.processed.)
    NOTE: include content_ciphertext so we can pass the actual human text to the LLM.
    """
    sql = """
      SELECT id, thread_id, created_by, author_member_id, content_ciphertext
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

def _insert_bot_dm(
    conn: psycopg.Connection,
    *,
    thread_id: str,
    bot_profile_id: str,
    bot_member_id: str,
    recipient_profile_id: str,
    content_ciphertext: str,   # plaintext for now; replace when encryption is wired
) -> str:
    """
    Minimal, schema-correct insert:
      Only set the fields that must be explicit:
        - thread_id
        - created_by (bot profile id)
        - author_member_id (bot's member id)
        - audience ('bot_to_user')
        - recipient_profile_id
        - content_ciphertext
      Let DB defaults populate role/channel/visibility/created_at.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO messages
              (thread_id, created_by, author_member_id,
               audience, recipient_profile_id, content_ciphertext)
            VALUES
              (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                thread_id,
                bot_profile_id,
                bot_member_id,
                "bot_to_user",
                recipient_profile_id,
                content_ciphertext,
            ),
        )
        row = cur.fetchone()
        return str(row["id"])

def _mark_human_processed(conn: psycopg.Connection, human_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE messages SET bot_processed_at = NOW() WHERE id = %s AND bot_processed_at IS NULL",
            (human_id,),
        )

# =============================== Route ================================

@router.post("/process", response_model=BotProcessResponse)
def process_queue(
    thread_id: Optional[str] = Query(None, description="Only process this thread"),
    limit: int = Query(10, ge=1, le=100),
    dry_run: bool = Query(True),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),  # bot profile id
):
    """
    Process human→bot messages and fan out bot→user DMs.

    - dry_run=True  → preview only (no DB writes; DO NOT mark processed)
    - dry_run=False → insert bot_to_user rows + mark source human with bot_processed_at
    """
    bot_profile_id = _require_bot_id(x_user_id)

    stats = BotProcessStats(dry_run=bool(dry_run))
    items: List[BotProcessItem] = []

    try:
        with _conn() as conn:
            humans = _fetch_unprocessed_humans(conn, thread_id=thread_id, limit=limit)
            stats.scanned = len(humans)

            if not humans:
                return BotProcessResponse(ok=True, reason=None, stats=stats, items=[])

            # Timestamp context for the model (Asia/Singapore by default)
            current_date, current_time, tz_name = _now_singapore()

            for h in humans:
                src_id = str(h["id"])
                t_id = str(h["thread_id"])
                author_profile_id = str(h["created_by"])
                author_member_id = h.get("author_member_id")
                human_plaintext = _decode_ciphertext(h.get("content_ciphertext"))

                item = BotProcessItem(human_message_id=src_id, thread_id=t_id)

                # Need author's member to resolve loop
                if not author_member_id:
                    stats.skipped += 1
                    item.skipped_reason = "missing author_member_id"
                    items.append(item)
                    continue

                loop_id = _loop_id_for_member(conn, str(author_member_id))
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

                # recipients = everyone in loop except {author, bot}
                recipients = _recipients_for_loop(conn, loop_id, exclude_profile_ids=[author_profile_id, bot_profile_id])
                item.recipients = recipients[:]

                previews: List[Dict[str, str]] = []
                new_ids: List[str] = []

                # Author label for nicer summaries
                author = _author_label(conn, author_profile_id)

                for pid in recipients:
                    reply_text = ""
                    if human_plaintext:
                        reply_text = generate_reply(
                            context_messages=[{"author": author, "text": human_plaintext}],
                            current_date=current_date,
                            current_time=current_time,
                            timezone=tz_name,
                            user_id=f"loop:{pid}",
                        )

                    if dry_run:
                        previews.append({"recipient_profile_id": pid, "content": reply_text})
                    else:
                        if reply_text:
                            new_id = _insert_bot_dm(
                                conn,
                                thread_id=t_id,
                                bot_profile_id=bot_profile_id,
                                bot_member_id=str(bot_member_id),
                                recipient_profile_id=pid,
                                content_ciphertext=reply_text,
                            )
                            new_ids.append(new_id)
                        else:
                            stats.skipped += 1

                if not dry_run:
                    _mark_human_processed(conn, src_id)
                    stats.processed += 1
                    stats.inserted += len(new_ids)
                    item.bot_rows = new_ids
                else:
                    item.previews = previews

                if not dry_run and not new_ids:
                    item.skipped_reason = item.skipped_reason or "empty_llm_reply"

                items.append(item)

        return BotProcessResponse(ok=True, reason=None, stats=stats, items=items)

    except HTTPException:
        raise
    except Exception as e:
        # Bubble up the real error text to speed up fixes
        return BotProcessResponse(
            ok=False,
            reason=f"bot_process_exception: {e}",
            stats=stats,
            items=items,
        )