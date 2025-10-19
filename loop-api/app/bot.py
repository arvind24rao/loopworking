# app/bot.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import os
import psycopg  # psycopg 3.x
from psycopg.rows import dict_row
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from app.llm import generate_reply

# --------------------------------------------------------------------------------------
# If you have an LLM helper already, import it and swap into generate_reply().
# For now we keep the same "FYI." behavior you saw in your feeds/smoke tests.
# --------------------------------------------------------------------------------------
def generate_reply(human_text: str, author_profile_id: str, recipient_profile_id: str, thread_id: str) -> str:
    return "FYI."

router = APIRouter(prefix="/api", tags=["bot"])  # => /api/bot/process

# ----------------------------------- DB utils ----------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _get_conn() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL (or SUPABASE_DB_URL) is not set")
    # row_factory=dict_row gives dict-like rows: row["col"]
    return psycopg.connect(dsn, row_factory=dict_row)

# ----------------------------------- Queries -----------------------------------------

def _select_unprocessed(conn: psycopg.Connection, thread_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """
    Select human messages awaiting processing:
      audience='inbox_to_bot' AND COALESCE(processed,false)=false
    Ordered oldest first to maintain causal order.
    """
    base = """
      SELECT id, thread_id, created_at, created_by, author_member_id
      FROM messages
      WHERE audience='inbox_to_bot'
        AND COALESCE(processed,FALSE)=FALSE
    """
    args: List[Any] = []
    if thread_id:
        base += " AND thread_id=%s"
        args.append(thread_id)
    base += " ORDER BY created_at ASC LIMIT %s"
    args.append(limit)

    with conn.cursor() as cur:
        cur.execute(base, tuple(args))
        return list(cur.fetchall())

def _loop_id_for_member(conn: psycopg.Connection, author_member_id: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT loop_id FROM members WHERE id=%s", (author_member_id,))
        row = cur.fetchone()
        return row["loop_id"] if row else None

def _bot_member_id_for_loop(conn: psycopg.Connection, loop_id: str, bot_profile_id: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM members WHERE loop_id=%s AND profile_id=%s",
            (loop_id, bot_profile_id),
        )
        row = cur.fetchone()
        return row["id"] if row else None

def _recipients_for_loop(conn: psycopg.Connection, loop_id: str, exclude_ids: List[str]) -> List[str]:
    """
    Recipients = all profile_ids in loop MINUS {author_profile_id, bot_profile_id}.
    """
    if exclude_ids:
        ph = ", ".join(["%s"] * len(exclude_ids))
        q = f"SELECT profile_id FROM members WHERE loop_id=%s AND profile_id NOT IN ({ph})"
        args = (loop_id, *exclude_ids)
    else:
        q = "SELECT profile_id FROM members WHERE loop_id=%s"
        args = (loop_id,)
    with conn.cursor() as cur:
        cur.execute(q, args)
        return [r["profile_id"] for r in cur.fetchall()]

def _insert_and_mark(
    conn: psycopg.Connection,
    *,
    bot_profile_id: str,
    rows_to_publish: List[Dict[str, Any]],
    source_ids: List[str],
) -> Tuple[int, int]:
    """
    Insert bot_to_user messages that satisfy NOT NULL columns and then mark sources processed.
    rows_to_publish expects per-row:
      thread_id, created_at, author_member_id (bot's), recipient_profile_id, content_ciphertext
    Returns (inserted_count, processed_count).
    """
    inserted = processed = 0
    with conn.cursor() as cur:
        if rows_to_publish:
            values_sql = ", ".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"] * len(rows_to_publish))
            args: List[Any] = []
            for r in rows_to_publish:
                args.extend([
                    r["thread_id"],           # thread_id
                    r["created_at"],          # created_at
                    bot_profile_id,           # created_by
                    r["bot_member_id"],       # author_member_id (bot's membership in that loop)
                    "bot",                    # role
                    "outbox",                 # channel
                    "private",                # visibility
                    "bot_to_user",            # audience
                    r["recipient_profile_id"],
                    r["content_ciphertext"],  # storing plaintext here until encryptor is wired
                ])
            cur.execute(
                f"""
                INSERT INTO messages
                  (thread_id, created_at, created_by, author_member_id,
                   role, channel, visibility, audience, recipient_profile_id, content_ciphertext)
                VALUES {values_sql}
                """,
                args,
            )
            inserted = cur.rowcount

        if source_ids:
            ph = ", ".join(["%s"] * len(source_ids))
            cur.execute(
                f"UPDATE messages SET processed=TRUE, processed_at=NOW() WHERE id IN ({ph})",
                source_ids,
            )
            processed = cur.rowcount

    return inserted, processed

# ----------------------------- Response Models (stable shape) -------------------------

class Preview(BaseModel):
    recipient_profile_id: str
    content: str

class Item(BaseModel):
    human_message_id: str
    thread_id: str
    recipients: List[str]
    bot_rows: List[Dict[str, Any]] = []  # kept for shape compatibility
    previews: List[Preview] = []
    skipped_reason: Optional[str] = None

class Stats(BaseModel):
    scanned: int
    processed: int
    inserted: int
    skipped: int
    dry_run: bool

class ProcessResponse(BaseModel):
    ok: bool
    reason: Optional[str]
    stats: Stats
    items: List[Item]

# ------------------------------------ Route ------------------------------------------

@router.post("/bot/process", response_model=ProcessResponse)
def process(
    thread_id: Optional[str] = Query(default=None, description="Conversation/thread id"),
    limit: int = Query(default=10, ge=1, le=100),
    dry_run: bool = Query(default=True),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),  # bot profile id
):
    """
    Process human inbox_to_bot messages into bot_to_user messages.

    Fixes:
      - Only select COALESCE(processed,false)=false
      - Derive loop_id via author_member_id -> members.loop_id
      - Compute recipients from members(loop_id) minus {author, bot}
      - Insert rows satisfying NOT NULLs (incl. author_member_id + content_ciphertext)
      - Mark sources processed (only when dry_run=false)
    """
    if not x_user_id:
        raise HTTPException(status_code=400, detail="missing_bot_profile_id_header")

    scanned = processed = inserted = skipped = 0
    items: List[Item] = []

    try:
        with _get_conn() as conn:
            # 1) pull work
            rows = _select_unprocessed(conn, thread_id, limit)
            scanned = len(rows)
            if not rows:
                return ProcessResponse(
                    ok=True, reason=None,
                    stats=Stats(scanned=scanned, processed=0, inserted=0, skipped=0, dry_run=dry_run),
                    items=[],
                )

            to_publish: List[Dict[str, Any]] = []
            source_ids: List[str] = []

            # 2) per message: resolve loop, bot membership, recipients; build previews/publish rows
            for r in rows:
                msg_id = r["id"]
                thread = r["thread_id"]
                author_profile_id = r["created_by"]
                author_member_id = r["author_member_id"]

                recipients: List[str] = []
                previews: List[Preview] = []
                skipped_reason: Optional[str] = None

                try:
                    if not author_member_id:
                        skipped += 1
                        skipped_reason = "missing author_member_id"
                    else:
                        loop_id = _loop_id_for_member(conn, author_member_id)
                        if not loop_id:
                            skipped += 1
                            skipped_reason = "missing loop_id"
                        else:
                            bot_member_id = _bot_member_id_for_loop(conn, loop_id, x_user_id)
                            if not bot_member_id:
                                skipped += 1
                                skipped_reason = "bot not a member of loop"
                            else:
                                # Exclude author and bot
                                recipients = _recipients_for_loop(conn, loop_id, exclude_ids=[author_profile_id, x_user_id])

                                # Build previews (publish only writes; dry_run only previews)
                                for pid in recipients:
                                    txt = generate_reply("", author_profile_id, pid, thread)
                                    previews.append(Preview(recipient_profile_id=pid, content=txt))
                                    if not dry_run:
                                        to_publish.append(
                                            {
                                                "thread_id": thread,
                                                "created_at": _now_iso(),
                                                "bot_member_id": bot_member_id,        # REQUIRED (NOT NULL author_member_id)
                                                "recipient_profile_id": pid,
                                                "content_ciphertext": txt,             # store plaintext for now
                                            }
                                        )

                                if not dry_run:
                                    source_ids.append(msg_id)

                except Exception as e:
                    skipped += 1
                    skipped_reason = f"error: {e}"

                items.append(
                    Item(
                        human_message_id=msg_id,
                        thread_id=thread,
                        recipients=recipients,
                        previews=previews if dry_run else [],
                        bot_rows=[],  # keep shape compatibility
                        skipped_reason=skipped_reason,
                    )
                )

            # 3) publish (insert + mark processed) atomically
            if not dry_run and (to_publish or source_ids):
                with _get_conn() as conn2, conn2.transaction():
                    ins, proc = _insert_and_mark(
                        conn2,
                        bot_profile_id=x_user_id,
                        rows_to_publish=to_publish,
                        source_ids=source_ids,
                    )
                    inserted += ins
                    processed += proc

        return ProcessResponse(
            ok=True,
            reason=None,
            stats=Stats(scanned=scanned, processed=processed, inserted=inserted, skipped=skipped, dry_run=dry_run),
            items=items,
        )

    except HTTPException:
        raise
    except Exception as e:
        return ProcessResponse(
            ok=False,
            reason=str(e),
            stats=Stats(scanned=scanned, processed=processed, inserted=inserted, skipped=skipped, dry_run=dry_run),
            items=items,
        )