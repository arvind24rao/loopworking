# /Users/arvindrao/loop/loop-api/app/main.py
import os
import time
import base64
import json
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Path as FPath, Query
from fastapi.responses import ORJSONResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger

from .supa import supa, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
from .crypto import encrypt_plaintext
from .models import (
    InboxRequest, InboxResponse,
    PublishRequest, PublishResponse,
    FeedResponse, FeedItem,
)

# --- env/bootstrap ---
ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env.dev")
ENV = os.getenv("ENV", "dev")

app = FastAPI(title="loop-mvp-api", default_response_class=ORJSONResponse)

# --- CORS (demo origins only; add your domain later) ---
ALLOWED_ORIGINS = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:8080",  # harmless on same-origin, helpful if proxied
    "http://localhost:8080",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-User-Id"],
    allow_credentials=False,
)

# --- Static: /static + /demo ---
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR), html=False), name="static")

@app.get("/demo")
def demo_page():
    demo_file = STATIC_DIR / "demo.html"
    if not demo_file.exists():
        return JSONResponse({"ok": False, "message": "demo.html not found in app/static/"}, status_code=404)
    return FileResponse(str(demo_file), media_type="text/html")

# -------- Helpers --------
def _get_thread_loop_id(thread_id: str) -> str:
    th = supa.select_one("threads", {"id": thread_id}, select="id,loop_id")
    if not th or not th.get("loop_id"):
        raise HTTPException(status_code=404, detail="Thread not found")
    return th["loop_id"]

def _member_id_for(profile_id: str, loop_id: str) -> str:
    try:
        mid = supa.rpc("member_id_for", {"u": profile_id, "l": loop_id})
        if not mid:
            raise HTTPException(status_code=403, detail="User is not a member of this loop")
        return mid
    except HTTPException:
        raise
    except Exception as e:
        logger.error("RPC member_id_for failed: {}", e)
        raise HTTPException(status_code=500, detail="Membership check failed")

def _decode_cursor(cursor: Optional[str]) -> Optional[Tuple[str, str]]:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        obj = json.loads(raw)
        return obj.get("published_at"), obj.get("message_id")
    except Exception:
        return None

def _encode_cursor(published_at: str, message_id: str) -> str:
    raw = json.dumps({"published_at": published_at, "message_id": message_id})
    return base64.urlsafe_b64encode(raw.encode()).decode()

# -------- Health --------
@app.get("/health")
def health():
    info = {"ok": True, "env": ENV, "ts": int(time.time()), "rest": {"supabase_url_present": bool(SUPABASE_URL)}}
    try:
        _ = supa.rpc(
            "member_id_for",
            {"u": "00000000-0000-0000-0000-000000000000", "l": "00000000-0000-0000-0000-000000000000"},
        )
        info["rest"]["rpc_reachable"] = True
    except Exception as e:
        info["rest"]["rpc_reachable"] = False
        info["rest"]["error"] = str(e)[:200]
    return info

# -------- Inbox (existing) --------
@app.post("/messages/inbox", status_code=201)
def inbox(payload: InboxRequest, x_user_id: str = Header(..., alias="X-User-Id")):
    loop_id = _get_thread_loop_id(payload.thread_id)
    author_member_id = _member_id_for(x_user_id, loop_id)

    content_ciphertext, dek_wrapped, nonce, aead_tag = encrypt_plaintext(payload.content_plain)
    try:
        rec = supa.insert(
            "messages",
            {
                "thread_id": payload.thread_id,
                "created_by": x_user_id,
                "author_member_id": author_member_id,
                "role": "user",
                "channel": "inbox",
                "visibility": "private",
                "content_ciphertext": content_ciphertext,
                "dek_wrapped": None,
                "nonce": None,
                "aead_tag": None,
                "lang": "en",
            },
        )
        new_id = rec["id"]
    except Exception as e:
        logger.error("Insert messages failed: {}", e)
        raise HTTPException(status_code=500, detail="Insert failed")

    return InboxResponse(
        message_id=str(new_id),
        thread_id=payload.thread_id,
        role="user",
        channel="inbox",
        visibility="private",
        ok=True,
    )

