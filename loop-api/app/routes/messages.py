# app/routes/messages.py
from __future__ import annotations

import uuid
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Query, Body
from pydantic import BaseModel, Field

from app.db import get_conn  # psycopg connection factory
from app.crypto import seal_plaintext  # returns "cipher:<text>" per handbook

router = APIRouter(prefix="/api", tags=["website-facade"])

INBOX_TO_BOT = "inbox_to_bot"
BOT_TO_USER = "bot_to_user"


# ----------------------------- Models ---------------------------------------

class SendMessagePayload(BaseModel):
    thread_id: str = Field(..., description="UUID of the thread")
    user_id: str = Field(..., description="Profile UUID of the human sender")
    content: str = Field(..., min_length=1, max_length=4000)


class MessageOut(BaseModel):
    id: str
    thread_id: str
    created_at: str
    created_by: str
    author_member_id: Optional[str] = None
    audience: str
    recipient_profile_id: Optional[str] = None
    content: str  # plaintext (cipher shim removed)


class GetMessagesResponse(BaseModel):
    ok: bool = True
    items: List[MessageOut] = []


# --------------------------- Helpers ----------------------------------------

def _strip_cipher(cipher_text: Optional[str]) -> str:
    """
    Remove the 'cipher:' shim prefix. Returns empty string on None.
    """
    if not cipher_text:
        return ""
    if cipher_text.startswith("cipher:"):
        return cipher_text[len("cipher:") :].strip()
    return cipher_text.strip()


def _thread_exists_and_loop_id(conn, thread_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("select loop_id from threads where id = %s", (uuid.UUID(thread_id),))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Thread not found")
        (loop_id,) = row
        return str(loop_id)


def _author_member_id(conn, *, loop_id: str, profile_id: str) -> str:
    """
    Resolve loop_members.id for (loop_id, profile_id).
    """
    with conn.cursor() as cur:
        cur.execute(
            "select id from loop_members where loop_id = %s and profile_id = %s",
            (uuid.UUID(loop_id), uuid.UUID(profile_id)),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail="Sender is not a member of this loop",
            )
        (member_id,) = row
        return str(member_id)


def _row_to_message_out(row: Dict[str, Any]) -> MessageOut:
    return MessageOut(
        id=str(row["id"]),
        thread_id=str(row["thread_id"]),
        created_at=row["created_at"].isoformat(),
        created_by=str(row["created_by"]) if row["created_by"] else "",
        author_member_id=str(row["author_member_id"]) if row["author_member_id"] else None,
        audience=row["audience"],
        recipient_profile_id=str(row["recipient_profile_id"]) if row["recipient_profile_id"] else None,
        content=_strip_cipher(row["content_ciphertext"]),
    )


# ---------------------------- Routes ----------------------------------------

@router.post("/send_message", response_model=MessageOut)
def send_message(payload: SendMessagePayload = Body(...)):
    """
    Insert a human -> bot message in the given thread.

    Behaviour:
    - Validates thread and membership.
    - Inserts a row into messages with audience='inbox_to_bot'.
    - Returns the inserted row (plaintext content).
    """
    try:
        thread_uuid = str(uuid.UUID(payload.thread_id))
        user_uuid = str(uuid.UUID(payload.user_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID in thread_id or user_id")

    with get_conn() as conn:
        conn.autocommit = False
        try:
            loop_id = _thread_exists_and_loop_id(conn, thread_uuid)
            author_member_id = _author_member_id(conn, loop_id=loop_id, profile_id=user_uuid)

            sealed = seal_plaintext(payload.content)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into messages
                        (thread_id, created_by, author_member_id, audience, recipient_profile_id,
                         content_ciphertext, created_at)
                    values
                        (%s, %s, %s, %s, %s, %s, now() at time zone 'utc')
                    returning id, thread_id, created_at, created_by, author_member_id,
                              audience, recipient_profile_id, content_ciphertext
                    """,
                    (
                        uuid.UUID(thread_uuid),
                        uuid.UUID(user_uuid),
                        uuid.UUID(author_member_id),
                        INBOX_TO_BOT,
                        None,  # no recipient for human->bot
                        sealed,
                    ),
                )
                (
                    mid,
                    t_id,
                    created_at,
                    created_by,
                    author_member_id_row,
                    audience,
                    recipient_profile_id,
                    content_ciphertext,
                ) = cur.fetchone()

            conn.commit()

            return MessageOut(
                id=str(mid),
                thread_id=str(t_id),
                created_at=created_at.isoformat(),
                created_by=str(created_by),
                author_member_id=str(author_member_id_row) if author_member_id_row else None,
                audience=audience,
                recipient_profile_id=None,
                content=_strip_cipher(content_ciphertext),
            )
        except Exception:
            conn.rollback()
            raise


@router.get("/get_messages", response_model=GetMessagesResponse)
def get_messages(
    thread_id: str = Query(..., description="Thread UUID"),
    user_id: str = Query(..., description="Profile UUID of the viewer whose DM stream we are showing"),
    limit: int = Query(200, ge=1, le=1000),
):
    """
    Return a merged chronological stream for the viewer:

    - Human->Bot rows authored by this viewer (their own sent messages), AND
    - Bot->User rows targeted at this viewer (bot's DMs to them).

    This allows the frontend to render:
      - A or B (viewer’s human posts)      -> audience='inbox_to_bot'
      - Bot→Viewer (personalised bot rows) -> audience='bot_to_user' AND recipient_profile_id=user_id

    Response items include `audience` and `recipient_profile_id` so the UI can
    split panes for A, B, Bot→A, Bot→B if needed.
    """
    try:
        thread_uuid = uuid.UUID(thread_id)
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID in thread_id or user_id")

    with get_conn() as conn:
        # Validate the thread exists (and implicitly that it belongs to some loop)
        _ = _thread_exists_and_loop_id(conn, str(thread_uuid))

        with conn.cursor() as cur:
            # We select both sets and UNION ALL with ordering by created_at asc
            cur.execute(
                """
                select id, thread_id, created_at, created_by, author_member_id,
                       audience, recipient_profile_id, content_ciphertext
                from (
                    -- Viewer’s own human->bot posts
                    select m.id, m.thread_id, m.created_at, m.created_by, m.author_member_id,
                           m.audience, m.recipient_profile_id, m.content_ciphertext
                    from messages m
                    where m.thread_id = %s
                      and m.audience = %s
                      and m.created_by = %s

                    union all

                    -- Bot’s per-recipient DMs to the viewer
                    select m2.id, m2.thread_id, m2.created_at, m2.created_by, m2.author_member_id,
                           m2.audience, m2.recipient_profile_id, m2.content_ciphertext
                    from messages m2
                    where m2.thread_id = %s
                      and m2.audience = %s
                      and m2.recipient_profile_id = %s
                ) x
                order by created_at asc
                limit %s
                """,
                (
                    thread_uuid,
                    INBOX_TO_BOT,
                    user_uuid,
                    thread_uuid,
                    BOT_TO_USER,
                    user_uuid,
                    limit,
                ),
            )
            rows = cur.fetchall()

        cols = [
            "id",
            "thread_id",
            "created_at",
            "created_by",
            "author_member_id",
            "audience",
            "recipient_profile_id",
            "content_ciphertext",
        ]
        items = [_row_to_message_out(dict(zip(cols, r))) for r in rows]

        return GetMessagesResponse(ok=True, items=items)