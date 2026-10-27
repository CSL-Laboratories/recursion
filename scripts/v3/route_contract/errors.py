"""Typed pre-spawn refusals. ``str(error)`` is a fixed token, never request text."""

from __future__ import annotations


class RouteRefusal(Exception):
    """A request refused before any process was spawned."""

    code = "route-refusal"

    def __init__(self, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(self.code)

    def to_record(self, request_id: str) -> dict[str, object]:
        return {
            "type": "RouteRefusal@0",
            "request_id": request_id,
            "code": self.code,
            "dispatched": False,
        }


class ToolPolicyRefused(RouteRefusal):
    code = "tool_policy_refused"


class SecretRefused(RouteRefusal):
    code = "secret_refused"


class CwdRefused(RouteRefusal):
    code = "cwd_refused"


class Unsupported(RouteRefusal):
    code = "unsupported"
