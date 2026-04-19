"""
BCI Navigator — simple local app.

Two signals from Emotiv EPOC X:
  blink  → move highlight to next tile
  clench → select highlighted tile

Menus:
  home  → Music | Help
  music → Play | Pause | Next | Previous | ← Back
  help  → Coming Soon | ← Back

Apple Music controlled via osascript (macOS built-in).

Run:
  python app.py            # real BCI headset
  python app.py --mock     # keyboard: B=blink  J=clench  Ctrl-C to quit

Then open http://localhost:5001 in a browser.
"""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time

import websockets
from dotenv import load_dotenv
from flask import Flask, jsonify, send_from_directory

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger("bci")

# ── Menu definitions ───────────────────────────────────────────────────────────
# Each item: (display_label, action_key)
MENUS = {
    "home": [
        ("Music", "goto:music"),
        ("Help",  "goto:help"),
    ],
    "music": [
        ("Play",       "music:play"),
        ("Pause",      "music:pause"),
        ("Next",       "music:next"),
        ("Previous",   "music:prev"),
        ("← Back",     "goto:home"),
    ],
    "help": [
        ("Coming Soon", "noop"),
        ("← Back",      "goto:home"),
    ],
}

# ── State ──────────────────────────────────────────────────────────────────────
_state_lock = threading.Lock()
_state = {
    "menu": "home",
    "index": 0,
    "flash": None,       # brief feedback message shown in UI
    "flash_until": 0.0,
}


def _get_state():
    with _state_lock:
        s = dict(_state)
        items = [item[0] for item in MENUS[s["menu"]]]
        s["items"] = items
        if time.monotonic() > s["flash_until"]:
            s["flash"] = None
        return s


def _set_flash(msg: str):
    with _state_lock:
        _state["flash"] = msg
        _state["flash_until"] = time.monotonic() + 1.5


# ── Signal handling ────────────────────────────────────────────────────────────
CLENCH_THRESHOLD = float(os.getenv("CLENCH_POWER_THRESHOLD", "0.6"))
BLINK_DEBOUNCE = float(os.getenv("BLINK_DEBOUNCE_SECONDS", "0.8"))

_last_signal_time = 0.0
_in_blink = False


def on_blink():
    global _last_signal_time
    now = time.monotonic()
    if now - _last_signal_time < BLINK_DEBOUNCE:
        return
    _last_signal_time = now
    with _state_lock:
        menu = _state["menu"]
        n = len(MENUS[menu])
        _state["index"] = (_state["index"] + 1) % n
    log.info("Blink → index %d", _state["index"])


def on_clench():
    global _last_signal_time
    now = time.monotonic()
    if now - _last_signal_time < BLINK_DEBOUNCE:
        return
    _last_signal_time = now

    with _state_lock:
        menu = _state["menu"]
        idx = _state["index"]
        _, action = MENUS[menu][idx]

    log.info("Clench → action: %s", action)
    _execute(action)


def _execute(action: str):
    if action.startswith("goto:"):
        target = action.split(":", 1)[1]
        with _state_lock:
            _state["menu"] = target
            _state["index"] = 0
        log.info("Navigated to menu: %s", target)

    elif action.startswith("music:"):
        cmd = action.split(":", 1)[1]
        _music(cmd)

    elif action == "noop":
        _set_flash("Coming soon!")


_MUSIC_SCRIPTS = {
    "play":     'tell application "Music" to play',
    "pause":    'tell application "Music" to pause',
    "next":     'tell application "Music" to next track',
    "prev":     'tell application "Music" to previous track',
}

_MUSIC_LABELS = {
    "play": "Playing",
    "pause": "Paused",
    "next": "Next track",
    "prev": "Previous track",
}


def _music(cmd: str):
    script = _MUSIC_SCRIPTS.get(cmd)
    if not script:
        return
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            log.warning("osascript error: %s", result.stderr.strip())
            _set_flash("Music error")
        else:
            _set_flash(_MUSIC_LABELS.get(cmd, cmd))
            log.info("Music: %s", cmd)
    except Exception as e:
        log.error("osascript failed: %s", e)
        _set_flash("Music unavailable")


