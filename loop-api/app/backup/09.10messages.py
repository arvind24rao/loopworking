# app/routes/messages.py
from __future__ import annotations
import os
from typing import Optional, List, Dict, Any
import requests
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from uuid import UUID

router = APIRouter(prefix="/api", tags=["messages"])

# ---------- helpers ----------
def get_env():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return url, key

def _h(key: str, prefer: Optional[str] = None) -> dict:
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    if prefer: h["Prefer"] = prefer
    return h

def supa_select(base: str, key: str, path: str, params: dict) -> List[dict]:
    r = requests.get(f"{base}/rest/v1/{path}", params=params, headers=_h(key))
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Supabase select failed: {r.text}")
    return r.json()

def supa_single(base: str, key: str, path: str, params: dict) -> Optional[dict]:
    rows = supa_select(base, key, path, params)
    return rows[0] if rows else None

def supa_insert(base: str, key: str, path: str, json_body: dict) -> dict:
    r = requests.post(f"{base}/rest/v1/{path}", json=[json_body], headers=_h(key, "return=representation"))
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Supabase insert failed: {r.text}")
    rows = r.json()
    return rows[0] if isinstance(rows, list) and rows else {}

# ---------- models ----------
class SendMessageBody(BaseModel):
    thread_id: UUID
    user_id: UUID                     # profile_id of the sender
    content: str = Field(min_length=1)

class SendMessageResponse(BaseModel):
    ok: bool
    message: Dict[str, Any]

class MessagesResponse(BaseModel):
    messages: List[Dict[str, Any]]

# ---------- routes ----------
@router.post("/send_message", response_model=SendMessageResponse)
def send_message(body: SendMessageBody):
    base, key = get_env()

    # 1) Find the thread to get its loop_id
    thread = supa_single(
        base, key, "threads",
        {"select": "id,loop_id", "id": f"eq.{body.thread_id}"}
    )
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")

    loop_id = thread["loop_id"]

    # 2) Find the member row (author_member_id) for (loop_id, profile_id=user_id)
    member = supa_single(
        base, key, "loop_members",
        {"select": "id,loop_id,profile_id", "loop_id": f"eq.{loop_id}", "profile_id": f"eq.{body.user_id}"}
    )
    if not member:
        # Helpful message so you can fix data quickly
        raise HTTPException(
            status_code=400,
            detail=f"Sender profile {body.user_id} is not a member of loop {loop_id} (no loop_members row)."
        )

    author_member_id = member["id"]

    # 3) Insert the message (defaults for role/channel/visibility come from DB)
    row = supa_insert(
        base, key, "messages",
        {
            "thread_id": str(body.thread_id),
            "created_by": str(body.user_id),       # profile_id
            "author_member_id": str(author_member_id),
            "content_ciphertext": f"cipher:{body.content}",
            # leave role/channel/visibility/audience to DB defaults
        }
    )
    return {"ok": True, "message": row}

@router.get("/get_messages", response_model=MessagesResponse)
def get_messages(thread_id: UUID = Query(...), user_id: UUID = Query(...)):
    # Note: user_id is kept for compatibility with the frontend; we don't filter by it
    base, key = get_env()
    rows = supa_select(
        base, key, "messages",
        {
            "select": "id,thread_id,created_at,created_by,author_member_id,content_ciphertext",
            "thread_id": f"eq.{thread_id}",
            "order": "created_at.asc",
        }
    )
    return {"messages": rows}