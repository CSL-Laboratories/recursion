"""Typed refusal for the approval scope package."""

from __future__ import annotations

__all__ = ["Refused"]


class Refused(Exception):
    """A refusal with a stable machine code and an optional human detail."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail
