"""Compaction: when the conversation nears the model's window, replace its older part with a summary.

From the contract (F11, PROPOSED: "Compaction should keep late constraints verbatim"; "a window ceiling, never
cutting at a tool result, and a shrink ladder") and vault records 20261028-recursion-frontier-dr-a1-compaction-02,
-b1-background-compaction-02 and -dr-b-direction-brief-02:

- The ceiling is ``W - max(15% of W, 16,384 if W >= 32,768)`` (the 16k reserve only on windows of 32k or more:
  record A's flat reserve went negative on small windows). Below 16,384 tokens a tool session is refused.
- The cut never separates a tool call from its result: the kept tail starts at an assistant message that keeps a
  part once its thinking blocks are dropped, or at a user message that holds text, never at one that holds tool
  results.
- What the user said is kept VERBATIM, every message of the session: re-injected from the loop's own ledger and
  never from the summary ("re-inject the control kernel from the ledger, never from the summary"; record G: late
  restrictions are compiled, not summarised), except a message the kept tail still holds word for word, which
  is not repeated. The summary covers the work; the constraints stand beside it, word for word, however many
  times the conversation is compacted. Each is checked to be in the result, in its place, and a ledger that is
  empty while the part replaced holds the user's text is refused (LedgerMismatch).
- The result fits the room it is given (the ceiling less the system prompt and the tool list), by a shrink
  ladder: the tail is sized to the room left after the user's words; a tail that still does not fit goes into
  the summary too; the summary is cut to what is left. When the user's words alone do not fit, nothing is
  dropped: ContextFull, and the turn ends with ``context_full`` before any call.
- The summary is the model's, from one call without tools, whose request is itself kept under the ceiling (the
  longest texts of the part to summarise are shortened in that request only). It is rejected, for a
  deterministic summary made from the record of what was done, if the call fails, is refused, is cut off, asks
  for a tool, comes back empty, or is longer than its limit (Hermes' rule, competitor notes row 24). A typed
  usage limit is not a failed summary: it ends the turn like any limit.
- Thinking blocks in the kept tail are dropped: they are bound to the conversation that produced them, and
  the conversation before them has changed (the provider refuses them otherwise). Text and tool calls stay.
- The result alternates roles like every request: the head joins a kept tail that starts with the user's
  message, and messages a dropped thinking block left side by side are joined.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from recursion_core.loop.providers import base as b

MIN_WINDOW = 16_384
KEEP_TAIL_TOKENS = 20_000  # Pi keeps 20,000 (record F)
SUMMARY_MAX_CHARS = 32_000
SUMMARY_MIN_TOKENS = 64  # the least room a summary is left before the tail goes into it
CHARS_PER_TOKEN = 4  # an estimate, used to size requests; the trigger also uses the provider's own count
HARNESS = "[harness]"  # what the loop itself writes on the user's side starts with this

SUMMARY_PROMPT = (
    "Summarize the conversation so far for yourself, so you can continue the work without it. Say what the task "
    "is, what has been done (files read, changed and created, commands run and what they showed), what is "
    "still to do, and anything learned that matters. Do not repeat the user's messages: they are kept word for "
    "word beside your summary. Do not call any tool. Write plain text."
)
HEAD = "[harness] The earlier part of this conversation was compacted."
CUT_NOTE = "\n[... {n} characters left out of this request ...]\n"


class WindowTooSmall(ValueError):
    pass


class ContextFull(Exception):
    """The conversation cannot be brought under the ceiling without dropping what the user said."""


class LedgerMismatch(ValueError):
    """The ledger cannot be what the user said: compaction would drop the user's words."""


class NoSummary(Exception):
    """No summary call was made; the text says why (the record fallback is used)."""


def ceiling(window: int) -> int:
    if window < MIN_WINDOW:
        raise WindowTooSmall(f"a context window of {window} tokens is below the {MIN_WINDOW} a tool session needs")
    reserve = max(int(window * 0.15), 16_384 if window >= 32_768 else 0)
    return window - reserve


def _json_len(value: object) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (ValueError, TypeError, RecursionError):
        return 256


def _chars(part: b.Part) -> int:
    if isinstance(part, b.TextPart):
        return len(part.text)
    if isinstance(part, b.ToolCall):
        # What is sent is the parsed input, as JSON; the raw text when there is no parsed input.
        body = _json_len(dict(part.input)) if part.input is not None else len(part.raw_input or "")
        return max(body, len(part.raw_input or "")) + len(part.name) + 32
    if isinstance(part, b.ToolResult):
        return len(part.text) + 32
    if isinstance(part, b.OpaquePart):
        return _json_len(dict(part.data)) + 32
    return 256


def estimate(messages: Sequence[b.Message]) -> int:
    return sum(_chars(part) for m in messages for part in m.parts) // CHARS_PER_TOKEN + 1


