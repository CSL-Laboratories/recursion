"""Operator Codex config snippet for the Control MCP (CSL-798, candidate). See README.md.

PRODUCT.md 055cc7a5, O41 (decided Nov 7, "All picks"): the operator's Codex config sets stop's
per-tool ``approval_mode = "approve"`` under ``mcp_servers.<id>.tools.<tool>``, so stop does not
prompt where that setting is in place. Exception: ``strict_auto_review`` or a managed approval
requirement can still force a prompt. Without the setting, stop prompts in Auto mode and is refused
under ``codex exec``. Start is left to Codex's default (it prompts unless the operator approves it);
this snippet does not auto-approve it.
"""

from __future__ import annotations

import re

from recursion_core.control.mcp_tools import STOP_TOOL

# fullmatch, not match with "$": "$" also matches before a trailing newline, so "rc\n" passed and the
# table header broke the file (Nov 12 review, finding 14).
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_SHORT_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def toml_string(value: str) -> str:
    """``value`` as a TOML basic string, ASCII only.

    Printable ASCII is written as itself; the short escapes are used where TOML has one; every other
    character is a Unicode scalar escape, ``\\uXXXX`` or, above U+FFFF, ``\\UXXXXXXXX`` (JSON's
    surrogate pairs are not valid TOML). DEL is escaped too, because TOML refuses it bare; ``json.dumps``
    escapes it as well (the read of #377 by DeepSeek V4.1 Flash, F11). Up to U+FFFF the text is what
    ``json.dumps`` wrote before. A lone surrogate is no Unicode scalar value and has no TOML form:
    ``ValueError``.
    """

    if type(value) is not str:
        raise TypeError("a TOML string value must be a str")
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif 0x20 <= code < 0x7F:
            out.append(ch)
        elif 0xD800 <= code <= 0xDFFF:
            raise ValueError("a lone surrogate has no TOML form")
        elif code <= 0xFFFF:
            out.append(f"\\u{code:04x}")  # lower-case hex, as json.dumps wrote it before
        else:
            out.append(f"\\U{code:08x}")
    out.append('"')
    return "".join(out)


def codex_operator_snippet(server_id: str, command: str, args: list[str] | None = None) -> str:
    """TOML for the operator's ``~/.codex/config.toml``. ``command``/``args`` launch the Control MCP server."""

    if type(server_id) is not str or _BARE_KEY.fullmatch(server_id) is None:
        raise ValueError("server_id must be a bare TOML key")
    lines = [
        "# Recursion Control MCP (PRODUCT.md 055cc7a5, O41).",
        "# stop is marked destructiveHint: true, openWorldHint: false. The per-tool approval_mode below",
        "# makes Codex approve it without a prompt. strict_auto_review or a managed approval requirement",
        "# can still force a prompt. Without this setting, stop prompts in Auto mode and is refused under",
        "# `codex exec`. start is not auto-approved here.",
        f"[mcp_servers.{server_id}]",
        f"command = {toml_string(command)}",
        f"args = [{', '.join(toml_string(a) for a in args or [])}]",
        "",
        f"[mcp_servers.{server_id}.tools.{STOP_TOOL}]",
        'approval_mode = "approve"',
        "",
    ]
    return "\n".join(lines)
