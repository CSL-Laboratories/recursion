"""Separate assistant text from headless JSON streams.

Tool output and reasoning chunks are not the assistant text. Claude Code
stream-json uses assistant message content when that content is non-empty,
with a line break between assistant messages. When that content is empty,
the result string is the fallback. A stream with neither becomes an empty
string, not the raw JSON.
"""

from __future__ import annotations

import json


def assistant_text(raw: str) -> str:
    """Return the assistant's text, unwrapping JSON streams when present."""

    events: list[dict] = []
    plain: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        payload = _object(stripped)
        if payload is None:
            plain.append(line)
        else:
            events.append(payload)
    if not events:
        return raw
    grok = _grok_text(events)
    if grok:
        return grok
    for event in reversed(events):
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
            and item["text"].strip()
        ):
            return item["text"]
    claude = _claude_text(events)
    if claude:
        return claude
    for event in reversed(events):
        for key in ("text", "result"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value
    if plain:
        return "\n".join(plain)
    return ""


def _object(line: str) -> dict | None:
    if not line.startswith("{"):
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _claude_text(events: list[dict]) -> str:
    """Join Claude ``assistant`` message content, one line break between messages.

    Text blocks inside one message stay adjacent. A non-text block such as
    ``tool_use`` starts the next text block on a new line. An empty text
    block after that tool call keeps the break, so a later ``VERDICT`` line
    is not glued to the previous sentence. The caller falls back to the
    ``result`` string only when no assistant text exists.
    """

    messages: list[str] = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        text = _claude_content_text(message.get("content"))
        if text.strip():
            messages.append(text)
    return "\n".join(messages)


def _claude_content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    gap = False
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ):
            if gap and chunks:
                chunks.append("\n")
            chunks.append(block["text"])
            gap = False
        else:
            gap = True
    return "".join(chunks)


def _grok_text(events: list[dict]) -> str:
    """Join Grok ``type: text`` chunks. Thoughts and tool calls are skipped."""

    parts: list[str] = []
    for event in events:
        if event.get("type") != "text":
            continue
        for key in ("data", "text", "content"):
            value = event.get(key)
            if isinstance(value, str):
                parts.append(value)
                break
    return "".join(parts)
