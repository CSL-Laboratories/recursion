"""F60 Arm E: Codex MCP approval marks under ``codex exec`` (CSL-762, candidate).

PRODUCT.md at csl-research ``055cc7a5c793b7135e5d0565dc4198cf96230531`` (SHA-256
``e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf``), O41: under
``codex exec``, whose approval policy is never, a call that would prompt is refused,
unless a full-access permission profile (Disabled, External, or Managed with full-disk
write) or a per-tool ``approval_mode = "approve"`` approves it. ``strict_auto_review``
and a managed approval requirement can still force a prompt, which exec then refuses.

This module is an offline reading of openai/codex ``e0aa0434f415b0c5bc0fb64abd7f03c5510f9ed6``,
``codex-rs/core/src/mcp_tool_call.rs`` (``requires_mcp_tool_approval`` and
``requires_mcp_tool_approval_for_mode``). It does not spawn Codex, talk to a provider,
or open a network connection. The live ``codex`` binary version is unmeasured here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

# Source commit read 2026-11-08. Live ``codex --version`` was not run (no provider call).
CODEX_SOURCE_COMMIT = "e0aa0434f415b0c5bc0fb64abd7f03c5510f9ed6"
CODEX_SOURCE_FILE = "codex-rs/core/src/mcp_tool_call.rs"
CODEX_BINARY_VERSION: str | None = None

ApprovalMode = Literal["auto", "prompt", "writes", "approve"]
ExecOutcome = Literal["allowed", "refused"]

# PRODUCT.md O41: full-access profiles that auto-approve a prompt under exec.
FULL_ACCESS_PROFILES = frozenset({"disabled", "external", "managed_full_disk"})

_MODES = frozenset({"auto", "prompt", "writes", "approve"})


def _hint(annotations: Mapping[str, object] | None, name: str) -> bool | None:
    if not annotations or name not in annotations:
        return None
    value = annotations[name]
    if value is True:
        return True
    if value is False:
        return False
    return None


def requires_mcp_tool_approval(annotations: Mapping[str, object] | None) -> bool:
    """Codex Auto-mode mark check (e0aa0434 ``requires_mcp_tool_approval``).

    ``destructiveHint: true`` always prompts, even if ``readOnlyHint`` is also true
    (the code order; Codex docs say the reverse). Otherwise ``readOnlyHint: true``
    never prompts. Otherwise it prompts if ``destructiveHint`` is unset or true, or
    ``openWorldHint`` is unset or true. MCP defaults both of those to true.
    """

    destructive = _hint(annotations, "destructiveHint")
    if destructive is True:
        return True
    if _hint(annotations, "readOnlyHint") is True:
        return False
    open_world = _hint(annotations, "openWorldHint")
    return (True if destructive is None else destructive) or (
        True if open_world is None else open_world
    )


def requires_approval_for_mode(
    annotations: Mapping[str, object] | None,
    approval_mode: str = "auto",
) -> bool:
    """Per-tool ``approval_mode`` overlay (e0aa0434 ``requires_mcp_tool_approval_for_mode``)."""

    mode = approval_mode or "auto"
    if mode not in _MODES:
        raise ValueError(f"unknown approval_mode: {mode!r}")
    if mode == "auto":
        return requires_mcp_tool_approval(annotations)
    if mode == "prompt":
        return True
    if mode == "writes":
        return _hint(annotations, "readOnlyHint") is not True
    return False  # approve


def exec_outcome(
    annotations: Mapping[str, object] | None,
    approval_mode: str = "auto",
    *,
    profile: str = "default",
    strict_auto_review: bool = False,
    managed_approval: bool = False,
) -> ExecOutcome:
    """Would this MCP call run or be refused under ``codex exec`` (approval policy never)?"""

    if strict_auto_review or managed_approval:
        return "refused"
    if profile in FULL_ACCESS_PROFILES:
        return "allowed"
    if requires_approval_for_mode(annotations, approval_mode):
        return "refused"
    return "allowed"
