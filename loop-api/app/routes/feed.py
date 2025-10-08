# app/routes/feed.py
from __future__ import annotations
import os
import datetime as dt
from typing import List, Optional, Any
import requests
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime, timedelta, timezone
import re

router = APIRouter(prefix="/api", tags=["feed"])
UTC = dt.timezone.utc

def ensure_dt(value: Any) -> dt.datetime:
    if value is None:
        return dt.datetime.fromtimestamp(0, tz=UTC)
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        s = value.strip()
        if s.endswith("Z"): s = s[:-1] + "+00:00"
        d = dt.datetime.fromisoformat(s.replace(" ", "T"))
        if d.tzinfo is None: d = d.replace(tzinfo=UTC)
        return d
    raise ValueError(f"Unsupported datetime value type: {type(value)}")

class FeedDigest(BaseModel):
    loop_id: UUID
    for_profile_id: UUID
    items_count: int
    window_start: Optional[dt.datetime] = None
    window_end: Optional[dt.datetime] = None
    digest_text: str = Field(default="")
    last_seen_at_prev: Optional[dt.datetime] = None
    last_seen_at_new: Optional[dt.datetime] = None

def get_env():
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not supabase_key:
        raise HTTPException(status_code=500, detail="Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return supabase_url, supabase_key

def _supa_headers(key: str, prefer: Optional[str] = None) -> dict:
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    if prefer: h["Prefer"] = prefer
    return h

def supa_select(base: str, key: str, path: str, params: dict) -> List[dict]:
    r = requests.get(f"{base}/rest/v1/{path}", params=params, headers=_supa_headers(key))
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Supabase select failed: {r.text}")
    return r.json()

def supa_single(base: str, key: str, path: str, params: dict) -> Optional[dict]:
    rows = supa_select(base, key, path, params)
    return rows[0] if rows else None

def supa_upsert(base: str, key: str, path: str, json_body: dict) -> None:
    r = requests.post(f"{base}/rest/v1/{path}", json=[json_body],
                      headers=_supa_headers(key, prefer="resolution=merge-duplicates"))
    if r.status_code not in (200, 201, 204):
        raise HTTPException(status_code=502, detail=f"Supabase upsert failed: {r.text}")

def supa_patch(base: str, key: str, path: str, params: dict, json_body: dict) -> None:
    r = requests.patch(f"{base}/rest/v1/{path}", params=params, json=json_body, headers=_supa_headers(key))
    if r.status_code not in (200, 204):
        raise HTTPException(status_code=502, detail=f"Supabase patch failed: {r.text}")

import re

def _clean_messages_for_summary(texts: list[str], per_msg_cap: int = 240) -> list[str]:
    cleaned = []
    seen = set()
    url_re = re.compile(r'https?://\S+')
    ws_re = re.compile(r'\s+')
    for t in texts:
        if not t:
            continue
        # strip urls
        t = url_re.sub('', t)
        # collapse whitespace and trim
        t = ws_re.sub(' ', t).strip()
        # cap each message length so one long paste can't dominate
        if len(t) > per_msg_cap:
            t = t[:per_msg_cap].rstrip() + '…'
        if len(t) < 2:
            continue
        # de-dup near duplicates
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(t)
    return cleaned[:200]  # global cap for safety

def summarise_messages(loop_name: str, requester_handle: str, messages: List[str]) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        joined = " ".join(messages)
        return (joined or "No new updates.")[:500]
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    from openai import OpenAI
    client = OpenAI(api_key=api_key)
    N, M = 30, 10
    msgs = messages if len(messages) <= (N + M) else (messages[:N] + messages[-M:])
    system = ("You are the loop bot for a private group. "
              "Write a concise shared update in natural, neutral EN-GB, "
              "3 sentences max. No quotes; third-person; no speculation.")
    user = (f"Loop name: {loop_name}\n"
            f"Requester (do not include their own posts): @{requester_handle}\n"
            f"Posts to summarise:\n- " + "\n- ".join(msgs))
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=0.2, max_tokens=220,
    )
    return resp.choices[0].message.content.strip()

