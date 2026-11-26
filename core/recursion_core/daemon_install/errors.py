"""Refusals from the Linux user-unit installer."""

from __future__ import annotations


class InstallRefused(Exception):
    """A fail-closed refusal. ``kind`` is a stable token, not a log line; ``detail`` is for a person."""

    def __init__(self, kind: str, detail: str = "") -> None:
        if type(kind) is not str or not kind or any(c in kind for c in "\n\r"):
            raise ValueError("kind")
        if type(detail) is not str:
            raise ValueError("detail")
        self.kind = kind
        self.detail = detail
        super().__init__(kind if not detail else f"{kind}: {detail}")
