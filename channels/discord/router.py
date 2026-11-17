"""
Discord channel adapter for RagLeap Core.

Receives Discord messages via webhook, answers them using the core RAG
pipeline (core.chat.ask), and sends the response back. Single-tenant:
one bot, one document set, configured entirely via .env.
"""
import os
import logging

import requests

from core.chat import ask
from core.workflows import call_n8n_workflows
from core.autonomy import process_approval_from
from core.employees.feedback import record_last_reply, get_last_reply, detect_feedback_command
from core.employees.tools import TOOL_REGISTRY
from core.employees.channel_roles import resolve_role

logger = logging.getLogger(__name__)

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY")

DISCORD_API_URL = "https://discord.com/api/v10"
MAX_DISCORD_MESSAGE_LENGTH = 2000


def verify_discord_signature(signature: str, timestamp: str, body: bytes) -> bool:
    """Verify a Discord interaction request using Ed25519, per Discord's official algorithm."""
    if not signature or not timestamp or not DISCORD_PUBLIC_KEY:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(DISCORD_PUBLIC_KEY))
        message = timestamp.encode("utf-8") + body
        public_key.verify(bytes.fromhex(signature), message)
        return True
    except Exception as e:
        logger.warning(f"Discord signature verification failed: {e}")
        return False


def send_discord_message(channel_id, message_text: str) -> bool:
    """Send a message to a Discord channel via the Bot API."""
    if not DISCORD_BOT_TOKEN:
        logger.error("Discord bot not configured — set DISCORD_BOT_TOKEN in .env")
        return False

    if len(message_text) > MAX_DISCORD_MESSAGE_LENGTH:
        message_text = message_text[:MAX_DISCORD_MESSAGE_LENGTH - 20] + "...\n(truncated)"

    url = f"{DISCORD_API_URL}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}
    response = requests.post(url, headers=headers, json={"content": message_text}, timeout=30)

    if response.status_code in (200, 201):
        logger.info(f"Discord: message sent to channel {channel_id}")
        return True

    logger.error(f"Discord send failed: {response.status_code} {response.text}")
    return False


def handle_incoming_message(channel_id, message_text: str, private: bool = True) -> str:
    """
    Process an incoming Discord message: ask the core RAG pipeline,
    send the answer back to the channel, and return the answer text.
    """
    if not message_text or not message_text.strip():
        reply = "Please send a question and I'll do my best to answer from the documents I have."
        send_discord_message(channel_id, reply)
        return reply

    try:
        approval_reply = process_approval_from("discord", channel_id, message_text, private=private)
        if approval_reply is not None:
            send_discord_message(channel_id, approval_reply)
            return approval_reply
    except Exception as e:
        logger.warning(f"Discord: approval-check error: {e}")

    feedback = detect_feedback_command(message_text)
    if feedback is not None:
        last_ids = get_last_reply("discord", str(channel_id))
        if last_ids:
            from core.employees.learning import record_role_memory_outcome
            record_role_memory_outcome(last_ids, success=feedback)
        reply = "Thanks for the feedback!" if feedback else "Thanks, I'll do better next time."
        send_discord_message(channel_id, reply)
        return reply

    escalation = TOOL_REGISTRY["escalate_to_owner"].handler(
        channel="discord", target=str(channel_id), message_text=message_text
    )
    if escalation and escalation.get("status") in ("executed", "pending_approval"):
        reply = "I've flagged this for a team member -- they'll follow up with you soon."
        send_discord_message(channel_id, reply)
        return reply
    # else: escalation not configured for this action/channel -- fall through to a normal answer

    try:
        result = ask(message_text, role=resolve_role("discord", message_text))
        answer = result.get("answer", "Sorry, I couldn't generate an answer.")
        record_last_reply("discord", str(channel_id), result.get("role_memory_ids", []))
    except Exception as e:
        logger.error(f"Discord: error answering message from channel {channel_id}: {e}", exc_info=True)
        answer = "Sorry, something went wrong answering your question. Please try again."

    send_discord_message(channel_id, answer)

    try:
        call_n8n_workflows(channel="discord", message=message_text, ai_reply=answer)
    except Exception as e:
        logger.warning(f"n8n workflow trigger failed (non-fatal): {e}")

    return answer
