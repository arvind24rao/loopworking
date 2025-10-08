# /Users/arvindrao/loop/loop-api/app/main.py
import os
import time
import base64
import json
from pathlib import Path
from typing import Optional, Tuple, List, Dict

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import ORJSONResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from loguru import logger

from .supa import supa, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
from .crypto import encrypt_plaintext
from .models import (
    InboxRequest, InboxResponse,
    PublishRequest, PublishResponse,
    MeInboxResponse, MeInboxItem,
    BotInboxResponse, BotInboxItem,
    BotReplyRequest, BotReplyResponse,
)
from .bot import process_queue  # LLM queue processor

# --- env/bootstrap ---
ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env.dev")
ENV = os.getenv("ENV", "dev")
BOT_PROFILE_ID = os.getenv("BOT_PROFILE_ID")
if not BOT_PROFILE_ID:
    raise RuntimeError("BOT_PROFILE_ID missing in environment")

app = FastAPI(title="loop-mvp-api", default_response_class=ORJSONResponse)

# --- CORS (add DELETE for dev clear endpoint) ---
ALLOWED_ORIGINS = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:8080",
    "http://localhost:8080",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
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
        return obj.get("ts"), obj.get("id")
    except Exception:
        return None

def _encode_cursor(ts: str, id_: str) -> str:
    raw = json.dumps({"ts": ts, "id": id_})
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

# ====================================================================================
# HUMAN -> BOT : POST /messages/inbox
# ====================================================================================
@app.post("/messages/inbox", status_code=201)
def inbox(payload: InboxRequest, x_user_id: str = Header(..., alias="X-User-Id")):
    if x_user_id == BOT_PROFILE_ID:
        raise HTTPException(status_code=400, detail="Bot cannot use /messages/inbox")

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
                "audience": "inbox_to_bot",
                "recipient_profile_id": None,
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

