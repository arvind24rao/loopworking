# app/main.py
from __future__ import annotations

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Import routers (each file defines its own prefix)
from app.routes.messages import router as messages_router  # prefix="/api"
from app.routes.bot import router as bot_router            # prefix="/bot"
try:
    from app.routes.feed import router as feed_router      # likely prefix="/api"
except Exception:
    feed_router = None
from app.routes.diag import router as diag_router
from app.diagnostics import router as diagnostics_router

def _cors_origins() -> list[str]:
    # Allow local dev + Netlify preview + prod by default; override via CORS_ORIGINS
    raw = os.getenv("CORS_ORIGINS", "")
    if raw.strip():
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "https://localhost",
        "https://127.0.0.1",
        # Netlify preview/prod domains (wildcards are okay when allow_credentials=False)
        # Add your actual site domain(s) here if you need credentials.
        "*",
    ]

def create_app() -> FastAPI:
    app = FastAPI(title="Loop API", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        max_age=600,
    )

    @app.get("/health")
    def health():
        return {"ok": True}

    # Include routers WITHOUT extra prefixes. Each router carries its own.
    app.include_router(messages_router)  # /api/...
    app.include_router(bot_router)       # /api/bot/...
    app.include_router(diagnostics_router)
    # app.include_router(bot_router, prefix="/bot")       # -> /bot/process
    # app.include_router(bot_router, prefix="/api/bot")   # -> /api/bot/process
    app.include_router(diag_router)  # /health/db
    if feed_router is not None:
        app.include_router(feed_router)  # /api/feed...

    return app

app = create_app()