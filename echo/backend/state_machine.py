"""
Signal processor + state machine for Echo.

Signal processing (raw fac → named signals):
  - Rising-edge blink detection at 32 Hz
  - 0.8s debounce between signals
  - Triple-blink: 3 rising edges within 1.5s window
  - Single-blink: deferred 0.6s — fires only if no triple accumulates

States: IDLE → PROCESSING → TREE → CONFIRM_SEND → SENDING → IDLE
  - IDLE:         clench wakes; blinks ignored
  - PROCESSING:   waiting for Claude; no signals processed
  - TREE:         blink=advance, clench=select, triple=back/cancel, 10s timeout
  - CONFIRM_SEND: clench=send, triple=back to tree, 10s timeout → IDLE
  - SENDING:      transient, no signals
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

CLENCH_THRESHOLD = float(os.getenv("CLENCH_POWER_THRESHOLD", "0.6"))
BLINK_DEBOUNCE = float(os.getenv("BLINK_DEBOUNCE_SECONDS", "0.8"))
TRIPLE_WINDOW = float(os.getenv("TRIPLE_BLINK_WINDOW_SECONDS", "1.5"))
TREE_TIMEOUT = float(os.getenv("MENU_TIMEOUT_SECONDS", "10"))


class State(Enum):
    IDLE = auto()
    PROCESSING = auto()
    TREE = auto()
    CONFIRM_SEND = auto()
    SENDING = auto()


@dataclass
class TreeNode:
    node_type: str  # "confirm_intent" | "generate_decision_node" | "generate_final_reply"
    question: str
    options: list[dict]  # [{label, tts, description}]
    raw_data: dict


@dataclass
class PathEntry:
    node: TreeNode
    selected_index: int


@dataclass
class MachineState:
    state: State = State.IDLE
    current_node: TreeNode | None = None
    current_option: int = 0
    path_stack: list[PathEntry] = field(default_factory=list)
    final_reply: dict | None = None        # {reply_text, tts_confirmation}
    active_message: dict | None = None     # {body, sender, timestamp}
    timeout_task: asyncio.Task | None = None


class SignalProcessor:
    """Converts raw fac stream to named signals via rising-edge + debounce logic."""

    def __init__(self, signal_queue: asyncio.Queue):
        self._raw_queue = signal_queue
        self._out_queue: asyncio.Queue = asyncio.Queue()
        self._in_blink = False
        self._blink_timestamps: list[float] = []
        self._last_signal_time: float = 0.0
        self._pending_single_tasks: list[asyncio.Task] = []

    @property
    def output(self) -> asyncio.Queue:
        return self._out_queue

    async def run(self):
        while True:
            msg = await self._raw_queue.get()
            if msg["type"] == "raw_fac":
                await self._process_fac(msg["data"])
            elif msg["type"] == "injected_signal":
                # Keyboard/demo override bypasses all debounce
                await self._out_queue.put({"type": msg["signal"]})

    async def _process_fac(self, fac: list):
        eye_act = fac[0]   # "blink", "neutral", ...
        l_act = fac[3]     # "clench", "neutral", ...
        l_pow = fac[4]     # 0.0–1.0

        now = time.monotonic()

        # Clench detection (level-based with power threshold + debounce)
        if l_act == "clench" and l_pow >= CLENCH_THRESHOLD:
            if now - self._last_signal_time >= BLINK_DEBOUNCE:
                self._last_signal_time = now
                log.debug("Signal: clench (pow=%.2f)", l_pow)
                await self._out_queue.put({"type": "clench"})

        # Blink: rising-edge detection
        if eye_act == "blink" and not self._in_blink:
            self._in_blink = True
            if now - self._last_signal_time >= BLINK_DEBOUNCE:
                self._last_signal_time = now
                # Prune old timestamps
                self._blink_timestamps = [
                    t for t in self._blink_timestamps if now - t <= TRIPLE_WINDOW
                ]
                self._blink_timestamps.append(now)

                if len(self._blink_timestamps) >= 3:
                    log.debug("Signal: triple_blink")
                    await self._out_queue.put({"type": "triple_blink"})
                    self._blink_timestamps.clear()
                else:
                    # Defer single-blink emission — cancel if triple accumulates
                    blink_time = now
                    task = asyncio.create_task(
                        self._deferred_single_blink(blink_time)
                    )
                    self._pending_single_tasks.append(task)

        elif eye_act != "blink":
            self._in_blink = False

    async def _deferred_single_blink(self, blink_time: float):
        await asyncio.sleep(0.6)
        # Only emit if this blink wasn't consumed by a triple
        if blink_time in self._blink_timestamps:
            self._blink_timestamps.remove(blink_time)
            log.debug("Signal: single_blink")
            await self._out_queue.put({"type": "single_blink"})


class StateMachine:
    """
    Consumes named signals from SignalProcessor.output.
    Calls back into the orchestrator via async callbacks.
    """

    def __init__(
        self,
        signal_queue: asyncio.Queue,
        on_wake: Any,           # async () → None  — clench in IDLE
        on_select: Any,         # async (path_stack, node, option_idx) → None
        on_advance: Any,        # async (new_index, node) → None
        on_cancel: Any,         # async () → None
        on_back: Any,           # async (path_stack) → None
        on_confirm_send: Any,   # async () → None
        on_timeout: Any,        # async (state) → None
        broadcast: Any,         # async (msg: dict) → None
    ):
        self._signals = signal_queue
        self._on_wake = on_wake
        self._on_select = on_select
        self._on_advance = on_advance
        self._on_cancel = on_cancel
        self._on_back = on_back
        self._on_confirm_send = on_confirm_send
        self._on_timeout = on_timeout
        self._broadcast = broadcast
        self.ms = MachineState()

    # ── Public API called by orchestrator ──────────────────────────────────────

    def enter_processing(self):
        self._cancel_timeout()
        self.ms.state = State.PROCESSING
        log.info("State → PROCESSING")

    def enter_tree(self, node: TreeNode):
        self._cancel_timeout()
        self.ms.state = State.TREE
        self.ms.current_node = node
        self.ms.current_option = 0
        log.info("State → TREE (node_type=%s)", node.node_type)
        self._start_timeout()

    def enter_confirm_send(self, final_reply: dict):
        self._cancel_timeout()
        self.ms.state = State.CONFIRM_SEND
        self.ms.final_reply = final_reply
        log.info("State → CONFIRM_SEND")
        self._start_timeout()

    def enter_sending(self):
        self._cancel_timeout()
        self.ms.state = State.SENDING
        log.info("State → SENDING")

    def enter_idle(self):
        self._cancel_timeout()
        self.ms.state = State.IDLE
        self.ms.current_node = None
        self.ms.current_option = 0
        self.ms.path_stack.clear()
        self.ms.final_reply = None
        log.info("State → IDLE")

    def set_active_message(self, msg: dict):
        self.ms.active_message = msg

    # ── Signal dispatch loop ───────────────────────────────────────────────────

    async def run(self):
        while True:
            sig = await self._signals.get()
            await self._dispatch(sig["type"])

    async def _dispatch(self, signal: str):
        state = self.ms.state

        if state == State.IDLE:
            if signal == "clench":
                await self._on_wake()

        elif state == State.TREE:
            if signal == "single_blink":
                node = self.ms.current_node
                if node:
                    self.ms.current_option = (self.ms.current_option + 1) % 3
                    await self._on_advance(self.ms.current_option, node)
            elif signal == "clench":
                node = self.ms.current_node
                if node:
                    idx = self.ms.current_option
                    self.ms.path_stack.append(PathEntry(node=node, selected_index=idx))
                    await self._on_select(list(self.ms.path_stack), node, idx)
            elif signal == "triple_blink":
                if self.ms.path_stack:
                    popped = self.ms.path_stack.pop()
                    # Restore to the popped node
                    await self._on_back(list(self.ms.path_stack), popped.node)
                else:
                    await self._on_cancel()

        elif state == State.CONFIRM_SEND:
            if signal == "clench":
                await self._on_confirm_send()
            elif signal == "triple_blink":
                # Go back to the last tree node
                if self.ms.path_stack:
                    popped = self.ms.path_stack.pop()
                    await self._on_back(list(self.ms.path_stack), popped.node)
                else:
                    await self._on_cancel()

        # PROCESSING and SENDING: all signals ignored

    # ── Timeout ───────────────────────────────────────────────────────────────

    def _start_timeout(self):
        self.ms.timeout_task = asyncio.create_task(self._timeout_coro())

    def _cancel_timeout(self):
        if self.ms.timeout_task and not self.ms.timeout_task.done():
            self.ms.timeout_task.cancel()
        self.ms.timeout_task = None

    async def _timeout_coro(self):
        await asyncio.sleep(TREE_TIMEOUT)
        state = self.ms.state
        if state in (State.TREE, State.CONFIRM_SEND):
            log.info("Timeout in state %s", state)
            await self._on_timeout(state)
