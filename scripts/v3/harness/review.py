"""Read a reviewer's approve or revise verdict.

Grok's headless mode writes a JSON stream. The verdict is inside the
assistant text, often after a preamble, so the first raw line is not the
verdict. Anything that is not a verdict line is revise, and that failure
does not keep the reviewer's body.
"""

from __future__ import annotations

import re

from scripts.v3.harness.streams import assistant_text


NOTE_LIMIT = 1000
OUTPUT_LIMIT = 8000
DIFF_LIMIT = 2000
PROMPT_LIMIT = 16000
_VERDICT = re.compile(
    r"^(?:[*_`]+)?\s*verdict\s*:?\s*(approve|revise)\b[.!]?\s*(?:[*_`]+)?\s*$",
    re.IGNORECASE,
)


def parse_verdict(text: str) -> tuple[str, str]:
    """Return ``(approve|revise, notes)`` from plain text or a JSON stream."""

    body = assistant_text(text)
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


def review_prompt(task: str, output: str, diff: str = "") -> str:
    """Build a review prompt. Output, diff, and the whole prompt are capped."""

    lines = [
        "Review this completed task.",
        "Reply with a line of exactly 'VERDICT approve' or 'VERDICT revise'.",
        "",
        "Task:",
        task,
        "",
        "Output:",
        _clip(output, OUTPUT_LIMIT),
    ]
    if diff:
        lines.extend(("", "Diff:", _clip(diff, DIFF_LIMIT)))
    text = "\n".join(lines)
    if len(text) <= PROMPT_LIMIT:
        return text
    return text[:PROMPT_LIMIT] + "\n[truncated]"


def _clean_line(line: str) -> str:
    text = line.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        text = text[1:-1].strip()
    return text


def _clip(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"
