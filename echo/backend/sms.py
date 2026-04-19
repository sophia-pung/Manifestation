"""
Twilio SMS: inbound webhook (Flask) + outbound send.

Flask runs in a daemon thread. Inbound messages are pushed onto `sms_queue`
via asyncio.run_coroutine_threadsafe so the main asyncio loop receives them.

Usage:
    start_flask(loop, sms_queue)  — call once at startup
    send_sms(to, body)            — async, call from asyncio loop
"""

import asyncio
import logging
import os
import threading

from dotenv import load_dotenv
from flask import Flask, request
from twilio.rest import Client

load_dotenv()

log = logging.getLogger(__name__)

ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
FROM_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))

flask_app = Flask("echo_sms")
_twilio_client: Client | None = None


def _get_twilio() -> Client:
    global _twilio_client
    if _twilio_client is None:
        _twilio_client = Client(ACCOUNT_SID, AUTH_TOKEN)
    return _twilio_client


@flask_app.route("/sms", methods=["POST"])
def receive_sms():
    body = request.form.get("Body", "").strip()
    sender = request.form.get("From", "Unknown")
    log.info("Inbound SMS from %s: %s", sender, body[:80])
    if _loop and _sms_queue:
        asyncio.run_coroutine_threadsafe(
            _sms_queue.put({"body": body, "sender": sender}),
            _loop,
        )
    return ("", 204)


_loop: asyncio.AbstractEventLoop | None = None
_sms_queue: asyncio.Queue | None = None


def start_flask(loop: asyncio.AbstractEventLoop, sms_queue: asyncio.Queue):
    global _loop, _sms_queue
    _loop = loop
    _sms_queue = sms_queue
    thread = threading.Thread(
        target=lambda: flask_app.run(
            host="0.0.0.0",
            port=FLASK_PORT,
            debug=False,
            use_reloader=False,
        ),
        daemon=True,
        name="flask-sms",
    )
    thread.start()
    log.info("Flask SMS webhook listening on port %d", FLASK_PORT)


async def send_sms(to: str, body: str) -> bool:
    if not ACCOUNT_SID or not AUTH_TOKEN or not FROM_NUMBER:
        log.error("Twilio credentials not configured — cannot send SMS")
        return False
    try:
        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(
            None,
            lambda: _get_twilio().messages.create(
                body=body,
                from_=FROM_NUMBER,
                to=to,
            ),
        )
        log.info("SMS sent (SID=%s) to %s", msg.sid, to)
        return True
    except Exception as e:
        log.error("Failed to send SMS: %s", e)
        return False
