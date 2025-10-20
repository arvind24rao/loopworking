# /Users/arvindrao/loop/loop-api/app/models.py
from pydantic import BaseModel, Field
from typing import Optional, List

# ---------- Inbox ----------
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

# ---------- Publish ----------
class PublishRequest(BaseModel):
    message_id: Optional[str] = None
    thread_id: Optional[str] = None
    latest: Optional[bool] = Field(default=None, description="If true with thread_id, publish latest inbox message")

class PublishResponse(BaseModel):
    publish_id: str
    message_id: str
    thread_id: str
    visibility: str
    channel: str
    published_at: str   # ISO string
    ok: bool = True

# ---------- Feed ----------
class FeedItem(BaseModel):
    message_id: str
    thread_id: str
    content_plain: str
    published_at: str   # ISO string

class FeedResponse(BaseModel):
    items: List[FeedItem]
    next_cursor: Optional[str] = None