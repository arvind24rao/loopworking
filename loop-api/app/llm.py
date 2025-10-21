# app/llm.py
from __future__ import annotations

import os
import time
from typing import Optional, List, Dict, Any, Tuple

from openai import OpenAI

# ── Env knobs ──────────────────────────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini"))

# Completion controls
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_COMPLETION_TOKENS", "256"))  # kept var name for env b/w compat
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
LLM_TOP_P = float(os.getenv("LLM_TOP_P", "1.0"))

# Timeouts & retries
LLM_TIMEOUT_SEC = int(os.getenv("LLM_TIMEOUT_SEC", "45"))
LLM_RETRIES = int(os.getenv("LLM_RETRIES", "2"))  # default up to 2 retries (3 total attempts)
LLM_RETRY_MAX_S = int(os.getenv("LLM_RETRY_MAX_SEC", "10"))

# Output guardrail (post-trim)
LLM_MAX_CHARS = int(os.getenv("LLM_MAX_CHARS", "600"))

# Diagnostics
LLM_LOG_USAGE = os.getenv("LLM_LOG_USAGE", "0") == "1"

# ── Client init (single global) ────────────────────────────────────────────────
_client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    _client = OpenAI(api_key=OPENAI_API_KEY, timeout=LLM_TIMEOUT_SEC)


# ── Prompt: Loop Relay Composer (third-person digest with AIOK) ────────────────
SYSTEM_PROMPT = """You synthesize short messages from multiple authors in a shared loop into one concise, third-person update tailored for a specific recipient.

ROLE & PURPOSE
- Convert first-person statements into neutral third-person sentences with clear attribution to each author (e.g., “User A…”, “User C…”).
- Output should be glanceable and practical.

AUDIENCE
- One recipient (the reader). Do not address them as “you”. No pleasantries or filler.

STYLE
- Third person, neutral, matter-of-fact.
- Prefer 1 tight sentence; allow up to 2 sentences total and ~40–50 words maximum.
- Combine related items into a single flowing sentence; use a semicolon if needed.
- No commands, apologies, or speculation.
- Include one relevant emoji at the end of your crafted summary.
- Let the tone be bright and endearing and social.

USER GUIDE:
-Instead of saying 'User 21520d4c', say 'User B'. Instead of 'User c9cf9661' say 'User A'.

ATTRIBUTION & CONTENT RULES
- Attribute each item to its author label (e.g., “User A”, “Alice” if provided).
- Transform first-person to third-person (“I had a fun time…” → “User A enjoyed…”).
- Preserve intent: info / question / request / invite (e.g., “User C is asking whether…”, “User C invites others to…”).
- De-duplicate overlaps; include each author at most once unless essential.
- Do not add facts beyond the provided content. If details are missing, omit them.

TIME & DATE HANDLING
- You are given CURRENT_DATE, CURRENT_TIME, and TIMEZONE context.
- When an item references a relative time (e.g., “next Sunday”), keep the human phrase and append a normalized explicit date in parentheses using the given timezone, e.g., “next Sunday (26 October 2025)”.
- Use correct tense for past vs. future.

NO-UPDATE BEHAVIOUR
- If there are no new or relevant messages to summarise, output exactly:
  No new updates in this loop since you last refreshed at {CURRENT_TIME} on {CURRENT_DATE}.-AIOK
- Use 24-hour time format (HH:MM). Do not add quotes or extra text.

STAMPING RULE (DIAGNOSTIC)
- At the end of every valid summary you generate, append the literal string “-AIOK”. Do not add spaces, punctuation, or any other text after it.
- Never produce the stamp unless you are producing an actual summary or the explicit no-update line above.

OUTPUT FORMAT
- Single paragraph. No heading, bullets, or labels.

EXAMPLE (for style illustration; adapt names/dates based on inputs)
INPUT MESSAGES (2):
  - Author: User A — “I had a fun time at the lake last week with the kids.”
  - Author: User C — “Does anyone want to join me at the market next Sunday?”
CURRENT_DATE: 20 October 2025
CURRENT_TIME: 14:32
TIMEZONE: Asia/Singapore

EXPECTED OUTPUT:
User A enjoyed time at the lake with the kids last week; User C is asking if anyone wants to join them at the market next Sunday (26 October 2025).-AIOK
"""


