# app/llm.py
from __future__ import annotations

import os
import time
from typing import Optional, List, Dict, Any

from openai import OpenAI

# --- Env knobs (works with both OPENAI_MODEL and LLM_MODEL) ---
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL        = os.getenv("LLM_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
LLM_MAX_COMPLETION_TOKENS   = int(os.getenv("LLM_MAX_COMPLETION_TOKENS", "256"))
LLM_TIMEOUT_SEC  = int(os.getenv("LLM_TIMEOUT_SEC", "45"))
LLM_RETRIES      = int(os.getenv("LLM_RETRIES", "1"))
LLM_RETRY_MAX_S  = int(os.getenv("LLM_RETRY_MAX_SEC", "10"))

_client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    # NOTE: openai>=1.0 supports a per-client timeout
    _client = OpenAI(api_key=OPENAI_API_KEY, timeout=LLM_TIMEOUT_SEC)


def _chat_with_retry(messages: List[Dict[str, Any]],
                     model: Optional[str] = None,
                     max_tokens: Optional[int] = None) -> str:
    """
    Small, bounded retry wrapper around Chat Completions.
    Returns the final text or raises an exception after retries.
    """
    if _client is None:
        # Hard fail early if no key is present in the process
        raise RuntimeError("OPENAI_API_KEY not configured")

    model = model or LLM_MODEL
    max_tokens = max_tokens or LLM_MAX_TOKENS

    delay = 1.0
    for attempt in range(LLM_RETRIES + 1):
        try:
            resp = _client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                # If your client supports per-call timeout param, you may pass it here.
            )
            text = (resp.choices[0].message.content or "").strip()
            # keep messages short to avoid downstream bloat
            return (text[:600].strip() or "FYI.")
        except Exception as e:
            if attempt >= LLM_RETRIES:
                # Final fail
                raise
            time.sleep(min(delay, LLM_RETRY_MAX_S))
            delay *= 2.0


def generate_reply(*args, **kwargs) -> str:
    """
    Backwards-compatible public entry:
    - generate_reply(message_text: str, thread_id: Optional[str] = None, recipients: Optional[List[str]] = None)
    - or called with positional args (message_text, thread_id, recipients)
    """
    # Preferred keyword use
    message_text: str = kwargs.get("message_text") if "message_text" in kwargs else (args[0] if len(args) > 0 else "")
    # Keep a tiny, safe prompt. If upstream provides a richer history, that should be
    # capped before calling us to control token/latency.
    messages = [
        {"role": "system", "content": "You are a concise assistant helping two users coordinate. Keep replies brief and useful."},
        {"role": "user", "content": message_text or "Respond helpfully."}
    ]
    return _chat_with_retry(messages, model=LLM_MODEL, max_tokens=LLM_MAX_TOKENS)


__all__ = ["generate_reply"]