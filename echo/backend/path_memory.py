"""
In-memory path history.

Stores successful send paths keyed by a coarse message fingerprint (first 3 words).
Surfaces relevant past paths to Claude as a context string.
Resets on server restart — no persistence needed for MVP.
"""

import logging
import time
from collections import defaultdict

log = logging.getLogger(__name__)

_MAX_RECORDS_PER_KEY = 5


def _fingerprint(message: str) -> str:
    words = [w.lower().strip(".,!?") for w in message.split() if w.strip()]
    # Remove common stop words for better key grouping
    stop = {"are", "you", "i", "is", "the", "a", "an", "do", "did", "can"}
    content_words = [w for w in words if w not in stop]
    return " ".join(content_words[:3]) if content_words else " ".join(words[:3])


class PathMemory:
    def __init__(self):
        self._store: dict[str, list[dict]] = defaultdict(list)

    def record(self, message: str, path_taken: list[dict], reply_sent: str):
        key = _fingerprint(message)
        record = {
            "path": path_taken,
            "reply": reply_sent,
            "timestamp": time.time(),
        }
        bucket = self._store[key]
        bucket.append(record)
        # FIFO eviction
        if len(bucket) > _MAX_RECORDS_PER_KEY:
            self._store[key] = bucket[-_MAX_RECORDS_PER_KEY:]
        log.info("Recorded path for key '%s' (%d records)", key, len(self._store[key]))

    def suggest(self, message: str) -> str:
        key = _fingerprint(message)
        records = self._store.get(key, [])
        if not records:
            return ""

        # Most recent 3 records
        recent = records[-3:]
        lines = []
        for r in recent:
            path_str = " → ".join(
                p.get("selected_label", "?") for p in r["path"]
            )
            lines.append(f'  Path taken: [{path_str}] → Reply: "{r["reply"][:60]}..."')

        return "For similar messages, past successful paths were:\n" + "\n".join(lines)
