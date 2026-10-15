"""Separate assistant text from headless JSON streams.

Tool output and reasoning chunks are not the assistant text. A stream with
no assistant text becomes an empty string, not the raw JSON.
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
    for event in reversed(events):
        for key in ("text", "result"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value
    nested = _nested_text(events)
    if nested:
        return nested
    if plain:
        return "\n".join(plain)
    return ""


def _nested_text(events: list[dict]) -> str:
    """Join assistant text from Gemini, Cursor and OpenCode event shapes."""

    parts: list[str] = []
    for event in events:
        kind = event.get("type")
        if kind == "message" and event.get("role") == "assistant":
            content = event.get("content")
            if isinstance(content, str):
                parts.append(content)
        elif kind == "assistant" and isinstance(event.get("message"), dict):
            content = event["message"].get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                blocks = [
                    block["text"]
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ]
                if blocks:
                    parts.append("\n".join(blocks))
        elif kind == "text" and isinstance(event.get("part"), dict):
            value = event["part"].get("text")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


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