def estimate_tools(tools: Sequence[b.ToolSpec]) -> int:
    return sum(_json_len({"name": t.name, "description": t.description, "input_schema": t.input_schema})
               for t in tools) // CHARS_PER_TOKEN


def _kept_parts(message: b.Message) -> tuple[b.Part, ...]:
    return tuple(p for p in message.parts if not isinstance(p, b.OpaquePart))


def can_start_tail(message: b.Message) -> bool:
    if message.role == "assistant":
        return bool(_kept_parts(message))  # an assistant message of thinking blocks alone is dropped: never a start
    return not any(isinstance(p, b.ToolResult) for p in message.parts)


def split(messages: Sequence[b.Message], keep_tokens: int = KEEP_TAIL_TOKENS) -> int:
    """The index where the kept tail starts: the earliest allowed cut whose tail fits ``keep_tokens``, and never
    the whole conversation. Returns 0 when nothing can be compacted."""

    best = 0
    for i in range(len(messages) - 1, 0, -1):
        if not can_start_tail(messages[i]):
            continue
        if estimate(messages[i:]) > keep_tokens and best:
            break
        best = i
    return best


@dataclass(frozen=True)
class Compacted:
    messages: list[b.Message]
    summary_source: str  # "model" or "fallback"
    replaced: int
    kept: int
    reason: str = ""


def fallback_summary(prefix: Sequence[b.Message]) -> str:
    """A summary made from the record alone: the tool calls made and the model's last words."""

    calls: list[str] = []
    last_text = ""
    for m in prefix:
        for part in m.parts:
            if isinstance(part, b.ToolCall) and part.input is not None:
                key = next((part.input[k] for k in ("path", "command", "pattern") if k in part.input), "")
                calls.append(f"- {part.name} {str(key)[:200]}".rstrip())
            elif isinstance(part, b.TextPart) and m.role == "assistant" and part.text.strip():
                last_text = part.text.strip()
    lines = ["Summary made from the record (the model's summary was not usable):"]
    lines.append(f"Tool calls made ({len(calls)}):" if calls else "No tool calls were made.")
    lines.extend(calls[-60:])
    if len(calls) > 60:
        lines.insert(2, f"(the first {len(calls) - 60} are not listed)")
    if last_text:
        lines.append(f"The model's last words before this point: {last_text[-2000:]}")
    return "\n".join(lines)


def check_summary(response: b.TurnResponse | None) -> tuple[str | None, str]:
    """The model's summary, or None and the reason it is rejected."""

    if response is None:
        return None, "the summary call failed"
    if response.stop_reason == b.REFUSAL:
        return None, "the summary was refused"
    if response.stop_reason == b.MAX_TOKENS:
        return None, "the summary was cut off"
    if response.tool_calls:
        return None, "the summary call asked for a tool"
    text = response.text.strip()
    if not text:
        return None, "the summary was empty"
    if len(text) > SUMMARY_MAX_CHARS:
        return None, "the summary was longer than its limit"
    return text, ""


def _entry(n: int, turn_id: str, text: str) -> str:
    return f"--- message {n} ({turn_id}) ---\n{text}"


def kernel(user_messages: Sequence[tuple[str, str]]) -> str:
    if not user_messages:
        return ""
    lines = ["The user's messages in this session, word for word, oldest first (any still in the conversation "
             "below are not repeated here). They still apply; where two disagree, the later one wins:"]
    for n, (turn_id, text) in enumerate(user_messages, 1):
        lines.append(_entry(n, turn_id, text))
    return "\n".join(lines)


def _head_text(summary: str, entries: Sequence[tuple[str, str]]) -> str:
    text = f"{HEAD}\n\nSummary of the work so far:\n{summary}"
    k = kernel(entries)
    return f"{text}\n\n{k}" if k else text


def _user_texts(messages: Sequence[b.Message]) -> list[str]:
    return [p.text for m in messages if m.role == "user" for p in m.parts if isinstance(p, b.TextPart)]


def alternate(messages: Sequence[b.Message]) -> list[b.Message]:
    """``messages`` with neighbours of one role joined, as the engine joins them when it appends."""

    out: list[b.Message] = []
    for m in messages:
        if out and out[-1].role == m.role:
            m = b.Message(m.role, out.pop().parts + m.parts)
        out.append(m)
    return out


def summary_request(prefix: Sequence[b.Message]) -> list[b.Message]:
    """The messages of the summary call: the part to summarise and the instruction, the roles alternating."""

    msgs = list(prefix)
    prompt = b.TextPart(SUMMARY_PROMPT)
    if msgs and msgs[-1].role == "user":
        msgs[-1] = b.Message("user", msgs[-1].parts + (prompt,))
    else:
        msgs.append(b.Message("user", (prompt,)))
    return msgs


