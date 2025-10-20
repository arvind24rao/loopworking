# app/bot.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel
from loguru import logger

# Project-local helpers (expected in repo)
from app.llm import generate_reply
from app.supa import supa  # Supabase Python client (postgrest)

router = APIRouter(prefix="/api", tags=["bot"])


# --------- Models (response wiring) ---------

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
    recipient_ids: List[str] = []  # optional for quick console visibility


class BotProcessResponse(BaseModel):
    ok: bool = True
    reason: Optional[str] = None
    stats: BotProcessStats
    items: List[BotProcessItem] = []


# --------- Small utils ---------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _select_unprocessed(thread_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """
    Fetch inbox_to_bot messages awaiting processing for a given thread.
    Enforces processed=false semantics (COALESCE handled via default/trigger in DB).
    Returns a list of raw dict rows (PostgREST style).
    """
    query = (
        supa.table("messages")
        .select(
            "id, thread_id, created_at, created_by, author_member_id, audience, content, processed, processed_at"
        )
        .eq("audience", "inbox_to_bot")
        .eq("processed", False)
        .order("created_at", desc=False)
        .limit(limit)
    )
    if thread_id:
        query = query.eq("thread_id", thread_id)
    res = query.execute()
    data = getattr(res, "data", None) or res.get("data", [])
    return data or []


def _mark_processed(message_ids: List[str]) -> None:
    if not message_ids:
        return
    supa.table("messages").update(
        {"processed": True, "processed_at": _now_iso()}
    ).in_("id", message_ids).execute()


def _insert_bot_messages(rows: List[Dict[str, Any]]) -> int:
    """
    Insert bot_to_user messages; returns number inserted.
    rows must contain: thread_id, created_at, created_by (bot), audience='bot_to_user',
                       recipient_profile_id, content
    """
    if not rows:
        return 0
    res = supa.table("messages").insert(rows).execute()
    data = getattr(res, "data", None) or res.get("data", [])
    return len(data or rows)


def _resolve_recipients_via_supabase(
    author_member_id: str,
    author_profile_id: str,
    bot_profile_id: str,
) -> List[str]:
    """
    Canonical recipient logic using Supabase:
      loop_id := members(loop_id) via author_member_id
      recipients := all members.profile_id in loop_id MINUS {author_profile_id, bot_profile_id}
    """
    # 1) loop_id from members
    mem = (
        supa.table("members")
        .select("loop_id")
        .eq("id", author_member_id)
        .single()
        .execute()
    )
    loop_id = (getattr(mem, "data", None) or mem.get("data", {})).get("loop_id")
    if not loop_id:
        return []

    # 2) recipients from members in loop
    data = (
        supa.table("members")
        .select("profile_id")
        .eq("loop_id", loop_id)
        .execute()
    )
    profs = [d["profile_id"] for d in ((getattr(data, "data", None) or data.get("data", [])) or [])]
    exclude = {author_profile_id, bot_profile_id}
    return [pid for pid in profs if pid not in exclude]


# --------- Route implementation ---------

@router.post("/bot/process", response_model=BotProcessResponse)
def process_bot_messages(
    thread_id: Optional[str] = Query(default=None, description="Thread (conversation) id"),
    limit: int = Query(default=10, ge=1, le=100),
    dry_run: bool = Query(default=True),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """
    Processes human inbox_to_bot messages and either previews (dry_run) or publishes
    bot_to_user replies. Uses members(loop_id) derived from author_member_id to determine
    recipients, excluding the author and bot(s).
    """
    if not x_user_id:
        raise HTTPException(status_code=400, detail="missing_bot_profile_id_header")

    scanned = 0
    processed = 0
    inserted = 0
    skipped = 0
    items: List[BotProcessItem] = []
    all_recipient_ids: List[str] = []

    try:
        # 1) select eligible work
        rows = _select_unprocessed(thread_id, limit=limit)
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

        # 2) for each message, resolve recipients and generate previews
        to_insert: List[Dict[str, Any]] = []
        processed_ids: List[str] = []

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

            # resolve recipients canonically
            recipients = _resolve_recipients_via_supabase(
                author_member_id=author_member_id,
                author_profile_id=author_profile_id,
                bot_profile_id=x_user_id,
            )
            all_recipient_ids.extend(recipients)

            # build previews
            previews = []
            for pid in recipients:
                reply_text = generate_reply(
                    human_text=content,
                    author_profile_id=author_profile_id,
                    recipient_profile_id=pid,
                    thread_id=thread,
                )
                previews.append(BotProcessItemPreview(recipient_profile_id=pid, text=reply_text))

                if not dry_run:
                    to_insert.append(
                        {
                            "thread_id": thread,
                            "created_at": _now_iso(),
                            "created_by": x_user_id,
                            "audience": "bot_to_user",
                            "recipient_profile_id": pid,
                            "content": reply_text,
                        }
                    )

            items.append(BotProcessItem(human_message_id=msg_id, previews=previews))

            if not dry_run:
                processed_ids.append(msg_id)

        # 3) publish (insert + mark processed) or just return previews
        if not dry_run:
            if to_insert:
                inserted = _insert_bot_messages(to_insert)
            if processed_ids:
                _mark_processed(processed_ids)
                processed = len(processed_ids)

        return BotProcessResponse(
            stats=BotProcessStats(
                scanned=scanned,
                processed=processed,
                inserted=inserted,
                skipped=skipped,
                dry_run=dry_run,
                recipient_ids=list(dict.fromkeys(all_recipient_ids)),
            ),
            items=items,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("bot.process failed: {}", e)
        raise HTTPException(status_code=500, detail="bot_process_exception")