"""One refusal type with a stable code, shared by every part of the plugin-security core."""

from __future__ import annotations

__all__ = ["Refused"]


class Refused(Exception):
    """A typed refusal. ``code`` is stable and goes on the wire; ``detail`` is never secret."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail
