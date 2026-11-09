"""Refusals from the Linux user-unit installer (CSL-758)."""

from __future__ import annotations


class InstallRefused(Exception):
    """A fail-closed refusal. ``kind`` is a stable token, not a log line."""

    def __init__(self, kind: str) -> None:
        if type(kind) is not str or not kind or any(c in kind for c in "\n\r"):
            raise ValueError("kind")
        self.kind = kind
        super().__init__(kind)
