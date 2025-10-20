# app/llm.py
from __future__ import annotations

import os
from typing import List, Optional

from openai import OpenAI

# Read env (no .env file assumption in prod)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

_client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    _client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = (
    "You are Loop's relay bot. You write short, neutral, actionable summaries "
    "to forward from one participant to another. Never include private messages "
    "from the recipient themself; only summarize what the sender said. Be concise."
)

TEMPLATE_RULES = (
    "Write a 1–3 sentence update for {recipient_label} about what {sender_label} said. "
    "Prefer concrete facts, dates, owners, and next steps. Avoid speculation. "
    "If there is nothing meaningful, reply with a very brief courtesy note."
)

def _join_context(snippets: Optional[List[str]], max_items: int = 5) -> str:
    parts = (snippets or [])[-max_items:]
    parts = [p.strip() for p in parts if isinstance(p, str)]
    return "\n\n".join(parts) if parts else "(no context)"

def _llm_generate(user_content: str, *, max_tokens: int = 160) -> str:
    if not _client:
        # Fallback if key missing: return first line of context
        return user_content.splitlines()[-1][:220].strip() or "FYI."
    try:
        resp = _client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
            max_tokens=max_tokens,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text[:600].strip() or "FYI."
    except Exception:
        # Safe fallback
        return "FYI."

def _generate_reply_new(
    *,
    sender_profile_id: str,
    recipient_profile_id: str,
    thread_id: str,
    loop_id: str,
    recent_messages: Optional[List[str]] = None,
    max_tokens: int = 160,
    **_unused,
) -> str:
    sender_label = f"sender {sender_profile_id}"
    recipient_label = f"recipient {recipient_profile_id}"
    context_blob = _join_context(recent_messages, max_items=5)
    prompt = (
        TEMPLATE_RULES.format(recipient_label=recipient_label, sender_label=sender_label)
        + f"\n\nThread: {thread_id}\nLoop: {loop_id}\n"
        + f"Context from {sender_label} (oldest→newest):\n{context_blob}\n"
    )
    return _llm_generate(prompt, max_tokens=max_tokens)

def _generate_reply_legacy(
    message_text: str = "",
    thread_id: str = "",
    recipients: Optional[List[str]] = None,
    **_unused,
) -> str:
    recipient_label = f"recipient {recipients[0]}" if recipients else "recipient"
    sender_label = "sender"
    context_blob = _join_context([message_text], max_items=1)
    prompt = (
        TEMPLATE_RULES.format(recipient_label=recipient_label, sender_label=sender_label)
        + f"\n\nThread: {thread_id}\n"
        + f"Context from {sender_label}:\n{context_blob}\n"
    )
    return _llm_generate(prompt, max_tokens=140)

def generate_reply(*args, **kwargs) -> str:
    """
    Dispatch to the new or legacy signature.

    New (used by /bot/process):
        generate_reply(
            sender_profile_id=..., recipient_profile_id=...,
            thread_id=..., loop_id=..., recent_messages=[...]
        )

    Legacy:
        generate_reply(message_text: str, thread_id: str, recipients: List[str])
    """
    if "sender_profile_id" in kwargs or "recipient_profile_id" in kwargs:
        return _generate_reply_new(**kwargs)
    # legacy positional support
    if args:
        # (message_text, thread_id, recipients)
        message_text = args[0] if len(args) > 0 else ""
        thread_id = args[1] if len(args) > 1 else ""
        recipients = args[2] if len(args) > 2 else None
        return _generate_reply_legacy(message_text, thread_id, recipients)
    # legacy keyword support
    return _generate_reply_legacy(
        kwargs.get("message_text", ""),
        kwargs.get("thread_id", ""),
        kwargs.get("recipients"),
    )

__all__ = ["generate_reply"]