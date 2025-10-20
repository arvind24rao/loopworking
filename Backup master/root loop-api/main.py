import os
import time
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import ORJSONResponse
from loguru import logger

# keep the old ping import for later, but we won't use it right now
from app.db import ping_db
from app.models import InboxRequest, InboxResponse, MessagesResponse, MessageOut, PublishRequest, PublishResponse
from app.crypto import encrypt_plaintext
from app.supa import supa  # NEW: REST client

# Load env from project root (.env.dev lives in /Users/arvindrao/loop)
ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env.dev")

ENV = os.getenv("ENV", "dev")
PORT = int(os.getenv("PORT", "8080"))
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
AUTH_MODE = os.getenv("AUTH_MODE", "service")
LOOP_BOT_USER_ID = os.getenv("LOOP_BOT_USER_ID")
DATABASE_URL = os.getenv("DATABASE_URL")

app = FastAPI(title="loop-mvp-api", default_response_class=ORJSONResponse)

def _summarize_with_llm(txt: str) -> str:
    """Summarize plaintext with OpenAI; returns 'cipher: <summary>'. Falls back by raising if key/model missing or API fails."""
    import os
    # Strip MVP 'cipher:' prefix
    t = txt or ""
    if t.lower().startswith("cipher:"):
        t = t[len("cipher:"):].lstrip()

    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")

    model = os.getenv("AI_SUMMARY_MODEL", "gpt-4o-mini")
    try:
        max_tokens = int(os.getenv("AI_SUMMARY_MAX_TOKENS", "80"))
    except Exception:
        max_tokens = 80

    # Lazy import to avoid import issues on startup
    from openai import OpenAI
    client = OpenAI(api_key=key)

    system = (
        "You summarize a single short user message into ONE concise, neutral sentence. "
        "No emojis. No extra context. 25 words max. Keep names as-is."
    )
    # Safety clamp: keep prompt small
    t = t.strip()
    if len(t) > 500:
        t = t[:500]

    resp = client.chat.completions.create(
        model=model,
        temperature=0.2,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": t},
        ],
    )
    out = (resp.choices[0].message.content or "").strip()
    # Final clamp
    if len(out) > 240:
        out = out[:237] + "..."
    return "cipher: " + out


