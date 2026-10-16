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
    # A turn.started event scopes Codex text to that turn. Without one, every
    # agent message is the reply, so notes before a later VERDICT stay.
    # Other providers run only when this stream has no Codex agent message.
    starts = [i for i, event in enumerate(events) if event.get("type") == "turn.started"]
    if starts:
        scoped = events[starts[-1] + 1 :]
        messages = [text for text in (_agent_message(event) for event in scoped) if text]
        if messages:
            return "\n".join(messages)
        return ""
    messages = [text for text in (_agent_message(event) for event in events) if text]
    if messages:
        return "\n".join(messages)
    claude = _claude_text(events)
    if claude:
        return claude
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


def _agent_message(event: dict) -> str:
    item = event.get("item")
    if (
        event.get("type") == "item.completed"
        and isinstance(item, dict)
        and item.get("type") == "agent_message"
        and isinstance(item.get("text"), str)
        and item["text"].strip()
    ):
        return item["text"]
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


def claude_last_message(raw: str) -> str | None:
    """Return the last Claude assistant message that has text.

    ``None`` when the stream has no such message, so other providers keep
    their own text. A verdict is read from this message only. An earlier
    message's verdict does not apply when this message has no verdict line.
    """

    messages = _claude_messages(_events(raw))
    if not messages:
        return None
    return messages[-1]


def _events(raw: str) -> list[dict]:
    events: list[dict] = []
    for line in raw.splitlines():
        payload = _object(line.strip())
        if payload is not None:
            events.append(payload)
    return events


def _claude_text(events: list[dict]) -> str:
    """Join Claude ``assistant`` message content, one line break between messages.

    Text blocks inside one message stay adjacent. A non-text block such as
    ``tool_use`` starts the next text block on a new line. An empty text
    block after that tool call keeps the break, so a later ``VERDICT`` line
    is not glued to the previous sentence. The caller falls back to the
    ``result`` string only when no assistant text exists. Verdict parsing
    uses ``claude_last_message``, not this joined text.
    """

    return "\n".join(_claude_messages(events))


def _claude_messages(events: list[dict]) -> list[str]:
    """Claude assistant messages that have text, in stream order."""

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
    return messages


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


def _nested_text(events: list[dict]) -> str:
    """Join Gemini and OpenCode assistant text. Claude content is handled earlier."""

    parts: list[str] = []
    for event in events:
        kind = event.get("type")
        if kind == "message" and event.get("role") == "assistant":
            content = event.get("content")
            if isinstance(content, str):
                parts.append(content)
        elif kind == "text" and isinstance(event.get("part"), dict):
            value = event["part"].get("text")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)
