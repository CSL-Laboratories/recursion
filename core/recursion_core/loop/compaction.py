"""Compaction: when the conversation nears the model's window, replace its older part with a summary.

From the contract (F11, PROPOSED: "Compaction should keep late constraints verbatim"; "a window ceiling, never
cutting at a tool result, and a shrink ladder") and vault records 20261028-recursion-frontier-dr-a1-compaction-02,
-b1-background-compaction-02 and -dr-b-direction-brief-02:

- The ceiling is ``W - max(15% of W, 16,384 if W >= 32,768)`` (the 16k reserve only on windows of 32k or more:
  record A's flat reserve went negative on small windows). Below 16,384 tokens a tool session is refused.
- The cut never separates a tool call from its result: the kept tail starts at an assistant message or at a
  user message that holds text, never at one that holds tool results; and it starts with what the user said.
- What the user said is kept VERBATIM, every message of the session, re-injected from the loop's own ledger
  and never from the summary ("re-inject the control kernel from the ledger, never from the summary"; record
  G: late restrictions are compiled, not summarised). The summary covers the work; the constraints stand
  beside it, word for word, however many times the conversation is compacted.
- The summary is the model's, from one call without tools. It is rejected, for a deterministic summary made
  from the record of what was done, if the call fails, is refused, is cut off, asks for a tool, comes back
  empty, or is longer than its limit (Hermes' rule, competitor notes row 24).
- Thinking blocks in the kept tail are dropped: they are bound to the conversation that produced them, and
  the conversation before them has changed (the provider refuses them otherwise). Text and tool calls stay.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from recursion_core.loop.providers import base as b

MIN_WINDOW = 16_384
KEEP_TAIL_TOKENS = 20_000  # Pi keeps 20,000 (record F)
SUMMARY_MAX_CHARS = 32_000
CHARS_PER_TOKEN = 4  # an estimate, used only to size the kept tail; the trigger uses the provider's own count

SUMMARY_PROMPT = (
    "Summarize the conversation so far for yourself, so you can continue the work without it. Say what the task "
    "is, what has been done (files read, changed and created, commands run and what they showed), what is "
    "still to do, and anything learned that matters. Do not repeat the user's messages: they are kept word for "
    "word beside your summary. Do not call any tool. Write plain text."
)
HEAD = "[harness] The earlier part of this conversation was compacted."


class WindowTooSmall(ValueError):
    pass


def ceiling(window: int) -> int:
    if window < MIN_WINDOW:
        raise WindowTooSmall(f"a context window of {window} tokens is below the {MIN_WINDOW} a tool session needs")
    reserve = max(int(window * 0.15), 16_384 if window >= 32_768 else 0)
    return window - reserve


def estimate(messages: Sequence[b.Message]) -> int:
    chars = 0
    for m in messages:
        for part in m.parts:
            if isinstance(part, b.TextPart):
                chars += len(part.text)
            elif isinstance(part, b.ToolCall):
                chars += len(part.raw_input or "") + len(part.name) + 32
            elif isinstance(part, b.ToolResult):
                chars += len(part.text) + 32
            else:
                chars += 256
    return chars // CHARS_PER_TOKEN + 1


def can_start_tail(message: b.Message) -> bool:
    if message.role == "assistant":
        return True
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


def kernel(user_messages: Sequence[tuple[str, str]]) -> str:
    lines = ["The user's messages in this session, word for word, oldest first. They still apply; where two "
             "disagree, the later one wins:"]
    for n, (turn_id, text) in enumerate(user_messages, 1):
        lines.append(f"--- message {n} ({turn_id}) ---\n{text}")
    return "\n".join(lines)


def compact(messages: Sequence[b.Message], user_messages: Sequence[tuple[str, str]],
            summarize: Callable[[list[b.Message]], b.TurnResponse | None],
            keep_tokens: int = KEEP_TAIL_TOKENS) -> Compacted | None:
    """Compact ``messages``; None when there is nothing to replace."""

    cut = split(messages, keep_tokens)
    if cut == 0:
        return None
    prefix, tail = list(messages[:cut]), list(messages[cut:])
    summary, reason = check_summary(summarize(prefix))
    source = "model"
    if summary is None:
        summary, source = fallback_summary(prefix), "fallback"
    head = b.Message("user", (b.TextPart(f"{HEAD}\n\nSummary of the work so far:\n{summary}\n\n"
                                         f"{kernel(user_messages)}"),))
    kept = [b.Message(m.role, tuple(p for p in m.parts if not isinstance(p, b.OpaquePart))) for m in tail]
    kept = [m for m in kept if m.parts]
    out = [head] + kept
    for _, text in user_messages:  # the verbatim rule, checked rather than assumed
        if not any(isinstance(p, b.TextPart) and text in p.text for m in out for p in m.parts):
            raise AssertionError("a user message did not survive compaction verbatim")
    return Compacted(out, source, len(prefix), len(kept), reason)
