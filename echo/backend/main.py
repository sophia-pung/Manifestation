"""
Echo orchestrator.

Wires together:
  - CortexClient          (BCI signals via WebSocket)
  - SignalProcessor       (raw fac → named signals)
  - StateMachine          (state transitions + callbacks)
  - ReplyAgent            (Claude tool-call tree)
  - Validator             (output validation)
  - PathMemory            (past path suggestions)
  - Voice                 (ElevenLabs TTS)
  - SMS                   (Twilio inbound/outbound via Flask thread)
  - WebSocket server      (real-time frontend sync)

CLI flags:
  --mock-bci              Use keyboard stdin instead of Cortex headset
                          J = clench, B = single blink, T = triple blink
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

import websockets
from dotenv import load_dotenv

load_dotenv()

import cortex_client as cortex_module
import path_memory as pm_module
import reply_agent
import sms as sms_module
import validator as val_module
import voice
from state_machine import (
    MachineState,
    SignalProcessor,
    State,
    StateMachine,
    TreeNode,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-20s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger("echo.main")

BACKEND_WS_PORT = int(os.getenv("BACKEND_WS_PORT", "8765"))
PATIENT_PHONE = os.getenv("PATIENT_PHONE_NUMBER", "")

# ── Global shared state ────────────────────────────────────────────────────────

_ws_clients: set[Any] = set()
_conversation_history: list[dict] = []
_path_memory = pm_module.PathMemory()
_current_message: dict | None = None


# ── WebSocket broadcast ────────────────────────────────────────────────────────

async def broadcast(msg: dict):
    if not _ws_clients:
        return
    data = json.dumps(msg)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send(data)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


# ── Orchestrator callbacks ─────────────────────────────────────────────────────

async def on_wake():
    global _current_message
    if not _current_message:
        log.info("Clench received but no active message")
        await voice.speak("No message to reply to.")
        return

    log.info("Wake: starting reply tree for message from %s", _current_message["sender"])
    await voice.speak("Generating your reply options.")
    sm.enter_processing()
    await broadcast({"type": "state_change", "state": "PROCESSING"})
    await _run_next_tree_level()


async def on_select(path_stack, node: TreeNode, option_idx: int):
    selected_opt = node.options[option_idx]
    log.info("Selected option %d: %s", option_idx, selected_opt["label"])

    # Build path_taken for Claude
    path_taken = [
        {
            "question": entry.node.question,
            "selected_label": entry.node.options[entry.selected_index]["label"],
            "selected_description": entry.node.options[entry.selected_index].get("description", ""),
        }
        for entry in path_stack
    ]

    await broadcast({
        "type": "path_updated",
        "path_so_far": [
            {"question": p["question"], "selected_label": p["selected_label"]}
            for p in path_taken
        ],
    })

    sm.enter_processing()
    await broadcast({"type": "state_change", "state": "PROCESSING"})
    await _run_next_tree_level(path_taken=path_taken)


async def on_advance(new_index: int, node: TreeNode):
    opt = node.options[new_index]
    await broadcast({"type": "option_changed", "index": new_index})
    await voice.speak(opt["tts"])


async def on_cancel():
    log.info("Cancelled — returning to IDLE")
    sm.enter_idle()
    await broadcast({"type": "state_change", "state": "IDLE"})
    await voice.speak("Cancelled.")


async def on_back(path_stack, node: TreeNode):
    log.info("Back — restoring node: %s", node.question)
    sm.enter_tree(node)
    await broadcast({"type": "state_change", "state": "TREE"})
    await broadcast({
        "type": "tree_node",
        "node_type": node.node_type,
        "question": node.question,
        "options": [{"label": o["label"], "tts": o["tts"]} for o in node.options],
        "depth": len(path_stack),
        "path_so_far": [
            {
                "question": e.node.question,
                "selected_label": e.node.options[e.selected_index]["label"],
            }
            for e in path_stack
        ],
    })
    await voice.speak(f"{node.question}. {node.options[0]['tts']}")


async def on_confirm_send():
    global _current_message
    if not sm.ms.final_reply or not _current_message:
        return
    reply_text = sm.ms.final_reply["reply_text"]
    sm.enter_sending()
    await broadcast({"type": "state_change", "state": "SENDING"})
    await voice.speak("Sending.")

    to_number = _current_message.get("sender", PATIENT_PHONE)
    success = await sms_module.send_sms(to_number, reply_text)

    if success:
        ts = time.time()
        _conversation_history.append({
            "body": reply_text,
            "outbound": True,
            "timestamp": ts,
        })
        # Record path for memory
        path_taken = [
            {
                "question": e.node.question,
                "selected_label": e.node.options[e.selected_index]["label"],
            }
            for e in sm.ms.path_stack
        ]
        _path_memory.record(_current_message["body"], path_taken, reply_text)

        await broadcast({"type": "sms_sent", "text": reply_text, "timestamp": ts})
        await voice.speak(f"Sent.")
    else:
        await voice.speak("Something went wrong sending. Please try again.")

    _current_message = None
    sm.enter_idle()
    await broadcast({"type": "state_change", "state": "IDLE"})


async def on_timeout(state: State):
    log.info("Timeout in state %s", state)
    sm.enter_idle()
    await broadcast({"type": "state_change", "state": "IDLE"})
    await voice.speak("Timed out. Clench to try again.")


# ── Core tree logic ────────────────────────────────────────────────────────────

async def _run_next_tree_level(path_taken: list[dict] | None = None):
    global _current_message
    if path_taken is None:
        path_taken = []

    msg = _current_message
    if not msg:
        sm.enter_idle()
        return

    depth = len(path_taken)
    memory_suggestions = _path_memory.suggest(msg["body"])

    await broadcast({"type": "tool_call", "tool": "thinking...", "reasoning": f"depth={depth}"})

    try:
        result = await reply_agent.get_next_node(
            message=msg["body"],
            sender=msg["sender"],
            history=_conversation_history,
            path_taken=path_taken,
            memory_suggestions=memory_suggestions,
            depth=depth,
        )
    except Exception as e:
        log.error("Claude API error: %s", e)
        await broadcast({"type": "error", "message": str(e)})
        sm.enter_idle()
        await broadcast({"type": "state_change", "state": "IDLE"})
        await voice.speak("Something went wrong. Please try again.")
        return

    # Validate
    try:
        val_module.validate_node(result)
    except val_module.ValidationError as e:
        log.warning("Validation failed: %s — retrying once", e)
        await broadcast({"type": "validation_event", "passed": False, "issue": str(e)})
        # Retry once
        try:
            result = await reply_agent.get_next_node(
                message=msg["body"],
                sender=msg["sender"],
                history=_conversation_history,
                path_taken=path_taken,
                memory_suggestions=memory_suggestions,
                depth=depth,
            )
            val_module.validate_node(result)
        except Exception as e2:
            log.error("Second attempt also failed: %s", e2)
            sm.enter_idle()
            await broadcast({"type": "state_change", "state": "IDLE"})
            await voice.speak("Let me try again later. Clench to retry.")
            return
    else:
        await broadcast({"type": "validation_event", "passed": True, "issue": ""})

    tool_type = result["type"]
    data = result["data"]

    await broadcast({
        "type": "tool_call",
        "tool": tool_type,
        "reasoning": data.get("reasoning") or data.get("reason_for_ambiguity") or data.get("path_summary", ""),
    })

    if tool_type == "generate_final_reply":
        # Hallucination check
        if not val_module.check_hallucination(data["reply_text"], _conversation_history):
            log.warning("Hallucination detected in reply — using with caution")

        sm.enter_confirm_send(data)
        await broadcast({"type": "state_change", "state": "CONFIRM_SEND"})
        await broadcast({
            "type": "confirm_send",
            "reply_text": data["reply_text"],
            "tts_confirmation": data["tts_confirmation"],
        })
        await voice.speak(
            f"Your reply: {data['tts_confirmation']}. "
            "Clench to send. Triple blink to go back."
        )

    else:
        # Decision or intent-confirm node
        node = TreeNode(
            node_type=tool_type,
            question=data["question"],
            options=data["options"],
            raw_data=data,
        )
        sm.enter_tree(node)
        await broadcast({"type": "state_change", "state": "TREE"})
        await broadcast({
            "type": "tree_node",
            "node_type": tool_type,
            "question": data["question"],
            "options": [{"label": o["label"], "tts": o["tts"]} for o in data["options"]],
            "depth": depth,
            "path_so_far": [
                {"question": p["question"], "selected_label": p["selected_label"]}
                for p in path_taken
            ],
        })
        await voice.speak(f"{data['question']}. {data['options'][0]['tts']}")


# ── Incoming SMS handler ───────────────────────────────────────────────────────

async def handle_incoming_sms(msg: dict):
    global _current_message
    ts = time.time()
    msg["timestamp"] = ts
    _current_message = msg
    _conversation_history.append({"body": msg["body"], "sender": msg["sender"], "timestamp": ts, "outbound": False})

    await broadcast({"type": "sms_received", "sender": msg["sender"], "body": msg["body"], "timestamp": ts})
    await voice.speak(f"New message from {msg['sender']}: {msg['body']}. Clench to reply.")
    sm.enter_idle()
    await broadcast({"type": "state_change", "state": "IDLE"})


# ── WebSocket server (frontend) ────────────────────────────────────────────────

async def ws_handler(websocket):
    _ws_clients.add(websocket)
    log.info("Frontend connected (%d total)", len(_ws_clients))
    # Send current state on connect
    await websocket.send(json.dumps({
        "type": "state_change",
        "state": sm.ms.state.name,
    }))
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            await _handle_frontend_msg(msg)
    except Exception:
        pass
    finally:
        _ws_clients.discard(websocket)
        log.info("Frontend disconnected (%d remaining)", len(_ws_clients))


async def _handle_frontend_msg(msg: dict):
    t = msg.get("type")
    if t == "keyboard_override":
        signal = msg.get("signal")
        if signal in ("clench", "single_blink", "triple_blink"):
            await signal_proc.output.put({"type": signal})
    elif t == "inject_sms":
        body = msg.get("body", "Hello!")
        sender = msg.get("sender", "Demo")
        await handle_incoming_sms({"body": body, "sender": sender})
    elif t == "request_status":
        await broadcast({"type": "state_change", "state": sm.ms.state.name})


# ── Mock BCI (keyboard stdin) ─────────────────────────────────────────────────

async def mock_bci_loop():
    log.info("Mock BCI mode: J=clench, B=blink, T=triple_blink. Type and press Enter.")
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        key = line.strip().upper()
        if key == "J":
            await signal_proc.output.put({"type": "clench"})
        elif key == "B":
            await signal_proc.output.put({"type": "single_blink"})
        elif key == "T":
            await signal_proc.output.put({"type": "triple_blink"})
        elif key.startswith("SMS:"):
            body = line.strip()[4:].strip()
            await handle_incoming_sms({"body": body, "sender": "+15550001234"})


# ── Main ──────────────────────────────────────────────────────────────────────

# Module-level singletons (initialized in main())
raw_signal_queue: asyncio.Queue | None = None
signal_proc: SignalProcessor | None = None
sm: StateMachine | None = None


async def _main(mock: bool):
    global raw_signal_queue, signal_proc, sm

    raw_signal_queue = asyncio.Queue()
    signal_proc = SignalProcessor(raw_signal_queue)

    sm = StateMachine(
        signal_queue=signal_proc.output,
        on_wake=on_wake,
        on_select=on_select,
        on_advance=on_advance,
        on_cancel=on_cancel,
        on_back=on_back,
        on_confirm_send=on_confirm_send,
        on_timeout=on_timeout,
        broadcast=broadcast,
    )

    sms_queue: asyncio.Queue = asyncio.Queue()

    # Start Flask SMS webhook
    loop = asyncio.get_event_loop()
    sms_module.start_flask(loop, sms_queue)

    # Start WebSocket server for frontend
    ws_server = await websockets.serve(ws_handler, "0.0.0.0", BACKEND_WS_PORT)
    log.info("WebSocket server on ws://localhost:%d", BACKEND_WS_PORT)

    async def sms_listener():
        while True:
            msg = await sms_queue.get()
            await handle_incoming_sms(msg)

    tasks = [
        asyncio.create_task(signal_proc.run(), name="signal-processor"),
        asyncio.create_task(sm.run(), name="state-machine"),
        asyncio.create_task(sms_listener(), name="sms-listener"),
    ]

    if mock:
        tasks.append(asyncio.create_task(mock_bci_loop(), name="mock-bci"))
    else:
        cortex = cortex_module.CortexClient(raw_signal_queue)
        tasks.append(asyncio.create_task(cortex.run(), name="cortex-client"))

    log.info("Echo is running. %s", "Mock BCI mode." if mock else "BCI mode.")
    try:
        await asyncio.gather(*tasks)
    finally:
        ws_server.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Echo BCI SMS Reply System")
    parser.add_argument("--mock-bci", action="store_true", help="Use keyboard instead of headset")
    args = parser.parse_args()
    asyncio.run(_main(mock=args.mock_bci))
