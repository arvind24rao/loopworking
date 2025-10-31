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
    # adjust or externalise if you prefer
    *(os.getenv("ALLOWED_ORIGINS", "").split(",") if os.getenv("ALLOWED_ORIGINS") else []),
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
]

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
        # refresh every 10 minutes
        if time.time() - self._last_fetch > 600:
            try:
                self._fetch()
            except Exception:
                # best-effort; keep old keys if fetch fails
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
    """
    Verifies a Supabase JWT in two ways:
    1) If SUPABASE_JWT_SECRET is set → HS256 verification.
    2) Else → RS256 via JWKS from {SUPABASE_URL}/auth/v1/.well-known/jwks.json
    Returns AuthResult(uid=sub, claims=...).
    Raises HTTPException(401) if invalid.
    """
    options = {"verify_aud": False, "verify_signature": True}

    # HS256 path (older Supabase projects / if explicitly configured)
    if SUPABASE_JWT_SECRET:
        try:
            claims = jwt.decode(
                token,
                SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                options=options,
            )
            sub = claims.get("sub") or claims.get("user_id")
            if not sub:
                raise HTTPException(status_code=401, detail="Invalid token (no sub)")
            return AuthResult(sub, claims)
        except jwt.PyJWTError:
            raise HTTPException(status_code=401, detail="Invalid token")

    # RS256 via JWKS
    try:
        unverified = jwt.get_unverified_header(token)
        kid = unverified.get("kid")
        if not kid:
            raise HTTPException(status_code=401, detail="Invalid token (no kid)")

        jwk = _JWKS.get_key(kid)
        if not jwk:
            # Force refresh once if missing
            _JWKS._fetch()
            jwk = _JWKS.get_key(kid)
            if not jwk:
                raise HTTPException(status_code=401, detail="JWKS key not found")

        # PyJWT expects PEM; use algorithms with key in jwk form
        claims = jwt.decode(token, jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk)), algorithms=["RS256"], options=options)
        sub = claims.get("sub") or claims.get("user_id")
        if not sub:
            raise HTTPException(status_code=401, detail="Invalid token (no sub)")
        return AuthResult(sub, claims)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

# ------------------------------------------------------------------------------
# FastAPI app and global auth dependency
# ------------------------------------------------------------------------------

app = FastAPI(title="Loop API (Auth-wired)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in ALLOWED_ORIGINS if o],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def auth_injector(request: Request, call_next):
    """
    Global middleware that:
      - Parses Bearer token (if any).
      - When valid, sets request.state.auth_uid = sub
      - Enforces AUTH_MODE=strict when no/invalid token is present
    """
    request.state.auth_uid = None

    token = _parse_bearer(request.headers.get("Authorization"))

    if token:
        # Validate; 401 on invalid token
        auth = _verify_token(token)
        request.state.auth_uid = auth.uid
    else:
        # No token
        if AUTH_MODE == "strict":
            return _unauthorized("Authorization required")

    # Continue
    return await call_next(request)

def _unauthorized(msg: str):
    return _json_response({"detail": msg}, status_code=401)

def _json_response(payload: Dict[str, Any], status_code: int = 200):
    from fastapi.responses import JSONResponse
    return JSONResponse(content=payload, status_code=status_code)

# Routers (your routes already use absolute paths like /api/send_message, etc.)
app.include_router(messages_router)
app.include_router(bot_router)

@app.get("/health")
def health() -> Dict[str, str]:
    return {"ok": "true"}