# app/routes/feed.py
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any
from uuid import UUID

import requests
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

UTC = timezone.utc
router = APIRouter(prefix="/api", tags=["feed"])

# openai test
@router.get("/feed/selftest")
def feed_selftest():
    try:
        from openai import OpenAI
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return {"ok": False, "reason": "missing OPENAI_API_KEY"}
        client = OpenAI(api_key=api_key)
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        r = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Reply with a single short sentence."},
                {"role": "user", "content": "Say hello from Loop API."}
            ],
            max_tokens=20,
            temperature=0
        )
        return {"ok": True, "engine": "openai", "sample": r.choices[0].message.content.strip()}
    except Exception as e:
        return {"ok": False, "reason": "openai_exception", "detail": str(e)[:300]}

# ----------------------------
# Supabase helpers
# ----------------------------
def get_env():
    base = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base or not key:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return base, key

def _h(key: str, prefer: Optional[str] = None) -> Dict[str, str]:
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    if prefer:
        h["Prefer"] = prefer
    return h

def supa_select(base: str, key: str, path: str, params: Dict[str, str]) -> List[dict]:
    r = requests.get(f"{base}/rest/v1/{path}", params=params, headers=_h(key))
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Supabase select failed: {r.text}")
    return r.json()

def supa_single(base: str, key: str, path: str, params: Dict[str, str]) -> Optional[dict]:
    rows = supa_select(base, key, path, params)
    return rows[0] if rows else None

def supa_upsert(base: str, key: str, path: str, json_body: dict) -> dict:
    # Use 'resolution=merge-duplicates' to upsert on PK/unique constraint
    r = requests.post(
        f"{base}/rest/v1/{path}",
        json=[json_body],
        headers=_h(key, "return=representation,resolution=merge-duplicates"),
    )
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Supabase upsert failed: {r.text}")
    rows = r.json()
    return rows[0] if isinstance(rows, list) and rows else {}

# ----------------------------
# Utilities
# ----------------------------
def ensure_dt(x: Any) -> datetime:
    if x is None:
        return datetime.fromtimestamp(0, tz=UTC)
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except Exception:
        return datetime.fromtimestamp(0, tz=UTC)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

def _clean_messages_for_summary(texts: List[str], per_msg_cap: int = 240) -> List[str]:
    cleaned: List[str] = []
    seen = set()
    url_re = re.compile(r"https?://\S+")
    ws_re = re.compile(r"\s+")
    for t in texts:
        if not t:
            continue
        t = url_re.sub("", t)
        t = ws_re.sub(" ", t).strip()
        if len(t) > per_msg_cap:
            t = t[:per_msg_cap].rstrip() + "…"
        if len(t) < 2:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(t)
    return cleaned[:200]

def summarise_messages(loop_name: str, requester_handle: str, messages: List[str]) -> tuple[str, str]:
    """
    Returns (summary_text, engine), engine is 'openai' or 'fallback'.
    """
    def simple() -> tuple[str, str]:
        joined = " ".join(messages)
        return ((joined or "No new updates.")[:500], "fallback")

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return simple()

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

        # Bound context size even if many messages slip through
        N, M = 30, 10
        msgs = messages if len(messages) <= (N + M) else (messages[:N] + messages[-M:])

        system = (
            "You are the loop bot for a private group. "
            "Write a concise shared update in neutral EN-GB, max 3 sentences. "
            "Third-person; no quotes; no speculation."
        )
        user = (
            f"Loop name: {loop_name}\n"
            f"Requester (exclude their posts): @{requester_handle}\n"
            "Posts to summarise:\n- " + "\n- ".join(msgs)
        )

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
            max_tokens=220,
        )
        text = resp.choices[0].message.content.strip()
        return (text, "openai")
    except Exception:
        return simple()

# ----------------------------
# Response model
# ----------------------------
class FeedDigest(BaseModel):
    loop_id: UUID
    for_profile_id: UUID
    items_count: int
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    digest_text: str
    last_seen_at_prev: Optional[datetime] = None
    last_seen_at_new: Optional[datetime] = None
    engine: Optional[str] = None

