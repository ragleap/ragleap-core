"""
Standalone WebSocket server for the RagLeap Core voice channel.

Twilio connects here directly via Media Streams — this is separate from
the main FastAPI app (core/api.py) because Twilio's real-time audio
protocol needs a raw WebSocket server, not an HTTP request/response route.

Run with: python3 -m channels.voice.server
Twilio's <Connect><Stream> TwiML must point to this server's address WITH the secret token:
wss://your-domain.com:8765/<VOICE_WS_TOKEN>. Without VOICE_WS_TOKEN the server refuses every call (see channels/voice/auth.py).
"""
import asyncio
import logging
import os
import time
from http import HTTPStatus

import websockets

from channels.voice import auth
from channels.voice.router import handle_call

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VOICE_SERVER_PORT = int(os.environ.get("VOICE_SERVER_PORT", "8765"))

_active = 0
_last_refusal_log = [None]


def _log_refusal():
    """One line a minute at most, so a flood of bad connections cannot flood the log. The token is never logged."""
    now = time.monotonic()
    if _last_refusal_log[0] is None or now - _last_refusal_log[0] >= 60:
        _last_refusal_log[0] = now
        if auth.configured_token():
            logger.warning("Voice: refused a connection without a valid token")
        else:
            logger.error("Voice: refused a connection because VOICE_WS_TOKEN is not set (16 characters or more are needed)")


def check_request(connection, request):
    """Runs before the WebSocket upgrade: a wrong or missing token gets a plain 401 and never becomes a call."""
    if auth.is_allowed(request.path):
        return None
    _log_refusal()
    return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")


def _request_path(ws):
    request = getattr(ws, "request", None)
    return getattr(request, "path", None) or getattr(ws, "path", "") or ""


async def serve_call(ws):
    """One call: checks the token again (in case a connection ever skips check_request), applies the limits, then handles it."""
    global _active
    if not auth.is_allowed(_request_path(ws)):
        await ws.close(code=1008, reason="unauthorized")
        return
    if _active >= auth.max_calls():
        logger.warning("Voice: refused a call, too many calls at the same time")
        await ws.close(code=1013, reason="busy")
        return
    _active += 1
    try:
        await asyncio.wait_for(handle_call(ws), timeout=auth.max_call_seconds())
    except asyncio.TimeoutError:
        logger.info("Voice: a call reached the time limit and was ended")
        try:
            await ws.close(code=1000, reason="time limit")
        except Exception:
            pass
    finally:
        _active -= 1


async def main():
    if not auth.configured_token():
        logger.error("VOICE_WS_TOKEN is not set (16 characters or more are needed): the voice server will refuse every call. "
                     "Set it in .env and use wss://your-domain.com:%s/<token> in the Twilio setup." % VOICE_SERVER_PORT)
    logger.info(f"Voice server starting on port {VOICE_SERVER_PORT}...")
    async with websockets.serve(serve_call, "0.0.0.0", VOICE_SERVER_PORT, process_request=check_request,
                                max_size=auth.MAX_MESSAGE_BYTES):
        logger.info(f"Voice server listening on 0.0.0.0:{VOICE_SERVER_PORT}")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
