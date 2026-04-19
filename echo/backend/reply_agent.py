"""
Claude agentic decision-tree reply generator.

Each call to get_next_node() makes ONE Claude API call with forced tool use.
Claude picks one of three tools:
  - confirm_intent:          depth 0 only, ambiguous messages
  - generate_decision_node:  need more context (max 2 times)
  - generate_final_reply:    enough context, produce reply text

Returns: {"type": tool_name, "data": tool_input_dict}
"""

import logging
import os

import anthropic
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

_client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """You are Echo, a communication assistant for a person with ALS who communicates via brain-computer interface signals. You guide them through a reply decision tree, one step at a time.

Each call you receive:
- The incoming SMS and last 20 messages of conversation history
- The path taken so far (BCI-selected choices at each prior level)
- Suggested past paths for similar messages
- Your depth in the tree (0 = first call)

You must call exactly one tool per turn:
- confirm_intent: ONLY at depth 0, and ONLY if the message is genuinely ambiguous or emotionally complex. Skip for simple or short messages.
- generate_decision_node: if you need one more dimension of context before writing the reply. Use at most 2 times total. By depth 2, always use generate_final_reply.
- generate_final_reply: when you have enough to write a natural, complete, sendable reply.

Rules:
- Each node has EXACTLY 3 options. Options must be meaningfully distinct — never paraphrases.
- Option labels ≤ 5 words (they are spoken aloud by TTS). Descriptions ≤ 15 words.
- Final reply: ≤ 2 sentences, complete, natural, sendable as-is.
- Must be consistent with the path taken.
- Never fabricate facts not present in the conversation history.
- Never reference the patient's disability, the BCI system, or the reply process.
- Never say "I" on behalf of the patient unless the path makes it clear."""

_TOOLS = [
    {
        "name": "confirm_intent",
        "description": "Use ONLY at depth 0 when the message is ambiguous or emotionally complex. Presents 3 interpretations for the patient to choose from.",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Short question read aloud (≤ 10 words): e.g. 'What is this message about?'"
                },
                "options": {
                    "type": "array",
                    "minItems": 3,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "≤ 5 words, spoken aloud"},
                            "description": {"type": "string", "description": "≤ 15 words, context for next call"},
                            "tts": {"type": "string", "description": "Full sentence read by TTS to patient"}
                        },
                        "required": ["label", "description", "tts"]
                    }
                },
                "reason_for_ambiguity": {"type": "string"}
            },
            "required": ["question", "options", "reason_for_ambiguity"]
        }
    },
    {
        "name": "generate_decision_node",
        "description": "Generate a decision node when you need one more dimension of context. Use at most 2 times total.",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Short question read aloud (≤ 10 words)"
                },
                "options": {
                    "type": "array",
                    "minItems": 3,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "≤ 5 words, spoken aloud"},
                            "description": {"type": "string", "description": "≤ 15 words, context for next call"},
                            "tts": {"type": "string", "description": "Sentence read by TTS"}
                        },
                        "required": ["label", "description", "tts"]
                    }
                },
                "reasoning": {"type": "string", "description": "Why this dimension matters for the reply"}
            },
            "required": ["question", "options", "reasoning"]
        }
    },
    {
        "name": "generate_final_reply",
        "description": "Generate the final reply text when you have sufficient context from the path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reply_text": {
                    "type": "string",
                    "description": "Complete reply to send via SMS (≤ 2 sentences)"
                },
                "tts_confirmation": {
                    "type": "string",
                    "description": "Short preview read before patient confirms send (≤ 10 words)"
                },
                "path_summary": {
                    "type": "string",
                    "description": "One sentence explaining how the path shaped this reply"
                },
                "confidence": {
                    "type": "number",
                    "description": "0.0–1.0 confidence this reply matches patient intent"
                }
            },
            "required": ["reply_text", "tts_confirmation", "path_summary", "confidence"]
        }
    }
]


def _build_prompt(
    message: str,
    sender: str,
    history: list[dict],
    path_taken: list[dict],
    memory_suggestions: str,
    depth: int,
) -> str:
    history_text = ""
    if history:
        lines = []
        for m in history[-20:]:
            who = m.get("sender", "Them") if not m.get("outbound") else "You"
            lines.append(f"  {who}: {m['body']}")
        history_text = "\n".join(lines)
    else:
        history_text = "  (no prior messages)"

    path_text = ""
    if path_taken:
        lines = []
        for i, p in enumerate(path_taken):
            lines.append(
                f"  Level {i}: Q: {p['question']} → "
                f"Selected: \"{p['selected_label']}\" ({p.get('selected_description', '')})"
            )
        path_text = "\n".join(lines)
    else:
        path_text = "  (no selections yet — this is the first call)"

    memory_section = (
        f"\nSuggested paths from memory:\n{memory_suggestions}"
        if memory_suggestions else ""
    )

    depth_note = "\nNote: depth ≥ 2 — you MUST call generate_final_reply now." if depth >= 2 else ""

    return (
        f"Incoming message from {sender}:\n\"{message}\"\n\n"
        f"Conversation history (last 20 messages):\n{history_text}\n\n"
        f"Path taken so far (BCI selections):\n{path_text}\n"
        f"{memory_section}\n"
        f"Current tree depth: {depth}{depth_note}\n\n"
        "Use exactly one tool now."
    )


async def get_next_node(
    message: str,
    sender: str,
    history: list[dict],
    path_taken: list[dict],
    memory_suggestions: str,
    depth: int,
) -> dict:
    """
    Makes one Claude API call.
    Returns {"type": tool_name, "data": tool_input_dict}
    """
    prompt = _build_prompt(message, sender, history, path_taken, memory_suggestions, depth)

    log.info("Calling Claude at depth %d (path_len=%d)", depth, len(path_taken))

    response = await _client.messages.create(
        model="claude-opus-4-7",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=_TOOLS,
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": prompt}],
    )

    tool_call = next(
        (b for b in response.content if b.type == "tool_use"),
        None,
    )
    if tool_call is None:
        raise RuntimeError(
            "Claude returned no tool call — stop_reason: " + str(response.stop_reason)
        )

    log.info("Claude called tool: %s", tool_call.name)
    return {"type": tool_call.name, "data": tool_call.input}
