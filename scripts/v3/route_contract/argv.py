"""Zero-tools argv profiles and the deny-by-default argv inspector.

The profiles restate the argv grammar of an existing production runner (the
oracle) so a conformance run can compare byte for byte. Prompt text travels on
stdin, never argv. ``inspect_argv`` accepts only the exact profile for the
named engine, so a reordered, duplicated, extended or resumed argv is refused.
The oracle accepts reordered Claude flags; this inspector is stricter there.
"""

from __future__ import annotations

import re
from pathlib import Path
from collections.abc import Callable, Iterable, Iterator

from scripts.v3.route_contract.errors import CwdRefused, SecretRefused, ToolPolicyRefused

ENGINE_CLAUDE = "claude"
ENGINE_CODEX = "codex"
ENGINES = (ENGINE_CLAUDE, ENGINE_CODEX)
TOOL_PROFILE = "zero-tools"

CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "image_generation",
    "multi_agent",
    "multi_agent_v2",
    "standalone_web_search",
    "tool_call_mcp_elicitation",
    "skill_search",
    "skill_mcp_dependency_install",
    "code_mode_host",
    "unified_exec",
)

_MCP_FLAG = re.compile(r"^--mcp(-|s|c|$|=)", re.IGNORECASE)


def build_argv(
    engine: str,
    *,
    cli_model: str,
    max_turns: int = 6,
    schema_path: str | None = None,
) -> list[str]:
    """The only argv a zero-tools request may launch."""

    if engine == ENGINE_CLAUDE:
        return [
            "claude", "-p", "--output-format", "json",
            "--model", cli_model,
            "--max-turns", str(max_turns),
            "--tools", "", "--disallowedTools", "*",
            "--strict-mcp-config", "--safe-mode", "--restricted",
            "--setting-sources", "", "--disable-slash-commands",
            "--no-session-persistence", "--no-chrome",
            "--allowedTools", "none",
        ]
    if engine == ENGINE_CODEX:
        argv = [
            "codex", "exec", "--sandbox", "read-only",
            "--ignore-user-config", "--ignore-rules", "--ephemeral", "--strict-config",
            "-c", 'web_search="disabled"',
        ]
        for feature in CODEX_DISABLED_FEATURES:
            argv += ["--disable", feature]
        argv += ["--model", cli_model]
        if schema_path:
            argv += ["--output-schema", schema_path]
        argv += ["--json", "-"]
        return argv
    raise ToolPolicyRefused()


def _single_value(tokens: list[str], flag: str) -> str | None:
    hits = [i for i, token in enumerate(tokens) if token == flag]
    if len(hits) != 1 or hits[0] + 1 >= len(tokens):
        return None
    return tokens[hits[0] + 1]


def inspect_argv(argv: Iterable[str]) -> None:
    """Refuse any argv that is not exactly a zero-tools profile."""

    tokens = [str(token) for token in argv]
    if not tokens:
        raise ToolPolicyRefused()
    if any(_MCP_FLAG.match(token) for token in tokens):
        raise ToolPolicyRefused()
    engine = Path(tokens[0]).name.lower()
    if engine not in ENGINES:
        raise ToolPolicyRefused()
    model = _single_value(tokens, "--model")
    if not model or model.startswith("-"):
        raise ToolPolicyRefused()
    turns = 6
    if engine == ENGINE_CLAUDE:
        raw_turns = _single_value(tokens, "--max-turns")
        if raw_turns is None or not raw_turns.isdecimal() or int(raw_turns) < 1:
            raise ToolPolicyRefused()
        turns = int(raw_turns)
    schema_path = None
    if engine == ENGINE_CODEX and "--output-schema" in tokens:
        schema_path = _single_value(tokens, "--output-schema")
        if not schema_path or schema_path.startswith("-"):
            raise ToolPolicyRefused()
    expected = build_argv(engine, cli_model=model, max_turns=turns, schema_path=schema_path)
    if tokens != expected:
        raise ToolPolicyRefused()


def assert_cwd(cwd: str | None, *, pinned_cwd: str | None) -> None:
    if pinned_cwd is not None and cwd != pinned_cwd:
        raise CwdRefused()


SecretSource = Iterable[str] | Callable[[], Iterable[str]] | None


def materialize_secrets(secret_values: SecretSource) -> SecretSource:
    """Copy a one-shot iterator once; keep anything re-iterable live.

    Callables and collections (a list the caller rotates in place, a set) are
    kept by reference and read on every check, so a rotated secret is refused
    from the next check on. Only a one-shot iterator is copied, because it can
    be read only once.
    """

    if isinstance(secret_values, Iterator):
        return tuple(secret_values)
    return secret_values


def assert_no_secret(argv: Iterable[str], prompt: str, secret_values: SecretSource) -> None:
    """Refuse when any provisioned secret value is in the prompt or any argv token."""

    values = secret_values() if callable(secret_values) else secret_values
    tokens = list(argv)
    for secret in tuple(values or ()):
        if secret and (secret in prompt or any(secret in token for token in tokens)):
            raise SecretRefused()
