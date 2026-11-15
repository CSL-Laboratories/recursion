"""Start a subscription-route child from an environment ``child_env`` built.

The route policy decides which names a Pi, Hermes, OpenCode, Cursor or Antigravity child may
carry. This module is the only place those decisions become a process environment: it rebuilds
the checked snapshot with ``build_child_env`` and passes that result to ``Popen``. The parent
environment and ``os.environ`` are not the mapping ``Popen`` receives.

``child_env`` knows Claude Code and Codex, not these harnesses. An Anthropic API key is the
Claude profile's route key, so it is inside the ``ValidatedEnv``. Any other credential the route
policy already accepted (OpenAI's ``OPENAI_API_KEY``) is copied onto that dict under the name the
harness reads. Copilot and Nous own-loop children have no such credential; they go through
``spawn`` with the Codex profile, which does not pass a Claude sign-in token through.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence

from recursion_core.child_env import ChildRoute, EnvRefused, ValidatedEnv, build_child_env, spawn

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
    upper = name.upper()
    if upper.startswith(("AWS_", "GIT_CONFIG")) or upper.endswith("_PAT"):
        return True
    return upper in _TOKEN_NAMES


def launch_env(
    child_env: Mapping[str, str],
    *,
    route_home: str,
    credential_env: str = "",
) -> tuple[ValidatedEnv, dict[str, str]]:
    """Rebuild ``child_env`` through ``build_child_env``. The second value is what a process receives.

    ``credential_env`` is the one variable ``check_route`` already accepted. It is injected by the
    Claude profile when it is ``ANTHROPIC_API_KEY``. Any other checked credential is set on the
    copy ``ValidatedEnv.to_dict`` returns, under the harness's own name. A secret that arrived only
    in the parent process is not in ``child_env`` and is not read from ``os.environ``.
    """

    checked = dict(child_env)
    if not isinstance(credential_env, str) or credential_env not in ("", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        raise EnvRefused(None, "route_key_malformed")
    credential = credential_env
    if credential:
        value = checked.get(credential)
        if not isinstance(value, str) or not value or "\0" in value:
            raise EnvRefused(credential, "route_key_malformed")
    parent = {
        name: value for name, value in checked.items()
        if name not in ("HOME", credential) and not _blocked_secret(name)
    }
    if credential == "ANTHROPIC_API_KEY":
        validated = build_child_env(
            "claude_code", home=route_home, auth_mode="api_key",
            route=ChildRoute(api_key=checked[credential]), parent=parent,
        )
        return validated, validated.to_dict()
    validated = build_child_env("codex", home=route_home, auth_mode="subscription", parent=parent)
    launched = validated.to_dict()
    if credential:
        launched[credential] = checked[credential]
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

    When the launched mapping is exactly the ``ValidatedEnv``, ``child_env.spawn`` is the caller.
    A harness credential that profile does not name is passed as an explicit ``env`` on that same
    ``Popen``, still not ``os.environ`` and still not the parent mapping.
    """

    validated, launched = launch_env(child_env, route_home=route_home, credential_env=credential_env)
    def launch(resolved_argv, **kwargs):
        # Only launch_env's checked credential overlay differs from the validated
        # mapping. Executable resolution and process options always go through spawn.
        kwargs["env"] = dict(launched)
        return subprocess.Popen(resolved_argv, **kwargs)

    return spawn(argv, validated, cwd=cwd, popen=launch)
