"""Start a subscription-route child from an environment ``child_env`` built.

The route policy decides which names a Pi, Hermes, OpenCode, Cursor or Antigravity child may
carry. This module is the only place those decisions become a process environment: it rebuilds
the checked snapshot with ``build_child_env`` and passes that result to ``Popen``. The parent
environment and ``os.environ`` are not the mapping ``Popen`` receives.

``child_env`` knows Claude Code, Codex and OpenAI. An Anthropic API key is the Claude profile's
route key. ``OPENAI_API_KEY`` is the OpenAI profile's route key. Both are inside the
``ValidatedEnv``, and every launch goes through ``spawn``. Any other credential name is refused.
Copilot and Nous own-loop children are started by ``own_loop``, not here.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence

from recursion_core.child_env import ChildRoute, EnvRefused, ValidatedEnv, build_child_env, spawn
from recursion_core.subscription_routes.own_loop import _ambient_secret

__all__ = ["launch_env", "spawn_route_process"]

# Names a poisoned parent must not be able to place on a route child, even if a later edit adds
# them to the route allowlist. The route credential is handled separately and is not in this set.
_TOKEN_NAMES = frozenset({
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GH_PAT",
    "GITHUB_COPILOT_API_TOKEN",
    "COPILOT_GITHUB_TOKEN",
    "COPILOT_API_URL",
    "ANTHROPIC_OAUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
})


def _blocked_secret(name: str) -> bool:
    """The own-loop families, plus harness tokens that do not match those suffixes.

    The route credential is removed from the parent mapping before this runs, then injected under
    its own name, so ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` are not dropped by the ``_KEY`` suffix.
    """

    if _ambient_secret(name):
        return True
    return name.upper() in _TOKEN_NAMES


# The only names a caller may ask this module to inject. HOME, PATH and loader variables are not
# credentials: accepting them would put back a value ``build_child_env`` had already refused.
_CREDENTIAL_NAMES = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY"})


def launch_env(
    child_env: Mapping[str, str],
    *,
    route_home: str,
    credential_env: str = "",
) -> tuple[ValidatedEnv, dict[str, str]]:
    """Rebuild ``child_env`` through ``build_child_env``. The second value is what a process receives.

    ``credential_env`` is the one variable ``check_route`` already accepted. ``ANTHROPIC_API_KEY`` is
    the Claude profile's route key. ``OPENAI_API_KEY`` is the OpenAI profile's route key. Both are
    inside the ``ValidatedEnv``. Any other name, including ``HOME`` and ``LD_PRELOAD``, is refused.
    A secret that arrived only in the parent process is not in ``child_env`` and is not read from
    ``os.environ``.
    """

    checked = dict(child_env)
    credential = credential_env if isinstance(credential_env, str) else ""
    if credential not in _CREDENTIAL_NAMES and credential != "":
        raise EnvRefused(credential or None, "credential_env_denied")
    parent = {
        name: value for name, value in checked.items()
        if name not in ("HOME", credential) and not _blocked_secret(name)
    }
    if credential == "ANTHROPIC_API_KEY":
        validated = build_child_env(
            "claude_code", home=route_home, auth_mode="api_key",
            route=ChildRoute(api_key=checked[credential]), parent=parent,
        )
    elif credential == "OPENAI_API_KEY":
        validated = build_child_env(
            "openai", home=route_home, auth_mode="api_key",
            route=ChildRoute(api_key=checked[credential]), parent=parent,
        )
    else:
        validated = build_child_env("codex", home=route_home, auth_mode="subscription", parent=parent)
    launched = validated.to_dict()
    return validated, launched


def spawn_route_process(
    argv: Sequence[str],
    child_env: Mapping[str, str],
    *,
    route_home: str,
    credential_env: str = "",
    cwd: str | None = None,
) -> subprocess.Popen:
    """Start ``argv`` with ``launch_env``. ``Popen`` is looked up at the call, so a test can patch it.

    Every launch goes through ``child_env.spawn``, including an OpenAI key. That is the executable,
    argv and ownership check. The launched mapping is the ``ValidatedEnv``; nothing is appended after it.
    """

    validated, _launched = launch_env(child_env, route_home=route_home, credential_env=credential_env)
    return spawn(argv, validated, cwd=cwd, popen=subprocess.Popen)
