"""Read a reviewer's approve or revise verdict.

Grok's headless mode writes a JSON stream. The verdict is inside the
assistant text, often after a preamble, so the first raw line is not the
verdict. Anything that is not a verdict line is revise, and that failure
does not keep the reviewer's body.
"""

from __future__ import annotations

import re

from scripts.v3.harness.streams import assistant_text, claude_last_message


NOTE_LIMIT = 1000
_VERDICT = re.compile(
    r"^(?:[*_`]+)?\s*verdict\s*:?\s*(approve|revise)\b[.!]?\s*(?:[*_`]+)?\s*$",
    re.IGNORECASE,
)


def parse_verdict(text: str) -> tuple[str, str]:
    """Return ``(approve|revise, notes)`` from plain text or a JSON stream.

    A Claude stream is judged only by its last assistant message that has
    text. No verdict line in that message is revise, even when an earlier
    message said approve.
    """

    last = claude_last_message(text)
    body = assistant_text(text) if last is None else last
    lines = [_clean_line(line) for line in body.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return "revise", "reviewer returned no verdict"
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        match = _VERDICT.fullmatch(line)
        if match is None:
            continue
        notes = "\n".join(lines[:index] + lines[index + 1 :])[:NOTE_LIMIT]
        return match.group(1).lower(), notes
    return "revise", "reviewer returned an unreadable verdict"


def review_prompt(task: str, output: str) -> str:
    return "\n".join(
        (
            "Review this completed task.",
            "Reply with a line of exactly 'VERDICT approve' or 'VERDICT revise'.",
            "",
            "Task:",
            task,
            "",
            "Output:",
            _clip(output),
        )
    )


def _clean_line(line: str) -> str:
    text = line.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1].strip()
    return text


def _clip(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"
