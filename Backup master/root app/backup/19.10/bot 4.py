# app/bot.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel
from loguru import logger

import os
import psycopg  # psycopg 3.x
from psycopg.rows import dict_row

# Project-local LLM helper
from app.llm import generate_reply

router = APIRouter(prefix="/api", tags=["bot"])


# ---------- Models ----------

class BotProcessItemPreview(BaseModel):
    recipient_profile_id: str
    text: str


class BotProcessItem(BaseModel):
    human_message_id: str
    previews: List[BotProcessItemPreview] = []


class BotProcessStats(BaseModel):
    scanned: int = 0
    processed: int = 0
    inserted: int = 0
    skipped: int = 0
    dry_run: bool = True
    recipient_ids: List[str] = []  # flattened unique ids
    # debug fields (only when debug=true)
    db_user: Optional[str] = None
    session_user: Optional[str] = None
    loop_id: Optional[str] = None
    bot_member_id: Optional[str] = None


class BotProcessResponse(BaseModel):
    ok: bool = True
    reason: Optional[str] = None
    stats: BotProcessStats
    items: List[BotProcessItem] = []


# ---------- Utility ----------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_conn() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL (or SUPABASE_DB_URL) is not set")
    return psycopg.connect(dsn, row_factory=dict_row)


def _as_bool(v) -> bool:
    if isinstance(v, bool): return v
    if v is None: return True
    return str(v).lower() in ("1", "true", "t", "yes", "y")


# ---------- Core SQL helpers ----------