def fit(messages: Sequence[b.Message], budget: int) -> list[b.Message] | None:
    """``messages`` with their longest texts shortened (the middle left out, with a note) until the estimate is
    within ``budget`` tokens, for a request that only summarises them; None when they cannot fit. The last part of
    the last message (the instruction) is never shortened."""

    msgs = [list(m.parts) for m in messages]
    floor = 400
    for _ in range(64):
        size = estimate([b.Message(m.role, tuple(parts)) for m, parts in zip(messages, msgs)])
        if size <= budget:
            return [b.Message(m.role, tuple(parts)) for m, parts in zip(messages, msgs)]
        excess = (size - budget) * CHARS_PER_TOKEN + 64
        best = None
        for i, parts in enumerate(msgs):
            for j, part in enumerate(parts):
                if i == len(msgs) - 1 and j == len(parts) - 1:
                    continue
                if isinstance(part, (b.TextPart, b.ToolResult)) and len(part.text) > floor:
                    if best is None or len(part.text) > len(msgs[best[0]][best[1]].text):
                        best = (i, j)
        if best is None:
            return None
        i, j = best
        part = msgs[i][j]
        text = part.text
        keep = max(floor, len(text) - excess - len(CUT_NOTE) - 8)
        note = CUT_NOTE.format(n=len(text) - keep)
        short = text[:keep // 2] + note + text[len(text) - (keep - keep // 2):]
        msgs[i][j] = (b.TextPart(short) if isinstance(part, b.TextPart)
                      else b.ToolResult(part.call_id, short, part.is_error))
    return None


def compact(messages: Sequence[b.Message], user_messages: Sequence[tuple[str, str]],
            summarize: Callable[[list[b.Message]], b.TurnResponse | None],
            keep_tokens: int = KEEP_TAIL_TOKENS, room: int | None = None) -> Compacted | None:
    """Compact ``messages``; None when there is nothing to replace. With ``room`` (tokens), the result fits it or
    ContextFull is raised before any summary call. ``summarize`` may raise NoSummary (no call was made) and lets a
    typed limit or a cancellation through."""

    if not user_messages and any(not t.startswith(HARNESS) for t in _user_texts(messages)):
        raise LedgerMismatch("the ledger is empty while the conversation holds the user's text")
    full = estimate([b.Message("user", (b.TextPart(_head_text("", user_messages)),))])
    limit = None if room is None else room - SUMMARY_MIN_TOKENS
    tail_tokens = keep_tokens if limit is None else max(1, min(keep_tokens, (limit - full) // 2))
    cut = split(messages, tail_tokens)
    if cut == 0:
        return None

    def build(cut: int, summary: str) -> list[b.Message]:
        kept = [b.Message(m.role, _kept_parts(m)) for m in messages[cut:]]
        kept = alternate([m for m in kept if m.parts])
        in_tail = set(_user_texts(kept))
        entries = [(t, x) for t, x in user_messages if x not in in_tail]
        head = b.Message("user", (b.TextPart(_head_text(summary, entries)),))
        if kept and kept[0].role == "user":
            return [b.Message("user", head.parts + kept[0].parts)] + kept[1:]
        return [head] + kept

    if limit is not None and estimate(build(cut, "")) > limit:
        cut = len(messages)  # the tail does not fit beside the user's words: it goes into the summary too
        if estimate(build(cut, "")) > limit:
            raise ContextFull("the user's messages alone are larger than the model's window")
    prefix = list(messages[:cut])
    try:
        summary, reason = check_summary(summarize(prefix))
    except NoSummary as exc:
        summary, reason = None, str(exc)
    source = "model"
    if summary is None:
        summary, source = fallback_summary(prefix), "fallback"
    if room is not None:
        spare = (room - estimate(build(cut, ""))) * CHARS_PER_TOKEN - 64
        if len(summary) > spare and source == "model":
            summary, source, reason = fallback_summary(prefix), "fallback", "the summary did not fit the window"
        if len(summary) > spare:
            summary = summary[:max(0, spare - 40)] + "\n[the rest of the summary is cut]"
    out = build(cut, summary)
    _check_verbatim(out, user_messages)
    if room is not None and estimate(out) > room:
        raise ContextFull("the compacted conversation does not fit the model's window")
    return Compacted(out, source, len(prefix), len([m for m in messages[cut:] if _kept_parts(m)]), reason)


def _check_verbatim(out: Sequence[b.Message], user_messages: Sequence[tuple[str, str]]) -> None:
    """Every message of the ledger is in the result: in the head's kernel, numbered in its place, or as a text of
    the user's in the kept tail, word for word."""

    head = out[0].parts[0].text if out and out[0].parts and isinstance(out[0].parts[0], b.TextPart) else ""
    tail = set(_user_texts([b.Message(m.role, m.parts[1:] if k == 0 else m.parts) for k, m in enumerate(out)]))
    entries = [(t, x) for t, x in user_messages if x not in tail]
    for n, (turn_id, text) in enumerate(entries, 1):
        if _entry(n, turn_id, text) not in head:
            raise LedgerMismatch("a user message did not survive compaction verbatim")
