"""
Validates every Claude tool output before it reaches the state machine.

Checks:
  1. Structural: correct fields, exactly 3 options, label length
  2. Option distinctiveness: Jaccard similarity on descriptions < 0.6
  3. Hallucination guard (final reply only): proper nouns in reply must
     appear somewhere in the conversation history
"""

import logging
import re
from itertools import combinations

log = logging.getLogger(__name__)


class ValidationError(Exception):
    pass


def validate_node(raw_node: dict) -> dict:
    """
    Validates a Claude tool output dict.
    Returns the dict unchanged if valid.
    Raises ValidationError with a user-readable message on failure.
    """
    tool_type = raw_node.get("type")
    data = raw_node.get("data", {})

    if tool_type in ("confirm_intent", "generate_decision_node"):
        _validate_decision_node(data, tool_type)
    elif tool_type == "generate_final_reply":
        _validate_final_reply(data)
    else:
        raise ValidationError(f"Unknown tool type: {tool_type}")

    return raw_node


def _validate_decision_node(data: dict, tool_type: str):
    options = data.get("options")
    if not options or len(options) != 3:
        raise ValidationError(
            f"{tool_type}: expected exactly 3 options, got {len(options) if options else 0}"
        )
    for i, opt in enumerate(options):
        label = opt.get("label", "")
        if len(label.split()) > 6:
            raise ValidationError(f"Option {i} label too long: '{label}' — max 5 words")
        if not opt.get("tts"):
            raise ValidationError(f"Option {i} missing tts field")
        if not opt.get("description"):
            raise ValidationError(f"Option {i} missing description field")

    _assert_options_distinct(options)

    if not data.get("question"):
        raise ValidationError(f"{tool_type}: missing question field")


def _validate_final_reply(data: dict):
    text = data.get("reply_text", "")
    if not text or not text.strip():
        raise ValidationError("generate_final_reply: reply_text is empty")
    if len(text) > 600:
        raise ValidationError(f"generate_final_reply: reply too long ({len(text)} chars)")
    if not data.get("tts_confirmation"):
        raise ValidationError("generate_final_reply: missing tts_confirmation")
    conf = data.get("confidence", -1)
    if not (0.0 <= conf <= 1.0):
        raise ValidationError(f"generate_final_reply: confidence out of range: {conf}")


def _jaccard(a: str, b: str) -> float:
    stop = {"a", "an", "the", "and", "or", "but", "to", "of", "in", "on", "with", "is", "are", "it"}
    tokens_a = {w for w in re.findall(r"\w+", a.lower()) if w not in stop}
    tokens_b = {w for w in re.findall(r"\w+", b.lower()) if w not in stop}
    if not tokens_a and not tokens_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _assert_options_distinct(options: list[dict]):
    descriptions = [o.get("description", "").lower() for o in options]
    for i, j in combinations(range(3), 2):
        sim = _jaccard(descriptions[i], descriptions[j])
        if sim > 0.6:
            raise ValidationError(
                f"Options {i} and {j} are too similar (Jaccard={sim:.2f}): "
                f"'{descriptions[i]}' vs '{descriptions[j]}'"
            )


def check_hallucination(reply_text: str, conversation_history: list[dict]) -> bool:
    """
    Returns True if no suspicious hallucinations detected.
    Logs a warning and returns False if proper nouns appear in reply
    but not in conversation history.
    """
    all_context = " ".join(
        m.get("body", "") for m in conversation_history
    ).lower()

    # Extract capitalized words that look like proper nouns (not sentence-start)
    words = reply_text.split()
    suspicious = []
    for i, word in enumerate(words):
        clean = re.sub(r"[^a-zA-Z]", "", word)
        if (
            len(clean) > 2
            and clean[0].isupper()
            and i > 0  # skip sentence-start capitals
            and clean.lower() not in all_context
        ):
            suspicious.append(clean)

    if suspicious:
        log.warning("Potential hallucination — words not in history: %s", suspicious)
        return False
    return True
