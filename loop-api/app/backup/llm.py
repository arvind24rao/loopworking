# /Users/arvindrao/loop/loop-api/app/llm.py
import os
from typing import List
from dotenv import load_dotenv
from openai import OpenAI

# Load env once (same .env.dev used by main)
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env.dev"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY missing in environment")

_client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = (
    "You are LoopBot, a neutral relay.\n"
    "- You NEVER imply the recipient experienced events written by the other user.\n"
    "- You speak concisely and do not add new facts.\n"
    "- If you ask a follow-up, keep it brief and optional.\n"
)

TEMPLATE_RULES = (
    "Compose ONE short message for {recipient_label}.\n"
    "The content below is from {sender_label}.\n\n"
    "OUTPUT FORMAT (strict):\n"
    "Update from {sender_label}: <one-sentence summary of what {sender_label} said>. "
    "<optional very brief follow-up question to {recipient_label} about how they'd like to respond to {sender_label}>.\n\n"
    "STYLE & CONSTRAINTS:\n"
    "- Refer to {sender_label} in third person (\"A said…\").\n"
    "- Do NOT use \"you\" to describe {sender_label}'s actions.\n"
    "- No emojis. Max ~25 words total.\n"
)

def generate_reply(context_messages: List[str], recipient_label: str, sender_label: str) -> str:
    """
    context_messages: ordered list of the other user's messages (latest last)
    recipient_label: 'A' or 'B' — who will receive the DM
    sender_label:    'A' or 'B' — who wrote the context
    """
    user_blob = "\n\n".join(context_messages[-5:]) if context_messages else "(no context)"
    prompt = TEMPLATE_RULES.format(recipient_label=recipient_label, sender_label=sender_label) + \
             f"\nContext messages from {sender_label} (latest last):\n{user_blob}\n"

    resp = _client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=120,
    )
    return (resp.choices[0].message.content or "").strip()