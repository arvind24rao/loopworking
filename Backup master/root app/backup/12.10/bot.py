# /Users/arvindrao/loop/loop-api/app/bot.py
import os
from typing import List, Dict, Any, Optional
from loguru import logger

from .supa import supa, SUPABASE_URL
from .crypto import encrypt_plaintext
from .llm import generate_reply

BOT_PROFILE_ID = os.getenv("BOT_PROFILE_ID")

# Seed participants for the MVP (make dynamic later if you want)
USER_A = "b8d99c3c-0d3a-4773-a324-a6bc60dee64e"
USER_B = "0dd8b495-6a25-440d-a6e4-d8b7a77bc688"

def _select_unprocessed(thread_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
    params = {
        "select": "id,thread_id,created_by,content_ciphertext,created_at",
        "audience": "eq.inbox_to_bot",
        "order": "created_at.asc,id.asc",
        "limit": str(limit),
        "bot_processed_at": "is.null",
    }
    if thread_id:
        params["thread_id"] = f"eq.{thread_id}"
    r = supa.client.get(f"{SUPABASE_URL}/rest/v1/messages", params=params)
    r.raise_for_status()
    return r.json()

def _mark_processed(ids: List[str]) -> None:
    if not ids:
        return
    import datetime
    for mid in ids:
        params = {"id": f"eq.{mid}"}
        payload = {"bot_processed_at": datetime.datetime.utcnow().isoformat() + "Z"}
        headers = {"Prefer": "return=representation"}
        r = supa.client.patch(f"{SUPABASE_URL}/rest/v1/messages", params=params, json=payload, headers=headers)
        r.raise_for_status()

def _other_party(profile_id: str) -> Optional[str]:
    if profile_id == USER_A:
        return USER_B
    if profile_id == USER_B:
        return USER_A
    return None

def _labels_for(sender: str, recipient: str) -> (str, str):
    # Return ('recipient_label','sender_label') as 'A'/'B'
    recipient_label = "A" if recipient == USER_A else "B"
    sender_label = "A" if sender == USER_A else "B"
    return recipient_label, sender_label

def _loop_id_for_thread(thread_id: str) -> str:
    row = supa.select_one("threads", {"id": thread_id}, select="loop_id")
    return row["loop_id"]

def _member_id_for(profile_id: str, loop_id: str) -> str:
    return supa.rpc("member_id_for", {"u": profile_id, "l": loop_id})

def _insert_bot_dm(thread_id: str, recipient: str, content_plain: str) -> str:
    loop_id = _loop_id_for_thread(thread_id)
    bot_member_id = _member_id_for(BOT_PROFILE_ID, loop_id)
    content_ciphertext, dek_wrapped, nonce, aead_tag = encrypt_plaintext(content_plain)
    rec = supa.insert(
        "messages",
        {
            "thread_id": thread_id,
            "created_by": BOT_PROFILE_ID,
            "author_member_id": bot_member_id,
            "role": "user",
            "channel": "inbox",
            "visibility": "private",
            "audience": "bot_to_user",
            "recipient_profile_id": recipient,
            "content_ciphertext": content_ciphertext,
            "dek_wrapped": None,
            "nonce": None,
            "aead_tag": None,
            "lang": "en",
        },
    )
    return rec["id"]

def process_queue(thread_id: Optional[str], limit: int, dry_run: bool = False) -> Dict[str, Any]:
    rows = _select_unprocessed(thread_id, limit)
    processed_ids: List[str] = []
    outputs: List[Dict[str, Any]] = []

    for row in rows:
        sender = row["created_by"]
        recipient = _other_party(sender)
        if not recipient:
            outputs.append({"message_id": row["id"], "skipped": True, "reason": "unknown sender"})
            continue

        # Build compact context from this sender in this thread
        ctx_params = {
            "select": "content_ciphertext,created_at",
            "thread_id": f"eq.{row['thread_id']}",
            "created_by": f"eq.{sender}",
            "audience": "eq.inbox_to_bot",
            "order": "created_at.asc",
            "limit": "5",
        }
        rr = supa.client.get(f"{SUPABASE_URL}/rest/v1/messages", params=ctx_params)
        rr.raise_for_status()
        ctx_rows = rr.json()
        context_messages = [r["content_ciphertext"] for r in ctx_rows]

        # Perspective-safe LLM call
        recipient_label, sender_label = _labels_for(sender, recipient)
        try:
            reply_text = generate_reply(context_messages, recipient_label=recipient_label, sender_label=sender_label)
        except Exception as e:
            logger.error("LLM call failed: {}", e)
            outputs.append({"message_id": row["id"], "skipped": True, "reason": f"llm_error: {e}"})
            continue

        new_id = None
        if not dry_run:
            try:
                new_id = _insert_bot_dm(row["thread_id"], recipient, reply_text)
            except Exception as e:
                logger.error("Insert bot DM failed: {}", e)
                outputs.append({"message_id": row["id"], "skipped": True, "reason": f"insert_error: {e}"})
                continue

        processed_ids.append(row["id"])
        outputs.append({
            "message_id": row["id"],
            "recipient": recipient,
            "dm_id": new_id,
            "preview": reply_text[:160],
            "dry_run": dry_run,
        })

    if processed_ids and not dry_run:
        _mark_processed(processed_ids)

    return {"count": len(rows), "processed": len(processed_ids), "dry_run": dry_run, "items": outputs}