def _supabase_rest_ping(timeout_sec: float = 0.8) -> bool:
    """Lightweight read via Supabase REST to validate connectivity/auth."""
    try:
        # Lazily import to avoid circulars
        from app.supa import supa
        r = supa.client.get(
            f"{supa.base}/rest/v1/threads",
            params={"select": "id", "limit": "1"},
            timeout=timeout_sec,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        try:
            from loguru import logger
            logger.warning("Health REST ping failed: {}", e)
        except Exception:
            pass
        return False


def _loguru_config():
    from loguru import logger
    import sys, os

    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
    logger.remove()

    SENSITIVE_KEYS = {
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_ANON_KEY",
        "OPENAI_API_KEY",
        "DATABASE_URL",
    }

    def redact(record):
        for k in SENSITIVE_KEYS:
            v = os.getenv(k)
            if v:
                record["message"] = record["message"].replace(v, "***")
        return True

    logger.add(
        sys.stdout,
        level=LOG_LEVEL,
        backtrace=False,
        diagnose=False,
        filter=redact,
        format="<{time:YYYY-MM-DD HH:mm:ss.SSS}> | {level:<7} | req={extra[req_id] if 'req_id' in extra else '-'} | {message}",
    )
    return logger

logger = _loguru_config()

@app.middleware("http")
async def log_with_request_id(request, call_next):
    import uuid
    req_id = getattr(request.state, "req_id", None) or str(uuid.uuid4())
    request.state.req_id = req_id
    from loguru import logger as _logger
    with _logger.contextualize(req_id=req_id):
        response = await call_next(request)
    return response

@app.middleware('http')
async def add_request_id_and_timing(request, call_next):
    req_id = str(uuid.uuid4())
    request.state.req_id = req_id
    t0 = time.time()
    response = await call_next(request)
    response.headers['X-Request-Id'] = req_id
    response.headers['X-Process-Time-ms'] = str(int((time.time() - t0) * 1000))
    return response


@app.get("/health")
def health():
    import time, os
    t0 = time.time()

    # REST path reflects how the app really talks to Supabase in production
    rest_ok = _supabase_rest_ping(timeout_sec=0.8)
    latency_ms = int((time.time() - t0) * 1000)
    db_ok = rest_ok

    env = "dev"
    auth_mode = os.getenv("AUTH_MODE", "service")
    has_service_key = bool(os.getenv("SUPABASE_SERVICE_ROLE_KEY"))
    has_supabase_url = bool(os.getenv("SUPABASE_URL"))
    has_database_url = bool(os.getenv("DATABASE_URL"))
    bot_user_id_set = bool(os.getenv("LOOP_BOT_USER_ID"))

    status = "ok" if rest_ok else "degraded"

    return {
        "status": status,
        "env": env,
        "auth_mode": auth_mode,
        "has_service_key": has_service_key,
        "has_supabase_url": has_supabase_url,
        "has_database_url": has_database_url,
        "bot_user_id_set": bot_user_id_set,
        "rest_ok": rest_ok,
        "db_ok": db_ok,
        "latency_ms": latency_ms,
    }

@app.post("/messages/inbox", response_model=InboxResponse, status_code=201)
def inbox_message(payload: InboxRequest, x_user_id: Optional[str] = Header(default=None, alias="X-User-Id")):
    """
    User -> AI private DM via Supabase REST.
    Steps:
      1) threads: get loop_id
      2) rpc: member_id_for(u, l) -> author_member_id
      3) messages: insert (role='user', channel='inbox', visibility='private')
    """
    if not x_user_id:
        raise HTTPException(status_code=400, detail="X-User-Id header is required")

    # 1) find loop_id for thread
    th = supa.select_one("threads", {"id": payload.thread_id}, select="loop_id")
    if not th or not th.get("loop_id"):
        raise HTTPException(status_code=404, detail="Thread not found")
    loop_id = th["loop_id"]

    # 2) author membership via RPC (EXPECTED ARG NAMES: u, l)
    try:
        author_member_id = supa.rpc("member_id_for", {"u": x_user_id, "l": loop_id})
        if not author_member_id:
            raise HTTPException(status_code=403, detail="User is not a member of this loop")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("RPC member_id_for failed (u/l) with x_user_id=%s loop_id=%s : %s", x_user_id, loop_id, e)
        raise HTTPException(status_code=500, detail="Membership check failed")

    # 3) insert the inbox message (crypto placeholders are NULLs for MVP)
    content_ciphertext, dek_wrapped, nonce, aead_tag = encrypt_plaintext(payload.content_plain)
    try:
        rec = supa.insert("messages", {
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
            "lang": "en"
        })
        new_id = rec["id"]
    except Exception as e:
        logger.error("Insert messages failed: %s", e)
        raise HTTPException(status_code=500, detail="Insert failed")

    return InboxResponse(
        message_id=str(new_id),
        thread_id=payload.thread_id,
        role="user",
        channel="inbox",
        visibility="private",
        ok=True,
        note=None,
    )


# --- DEBUG: mirror inbox param resolution and RPC call ---

    # 1) fetch the thread row like the handler does
    try:
        tr = supa.select_one("threads", {"id": thread_id}, select="id,loop_id")
        info["thread_row"] = tr
        loop_id = tr["loop_id"] if tr else None
        info["loop_id"] = loop_id
    except Exception as e:
        info["thread_error"] = str(e)
        return info

    # 2) build the exact RPC args
    rpc_args = {"u": x_user_id, "l": loop_id}
    info["rpc_args"] = rpc_args

    # 3) call the RPC with those args
    try:
        info["rpc_result"] = supa.rpc("member_id_for", rpc_args)
    except Exception as e:
        info["rpc_error"] = str(e)

    return info

@app.get("/threads/{thread_id}/messages", response_model=MessagesResponse)
def list_thread_messages(
    thread_id: str,
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    limit: int = 50,
    order: str = "created_at.asc"
):
    """
    Fetch messages for a thread. Caller must be a member of the loop.
    limit: default 50
    order: "created_at.asc" (oldest first) or "created_at.desc" (newest first)
    """
    if not x_user_id:
        raise HTTPException(status_code=400, detail="X-User-Id header is required")

    # 1) Find loop_id for this thread
    th = supa.select_one("threads", {"id": thread_id}, select="id,loop_id")
    if not th or not th.get("loop_id"):
        raise HTTPException(status_code=404, detail="Thread not found")
    loop_id = th["loop_id"]

    # 2) Verify membership
    try:
        member_uuid = supa.rpc("member_id_for", {"u": x_user_id, "l": loop_id})
        if not member_uuid:
            raise HTTPException(status_code=403, detail="Not a member of this loop")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Membership RPC failed: {}", e)
        raise HTTPException(status_code=500, detail="Membership check failed")

    # 3) Fetch messages
    try:
        rows = supa.select_many(
            "messages",
            filters={"thread_id": thread_id},
            select="id,thread_id,author_member_id,role,channel,visibility,lang,content_ciphertext,created_at",
            order=order,
            limit=limit
        )
    except Exception as e:
        logger.error("Select messages failed: {}", e)
        raise HTTPException(status_code=500, detail="Fetch failed")

    # 4) Build response
    items = [MessageOut(**r) for r in rows]
    return MessagesResponse(thread_id=thread_id, count=len(items), items=items)

def _summarize_plain(txt: str) -> str:
    # MVP summarizer: strip leading 'cipher:' and trim
    t = txt or ""
    if t.lower().startswith("cipher:"):
        t = t[len("cipher:"):].lstrip()
    # Naive shorten
    if len(t) > 160:
        t = t[:157] + "..."
    return f"cipher: {t}"


@app.post("/messages/publish", response_model=PublishResponse, status_code=201)
def publish_message(payload: PublishRequest, x_user_id: Optional[str] = Header(default=None, alias="X-User-Id")):
    if not x_user_id:
        raise HTTPException(status_code=400, detail="X-User-Id header is required")

    # 1) Load the inbox message
    msg = supa.select_one("messages", {"id": payload.inbox_message_id},
                          select="id,thread_id,author_member_id,role,channel,visibility,content_ciphertext")
    if not msg:
        raise HTTPException(status_code=404, detail="Inbox message not found")
    if msg.get("channel") != "inbox" or msg.get("visibility") != "private":
        raise HTTPException(status_code=400, detail="Not an inbox/private message")

    thread_id = msg["thread_id"]

    # 2) Find loop_id for this thread
    th = supa.select_one("threads", {"id": thread_id}, select="id,loop_id")
    if not th or not th.get("loop_id"):
        raise HTTPException(status_code=404, detail="Thread not found")
    loop_id = th["loop_id"]

    # 3) Verify caller is a member of this loop
    try:
        caller_member = supa.rpc("member_id_for", {"u": x_user_id, "l": loop_id})
        if not caller_member:
            raise HTTPException(status_code=403, detail="Not a member of this loop")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Membership RPC (caller) failed: {}", e)
        raise HTTPException(status_code=500, detail="Membership check failed")

    # 4) Resolve the loop's bot member (author of the loop message)
    LOOP_BOT_USER_ID = os.getenv("LOOP_BOT_USER_ID")
    if not LOOP_BOT_USER_ID:
        raise HTTPException(status_code=500, detail="Bot user not configured")
    try:
        bot_member = supa.rpc("member_id_for", {"u": LOOP_BOT_USER_ID, "l": loop_id})
        if not bot_member:
            raise HTTPException(status_code=500, detail="Bot is not a member of this loop")
    except Exception as e:
        logger.error("Membership RPC (bot) failed: {}", e)
        raise HTTPException(status_code=500, detail="Bot membership check failed")

    # 5) Generate summary text (LLM -> fallback)
    src_text = msg.get("content_ciphertext") or ""
    if payload.summary_override and payload.summary_override.strip():
        loop_text = "cipher: " + payload.summary_override.strip()
    else:
        try:
            loop_text = _summarize_with_llm(src_text)
        except Exception as e:
            logger.warning("LLM summary failed, using plain: {}", e)
            loop_text = _summarize_plain(src_text)

    # 6) Insert loop-visible AI message
    try:
        rec = supa.insert("messages", {
            "thread_id": thread_id,
            "created_by": x_user_id,           # initiator who triggered publish
            "author_member_id": bot_member,    # authored by the loop's agent
            "role": "ai",
            "channel": "loop",
            "visibility": "shared",
            "content_ciphertext": loop_text,
            "dek_wrapped": None,
            "nonce": None,
            "aead_tag": None,
            "lang": "en",
        })
        new_id = rec["id"]
    except Exception as e:
        logger.error("Insert loop message failed: {}", e)
        raise HTTPException(status_code=500, detail="Insert failed")

    return PublishResponse(published_message_id=str(new_id), thread_id=thread_id, ok=True)
