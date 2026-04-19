"""
Emotiv Cortex WebSocket client.

Auth flow: requestAccess → authorize → createSession → subscribe(fac)
fac payload: [eyeAct, uAct, uPow, lAct, lPow]
  - eyeAct index 0: "blink", "neutral", "wink_left", "wink_right", etc.
  - lAct  index 3: "clench", "smile", "neutral", etc.
  - lPow  index 4: intensity 0.0–1.0

Emits signal dicts onto `signal_queue`:
  {"type": "clench"}
  {"type": "blink"}
  {"type": "raw_fac", "data": [...], "time": float}
"""

import asyncio
import json
import logging
import os
import time

import websockets
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

CORTEX_URL = os.getenv("CORTEX_URL", "wss://localhost:6868")
CLIENT_ID = os.getenv("EMOTIV_CLIENT_ID", "placeholder_client_id")
CLIENT_SECRET = os.getenv("EMOTIV_CLIENT_SECRET", "placeholder_client_secret")

# Reconnect backoff
_BACKOFF_START = 1.0
_BACKOFF_MAX = 60.0
_BACKOFF_MULT = 2.0


class CortexClient:
    def __init__(self, signal_queue: asyncio.Queue):
        self.signal_queue = signal_queue
        self._stop = False
        self._cortex_token: str | None = None
        self._session_id: str | None = None
        self._ws = None
        self._req_id = 1

    def stop(self):
        self._stop = True

    async def run(self):
        backoff = _BACKOFF_START
        while not self._stop:
            try:
                log.info("Connecting to Cortex at %s", CORTEX_URL)
                # Cortex runs a self-signed cert; disable SSL verification for localhost
                ssl_ctx = False if "localhost" in CORTEX_URL else None
                async with websockets.connect(
                    CORTEX_URL,
                    ssl=ssl_ctx,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    backoff = _BACKOFF_START
                    log.info("Connected to Cortex")
                    await self._auth_and_subscribe(ws)
                    await self._read_loop(ws)
            except Exception as e:
                log.warning("Cortex connection error: %s — retrying in %.1fs", e, backoff)
                self._cortex_token = None
                self._session_id = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * _BACKOFF_MULT, _BACKOFF_MAX)

    # ── Auth sequence ──────────────────────────────────────────────────────────

    async def _send(self, ws, method: str, params: dict) -> dict:
        req_id = self._req_id
        self._req_id += 1
        msg = {"id": req_id, "jsonrpc": "2.0", "method": method, "params": params}
        await ws.send(json.dumps(msg))
        # Wait for the matching response (id match)
        while True:
            raw = await ws.recv()
            resp = json.loads(raw)
            if resp.get("id") == req_id:
                if "error" in resp:
                    raise RuntimeError(f"Cortex error on {method}: {resp['error']}")
                return resp.get("result", {})

    async def _auth_and_subscribe(self, ws):
        # Step 1: requestAccess (one-time; user must click Approve in EmotivApp)
        log.info("Requesting Cortex access...")
        result = await self._send(ws, "requestAccess", {
            "clientId": CLIENT_ID,
            "clientSecret": CLIENT_SECRET,
        })
        if not result.get("accessGranted", False):
            log.warning("Access not yet granted — open EmotivApp and click Approve, then restart")
            raise RuntimeError("Cortex access not granted")

        # Step 2: authorize → get cortex token
        if self._cortex_token is None:
            log.info("Authorizing...")
            result = await self._send(ws, "authorize", {
                "clientId": CLIENT_ID,
                "clientSecret": CLIENT_SECRET,
            })
            self._cortex_token = result["cortexToken"]
            log.info("Got cortex token")

        # Step 3: createSession
        log.info("Creating session...")
        result = await self._send(ws, "createSession", {
            "cortexToken": self._cortex_token,
            "status": "open",
        })
        self._session_id = result["id"]
        log.info("Session created: %s", self._session_id)

        # Step 4: subscribe to fac stream
        log.info("Subscribing to fac stream...")
        await self._send(ws, "subscribe", {
            "cortexToken": self._cortex_token,
            "session": self._session_id,
            "streams": ["fac"],
        })
        log.info("Subscribed to fac stream — streaming signals")

    # ── Data read loop ─────────────────────────────────────────────────────────

    async def _read_loop(self, ws):
        async for raw in ws:
            if self._stop:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # fac stream events look like: {"fac": [...], "sid": "...", "time": ...}
            if "fac" in msg:
                fac = msg["fac"]
                await self.signal_queue.put({
                    "type": "raw_fac",
                    "data": fac,
                    "time": msg.get("time", time.monotonic()),
                })
