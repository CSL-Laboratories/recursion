"""Operator Codex config snippet for the Control MCP (CSL-798, candidate). See README.md.

PRODUCT.md 055cc7a5, O41 (decided Nov 7, "All picks"): the operator's Codex config sets stop's
per-tool ``approval_mode = "approve"`` under ``mcp_servers.<id>.tools.<tool>``, so stop does not
prompt where that setting is in place. Exception: ``strict_auto_review`` or a managed approval
requirement can still force a prompt. Without the setting, stop prompts in Auto mode and is refused
under ``codex exec``. Start is left to Codex's default (it prompts unless the operator approves it);
this snippet does not auto-approve it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from recursion_core.control.mcp_tools import STOP_TOOL

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def codex_operator_snippet(server_id: str, command: str, args: list[str] | None = None) -> str:
    """TOML for the operator's ``~/.codex/config.toml``. ``command``/``args`` launch the Control MCP server."""

    if not _BARE_KEY.match(server_id):
        raise ValueError("server_id must be a bare TOML key")
    if not isinstance(command, str):
        raise ValueError("command must be a string")
    if args is None:
        argv: list[str] = []
    elif (
        isinstance(args, (str, bytes))
        or not isinstance(args, Sequence)
        or not all(isinstance(item, str) for item in args)
    ):
        raise ValueError("args must be a list of strings")
    else:
        argv = list(args)
    lines = [
        "# Recursion Control MCP (PRODUCT.md 055cc7a5, O41).",
        "# stop is marked destructiveHint: true, openWorldHint: false. The per-tool approval_mode below",
        "# makes Codex approve it without a prompt. strict_auto_review or a managed approval requirement",
        "# can still force a prompt. Without this setting, stop prompts in Auto mode and is refused under",
        "# `codex exec`. start is not auto-approved here.",
        f"[mcp_servers.{server_id}]",
        f"command = {json.dumps(command)}",
        f"args = [{', '.join(json.dumps(a) for a in argv)}]",
        "",
        f"[mcp_servers.{server_id}.tools.{STOP_TOOL}]",
        'approval_mode = "approve"',
        "",
    ]
    return "\n".join(lines)
