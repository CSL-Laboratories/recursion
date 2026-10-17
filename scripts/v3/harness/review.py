"""Read a reviewer's approve or revise verdict.

Grok's headless mode writes a JSON stream. The verdict is inside the
assistant text, often after a preamble, so the first raw line is not the
verdict. Anything that is not a verdict line is revise, and that failure
does not keep the reviewer's body.
"""

from __future__ import annotations

import re

from scripts.v3.harness.streams import assistant_text, claude_last_message, codex_last_message


NOTE_LIMIT = 1000
OUTPUT_LIMIT = 8000
DIFF_LIMIT = 2000
TASK_LIMIT = 5000
_VERDICT = re.compile(
    r"^(?:[*_`]+)?\s*verdict\s*:?\s*(approve|revise)\b[.!]?\s*(?:[*_`]+)?\s*$",
    re.IGNORECASE,
)


def parse_verdict(text: str) -> tuple[str, str]:
    """Return ``(approve|revise, notes)`` from plain text or a JSON stream.

    A Claude or Codex stream is judged only by its last assistant message
    that has text. No verdict line in that message is revise, even when an
    earlier message said approve.
    """

    last = claude_last_message(text)
    if last is None:
        last = codex_last_message(text)
    full = _lines(assistant_text(text))
    lines = full if last is None else _lines(last)
    if not lines:
        return "revise", "reviewer returned no verdict"
    for index in range(len(lines) - 1, -1, -1):
        match = _VERDICT.fullmatch(lines[index])
        if match is None:
            continue
        if last is None:
            kept = lines[:index] + lines[index + 1 :]
        else:
            # Verdict comes from the last message. Its notes come first so a
            # long earlier message cannot crowd them out of NOTE_LIMIT.
            final = [
                line
                for pos, line in enumerate(lines)
                if pos != index and _VERDICT.fullmatch(line) is None
            ]
            earlier = [
                line
                for line in full[: max(len(full) - len(lines), 0)]
                if _VERDICT.fullmatch(line) is None
            ]
            kept = final + earlier
        return match.group(1).lower(), "\n".join(kept)[:NOTE_LIMIT]
    return "revise", "reviewer returned an unreadable verdict"


def _lines(body: str) -> list[str]:
    lines = [_clean_line(line) for line in body.splitlines()]
    return [line for line in lines if line]


def review_prompt(task: str, output: str, diff: str = "") -> str:
    """Build a review prompt. Task, output, and diff each have their own cap."""

    lines = [
        "Review this completed task.",
        "Reply with a line of exactly 'VERDICT approve' or 'VERDICT revise'.",
        "",
        "Task:",
        _clip(task, TASK_LIMIT),
        "",
        "Output:",
        _clip(output, OUTPUT_LIMIT),
    ]
    if diff:
        lines.extend(("", "Diff:", _clip(diff, DIFF_LIMIT)))
    return "\n".join(lines)


def _clean_line(line: str) -> str:
    text = line.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1].strip()
    return text


def _clip(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    marker = "\n[truncated]"
    return text[: limit - len(marker)] + marker