# -------- Publish (MVP) --------
@app.post("/messages/publish", response_model=PublishResponse)
def publish(req: PublishRequest, x_user_id: str = Header(..., alias="X-User-Id")):
    if req.message_id:
        # Load that message
        rec = supa.select_one("messages", {"id": req.message_id}, select="id,thread_id,visibility,channel,created_at")
        if not rec:
            raise HTTPException(status_code=404, detail="Message not found")
        target = rec
        message_id = rec["id"]
        thread_id = rec["thread_id"]
    elif req.thread_id and req.latest:
        # Load latest inbox message for this thread
        params = {
            "select": "id,thread_id,visibility,channel,created_at",
            "thread_id": f"eq.{req.thread_id}",
            "channel": "eq.inbox",
            "order": "created_at.desc",
            "limit": "1",
        }
        try:
            r = supa.client.get(f"{SUPABASE_URL}/rest/v1/messages", params=params)
            r.raise_for_status()
            data = r.json()
            if not data:
                raise HTTPException(status_code=404, detail="No inbox messages to publish")
            target = data[0]
            message_id = target["id"]
            thread_id = target["thread_id"]
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Fetch latest inbox failed: {}", e)
            raise HTTPException(status_code=500, detail="Lookup failed")
    else:
        raise HTTPException(status_code=422, detail="Provide {message_id} or {thread_id, latest:true}")

    # Membership check
    loop_id = _get_thread_loop_id(thread_id)
    _ = _member_id_for(x_user_id, loop_id)

    # Idempotent: already shared?
    if target.get("visibility") == "shared" and target.get("channel") == "loop":
        published_at = target.get("created_at")
        return PublishResponse(
            publish_id=str(message_id),
            message_id=str(message_id),
            thread_id=str(thread_id),
            visibility="shared",
            channel="loop",
            published_at=published_at,
            ok=True,
        )

    # Update visibility/channel; fetch representation even if PostgREST returns minimal
    try:
        params = {"id": f"eq.{message_id}"}
        payload = {"visibility": "shared", "channel": "loop"}
        r = supa.client.patch(f"{SUPABASE_URL}/rest/v1/messages", params=params, json=payload)
        r.raise_for_status()
        data = r.json()
        if not data:
            # Fallback: re-select the row to get timestamps
            row = supa.select_one("messages", {"id": message_id}, select="id,thread_id,visibility,channel,created_at")
        else:
            row = data[0] if isinstance(data, list) else data
    except Exception as e:
        logger.error("Publish update failed: {}", e)
        raise HTTPException(status_code=500, detail="Publish failed")

    published_at = row.get("created_at")
    return PublishResponse(
        publish_id=str(message_id),
        message_id=str(message_id),
        thread_id=str(thread_id),
        visibility="shared",
        channel="loop",
        published_at=published_at,
        ok=True,
    )

# -------- Feed (MVP) --------
@app.get("/threads/{thread_id}/feed", response_model=FeedResponse)
def feed(
    thread_id: str = FPath(..., description="Thread UUID"),
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(None),
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    loop_id = _get_thread_loop_id(thread_id)
    _ = _member_id_for(x_user_id, loop_id)

    params = {
        "select": "id,thread_id,content_ciphertext,created_at",
        "thread_id": f"eq.{thread_id}",
        "visibility": "eq.shared",
        "channel": "eq.loop",
        "order": "created_at.asc,id.asc",
        "limit": str(limit),
    }

    after = _decode_cursor(cursor)
    if after:
        published_at, _ = after
        params["created_at"] = f"gt.{published_at}"

    try:
        r = supa.client.get(f"{SUPABASE_URL}/rest/v1/messages", params=params)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.error("Feed query failed: {}", e)
        raise HTTPException(status_code=500, detail="Feed query failed")

    items = [
        FeedItem(
            message_id=row["id"],
            thread_id=row["thread_id"],
            content_plain=row["content_ciphertext"],  # plaintext in MVP
            published_at=row["created_at"],
        )
        for row in data
    ]

    next_cursor = None
    if len(items) == limit:
        last = items[-1]
        next_cursor = _encode_cursor(last.published_at, last.message_id)

    return FeedResponse(items=items, next_cursor=next_cursor)

# -------- DEBUG ROUTES (keep) --------
@app.get("/__debug/supa")
def debug_supa():
    info = {"SUPABASE_URL": SUPABASE_URL, "service_key_len": len(SUPABASE_SERVICE_ROLE_KEY or "")}
    try:
        test = supa.rpc(
            "member_id_for",
            {"u": "b8d99c3c-0d3a-4773-a324-a6bc60dee64e", "l": "e94bd651-5bac-4e39-8537-fe8c788c1475"},
        )
        info["rpc_result"] = test
    except Exception as e:
        info["rpc_error"] = str(e)
    return info

@app.get("/__debug/inbox-params")
def debug_inbox_params(thread_id: str, x_user_id: str):
    info = {"thread_id": thread_id, "x_user_id": x_user_id}
    tr = supa.select_one("threads", {"id": thread_id}, select="id,loop_id")
    info["thread_row"] = tr
    info["loop_id"] = tr.get("loop_id") if tr else None
    rpc_args = {"u": x_user_id, "l": info["loop_id"]}
    info["rpc_args"] = rpc_args
    try:
        info["rpc_result"] = supa.rpc("member_id_for", rpc_args)
    except Exception as e:
        info["rpc_error"] = str(e)
    return info