# app/main.py
import os
import json
import time
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import jwt  # PyJWT
import requests

# Routers
from app.routes.messages import router as messages_router
from app.routes.bot import router as bot_router

# ------------------------------------------------------------------------------
# Config
# ------------------------------------------------------------------------------

AUTH_MODE = (os.getenv("AUTH_MODE") or "permissive").strip().lower()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")  # If using HS256 projects
JWKS_URL = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json" if SUPABASE_URL else ""

ALLOWED_ORIGINS = [
    *(os.getenv("ALLOWED_ORIGINS", "").split(",") if os.getenv("ALLOWED_ORIGINS") else []),
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
]

# Paths that should skip auth entirely (so health checks succeed)
ALLOW_ANON_PATHS = {"/health", "/health/dbinfo"}
ALLOW_ANON_PREFIXES = set()  # add "/public" etc. if you ever need

# ------------------------------------------------------------------------------
# Lightweight JWKS cache for RS256 Supabase projects
# ------------------------------------------------------------------------------

class _JWKSCache:
    def __init__(self) -> None:
        self._keys: Dict[str, Dict[str, Any]] = {}
        self._last_fetch = 0.0

    def _fetch(self) -> None:
        if not JWKS_URL:
            return
        resp = requests.get(JWKS_URL, timeout=3)
        resp.raise_for_status()
        jwks = resp.json()
        self._keys = {k["kid"]: k for k in jwks.get("keys", [])}
        self._last_fetch = time.time()

    def get_key(self, kid: str) -> Optional[Dict[str, Any]]:
        if time.time() - self._last_fetch > 600:
            try:
                self._fetch()
            except Exception:
                pass
        return self._keys.get(kid)

_JWKS = _JWKSCache()

# ------------------------------------------------------------------------------
# Token verification (Supabase GoTrue)
# ------------------------------------------------------------------------------

class AuthResult:
    def __init__(self, uid: Optional[str], raw_claims: Optional[dict]) -> None:
        self.uid = uid
        self.claims = raw_claims or {}

def _parse_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None

def _verify_token(token: str) -> AuthResult:
    options = {"verify_aud": False, "verify_signature": True}

    if SUPABASE_JWT_SECRET:
        try:
            claims = jwt.decode(token, SUPABASE_JWT_SECRET, algorithms=["HS256"], options=options)
            sub = claims.get("sub") or claims.get("user_id")
            if not sub:
                raise HTTPException(status_code=401, detail="Invalid token (no sub)")
            return AuthResult(sub, claims)
        except jwt.PyJWTError:
            raise HTTPException(status_code=401, detail="Invalid token")

    try:
        unverified = jwt.get_unverified_header(token)
        kid = unverified.get("kid")
        if not kid:
            raise HTTPException(status_code=401, detail="Invalid token (no kid)")

        jwk = _JWKS.get_key(kid)
        if not jwk:
            _JWKS._fetch()
            jwk = _JWKS.get_key(kid)
            if not jwk:
                raise HTTPException(status_code=401, detail="JWKS key not found")

        claims = jwt.decode(
            token,
            jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk)),
            algorithms=["RS256"],
            options=options,
        )
        sub = claims.get("sub") or claims.get("user_id")
        if not sub:
            raise HTTPException(status_code=401, detail="Invalid token (no sub)")
        return AuthResult(sub, claims)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

# ------------------------------------------------------------------------------
# FastAPI app and global auth middleware
# ------------------------------------------------------------------------------

app = FastAPI(title="Loop API (Auth-wired)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in ALLOWED_ORIGINS if o],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _unauthorized(msg: str):
    from fastapi.responses import JSONResponse
    return JSONResponse(content={"detail": msg}, status_code=401)

@app.middleware("http")
async def auth_injector(request: Request, call_next):
    """
    - Skips auth for ALLOW_ANON_PATHS and OPTIONS (health checks / preflights).
    - In strict mode: 401 when no/invalid token (for all other paths).
    - On valid token: sets request.state.auth_uid = subject.
    """
    path = request.url.path
    if path in ALLOW_ANON_PATHS or any(path.startswith(p) for p in ALLOW_ANON_PREFIXES) or request.method == "OPTIONS":
        return await call_next(request)

    request.state.auth_uid = None
    token = _parse_bearer(request.headers.get("Authorization"))

    if token:
        auth = _verify_token(token)
        request.state.auth_uid = auth.uid
    else:
        if AUTH_MODE == "strict":
            return _unauthorized("Authorization required")

    return await call_next(request)

# Routers
app.include_router(messages_router)
app.include_router(bot_router)

@app.get("/health")
def health() -> Dict[str, str]:
    return {"ok": "true"}

@app.get("/health/dbinfo")
def dbinfo() -> Dict[str, Any]:
    # optional: minimal diagnostic without touching DB
    return {"app": "loop-api", "mode": AUTH_MODE}