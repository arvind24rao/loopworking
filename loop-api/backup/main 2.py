# /Users/arvindrao/loop/loop-api/app/main.py
import os
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import ORJSONResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger

from .supa import supa, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
from .crypto import encrypt_plaintext
from .models import InboxRequest, InboxResponse

# --- env/bootstrap ---
ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env.dev")
ENV = os.getenv("ENV", "dev")

app = FastAPI(title="loop-mvp-api", default_response_class=ORJSONResponse)

# --- CORS (demo origins only; add your domain later) ---
ALLOWED_ORIGINS = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
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
        # Friendly message if file is missing
        return JSONResponse(
            {"ok": False, "message": "demo.html not found in app/static/"},
            status_code=404,
        )
    return FileResponse(str(demo_file), media_type="text/html")


# --- Health ---
@app.get("/health")
def health():
    info = {
        "ok": True,
        "env": ENV,
        "ts": int(time.time()),
        "rest": {"supabase_url_present": bool(SUPABASE_URL)},
    }
    # Light-touch check: attempt a no-op RPC call shape (will either succeed or raise)
    try:
        # Using known function name; will likely 400 if args invalid, which still proves REST reachable
        _ = supa.rpc("member_id_for", {"u": "00000000-0000-0000-0000-000000000000", "l": "00000000-0000-0000-0000-000000000000"})
        info["rest"]["rpc_reachable"] = True
    except Exception as e:
        info["rest"]["rpc_reachable"] = False
        info["rest"]["error"] = str(e)[:200]
    return info


# --- Core: Inbox write path (kept as-is) ---
@app.post("/messages/inbox", status_code=201)
def inbox(payload: InboxRequest, x_user_id: str = Header(..., alias="X-User-Id")):
    # 1) Resolve thread -> loop
    th = supa.select_one("threads", {"id": payload.thread_id}, select="loop_id")
    if not th or not th.get("loop_id"):
        raise HTTPException(status_code=404, detail="Thread not found")
    loop_id = th["loop_id"]

    # 2) Membership check via RPC member_id_for(u,l)
    try:
        author_member_id = supa.rpc("member_id_for", {"u": x_user_id, "l": loop_id})
        if not author_member_id:
            raise HTTPException(status_code=403, detail="User is not a member of this loop")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("RPC member_id_for failed: {}", e)
        raise HTTPException(status_code=500, detail="Membership check failed")

    # 3) Encrypt/plaintext stub + insert
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
        note=None,
    )


# --- DEBUG ROUTES (keep for now; hide links in UI) ---
@app.get("/__debug/supa")
def debug_supa():
    info = {
        "SUPABASE_URL": SUPABASE_URL,
        "service_key_len": len(SUPABASE_SERVICE_ROLE_KEY or ""),
    }
    try:
        test = supa.rpc(
            "member_id_for",
            {
                "u": "b8d99c3c-0d3a-4773-a324-a6bc60dee64e",
                "l": "e94bd651-5bac-4e39-8537-fe8c788c1475",
            },
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
