# app/routes/bot.py
from __future__ import annotations
import os
from typing import Optional, List, Dict, Any
import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from uuid import UUID

# reuse helpers that DO exist in feed.py (do NOT import supa_insert from there)
from .feed import get_env, supa_select, supa_single, ensure_dt, summarise_messages

router = APIRouter(prefix="/api", tags=["bot"])

# local helpers (avoid cross-imports)
def _h(key: str, prefer: Optional[str] = None) -> dict:
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    if prefer: h["Prefer"] = prefer
    return h

def supa_insert(base: str, key: str, path: str, json_body: dict) -> dict:
    r = requests.post(f"{base}/rest/v1/{path}", json=[json_body], headers=_h(key, "return=representation"))
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Supabase insert failed: {r.text}")
    rows = r.json()
    return rows[0] if isinstance(rows, list) and rows else {}

router = APIRouter(prefix="/api", tags=["bot"])

class BotPostBody(BaseModel):
    loop_id: UUID
    thread_id: UUID
    for_profile_id: UUID  # requester (whose own posts we exclude)

@router.post("/bot_post_digest")
def bot_post_digest(body: BotPostBody):
    base, key = get_env()

    # 1) Resolve loop + requester handle
    loop = supa_single(base, key, "loops", {"select":"id,name","id":f"eq.{body.loop_id}"}) or {}
    loop_name = loop.get("name") or str(body.loop_id)
    requester = supa_single(base, key, "profiles", {"select":"id,handle","id":f"eq.{body.for_profile_id}"}) or {}
    requester_handle = requester.get("handle") or str(body.for_profile_id)

    # 2) Find messages in this loop/thread excluding requester (last 48h to keep short)
    rows = supa_select(base, key, "messages", {
        "select":"id,created_at,content_ciphertext,created_by,threads!inner(id,loop_id)",
        "threads.id": f"eq.{body.thread_id}",
        "created_by": f"neq.{body.for_profile_id}",
        "order":"created_at.asc",
    })
    if not rows:
        digest = "No new updates."
    else:
        def decode(c: Optional[str]) -> str:
            if not c: return ""
            return c[7:].strip() if c.startswith("cipher:") else c
        texts = [decode(r.get("content_ciphertext")) for r in rows if r.get("content_ciphertext")]
        digest = summarise_messages(loop_name, requester_handle, texts)

    # 3) Identify the loop bot’s member id so we can write a message
    agent = supa_single(base, key, "loop_agents", {
        "select":"id,loop_id,agent_profile_id",
        "loop_id": f"eq.{body.loop_id}",
    })
    if not agent or not agent.get("agent_profile_id"):
        raise HTTPException(status_code=400, detail="No loop bot configured (loop_agents.agent_profile_id missing).")

    agent_profile_id = agent["agent_profile_id"]

    agent_member = supa_single(base, key, "loop_members", {
        "select":"id",
        "loop_id": f"eq.{body.loop_id}",
        "profile_id": f"eq.{agent_profile_id}",
    })
    if not agent_member:
        # auto-enrol the bot as a member of the loop (optional; comment out if you prefer to enforce pre-creation)
        agent_member = supa_insert(base, key, "loop_members", {
            "loop_id": str(body.loop_id),
            "profile_id": str(agent_profile_id),
        })

    # 4) Insert the bot message into this thread
    row = supa_insert(base, key, "messages", {
        "thread_id": str(body.thread_id),
        "created_by": str(agent_profile_id),               # bot profile_id
        "author_member_id": str(agent_member["id"]),       # bot's loop_member id
        "content_ciphertext": f"cipher:{digest}",
        # optionally set a distinct role/channel if your enum allows (e.g., role='assistant')
    })

    return {"ok": True, "message": row, "digest_text": digest}