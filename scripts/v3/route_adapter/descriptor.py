"""AT1 surface rows (D-RP2-4, D-RP2-5) and the live-driver skips.

These rows are adapter admission records. They are not the frozen M1
``RouteCapabilityDescriptor@1`` schema, and they do not claim a live
provider run.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# D-RP2-1. Live Codex App Server proof is out of this slice.
CODEX_APP_SERVER_PROOF = {
    "driver": "codex-app-server",
    "status": "skipped",
    "live_calls": False,
    "reason": (
        "D-RP2-1: AT1 closes on the fake driver. Codex App Server "
        "start, send, observe, and cancel are a separate follow-up cell, "
        "gated on Lucas confirming existing authorized access with no new "
        "spend and no borrowed tokens. This package places no live calls. "
        "There is no API-keyed Claude path in AT1. Grok Build remains a "
        "later ranking, not AT1 scope."
    ),
}

# D-RP2-4. Caller-only Control-MCP is not admitted until it is validated.
CONTROL_MCP_CALLER_ONLY_ADMITTED = False

# D-RP2-6. Scope exclusion. Not a legal finding.
F5_COUNSEL_GATE = "closed"
SUBSCRIPTION_FANOUT_IN_AT1 = False

_PRODUCTS = ("build", "bot", "api")
_INBOUND = ("supported", "unsupported", "unestablished")


@dataclass(frozen=True)
class RouteSurfaceDescriptor:
    """One product row. Build, Bot, and API are never collapsed together."""

    row_id: str
    product: str
    provider: str
    inbound_session_control: str
    experimental_driver_admitted: bool
    vendor_experimental_api: bool
    vendor_experimental_method: str | None = None
    control_mcp: str = "not-admitted"
    notes: str = ""

    def __post_init__(self) -> None:
        if self.product not in _PRODUCTS:
            raise ValueError(f"unknown product: {self.product}")
        if self.inbound_session_control not in _INBOUND:
            raise ValueError(f"unknown inbound state: {self.inbound_session_control}")
        if self.vendor_experimental_api and not self.vendor_experimental_method:
            raise ValueError("vendor experimentalApi requires a method name")
        if self.control_mcp not in ("not-admitted", "caller-only"):
            raise ValueError(f"unknown control_mcp state: {self.control_mcp}")
        if self.control_mcp == "caller-only" and not CONTROL_MCP_CALLER_ONLY_ADMITTED:
            raise ValueError("control-mcp caller-only is not validated")


def grok_rows() -> tuple[RouteSurfaceDescriptor, ...]:
    """Grok Build, Bot, and model API as three rows.

    Bot inbound session control is unsupported. Control-MCP is not
    labeled caller-only: that interim path is unvalidated, so claiming
    it would overstate what the adapter can do.
    """

    return (
        RouteSurfaceDescriptor(
            row_id="grok-build",
            product="build",
            provider="grok",
            inbound_session_control="unestablished",
            experimental_driver_admitted=False,
            vendor_experimental_api=False,
            notes=(
                "Separate from Bot and the model API. "
                "AT1 does not claim a live Build lifecycle."
            ),
        ),
        RouteSurfaceDescriptor(
            row_id="grok-bot",
            product="bot",
            provider="grok",
            inbound_session_control="unsupported",
            experimental_driver_admitted=False,
            vendor_experimental_api=False,
            notes=(
                "Bot inbound is unsupported by the AT1 adapter. "
                "A general lifecycle contract is unestablished. "
                "Control-MCP, if used later, would be Bot calling Recursion "
                "and is not validated here."
            ),
        ),
        RouteSurfaceDescriptor(
            row_id="grok-api",
            product="api",
            provider="grok",
            inbound_session_control="unestablished",
            experimental_driver_admitted=False,
            vendor_experimental_api=False,
            notes=(
                "Model API is a separate row. "
                "It is not evidence of Bot session control."
            ),
        ),
    )


def admit_experimental_driver(row: RouteSurfaceDescriptor) -> RouteSurfaceDescriptor:
    """Opt in a Recursion experimental driver. Leaves the vendor flag alone."""

    return replace(row, experimental_driver_admitted=True)


def require_vendor_experimental_api(
    row: RouteSurfaceDescriptor,
    method: str,
) -> RouteSurfaceDescriptor:
    """Set vendor ``experimentalApi`` only when a named method needs it."""

    if not isinstance(method, str) or not method.strip():
        raise ValueError("vendor experimentalApi stays false unless a required method needs it")
    return replace(
        row,
        vendor_experimental_api=True,
        vendor_experimental_method=method.strip(),
    )
