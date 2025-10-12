# app/routes/bot.py
from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from app.db import get_conn  # psycopg connection factory
from app.llm import generate_reply  # existing LLM relay helper
from app.crypto import seal_plaintext  # shim that returns "cipher:<text>" as per handbook

# router = APIRouter(prefix="/bot", tags=["bot"])
router = APIRouter(prefix="/api/bot", tags=["bot"])

# ---- Config helpers ---------------------------------------------------------

def _get_bot_ids_from_env() -> List[str]:
    """Supports BOT_PROFILE_IDS (comma-separated) or legacy BOT_PROFILE_ID."""
    env_multi = os.getenv("BOT_PROFILE_IDS", "")
    env_legacy = os.getenv("BOT_PROFILE_ID", "")
    ids: List[str] = []
    if env_multi.strip():
        ids.extend([x.strip() for x in env_multi.split(",") if x.strip()])
    if env_legacy.strip():
        ids.append(env_legacy.strip())
    # De-dup while preserving order
    seen = set()
    uniq = []
    for x in ids:
        if x not in seen:
            uniq.append(x)
            seen.add(x)
    return uniq


def _require_bot_caller(x_user_id: Optional[str]) -> str:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Missing X-User-Id (bot identity required)")
    bot_ids = _get_bot_ids_from_env()
    if not bot_ids:
        raise HTTPException(status_code=500, detail="No BOT_PROFILE_ID(S) configured")
    if x_user_id not in bot_ids:
        raise HTTPException(status_code=403, detail="X-User-Id is not an authorised bot")
    return x_user_id

# ---- Models -----------------------------------------------------------------

class ProcessStats(BaseModel):
    scanned: int = 0
    processed: int = 0
    inserted: int = 0
    skipped: int = 0
    dry_run: bool = False

class ProcessItemResult(BaseModel):
    human_message_id: str
    thread_id: str
    recipients: List[str] = []
    bot_rows: List[str] = []
    skipped_reason: Optional[str] = None

class BotProcessResponse(BaseModel):
    ok: bool = True
    reason: Optional[str] = None
    stats: ProcessStats
    items: List[ProcessItemResult] = []

# ---- Core query helpers -----------------------------------------------------

INBOX_TO_BOT = "inbox_to_bot"
BOT_TO_USER = "bot_to_user"

def _fetch_unprocessed_human_messages(conn, *, thread_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """
    Select oldest unprocessed human->bot messages (audience=inbox_to_bot and bot_processed_at is null).
    Optional thread filter.
    """
    with conn.cursor() as cur:
        if thread_id:
            cur.execute(
                """
                select id, thread_id, author_member_id, created_by, created_at
                from messages
                where audience = %s
                  and bot_processed_at is null
                  and thread_id = %s
                order by created_at asc
                limit %s
                """,
                (INBOX_TO_BOT, uuid.UUID(thread_id), limit),
            )
        else:
            cur.execute(
                """
                select id, thread_id, author_member_id, created_by, created_at
                from messages
                where audience = %s
                  and bot_processed_at is null
                order by created_at asc
                limit %s
                """,
                (INBOX_TO_BOT, limit),
            )
        rows = cur.fetchall()
    cols = ["id", "thread_id", "author_member_id", "created_by", "created_at"]
    return [dict(zip(cols, r)) for r in rows]


def _thread_loop_id_and_members(conn, thread_id: str) -> Tuple[str, List[str]]:
    """
    Return (loop_id, human_profile_ids[]) for the given thread.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select t.loop_id from threads t where t.id = %s",
            (uuid.UUID(thread_id),),
        )
        res = cur.fetchone()
        if not res:
            raise HTTPException(status_code=404, detail="Thread not found")
        (loop_id,) = res

        cur.execute(
            "select lm.profile_id from loop_members lm where lm.loop_id = %s",
            (loop_id,),
        )
        member_rows = cur.fetchall()
    member_ids = [str(r[0]) for r in member_rows]
    return (str(loop_id), member_ids)


def _resolve_bot_member_id(conn, *, loop_id: str, bot_profile_id: str) -> Optional[str]:
    """
    Finds the loop_agents entry for (loop_id, bot_profile_id) and returns its member id (the agent 'member' row).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select la.id
            from loop_agents la
            where la.loop_id = %s and la.agent_profile_id = %s
            """,
            (uuid.UUID(loop_id), uuid.UUID(bot_profile_id)),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


def _select_recent_sender_messages(conn, *, thread_id: str, author_member_id: str, limit: int = 5) -> List[str]:
    """
    Pull the last N plaintexts from the same sender within the thread, oldest->newest.
    (We rely on the crypto shim 'cipher:' to store plaintext after the prefix.)
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            select content_ciphertext
            from messages
            where thread_id = %s
              and author_member_id = %s
            order by created_at desc
            limit %s
            """,
            (uuid.UUID(thread_id), uuid.UUID(author_member_id), limit),
        )
        rows = cur.fetchall()
    out = []
    for (cipher_text,) in rows[::-1]:
        text = cipher_text or ""
        if text.startswith("cipher:"):
            text = text[len("cipher:") :]
        out.append(text.strip())
    return out


def _insert_bot_dm(
    conn,
    *,
    thread_id: str,
    loop_id: str,
    bot_profile_id: str,
    bot_member_id: Optional[str],
    recipient_profile_id: str,
    plaintext: str,
) -> str:
    """Insert one bot->user DM row and return its message id."""
    sealed = seal_plaintext(plaintext)
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into messages
                (thread_id, created_by, author_member_id, audience, recipient_profile_id,
                 content_ciphertext, created_at)
            values
                (%s, %s, %s, %s, %s, %s, now() at time zone 'utc')
            returning id
            """,
            (
                uuid.UUID(thread_id),
                uuid.UUID(bot_profile_id),
                uuid.UUID(bot_member_id) if bot_member_id else None,
                BOT_TO_USER,
                uuid.UUID(recipient_profile_id),
                sealed,
            ),
        )
        (msg_id,) = cur.fetchone()
    return str(msg_id)


