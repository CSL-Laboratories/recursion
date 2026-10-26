"""Vendor health classification and the injectable health port.

Only the engine's own channels (exit code, stderr, non-JSON stdout lines) can
signal a limit. Text inside the model's JSON payload never does, so untrusted
input cannot trip a breaker.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Protocol

_FENCE = re.compile(
    r"\A```[A-Za-z0-9_+-]*[ \t]*\r?\n(?P<body>.*?)\r?\n?```\s*\Z",
    re.S,
)
_LIMIT_PATTERNS = tuple(
    re.compile(pattern, flags)
    for pattern, flags in (
        (r"usage[_ -]?limit", re.IGNORECASE),
        (r"rate[_ -]?limit", re.IGNORECASE),
        (r"\b429\b", 0),
        (r"\b503\b", 0),
        (r"\b529\b", 0),
        (r"quota", re.IGNORECASE),
        (r"too many requests", re.IGNORECASE),
        (r"overloaded", re.IGNORECASE),
        (r"service unavailable", re.IGNORECASE),
        (r"resource[_ ]?exhausted", re.IGNORECASE),
        (r"error_max_turns", re.IGNORECASE),
        (r"\bcapacity\b", re.IGNORECASE),
        (r"context (?:length|window)", re.IGNORECASE),
        (r"too long", re.IGNORECASE),
    )
)

STATES = ("ok", "degraded", "exhausted")
_MIN_ECHO_LINE = 12


def looks_like_limit(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _LIMIT_PATTERNS)


def strip_fence(text: str) -> str:
    """Drop one surrounding Markdown fence. The body is the model payload."""

    stripped = (text or "").strip()
    match = _FENCE.match(stripped)
    return match.group("body").strip() if match else stripped


def no_nonfinite(constant: str) -> None:
    raise ValueError(f"non-finite JSON constant {constant!r}")


def scrub_prompt_echo(stderr: str, prompt: str | None) -> str:
    """Drop stderr lines that repeat the prompt verbatim (an echo of untrusted input)."""

    if not prompt or not stderr:
        return stderr or ""
    kept = [
        line
        for line in stderr.splitlines()
        if not (len(line.strip()) >= _MIN_ECHO_LINE and line.strip() in prompt)
    ]
    return "\n".join(kept)


def limit_signal(stdout: str, stderr: str, prompt: str | None = None) -> bool:
    """True when the engine, not the model, reported a limit or health problem."""

    if looks_like_limit(scrub_prompt_echo(stderr or "", prompt)):
        return True
    text = strip_fence(stdout or "")
    if not text:
        return False
    try:
        json.loads(text, parse_constant=no_nonfinite)
        return False
    except ValueError:
        pass
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    unparsed = []
    for line in lines:
        try:
            json.loads(line, parse_constant=no_nonfinite)
        except ValueError:
            unparsed.append(line)
    if not unparsed:
        return False
    if len(unparsed) < len(lines):
        return looks_like_limit("\n".join(unparsed))
    return looks_like_limit(text)


class HealthPort(Protocol):
    """Injection point for engine health. The executor never owns breaker state."""

    def is_open(self, engine_id: str) -> bool: ...

    def record_ok(self, engine_id: str) -> None: ...

    def state(self, engine_id: str) -> str: ...

    def fault(self, engine_id: str, state: str, error: str) -> str:
        """Record a health fault and return the engine's usage state afterwards."""


@dataclass
class _Window:
    state: str = "ok"
    used: int = 0
    generation: int = 0


class InMemoryHealth:
    """Process-local reference port. A breaker stays open until ``reset``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[str, _Window] = {}
        self.faults: list[tuple[str, str, str]] = []

    def _window(self, engine_id: str) -> _Window:
        return self._windows.setdefault(engine_id, _Window())

    def is_open(self, engine_id: str) -> bool:
        with self._lock:
            return self._window(engine_id).state != "ok"

    def record_ok(self, engine_id: str) -> None:
        with self._lock:
            self._window(engine_id).used += 1

    def fault(self, engine_id: str, state: str, error: str) -> str:
        if state not in STATES or state == "ok":
            raise ValueError(f"invalid health state {state!r}")
        with self._lock:
            window = self._window(engine_id)
            window.state = state
            window.generation += 1
            self.faults.append((engine_id, state, error))
            return window.state

    def state(self, engine_id: str) -> str:
        with self._lock:
            return self._window(engine_id).state

    def used(self, engine_id: str) -> int:
        with self._lock:
            return self._window(engine_id).used

    def reset(self, engine_id: str) -> None:
        with self._lock:
            self._windows[engine_id] = _Window()
