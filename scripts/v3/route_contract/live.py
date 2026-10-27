"""Live adapters. Every one reports ``unsupported`` until CSL-460 lands.

``LIVE_ADAPTERS_ENABLED`` is a constant, not configuration. Flipping it needs
the CSL-460 change and the recorded approvals in AGENTS.md hard rule 8.
"""

from __future__ import annotations

from scripts.v3.route_contract.errors import Unsupported

LIVE_ADAPTERS_ENABLED = False
LIVE_UNSUPPORTED_REASON = "awaiting-csl-460"

LIVE_ENGINE_BINARIES = frozenset(
    {"claude", "codex", "grok", "cursor-agent", "agent", "gemini", "opencode", "hermes"}
)
LIVE_ENGINE_IDS = tuple(sorted(LIVE_ENGINE_BINARIES))


class UnsupportedLiveAdapter:
    """Reports ``unsupported`` and refuses to execute. It holds no transport."""

    kind = "live"

    def __init__(self, engine_id: str) -> None:
        self.engine_id = engine_id

    def support(self) -> dict[str, object]:
        return {
            "type": "AdapterSupport@0",
            "engine_id": self.engine_id,
            "adapter_kind": "live",
            "state": "unsupported",
            "reason": LIVE_UNSUPPORTED_REASON,
            "live_calls": False,
        }

    def execute(self, *args: object, **kwargs: object) -> None:
        raise Unsupported()


def live_adapter(engine_id: str) -> UnsupportedLiveAdapter:
    """The only way to name a live adapter in v0. It is always unsupported."""

    return UnsupportedLiveAdapter(engine_id)
