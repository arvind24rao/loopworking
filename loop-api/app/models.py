# /Users/arvindrao/loop/loop-api/app/models.py
from typing import Optional, List, Literal
from pydantic import BaseModel, Field

Audience = Literal["inbox_to_bot", "bot_to_user", "loop_shared"]

# ---------- Inbox (human -> bot) ----------
class InboxRequest(BaseModel):
    thread_id: str
    content_plain: str

class InboxResponse(BaseModel):
    message_id: str
    thread_id: str
    role: str
    channel: str
    visibility: str
    ok: bool
    note: Optional[str] = None

# ---------- Publish (kept for backward-compat; not used by humans now) ----------
class PublishRequest(BaseModel):
    message_id: Optional[str] = None
    thread_id: Optional[str] = None
    latest: Optional[bool] = Field(default=None)

class PublishResponse(BaseModel):
    publish_id: str
    message_id: str
    thread_id: str
    visibility: str
    channel: str
    published_at: str
    ok: bool = True

# ---------- Human inbox (bot -> human) ----------
class MeInboxItem(BaseModel):
    message_id: str
    thread_id: str
    content_plain: str
    created_at: str

class MeInboxResponse(BaseModel):
    items: List[MeInboxItem]
    next_cursor: Optional[str] = None

# ---------- Bot inbox (human -> bot) ----------
class BotInboxItem(BaseModel):
    message_id: str
    thread_id: str
    created_by: str
    content_plain: str
    created_at: str

class BotInboxResponse(BaseModel):
    items: List[BotInboxItem]
    next_cursor: Optional[str] = None

# ---------- Bot reply (bot -> human) ----------
class BotReplyRequest(BaseModel):
    recipient_profile_id: str
    thread_id: str
    content_plain: str

class BotReplyResponse(BaseModel):
    message_id: str
    thread_id: str
    recipient_profile_id: str
    created_at: str
    ok: bool = True