def _mark_human_processed(conn, *, human_message_id: str):
    with conn.cursor() as cur:
        cur.execute(
            """
            update messages
            set bot_processed_at = now() at time zone 'utc'
            where id = %s and bot_processed_at is null
            """,
            (uuid.UUID(human_message_id),),
        )

# ---- Route ------------------------------------------------------------------

@router.post("/process", response_model=BotProcessResponse)
def process_queue(
    thread_id: Optional[str] = Query(None, description="Only process this thread"),
    limit: int = Query(20, ge=1, le=200),
    dry_run: bool = Query(False),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    """
    Process human->bot messages and emit per-recipient bot DMs.

    - Caller must set `X-User-Id` to an authorised bot profile id.
    - Writes two bot rows for typical 2-person loops (Bot→A, Bot→B), excluding the author's own view.
    - Idempotent by virtue of `bot_processed_at` on the human source row.
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
                human_message_id=str(h["id"]), thread_id=str(h["thread_id"])
            )
            try:
                loop_id, all_profiles = _thread_loop_id_and_members(conn, str(h["thread_id"]))

                author_member_id = str(h["author_member_id"])
                # find author profile id for exclusion
                with conn.cursor() as cur:
                    cur.execute(
                        "select profile_id from loop_members where id = %s",
                        (uuid.UUID(author_member_id),),
                    )
                    row = cur.fetchone()
                    if not row:
                        item.skipped_reason = "author member not found"
                        stats.skipped += 1
                        items.append(item)
                        conn.rollback()
                        continue
                    author_profile_id = str(row[0])

                recipients = [pid for pid in all_profiles if pid != author_profile_id]
                item.recipients = recipients

                # Gather short context from this author in this thread
                context_snippets = _select_recent_sender_messages(
                    conn, thread_id=str(h["thread_id"]), author_member_id=author_member_id, limit=5
                )

                # Resolve bot member id for this loop (if any)
                bot_member_id = _resolve_bot_member_id(conn, loop_id=loop_id, bot_profile_id=bot_profile_id)

                # For each recipient, generate one concise relay and insert
                new_ids: List[str] = []
                for recipient_profile_id in recipients:
                    if dry_run:
                        continue
                    relay_text = generate_reply(
                        sender_profile_id=author_profile_id,
                        recipient_profile_id=recipient_profile_id,
                        thread_id=str(h["thread_id"]),
                        loop_id=loop_id,
                        recent_messages=context_snippets,
                    ).strip()

                    if not relay_text:
                        continue

                    new_id = _insert_bot_dm(
                        conn,
                        thread_id=str(h["thread_id"]),
                        loop_id=loop_id,
                        bot_profile_id=bot_profile_id,
                        bot_member_id=bot_member_id,
                        recipient_profile_id=recipient_profile_id,
                        plaintext=relay_text,
                    )
                    new_ids.append(new_id)

                # Mark source human message as processed (unless dry_run)
                if not dry_run:
                    _mark_human_processed(conn, human_message_id=str(h["id"]))

                stats.processed += 1
                stats.inserted += len(new_ids)
                item.bot_rows = new_ids

                conn.commit()
                items.append(item)
            except HTTPException:
                conn.rollback()
                raise
            except Exception as e:
                conn.rollback()
                item.skipped_reason = f"error: {e}"
                stats.skipped += 1
                items.append(item)

    return BotProcessResponse(ok=True, stats=stats, items=items)