def _select_unprocessed(conn: psycopg.Connection, thread_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
    """
    Select unprocessed human messages (inbox_to_bot) for this thread.
    """
    if thread_id:
        q = """
            SELECT id, thread_id, created_at, created_by, author_member_id,
                   audience
            FROM messages
            WHERE thread_id = %s
              AND audience = 'inbox_to_bot'
              AND COALESCE(processed, FALSE) = FALSE
            ORDER BY created_at ASC
            LIMIT %s
        """
        args: Tuple[Any, ...] = (thread_id, limit)
    else:
        q = """
            SELECT id, thread_id, created_at, created_by, author_member_id,
                   audience
            FROM messages
            WHERE audience = 'inbox_to_bot'
              AND COALESCE(processed, FALSE) = FALSE
            ORDER BY created_at ASC
            LIMIT %s
        """
        args = (limit,)

    with conn.cursor() as cur:
        cur.execute(q, args)
        return list(cur.fetchall())


def _loop_id_for_member(conn: psycopg.Connection, author_member_id: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT loop_id FROM members WHERE id = %s", (author_member_id,))
        row = cur.fetchone()
        return row["loop_id"] if row else None


def _bot_member_id_for_loop(conn: psycopg.Connection, loop_id: str, bot_profile_id: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM members WHERE loop_id = %s AND profile_id = %s",
            (loop_id, bot_profile_id),
        )
        row = cur.fetchone()
        return row["id"] if row else None


def _recipients_for_loop(conn: psycopg.Connection, loop_id: str, exclude_ids: List[str]) -> List[str]:
    placeholders = ", ".join(["%s"] * len(exclude_ids)) if exclude_ids else ""
    if exclude_ids:
        q = f"""
            SELECT profile_id
            FROM members
            WHERE loop_id = %s
              AND profile_id NOT IN ({placeholders})
        """
        args: Tuple[Any, ...] = (loop_id, *exclude_ids)
    else:
        q = "SELECT profile_id FROM members WHERE loop_id = %s"
        args = (loop_id,)

    with conn.cursor() as cur:
        cur.execute(q, args)
        return [r["profile_id"] for r in cur.fetchall()]


def _insert_bot_messages_and_mark_processed(
    conn: psycopg.Connection,
    *,
    bot_profile_id: str,
    bot_member_id: str,
    to_publish: List[Dict[str, Any]],
    source_message_ids: List[str],
) -> Tuple[int, int]:
    """
    Insert rows into messages (audience='bot_to_user') with required columns
    and mark sources processed. Returns (inserted_count, processed_count).
    """
    inserted = 0
    processed = 0

    with conn.cursor() as cur:
        if to_publish:
            values_sql = ", ".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s)"] * len(to_publish))
            args: List[Any] = []
            for row in to_publish:
                # required: thread_id, created_at, created_by, author_member_id, role, channel,
                #          visibility, audience, recipient_profile_id, content_ciphertext
                args.extend([
                    row["thread_id"],
                    row["created_at"],
                    bot_profile_id,            # created_by
                    bot_member_id,             # author_member_id (bot's member in this loop)
                    "bot",                     # role
                    "outbox",                  # channel
                    "private",                 # visibility
                    "bot_to_user",             # audience
                    row["recipient_profile_id"],
                    row["content_ciphertext"], # NOTE: plaintext for now (no encryptor wired)
                ])

            cur.execute(
                f"""
                INSERT INTO messages
                  (thread_id, created_at, created_by, author_member_id,
                   role, channel, visibility, audience, recipient_profile_id, content_ciphertext)
                VALUES {values_sql}
                """,
                args,
            )
            inserted = cur.rowcount

        if source_message_ids:
            placeholders = ", ".join(["%s"] * len(source_message_ids))
            cur.execute(
                f"""
                UPDATE messages
                SET processed = TRUE, processed_at = NOW()
                WHERE id IN ({placeholders})
                """,
                source_message_ids,
            )
            processed = cur.rowcount

    return inserted, processed


# ---------- Route ----------

@router.post("/bot/process", response_model=BotProcessResponse)
def process_bot_messages(
    thread_id: Optional[str] = Query(default=None, description="Thread (conversation) id"),
    limit: int = Query(default=10, ge=1, le=100),
    dry_run: bool = Query(default=True),
    debug: Optional[bool] = Query(default=False),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    """
    Process human inbox_to_bot messages into bot_to_user messages.

    Key differences vs before:
      - Derive loop_id from author_member_id (members.loop_id)
      - Resolve bot_member_id in that loop (required NOT NULL)
      - Insert with required NOT NULL columns (incl. content_ciphertext)
      - Mark sources processed in the same transaction
      - debug=true echoes db_user/session_user/loop/bot_member/recipient samples
    """
    dry_run = _as_bool(dry_run)
    if not x_user_id:
        raise HTTPException(status_code=400, detail="missing_bot_profile_id_header")

    scanned = processed = inserted = skipped = 0
    items: List[BotProcessItem] = []
    flat_recips: List[str] = []

    dbg_db_user = dbg_session_user = dbg_loop_id = dbg_bot_member = None

    try:
        with _get_conn() as conn:
            # debug context (role + session)
            if debug:
                with conn.cursor() as cur:
                    cur.execute("select current_user as u, session_user as s")
                    row = cur.fetchone()
                    if row:
                        dbg_db_user = row["u"]
                        dbg_session_user = row["s"]

            rows = _select_unprocessed(conn, thread_id=thread_id, limit=limit)
            scanned = len(rows)
            if not rows:
                return BotProcessResponse(
                    stats=BotProcessStats(
                        scanned=scanned, processed=processed, inserted=inserted,
                        skipped=skipped, dry_run=dry_run,
                        recipient_ids=[], db_user=dbg_db_user, session_user=dbg_session_user,
                        loop_id=dbg_loop_id, bot_member_id=dbg_bot_member
                    ),
                    items=[],
                )

            to_publish: List[Dict[str, Any]] = []
            source_ids: List[str] = []

            # compute loop/bot membership once (messages can be from different loops; handle per-row)
            for r in rows:
                msg_id = r["id"]
                author_profile_id = r["created_by"]
                author_member_id = r.get("author_member_id")
                thread = r["thread_id"]

                if not author_member_id:
                    logger.warning("message %s missing author_member_id; skipping", msg_id)
                    skipped += 1
                    continue

                loop_id = _loop_id_for_member(conn, author_member_id)
                if debug and not dbg_loop_id:
                    dbg_loop_id = loop_id

                if not loop_id:
                    logger.warning("message %s could not resolve loop_id; skipping", msg_id)
                    skipped += 1
                    continue

                bot_member_id = _bot_member_id_for_loop(conn, loop_id, x_user_id)
                if debug and not dbg_bot_member:
                    dbg_bot_member = bot_member_id

                if not bot_member_id:
                    logger.warning("bot is not a member of loop %s; skipping message %s", loop_id, msg_id)
                    skipped += 1
                    continue

                exclude = [author_profile_id, x_user_id]
                recipients = _recipients_for_loop(conn, loop_id, exclude_ids=exclude)
                flat_recips.extend(recipients)

                previews: List[BotProcessItemPreview] = []
                for pid in recipients:
                    # NOTE: If you later wire encryption, encrypt here and set content_ciphertext accordingly
                    reply_text = generate_reply(
                        human_text="",  # content is encrypted in DB; omit plaintext fetch in this path
                        author_profile_id=author_profile_id,
                        recipient_profile_id=pid,
                        thread_id=thread,
                    )
                    previews.append(BotProcessItemPreview(recipient_profile_id=pid, text=reply_text))

                    if not dry_run:
                        to_publish.append(
                            {
                                "thread_id": thread,
                                "created_at": _now_iso(),
                                "recipient_profile_id": pid,
                                "content_ciphertext": reply_text,  # TEMP: store plaintext in ciphertext column
                            }
                        )

                items.append(BotProcessItem(human_message_id=msg_id, previews=previews))
                if not dry_run:
                    source_ids.append(msg_id)

            if not dry_run:
                with conn.transaction():
                    ins, proc = _insert_bot_messages_and_mark_processed(
                        conn,
                        bot_profile_id=x_user_id,
                        bot_member_id=bot_member_id,  # last loop’s bot member_id; safe because all rows share same loop under given thread_id
                        to_publish=to_publish,
                        source_message_ids=source_ids,
                    )
                    inserted += ins
                    processed += proc

        return BotProcessResponse(
            stats=BotProcessStats(
                scanned=scanned, processed=processed, inserted=inserted, skipped=skipped,
                dry_run=dry_run, recipient_ids=list(dict.fromkeys(flat_recips)),
                db_user=dbg_db_user, session_user=dbg_session_user,
                loop_id=dbg_loop_id, bot_member_id=dbg_bot_member
            ),
            items=items,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("bot.process failed: {}", e)
        raise HTTPException(status_code=500, detail="bot_process_exception")
    
# app/bot.py  (append this to the bottom of the file)
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel
from typing import Any, Dict, List, Optional, Tuple
import os, psycopg
from psycopg.rows import dict_row
from datetime import datetime, timezone
from app.llm import generate_reply

router = APIRouter(prefix="/api", tags=["bot"])  # keep your existing router

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _get_conn():
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not dsn: raise RuntimeError("DATABASE_URL not set")
    return psycopg.connect(dsn, row_factory=dict_row)

def _select_unprocessed(conn, thread_id: Optional[str], limit: int):
    q = """
      SELECT id, thread_id, created_at, created_by, author_member_id
      FROM messages
      WHERE audience='inbox_to_bot' AND COALESCE(processed,FALSE)=FALSE
    """
    args: List[Any] = []
    if thread_id:
        q += " AND thread_id=%s"
        args.append(thread_id)
    q += " ORDER BY created_at ASC LIMIT %s"
    args.append(limit)
    with conn.cursor() as cur:
        cur.execute(q, tuple(args))
        return list(cur.fetchall())

def _loop_id_for_member(conn, author_member_id: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT loop_id FROM members WHERE id=%s", (author_member_id,))
        row = cur.fetchone()
        return row["loop_id"] if row else None

def _bot_member_id_for_loop(conn, loop_id: str, bot_profile_id: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM members WHERE loop_id=%s AND profile_id=%s", (loop_id, bot_profile_id))
        row = cur.fetchone()
        return row["id"] if row else None

def _recipients_for_loop(conn, loop_id: str, exclude_ids: List[str]) -> List[str]:
    if exclude_ids:
        ph = ", ".join(["%s"]*len(exclude_ids))
        q = f"SELECT profile_id FROM members WHERE loop_id=%s AND profile_id NOT IN ({ph})"
        args = (loop_id, *exclude_ids)
    else:
        q = "SELECT profile_id FROM members WHERE loop_id=%s"
        args = (loop_id,)
    with conn.cursor() as cur:
        cur.execute(q, args)
        return [r["profile_id"] for r in cur.fetchall()]

def _insert_and_mark(conn, bot_profile_id: str, bot_member_id: str, to_publish: List[Dict[str, Any]], source_ids: List[str]):
    ins = proc = 0
    with conn.cursor() as cur:
        if to_publish:
            values_sql = ", ".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s)"]*len(to_publish))
            args: List[Any] = []
            for row in to_publish:
                args.extend([
                    row["thread_id"], row["created_at"], bot_profile_id,
                    bot_member_id, "bot", "outbox", "private", "bot_to_user",
                    row["recipient_profile_id"],
                    # messages schema requires content_ciphertext (no plaintext content column)
                    # if you add encryption later, replace next line with ciphertext
                    row["content_ciphertext"],
                ])
            cur.execute(
                f"""INSERT INTO messages
                    (thread_id, created_at, created_by, author_member_id,
                     role, channel, visibility, audience, recipient_profile_id, content_ciphertext)
                    VALUES {values_sql}""",
                args
            )
            ins = cur.rowcount
        if source_ids:
            ph = ", ".join(["%s"]*len(source_ids))
            cur.execute(
              f"UPDATE messages SET processed=TRUE, processed_at=NOW() WHERE id IN ({ph})",
              source_ids
            )
            proc = cur.rowcount
    return ins, proc

class _Stats(BaseModel):
    scanned:int=0; processed:int=0; inserted:int=0; skipped:int=0; dry_run:bool=True
    recipient_ids:List[str]=[]; db_user:Optional[str]=None; session_user:Optional[str]=None
    loop_id:Optional[str]=None; bot_member_id:Optional[str]=None

class _Item(BaseModel):
    human_message_id:str; recipients:List[str]=[]; previews:List[Dict[str,str]]=[]

class _Resp(BaseModel):
    ok:bool=True; reason:Optional[str]=None; stats:_Stats; items:List[_Item]=[]

@router.post("/bot/process_v2", response_model=_Resp)
def process_v2(
    thread_id: Optional[str] = Query(default=None),
    limit: int = Query(default=10, ge=1, le=100),
    dry_run: bool = Query(default=True),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
):
    if not x_user_id:
        raise HTTPException(status_code=400, detail="missing_bot_profile_id_header")

    scanned=processed=inserted=skipped=0
    items:List[_Item]=[]; flat_recips:List[str]=[]
    dbg_user=dbg_sess=None; dbg_loop=None; dbg_bot_mem=None

    try:
        with _get_conn() as conn, conn.cursor() as cur:
            cur.execute("select current_user as u, session_user as s")
            row = cur.fetchone()
            if row: dbg_user, dbg_sess = row["u"], row["s"]

        with _get_conn() as conn:
            rows = _select_unprocessed(conn, thread_id, limit)
            scanned = len(rows)
            if not rows:
                return _Resp(stats=_Stats(scanned=scanned, processed=0, inserted=0, skipped=0,
                                          dry_run=dry_run, recipient_ids=[],
                                          db_user=dbg_user, session_user=dbg_sess,
                                          loop_id=dbg_loop, bot_member_id=dbg_bot_mem),
                             items=[])

            to_publish:List[Dict[str,Any]]=[]; source_ids:List[str]=[]

            for r in rows:
                mid = r["id"]; author_profile_id = r["created_by"]; author_member_id = r["author_member_id"]; thread = r["thread_id"]
                if not author_member_id:
                    skipped += 1; continue
                with _get_conn() as c2:
                    loop_id = _loop_id_for_member(c2, author_member_id)
                    if not loop_id: skipped += 1; continue
                    dbg_loop = dbg_loop or loop_id
                    bot_member_id = _bot_member_id_for_loop(c2, loop_id, x_user_id)
                    dbg_bot_mem = dbg_bot_mem or bot_member_id
                    if not bot_member_id: skipped += 1; continue

                    exclude = [author_profile_id, x_user_id]
                    recips = _recipients_for_loop(c2, loop_id, exclude)
                    flat_recips.extend(recips)

                previews=[]
                for pid in recips:
                    reply = generate_reply(human_text="", author_profile_id=author_profile_id,
                                           recipient_profile_id=pid, thread_id=thread)
                    previews.append({"recipient_profile_id": pid, "content": reply})
                    if not dry_run:
                        to_publish.append({
                          "thread_id": thread,
                          "created_at": _now_iso(),
                          "recipient_profile_id": pid,
                          "content_ciphertext": reply,
                        })

                items.append(_Item(human_message_id=mid, recipients=recips, previews=previews))
                if not dry_run: source_ids.append(mid)

            if not dry_run:
                with _get_conn() as c3, c3.transaction():
                    ins, proc = _insert_and_mark(c3, x_user_id, dbg_bot_mem, to_publish, source_ids)
                    inserted += ins; processed += proc

        return _Resp(
          stats=_Stats(scanned=scanned, processed=processed, inserted=inserted, skipped=skipped,
                       dry_run=dry_run, recipient_ids=list(dict.fromkeys(flat_recips)),
                       db_user=dbg_user, session_user=dbg_sess, loop_id=dbg_loop, bot_member_id=dbg_bot_mem),
          items=items
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"process_v2_exception: {e}")