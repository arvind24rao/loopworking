# app/services/recipients.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Mapping, Optional

import psycopg  # psycopg 3.x sync client


@dataclass(frozen=True)
class MessageKey:
    """Identifies a source human message we want to respond to."""
    message_id: str
    author_profile_id: str
    author_member_id: str


def _fetch_loop_id_for_member(conn: psycopg.Connection, author_member_id: str) -> Optional[str]:
    """
    Resolve loop_id from a member id.
    Schema per your DB:
      members(id UUID PK, loop_id UUID, profile_id UUID, role TEXT)
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT loop_id
            FROM members
            WHERE id = %s
            """,
            (author_member_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _fetch_recipients_for_loop(
    conn: psycopg.Connection,
    loop_id: str,
    exclude_profile_ids: Iterable[str],
) -> List[str]:
    """
    Compute intended recipients for a loop:
      recipients = all members in loop
                   MINUS {author, any known bots/system accounts}

    Since there’s no is_human flag in your schema, we filter by a blocklist of known bot/system IDs.
    """
    exclude = tuple(set(exclude_profile_ids))
    with conn.cursor() as cur:
        if exclude:
            # Parameter placeholders need to be expanded dynamically
            placeholders = ", ".join(["%s"] * len(exclude))
            cur.execute(
                f"""
                SELECT m.profile_id
                FROM members m
                WHERE m.loop_id = %s
                  AND m.profile_id NOT IN ({placeholders})
                """,
                (loop_id, *exclude),
            )
        else:
            cur.execute(
                """
                SELECT m.profile_id
                FROM members m
                WHERE m.loop_id = %s
                """,
                (loop_id,),
            )
        return [r[0] for r in cur.fetchall()]


def resolve_recipients_for_message(
    conn: psycopg.Connection,
    *,
    author_member_id: str,
    author_profile_id: str,
    known_bot_profile_ids: Iterable[str],
) -> List[str]:
    """
    Resolve recipients for ONE human message using canonical logic:

      loop_id := SELECT loop_id FROM members WHERE id = author_member_id
      recipients := SELECT profile_id FROM members WHERE loop_id = loop_id
                    EXCEPT {author_profile_id} ∪ {known_bot_profile_ids}

    Returns a list of profile_id (strings). Empty list means no targets.
    """
    loop_id = _fetch_loop_id_for_member(conn, author_member_id)
    if not loop_id:
        return []

    exclude = set(known_bot_profile_ids or [])
    exclude.add(author_profile_id)

    return _fetch_recipients_for_loop(conn, loop_id, exclude_profile_ids=exclude)


def resolve_recipients_batched(
    conn: psycopg.Connection,
    *,
    messages: Iterable[MessageKey],
    known_bot_profile_ids: Iterable[str],
) -> Mapping[str, List[str]]:
    """
    Batched variant:
      Input: iterable of MessageKey
      Output: dict[message_id] -> list[recipient_profile_id]

    This reduces round-trips by prefetching loop_ids and then recipients per loop once,
    and reusing the results for messages within the same loop.
    """
    msgs = list(messages)
    if not msgs:
        return {}

    # 1) Prefetch loop_id per author_member_id
    author_member_ids = tuple(m.author_member_id for m in msgs)
    loop_by_member: dict[str, str] = {}

    with conn.cursor() as cur:
        placeholders = ", ".join(["%s"] * len(author_member_ids))
        cur.execute(
            f"""
            SELECT id AS author_member_id, loop_id
            FROM members
            WHERE id IN ({placeholders})
            """,
            author_member_ids,
        )
        for row in cur.fetchall():
            loop_by_member[row[0]] = row[1]

    # 2) Group messages by loop_id
    by_loop: dict[str, List[MessageKey]] = {}
    for m in msgs:
        loop_id = loop_by_member.get(m.author_member_id)
        if not loop_id:
            continue
        by_loop.setdefault(loop_id, []).append(m)

    # 3) For each loop, fetch recipients once with the union exclude set
    bot_set = set(known_bot_profile_ids or [])
    recipients_by_loop: dict[str, List[str]] = {}
    for loop_id, group in by_loop.items():
        exclude = {m.author_profile_id for m in group} | bot_set
        recipients_by_loop[loop_id] = _fetch_recipients_for_loop(conn, loop_id, exclude)

    # 4) Map back per message_id (same loop recipients, but still exclude that message’s author)
    out: dict[str, List[str]] = {}
    for m in msgs:
        loop_id = loop_by_member.get(m.author_member_id)
        if not loop_id:
            out[m.message_id] = []
            continue
        base = recipients_by_loop.get(loop_id, [])
        if not base:
            out[m.message_id] = []
            continue
        # base already excludes all authors in the group; still guard locally
        out[m.message_id] = [pid for pid in base if pid != m.author_profile_id]
    return out