# ====================================================================================
# HUMAN PULL (bot -> human): GET /me/inbox
# ====================================================================================
@app.get("/me/inbox", response_model=MeInboxResponse)
def me_inbox(
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(None),
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    after = _decode_cursor(cursor)
    params = {
        "select": "id,thread_id,content_ciphertext,created_at",
        "audience": "eq.bot_to_user",
        "recipient_profile_id": f"eq.{x_user_id}",
        "order": "created_at.asc,id.asc",
        "limit": str(limit),
    }
    if after:
        ts, _ = after
        params["created_at"] = f"gt.{ts}"

    try:
        r = supa.client.get(f"{SUPABASE_URL}/rest/v1/messages", params=params)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        logger.error("me_inbox query failed: {}", e)
        raise HTTPException(status_code=500, detail="Inbox query failed")

    items = [
        MeInboxItem(
            message_id=row["id"],
            thread_id=row["thread_id"],
            content_plain=row["content_ciphertext"],
            created_at=row["created_at"],
        )
        for row in rows
    ]

    next_cursor = None
    if len(items) == limit:
        last = items[-1]
        next_cursor = _encode_cursor(last.created_at, last.message_id)

    return MeInboxResponse(items=items, next_cursor=next_cursor)

# ====================================================================================
# BOT PULL (human -> bot): GET /bot/inbox
# ====================================================================================
@app.get("/bot/inbox", response_model=BotInboxResponse)
def bot_inbox(
    thread_id: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(None),
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    if x_user_id != BOT_PROFILE_ID:
        raise HTTPException(status_code=403, detail="Bot only")

    after = _decode_cursor(cursor)
    params = {
        "select": "id,thread_id,created_by,content_ciphertext,created_at",
        "audience": "eq.inbox_to_bot",
        "order": "created_at.asc,id.asc",
        "limit": str(limit),
        "bot_processed_at": "is.null",
    }
    if thread_id:
        params["thread_id"] = f"eq.{thread_id}"
    if after:
        ts, _ = after
        params["created_at"] = f"gt.{ts}"

    try:
        r = supa.client.get(f"{SUPABASE_URL}/rest/v1/messages", params=params)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        logger.error("bot_inbox query failed: {}", e)
        raise HTTPException(status_code=500, detail="Bot inbox query failed")

    items = [
        BotInboxItem(
            message_id=row["id"],
            thread_id=row["thread_id"],
            created_by=row["created_by"],
            content_plain=row["content_ciphertext"],
            created_at=row["created_at"],
        )
        for row in rows
    ]

    next_cursor = None
    if len(items) == limit:
        last = items[-1]
        next_cursor = _encode_cursor(last.created_at, last.message_id)

    return BotInboxResponse(items=items, next_cursor=next_cursor)

# ====================================================================================
# BOT REPLY (manual): POST /bot/reply
# ====================================================================================
@app.post("/bot/reply", response_model=BotReplyResponse)
def bot_reply(req: BotReplyRequest, x_user_id: str = Header(..., alias="X-User-Id")):
    if x_user_id != BOT_PROFILE_ID:
        raise HTTPException(status_code=403, detail="Bot only")

    loop_id = _get_thread_loop_id(req.thread_id)
    bot_member_id = supa.rpc("member_id_for", {"u": os.getenv("BOT_PROFILE_ID"), "l": loop_id})

    content_ciphertext, dek_wrapped, nonce, aead_tag = encrypt_plaintext(req.content_plain)

    try:
        rec = supa.insert(
            "messages",
            {
                "thread_id": req.thread_id,
                "created_by": os.getenv("BOT_PROFILE_ID"),
                "author_member_id": bot_member_id,
                "role": "user",
                "channel": "inbox",
                "visibility": "private",
                "audience": "bot_to_user",
                "recipient_profile_id": req.recipient_profile_id,
                "content_ciphertext": content_ciphertext,
                "dek_wrapped": None,
                "nonce": None,
                "aead_tag": None,
                "lang": "en",
            },
        )
        row_id = rec["id"]
        created_at = rec.get("created_at") or supa.select_one("messages", {"id": row_id}, select="created_at")["created_at"]
    except Exception as e:
        logger.error("bot_reply insert failed: {}", e)
        raise HTTPException(status_code=500, detail="Bot reply failed")

    return BotReplyResponse(
        message_id=row_id,
        thread_id=req.thread_id,
        recipient_profile_id=req.recipient_profile_id,
        created_at=created_at,
        ok=True,
    )

# ====================================================================================
# BOT PROCESS (LLM): POST /bot/process
# ====================================================================================
@app.post("/bot/process")
def bot_process(
    thread_id: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    dry_run: bool = Query(False),
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    if x_user_id != BOT_PROFILE_ID:
        raise HTTPException(status_code=403, detail="Bot only")
    try:
        summary = process_queue(thread_id=thread_id, limit=limit, dry_run=dry_run)
        return {"ok": True, **summary}
    except Exception as e:
        logger.error("bot_process failed: {}", e)
        raise HTTPException(status_code=500, detail="Process failed")

# ====================================================================================
# HUMAN-TRIGGERED PROCESS (Bot stays the author): POST /me/process
# ====================================================================================
@app.post("/me/process")
def me_process(
    thread_id: str = Query(..., description="Thread to process (required)"),
    limit: int = Query(20, ge=1, le=100),
    x_user_id: str = Header(..., alias="X-User-Id"),
):
    # Humans only (don’t let the bot call this)
    if x_user_id == BOT_PROFILE_ID:
        raise HTTPException(status_code=400, detail="Bot should call /bot/process instead")

    # Guard: the caller must be a member of this thread’s loop
    loop_id = _get_thread_loop_id(thread_id)
    _ = _member_id_for(x_user_id, loop_id)  # raises if not a member

    try:
        summary = process_queue(thread_id=thread_id, limit=limit, dry_run=False)
        return {"ok": True, **summary}
    except Exception as e:
        logger.error("me_process failed: {}", e)
        raise HTTPException(status_code=500, detail="Process failed")

# ====================================================================================
# DEV RESET: DELETE all messages in a thread (Bot-only)
# ====================================================================================
@app.delete("/__debug/clear")
def debug_clear(thread_id: str, x_user_id: str = Header(..., alias="X-User-Id")):
    if x_user_id != BOT_PROFILE_ID:
        raise HTTPException(status_code=403, detail="Bot only")
    try:
        params = {"thread_id": f"eq.{thread_id}"}
        r = supa.client.delete(f"{SUPABASE_URL}/rest/v1/messages", params=params)
        r.raise_for_status()
    except Exception as e:
        logger.error("debug_clear failed: {}", e)
        raise HTTPException(status_code=500, detail="Clear failed")
    return {"ok": True}

# -------- DEBUG: Supabase info --------
@app.get("/__debug/supa")
def debug_supa():
    info = {
        "SUPABASE_URL": SUPABASE_URL,
        "service_key_len": len(SUPABASE_SERVICE_ROLE_KEY or ""),
        "bot_profile_id": os.getenv("BOT_PROFILE_ID"),
    }
    try:
        test = supa.rpc("member_id_for", {"u": os.getenv("BOT_PROFILE_ID"), "l": "e94bd651-5bac-4e39-8537-fe8c788c1475"})
        info["rpc_result"] = test
    except Exception as e:
        info["rpc_error"] = str(e)
    return info

# ====================================================================================
# WEBSITE FACADES for Netlify proxy:
# - POST /api/send_message    (maps to /messages/inbox)
# - GET  /api/get_messages    (combines user->bot and bot->user for a thread/user)
# ====================================================================================

class ApiSendMessageBody(BaseModel):
    thread_id: str
    user_id: str
    content: str

@app.post("/api/send_message")
def api_send_message(body: ApiSendMessageBody):
    """
    Facade used by the website.
    Translates to the internal /messages/inbox logic using body.user_id as X-User-Id.
    """
    if body.user_id == BOT_PROFILE_ID:
        raise HTTPException(status_code=400, detail="Bot cannot send via /api/send_message")

    # Membership & loop checks
    loop_id = _get_thread_loop_id(body.thread_id)
    author_member_id = _member_id_for(body.user_id, loop_id)

    # Encrypt (shim) and insert as inbox_to_bot
    content_ciphertext, dek_wrapped, nonce, aead_tag = encrypt_plaintext(body.content)
    try:
        rec = supa.insert(
            "messages",
            {
                "thread_id": body.thread_id,
                "created_by": body.user_id,
                "author_member_id": author_member_id,
                "role": "user",
                "channel": "inbox",
                "visibility": "private",
                "audience": "inbox_to_bot",
                "recipient_profile_id": None,
                "content_ciphertext": content_ciphertext,
                "dek_wrapped": None,
                "nonce": None,
                "aead_tag": None,
                "lang": "en",
            },
        )
        message_id = rec["id"]
        created_at = rec.get("created_at") or supa.select_one("messages", {"id": message_id}, select="created_at")["created_at"]
    except Exception as e:
        logger.error("api_send_message insert failed: {}", e)
        raise HTTPException(status_code=500, detail="Send failed")

    return {
        "ok": True,
        "message_id": message_id,
        "thread_id": body.thread_id,
        "user_id": body.user_id,
        "timestamp": created_at,
    }

@app.get("/api/get_messages")
def api_get_messages(thread_id: str = Query(...), user_id: str = Query(...)) -> Dict[str, List[Dict]]:
    """
    Facade used by the website.
    Returns a simple unified timeline for this user & thread:
      - human -> bot messages created by `user_id` (audience=inbox_to_bot)
      - bot -> human messages to `user_id` (audience=bot_to_user, recipient_profile_id=user_id)
    Sorted ascending by created_at.
    """
    try:
        # 1) Human (user_id) -> Bot
        params1 = {
            "select": "id,thread_id,created_by,content_ciphertext,created_at",
            "thread_id": f"eq.{thread_id}",
            "created_by": f"eq.{user_id}",
            "audience": "eq.inbox_to_bot",
            "order": "created_at.asc,id.asc",
            "limit": "500",
        }
        r1 = supa.client.get(f"{SUPABASE_URL}/rest/v1/messages", params=params1)
        r1.raise_for_status()
        rows_user = r1.json()

        # 2) Bot -> Human (recipient = user_id)
        params2 = {
            "select": "id,thread_id,created_by,recipient_profile_id,content_ciphertext,created_at",
            "thread_id": f"eq.{thread_id}",
            "recipient_profile_id": f"eq.{user_id}",
            "audience": "eq.bot_to_user",
            "order": "created_at.asc,id.asc",
            "limit": "500",
        }
        r2 = supa.client.get(f"{SUPABASE_URL}/rest/v1/messages", params=params2)
        r2.raise_for_status()
        rows_bot = r2.json()

        # Merge + map to website shape
        combined = []
        for row in rows_user:
            combined.append({
                "id": row["id"],
                "thread_id": row["thread_id"],
                "user_id": row["created_by"],                 # sender is the human
                "content": row["content_ciphertext"],         # shim passes through
                "timestamp": row["created_at"],
            })
        for row in rows_bot:
            combined.append({
                "id": row["id"],
                "thread_id": row["thread_id"],
                "user_id": row["created_by"],                 # sender is the bot (BOT_PROFILE_ID)
                "content": row["content_ciphertext"],
                "timestamp": row["created_at"],
            })

        # Sort by timestamp, then id
        combined.sort(key=lambda m: (m["timestamp"], m["id"]))

        return {"ok": True, "messages": combined}
    except Exception as e:
        logger.error("api_get_messages failed: {}", e)
        raise HTTPException(status_code=500, detail="Fetch failed")

from app.routes.feed import router as feed_router
app.include_router(feed_router)    