@router.get("/feed", response_model=FeedDigest)
def get_feed(
    loop_id: UUID = Query(...),
    for_profile_id: UUID = Query(...),
    since: Optional[str] = Query(None),
    preview: bool = Query(False),
):
    # Read env safely at request-time
    SUPABASE_URL, SUPABASE_KEY = get_env()

    # 1) Loop + requester
    loop_row = supa_single(SUPABASE_URL, SUPABASE_KEY, "loops",
                           {"select":"id,name,created_at","id":f"eq.{loop_id}"})
    loop_name = (loop_row or {}).get("name") or str(loop_id)
    requester = supa_single(SUPABASE_URL, SUPABASE_KEY, "profiles",
                            {"select":"id,handle","id":f"eq.{for_profile_id}"})
    requester_handle = (requester or {}).get("handle") or str(for_profile_id)

    # 2) read_state
    read_state = supa_single(SUPABASE_URL, SUPABASE_KEY, "loop_read_state",
                             {"select":"loop_id,profile_id,last_seen_at",
                              "loop_id":f"eq.{loop_id}", "profile_id":f"eq.{for_profile_id}"})
    last_seen_candidate = since if since is not None else (read_state or {}).get("last_seen_at")
    last_seen_at_prev = ensure_dt(last_seen_candidate)

    # 3) messages since pointer excluding requester, in this loop
    rows = supa_select(
        SUPABASE_URL, SUPABASE_KEY, "messages",
        {
            "select": "id,created_at,content_ciphertext,created_by,threads!inner(id,loop_id)",
            "created_at": f"gt.{last_seen_at_prev.isoformat()}",
            "order": "created_at.asc",
            "threads.loop_id": f"eq.{loop_id}",
            "created_by": f"neq.{for_profile_id}",
        },
    )
    if not rows:
        return FeedDigest(
            loop_id=loop_id, for_profile_id=for_profile_id, items_count=0,
            digest_text="No new updates.", last_seen_at_prev=last_seen_at_prev,
            last_seen_at_new=ensure_dt((read_state or {}).get("last_seen_at")) if read_state else last_seen_at_prev
        )

    def decode(c: Optional[str]) -> str:
        if not c: return ""
        return c[7:].strip() if c.startswith("cipher:") else c

    texts = [decode(r.get("content_ciphertext")) for r in rows if r.get("content_ciphertext")]
    texts = _clean_messages_for_summary(texts)
    created_ats = [ensure_dt(r.get("created_at")) for r in rows]
    window_start, window_end = min(created_ats), max(created_ats)

    digest_text = summarise_messages(loop_name, requester_handle, texts)

    last_seen_at_new = last_seen_at_prev
    if not preview:
        supa_upsert(SUPABASE_URL, SUPABASE_KEY, "loop_read_state", {
            "loop_id": str(loop_id), "profile_id": str(for_profile_id),
            "last_seen_at": last_seen_at_prev.isoformat(),
        })
        supa_patch(SUPABASE_URL, SUPABASE_KEY, "loop_read_state",
                   params={"loop_id": f"eq.{loop_id}", "profile_id": f"eq.{for_profile_id}"},
                   json_body={"last_seen_at": window_end.isoformat()})
        last_seen_at_new = window_end

    return FeedDigest(
        loop_id=loop_id, for_profile_id=for_profile_id, items_count=len(rows),
        window_start=window_start, window_end=window_end, digest_text=digest_text,
        last_seen_at_prev=last_seen_at_prev, last_seen_at_new=last_seen_at_new
    )

def summarise_messages(loop_name: str, requester_handle: str, messages: List[str]) -> str:
    # simple fallback summary
    def simple():
        joined = " ".join(messages)
        return (joined or "No new updates.")[:500]

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return simple()

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        N, M = 30, 10
        msgs = messages if len(messages) <= (N + M) else (messages[:N] + messages[-M:])
        system = ("You are the loop bot for a private group. "
                  "Write a concise shared update in natural, neutral EN-GB, "
                  "3 sentences max. No quotes; third-person; no speculation.")
        user = (f"Loop name: {loop_name}\n"
                f"Requester (do not include their own posts): @{requester_handle}\n"
                f"Posts to summarise:\n- " + "\n- ".join(msgs))
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.2, max_tokens=220,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        # any auth/network/model error → safe fallback
        return simple()