# ── Emotiv Cortex WebSocket ────────────────────────────────────────────────────
CORTEX_URL = os.getenv("CORTEX_URL", "wss://localhost:6868")
CLIENT_ID = os.getenv("EMOTIV_CLIENT_ID", "placeholder")
CLIENT_SECRET = os.getenv("EMOTIV_CLIENT_SECRET", "placeholder")

_BACKOFF_START = 1.0
_BACKOFF_MAX = 60.0


async def cortex_loop():
    backoff = _BACKOFF_START
    while True:
        try:
            ssl_ctx = False if "localhost" in CORTEX_URL else None
            async with websockets.connect(CORTEX_URL, ssl=ssl_ctx, ping_interval=20) as ws:
                backoff = _BACKOFF_START
                log.info("Connected to Cortex")
                await _cortex_auth(ws)
                await _cortex_read(ws)
        except Exception as e:
            log.warning("Cortex error: %s — retry in %.1fs", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)


_req_id = 1
_cortex_token = None
_session_id = None


async def _send(ws, method, params):
    global _req_id
    rid = _req_id
    _req_id += 1
    await ws.send(json.dumps({"id": rid, "jsonrpc": "2.0", "method": method, "params": params}))
    while True:
        raw = await ws.recv()
        resp = json.loads(raw)
        if resp.get("id") == rid:
            if "error" in resp:
                raise RuntimeError(f"{method} error: {resp['error']}")
            return resp.get("result", {})


async def _cortex_auth(ws):
    global _cortex_token, _session_id

    result = await _send(ws, "requestAccess", {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET})
    if not result.get("accessGranted", False):
        raise RuntimeError("Cortex access not granted — open EmotivApp and click Approve")

    if _cortex_token is None:
        result = await _send(ws, "authorize", {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET})
        _cortex_token = result["cortexToken"]

    result = await _send(ws, "createSession", {"cortexToken": _cortex_token, "status": "open"})
    _session_id = result["id"]

    await _send(ws, "subscribe", {
        "cortexToken": _cortex_token,
        "session": _session_id,
        "streams": ["fac"],
    })
    log.info("Subscribed to fac stream")


_in_blink_flag = False


async def _cortex_read(ws):
    global _in_blink_flag
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except Exception:
            continue

        if "fac" not in msg:
            continue

        fac = msg["fac"]
        eye_act = fac[0]   # "blink", "neutral", ...
        l_act   = fac[3]   # "clench", "neutral", ...
        l_pow   = fac[4]   # 0.0–1.0

        # Clench
        if l_act == "clench" and l_pow >= CLENCH_THRESHOLD:
            on_clench()

        # Blink — rising edge only
        if eye_act == "blink" and not _in_blink_flag:
            _in_blink_flag = True
            on_blink()
        elif eye_act != "blink":
            _in_blink_flag = False


# ── Mock BCI (keyboard) ────────────────────────────────────────────────────────
async def mock_loop():
    log.info("Mock BCI: B=blink  J=clench  (type + Enter)")
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        key = line.strip().upper()
        if key == "B":
            on_blink()
        elif key == "J":
            on_clench()


# ── Flask server ───────────────────────────────────────────────────────────────
flask_app = Flask(__name__, static_folder=None)


@flask_app.route("/")
def index():
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), "ui"),
        "index.html"
    )


@flask_app.route("/state")
def state():
    return jsonify(_get_state())


@flask_app.route("/signal/<name>", methods=["POST"])
def signal(name):
    """HTTP endpoint for on-screen button overrides (demo / debug)."""
    if name == "blink":
        on_blink()
    elif name == "clench":
        on_clench()
    return ("", 204)


def start_flask():
    flask_app.run(host="0.0.0.0", port=5001, debug=False, use_reloader=False)


# ── Entry point ────────────────────────────────────────────────────────────────
async def _async_main(mock: bool):
    if mock:
        await mock_loop()
    else:
        await cortex_loop()


def main():
    parser = argparse.ArgumentParser(description="BCI Navigator")
    parser.add_argument("--mock", action="store_true", help="Keyboard mode: B=blink J=clench")
    args = parser.parse_args()

    threading.Thread(target=start_flask, daemon=True, name="flask").start()
    log.info("Open http://localhost:5001 in your browser")

    asyncio.run(_async_main(mock=args.mock))


if __name__ == "__main__":
    main()