# ----------------------------
# Route
# ----------------------------
@router.get("/feed", response_model=FeedDigest)
def get_feed(
    loop_id: UUID = Query(...),
    for_profile_id: UUID = Query(...),
    since: Optional[str] = Query(None),
    preview: bool = Query(False),
    last_seen_hours: int = Query(48, ge=1, le=24 * 30),  # default rolling window: 48h
    max_messages: int = Query(50, ge=1, le=200),        # cap number of inputs to summariser
    include_self: bool = Query(False),
):
    base, key = get_env()

    # 1) Loop and requester
    loop_row = supa_single(base, key, "loops", {"select": "id,name,created_at", "id": f"eq.{loop_id}"})
    loop_name = (loop_row or {}).get("name") or str(loop_id)
    requester = supa_single(base, key, "profiles", {"select": "id,handle", "id": f"eq.{for_profile_id}"})
    requester_handle = (requester or {}).get("handle") or str(for_profile_id)

    # 2) Determine lower bound (pointer or rolling window)
    read_state = supa_single(
        base, key, "loop_read_state",
        {"select": "loop_id,profile_id,last_seen_at", "loop_id": f"eq.{loop_id}", "profile_id": f"eq.{for_profile_id}"}
    )
    if since:
        last_seen_at_prev = ensure_dt(since)
    else:
        if not read_state or preview:
            last_seen_at_prev = datetime.now(tz=UTC) - timedelta(hours=last_seen_hours)
        else:
            last_seen_at_prev = ensure_dt(read_state.get("last_seen_at")) if read_state else datetime.fromtimestamp(0, tz=UTC)

    # 3) Fetch messages newer than window start, from this loop
    params: Dict[str, str] = {
        "select": "id,created_at,content_ciphertext,created_by,threads!inner(id,loop_id)",
        "created_at": f"gt.{last_seen_at_prev.isoformat()}",
        "order": "created_at.asc",
        "threads.loop_id": f"eq.{loop_id}",
    }
    if not include_self:
        params["created_by"] = f"neq.{for_profile_id}"

    rows = supa_select(base, key, "messages", params)

    # Optional recency cap (e.g., last 7 days) and hard cap
    cutoff = datetime.now(tz=UTC) - timedelta(days=7)
    rows = [r for r in rows if ensure_dt(r.get("created_at")) >= cutoff]
    if rows:
        rows = rows[-max_messages:]

    if not rows:
        return FeedDigest(
            loop_id=loop_id,
            for_profile_id=for_profile_id,
            items_count=0,
            digest_text="No new updates.",
            last_seen_at_prev=last_seen_at_prev,
            last_seen_at_new=last_seen_at_prev,
            engine="fallback",
        )

    def decode(c: Optional[str]) -> str:
        if not c:
            return ""
        return c[7:].strip() if c.startswith("cipher:") else c

    texts = [decode(r.get("content_ciphertext")) for r in rows if r.get("content_ciphertext")]
    texts = _clean_messages_for_summary(texts)

    created_ats = [ensure_dt(r.get("created_at")) for r in rows]
    window_start, window_end = min(created_ats), max(created_ats)

    digest_text, engine = summarise_messages(loop_name, requester_handle, texts)

    # 4) Advance pointer if not preview
    last_seen_at_new = last_seen_at_prev
    if not preview:
        supa_upsert(base, key, "loop_read_state", {
            "loop_id": str(loop_id),
            "profile_id": str(for_profile_id),
            "last_seen_at": window_end.isoformat(),
        })
        last_seen_at_new = window_end

    return FeedDigest(
        loop_id=loop_id,
        for_profile_id=for_profile_id,
        items_count=len(rows),
        window_start=window_start,
        window_end=window_end,
        digest_text=digest_text,
        last_seen_at_prev=last_seen_at_prev,
        last_seen_at_new=last_seen_at_new,
        engine=engine,
    )