def get_feed(
    loop_id: UUID = Query(...),
    for_profile_id: UUID = Query(...),
    since: Optional[str] = Query(None),
    preview: bool = Query(False),
    last_seen_hours: int = Query(48, ge=1, le=24*30),  # default: 48h window
    max_messages: int = Query(50, ge=1, le=200),       # safety cap
):
    SUPABASE_URL, SUPABASE_KEY = get_env()

    # 1) Loop + requester
    loop_row = supa_single(SUPABASE_URL, SUPABASE_KEY, "loops",
                           {"select":"id,name,created_at","id":f"eq.{loop_id}"})
    loop_name = (loop_row or {}).get("name") or str(loop_id)
    requester = supa_single(SUPABASE_URL, SUPABASE_KEY, "profiles",
                            {"select":"id,handle","id":f"eq.{for_profile_id}"})
    requester_handle = (requester or {}).get("handle") or str(for_profile_id)

    # 2) read_state pointer
    read_state = supa_single(SUPABASE_URL, SUPABASE_KEY, "loop_read_state",
                             {"select":"loop_id,profile_id,last_seen_at",
                              "loop_id":f"eq.{loop_id}", "profile_id":f"eq.{for_profile_id}"})
    if since:
        last_seen_at_prev = ensure_dt(since)
    else:
        # If no saved pointer OR preview mode, use a rolling window
        if not read_state or preview:
            last_seen_at_prev = datetime.now(tz=UTC) - timedelta(hours=last_seen_hours)
        else:
            last_seen_at_prev = ensure_dt(read_state.get("last_seen_at")) if read_state else datetime.fromtimestamp(0, tz=UTC)

    # 3) Fetch messages newer than the window, in this loop, excluding requester, capped
    rows = supa_select(
        SUPABASE_URL, SUPABASE_KEY, "messages",
        {
            "select": "id,created_at,content_ciphertext,created_by,threads!inner(id,loop_id)",
            "created_at": f"gt.{last_seen_at_prev.isoformat()}",
            "order": "created_at.asc",
            "threads.loop_id": f"eq.{loop_id}",
            "created_by": f"neq.{for_profile_id}",
        },
    )
    if rows:
        rows = rows[-max_messages:]  # cap

    if not rows:
        return FeedDigest(
            loop_id=loop_id, for_profile_id=for_profile_id, items_count=0,
            digest_text="No new updates.", last_seen_at_prev=last_seen_at_prev,
            last_seen_at_new=last_seen_at_prev
        )

    def decode(c: Optional[str]) -> str:
        if not c: return ""
        return c[7:].strip() if c.startswith("cipher:") else c

    texts = [decode(r.get("content_ciphertext")) for r in rows if r.get("content_ciphertext")]
    created_ats = [ensure_dt(r.get("created_at")) for r in rows]
    window_start, window_end = min(created_ats), max(created_ats)

    digest_text = summarise_messages(loop_name, requester_handle, texts)

    last_seen_at_new = last_seen_at_prev
    if not preview:
        # upsert pointer forward to window_end
        supa_upsert(SUPABASE_URL, SUPABASE_KEY, "loop_read_state", {
            "loop_id": str(loop_id), "profile_id": str(for_profile_id),
            "last_seen_at": window_end.isoformat(),
        })
        last_seen_at_new = window_end

    return FeedDigest(
        loop_id=loop_id, for_profile_id=for_profile_id, items_count=len(rows),
        window_start=window_start, window_end=window_end, digest_text=digest_text,
        last_seen_at_prev=last_seen_at_prev, last_seen_at_new=last_seen_at_new
    )

