"""F60 Arm E: Codex MCP approval marks under ``codex exec`` (CSL-762, candidate).

PRODUCT.md at csl-research ``055cc7a5c793b7135e5d0565dc4198cf96230531`` (SHA-256
``e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf``), O41: under
``codex exec``, whose approval policy is never, a call that would prompt is refused,
unless a full-access permission profile (Disabled, External, or Managed with full-disk
write) or a per-tool ``approval_mode = "approve"`` approves it. ``strict_auto_review``
and a managed approval requirement can still force a prompt.

This module is an offline reading of openai/codex ``e0aa0434f415b0c5bc0fb64abd7f03c5510f9ed6``,
``codex-rs/core/src/mcp_tool_call.rs`` (``requires_mcp_tool_approval``,
``requires_mcp_tool_approval_for_mode`` and ``prepare_mcp_tool_approval``). At that commit
neither exception is a plain refusal under exec: ``strict_auto_review`` skips the
auto-approve shortcuts and sends the call to Codex's automatic reviewer
(``tools/approvals.rs`` ``request_guardian_approval``, ``require_guardian``), which
approves without a model review under a full-access profile and otherwise runs a model
review that can approve or deny. A managed requirement (``guardian/decision.rs``,
``auto_review_required_for_model`` or a reviewer that cannot be set to the user) does not
skip the shortcuts; a call that still needs approval goes to the same reviewer. The
outcome ``"auto_review"`` names that case: the call is neither known to run nor known to be
refused. It assumes no permission-request hook and the default approvals reviewer.

Status (PRODUCT.md Rule 4): grounded in that source reading, unmeasured. It does not spawn
Codex, talk to a provider, or open a network connection. No ``codex exec`` run backs it, and
the live ``codex`` binary version is unmeasured here.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

# Source commit read 2026-11-08. Live ``codex --version`` was not run (no provider call).
CODEX_SOURCE_COMMIT = "e0aa0434f415b0c5bc0fb64abd7f03c5510f9ed6"
CODEX_SOURCE_FILE = "codex-rs/core/src/mcp_tool_call.rs"
CODEX_BINARY_VERSION: str | None = None

ApprovalMode = Literal["auto", "prompt", "writes", "approve"]
ExecOutcome = Literal["allowed", "refused", "auto_review"]

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
    """Would this MCP call run, be refused, or go to the automatic reviewer under ``codex exec``?

    Approval policy never, default approvals reviewer, no permission-request hook.
    """

    needs_approval = requires_approval_for_mode(annotations, approval_mode)
    full_access = profile in FULL_ACCESS_PROFILES
    if strict_auto_review:
        # Shortcuts skipped; the reviewer approves outright only under full access.
        return "allowed" if full_access else "auto_review"
    if full_access or not needs_approval:  # per-tool approve never needs approval
        return "allowed"
    if managed_approval:
        return "auto_review"
    return "refused"
