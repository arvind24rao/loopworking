# app/main.py
from __future__ import annotations
import os
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Load env early (so reload workers see it)
load_dotenv()

# ---- Create app FIRST ----
app = FastAPI(title="Loop API", version="v2")

# ---- CORS ----
default_origins = [
    "http://127.0.0.1:5173", "http://localhost:5173",
    "http://127.0.0.1:3000", "http://localhost:3000",
    "https://fanciful-creponne-e1e59b.netlify.app", "https://loopasync.com",
]
extra = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=default_origins + extra,
    allow_origin_regex=r"^http://127\.0\.0\.1:\d+$|^http://localhost:\d+$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# ---- Health & CORS check ----
@app.get("/health")
def health():
    return {"status": "ok"}

@app.options("/__cors_check")
def cors_check_options():
    return {"ok": True}

@app.get("/__cors_check")
def cors_check_get():
    return {"ok": True}

# ---- Import routers AFTER app is created ----
from app.routes.messages import router as messages_router
from app.routes.feed import router as feed_router
from app.routes.bot import router as bot_router

# ---- Include routers ----
app.include_router(messages_router)
app.include_router(feed_router)
app.include_router(bot_router)

# optional: tiny debug to list mounted route prefixes
@app.get("/__debug/routes")
def _routes():
    return [r.path for r in app.router.routes]

# app/routes/feed.py (top)
from fastapi import APIRouter
router = APIRouter(prefix="/api", tags=["feed"])

@router.get("/feed/ping")
def feed_ping():
    return {"ok": True}