# ── Helpers ────────────────────────────────────────────────────────────────────
def _build_user_prompt(
    context_messages: Optional[List[Dict[str, str]]] = None,
    message_text: str = "",
    current_date: str = "",
    current_time: str = "",
    timezone: str = "Asia/Singapore",
) -> List[Dict[str, str]]:
    """
    Builds the chat payload. Supports:
    - context_messages: list of {"author": "...", "text": "..."} items (preferred)
    - message_text: fallback single raw text (legacy)
    """
    # System context variables for date/time normalization
    sys_context = (
        f"CURRENT_DATE: {current_date}\n"
        f"CURRENT_TIME: {current_time}\n"
        f"TIMEZONE: {timezone}\n"
    )

    # Prepare the 'user' content with a compact, unambiguous structure.
    if context_messages:
        # Keep it minimal; the prompt teaches the model how to use this structure.
        lines = []
        for idx, item in enumerate(context_messages, start=1):
            author = (item.get("author") or "Unknown").strip()
            text = (item.get("text") or "").strip()
            if not text:
                continue
            lines.append(f"{idx}) Author: {author} — {text}")
        if not lines:
            # No content; rely on no-update behaviour.
            user_payload = "INPUT MESSAGES: (none)"
        else:
            user_payload = "INPUT MESSAGES:\n" + "\n".join(lines)
    else:
        # Legacy single message
        msg = (message_text or "").strip()
        user_payload = "INPUT MESSAGES:\n1) Author: User — " + (msg if msg else "(none)")

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": sys_context},
        {"role": "user", "content": user_payload},
    ]


def _is_transient_error(exc: Exception) -> bool:
    """
    Heuristic: retry on timeouts, rate limits, and 5xx-like API errors.
    We avoid importing exception classes to keep compatibility across client versions.
    """
    name = exc.__class__.__name__
    msg = str(exc).lower()
    return any(
        key in (name.lower() + " " + msg)
        for key in [
            "timeout", "timed out", "rate", "limit", "overloaded", "server error", "503", "502", "500"
        ]
    )


def _chat_with_retry(messages: List[Dict[str, str]], user_id: Optional[str] = None) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Bounded retry wrapper around Chat Completions.
    Returns (text, usage_dict|None) or raises after final attempt.
    """
    if _client is None:
        raise RuntimeError("OPENAI_API_KEY not configured")

    delay = 1.0
    last_exc: Optional[Exception] = None
    for attempt in range(LLM_RETRIES + 1):
        try:
            resp = _client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                # IMPORTANT FIX: use max_completion_tokens for current models
                max_completion_tokens=LLM_MAX_TOKENS,
                temperature=LLM_TEMPERATURE,
                top_p=LLM_TOP_P,
                user=(user_id or None),
                # Per-call timeout is set at client-level; pass here if your client supports it.
            )
            text = (resp.choices[0].message.content or "").strip()
            usage = getattr(resp, "usage", None)
            return text, (usage.to_dict() if hasattr(usage, "to_dict") else dict(usage) if usage else None)
        except Exception as e:
            last_exc = e
            if attempt >= LLM_RETRIES or not _is_transient_error(e):
                # Final fail or non-transient error → raise
                raise
            time.sleep(min(delay, LLM_RETRY_MAX_S))
            delay *= 2.0
    # Should not reach
    raise last_exc or RuntimeError("Unknown LLM failure")


def _post_trim(text: str) -> str:
    """
    Light guardrail to avoid oversized downstream payloads.
    Does NOT add '-AIOK' — that must be produced by the model itself per spec.
    """
    if len(text) > LLM_MAX_CHARS:
        return text[:LLM_MAX_CHARS].rstrip()
    return text


# ── Public API ────────────────────────────────────────────────────────────────
def generate_reply(
    *args,
    **kwargs,
) -> str:
    """
    Backwards-compatible entrypoint.

    Usage patterns supported:
    1) Legacy single text:
       generate_reply(message_text="...")

    2) Structured multi-message:
       generate_reply(
           context_messages=[{"author": "User A", "text": "..."}, {"author": "User C", "text": "..."}],
           current_date="20 October 2025",
           current_time="14:32",
           timezone="Asia/Singapore",
           user_id="loop:user_b"   # optional; improves traceability
       )

    Args (kwargs):
      - message_text: str (legacy single message)
      - context_messages: List[Dict[str, str]] with keys {"author","text"}
      - current_date: str  (e.g., "20 October 2025")
      - current_time: str  (24h "HH:MM")
      - timezone:     str  (IANA, default "Asia/Singapore")
      - user_id:      str  (optional OpenAI 'user' field for traceability)

    Returns:
      - Model text output (already includes '-AIOK' when appropriate, produced by the model).
    """
    # Preferred keywords
    message_text: str = kwargs.get("message_text") if "message_text" in kwargs else (args[0] if len(args) > 0 else "")
    context_messages: Optional[List[Dict[str, str]]] = kwargs.get("context_messages")
    current_date: str = kwargs.get("current_date", "")
    current_time: str = kwargs.get("current_time", "")
    timezone: str = kwargs.get("timezone", "Asia/Singapore")
    user_id: Optional[str] = kwargs.get("user_id")

    messages = _build_user_prompt(
        context_messages=context_messages,
        message_text=message_text,
        current_date=current_date,
        current_time=current_time,
        timezone=timezone,
    )
    text, usage = _chat_with_retry(messages, user_id=user_id)

    if LLM_LOG_USAGE and usage:
        try:
            print(f"[llm] usage prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')} total={usage.get('total_tokens')}")
        except Exception:
            pass

    return _post_trim(text)


__all__ = ["generate_reply"]