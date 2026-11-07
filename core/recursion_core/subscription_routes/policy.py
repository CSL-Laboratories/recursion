"""Refuse Claude-subscription routes in Pi, Hermes and OpenCode profiles (CSL-786, candidate).

Q12 (PRODUCT.md at csl-research #1074 head 09d4e762): the Claude subscription is used only
inside Claude Code; other harnesses use an Anthropic API key. Three harnesses now offer a
Claude-subscription route (csl-research CSL-712 grounding, #1077):

* Pi reads ``ANTHROPIC_OAUTH_TOKEN`` and ``ANTHROPIC_AUTH_TOKEN`` as Anthropic credentials and
  has an "Anthropic OAuth flow (Claude Pro/Max)" (earendil-works/pi@085c84e9
  packages/ai/src/env-api-keys.ts:78-81, packages/ai/src/auth/oauth/anthropic.ts:1-2);
* Hermes discovers ``ANTHROPIC_TOKEN`` and ``CLAUDE_CODE_OAUTH_TOKEN`` (the latter routed "down
  the OAuth path by prefix") and "will use Claude Code's credential store directly when available"
  (NousResearch/hermes-agent@2f7e62ba hermes_cli/auth.py:203-209; Hermes providers docs);
* OpenCode 2.2.3+ has a Claude Agent provider that "runs Claude models through the user's own
  Claude Code login, using the Claude Agent SDK" (OpenCode PR #58782, merged 2026-10-30).

``check_route`` validates one engine profile and the environment the core intends to give the
child, and raises ``SubscriptionRouteRefused`` before any spawn. It fails closed: the provider,
auth kind and credential variable must be explicit and on the profile's list of API-key
providers, and the child env may hold only a small explicit baseline plus that one credential.
The vault records do not establish every name and file each engine reads to pick its login
(Pi's agent-dir variable and auth file, Hermes's own auth file), so every other name refuses;
the engine config and home selectors the records do name get their own cause. Stored-credential
discovery is refused by checking, with ``lstat`` only, that the child's home is route-owned, is
not the operator's home and holds no Claude Code store and no Pi, Hermes or OpenCode state or
login entry. No credential file is opened or read.

``guard_then_spawn`` takes the launcher and argv as trusted, core-owned inputs: it does not parse
argv, so an engine flag that selects a provider or config is the caller's responsibility. It
only refuses launch options that would replace the env or widen the launch.

Candidate code: no engine, provider or network is used, and nothing here claims a running core
calls it yet. Provider ids marked unverified below were not read from the engines' source.
"""

from __future__ import annotations

import os
import pwd
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

__all__ = [
    "CLAUDE_SUBSCRIPTION_ENV",
    "ENGINES",
    "ENGINE_SELECTOR_ENV",
    "ENGINE_STORE_ENTRIES",
    "RouteDecision",
    "SAFE_ENV",
    "SubscriptionRouteRefused",
    "check_route",
    "guard_then_spawn",
]


class SubscriptionRouteRefused(Exception):
    """A profile could route through a Claude subscription (Q12), or cannot be shown not to."""

    code = "subscription_route_refused"

    def __init__(self, engine: str | None, cause: str, detail: str = "") -> None:
        super().__init__(f"{engine or '-'}: {cause}" + (f" ({detail})" if detail else ""))
        self.engine, self.cause, self.detail = engine, cause, detail


# Every name that carries a Claude subscription (OAuth) credential for at least one of the three
# engines. Any of them in the child env refuses, whatever the engine: an engine update can start
# reading a name another engine already reads.
CLAUDE_SUBSCRIPTION_ENV = (
    "ANTHROPIC_OAUTH_TOKEN",  # Pi
    "ANTHROPIC_AUTH_TOKEN",  # Pi (and Claude Code's bearer-token name)
    "ANTHROPIC_TOKEN",  # Hermes
    "CLAUDE_CODE_OAUTH_TOKEN",  # Hermes; Claude Code setup-token
)
# Names that point an engine at a Claude Code credential store somewhere else.
CLAUDE_STORE_ENV = ("CLAUDE_CONFIG_DIR",)
# Engine config, home and store selectors named in the vault records: each can move an engine to a
# config or store that picks a stored login or defines a provider inline. Refused with their own cause;
# any other name outside SAFE_ENV refuses too. Sources: OPENCODE_CONFIG* (csl496 deep419/f6/f9 records,
# csl501 custody map); HERMES_HOME (Hermes frontier h2 records); XDG_CONFIG_HOME, XDG_DATA_HOME (csl653
# grounding, CX-306); PI_CODING_AGENT_DIR (named in review; not found in the records, refused anyway).
ENGINE_SELECTOR_ENV = (
    "HERMES_HOME",
    "PI_CODING_AGENT_DIR",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_CONFIG_CONTENT",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
)
# The only names a Pi/Hermes/OpenCode child may get besides its one selected credential.
SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "TMPDIR")
# Store entries under a home. Checked with lstat for presence only; never opened or read.
CLAUDE_STORE_ENTRIES = (".claude", ".claude.json", ".config/claude")
# Pi, Hermes and OpenCode state that can hold or point at a stored login. Pi's and Hermes's own auth files
# are not named in the records, so their whole state dirs refuse: ~/.pi/agent (Pi frontier records),
# ~/.hermes (Hermes frontier h2 records). OpenCode: ~/.local/share/opencode/auth.json (csl501 custody map)
# and its config dir ~/.config/opencode. Hermes imports ~/.codex/auth.json (CSL-712 H3).
ENGINE_STORE_ENTRIES = (".pi", ".hermes", ".local/share/opencode", ".config/opencode", ".codex/auth.json")
# Launch options a caller may not pass: each would replace the child env or widen the launch.
_FORBIDDEN_LAUNCH = frozenset(
    {"env", "shell", "executable", "preexec_fn", "close_fds", "pass_fds", "user", "group", "extra_groups", "cwd_fd"}
)
# A Claude Code OAuth token in an API-key variable. Inferred prefix (not read from engine source):
# Hermes routes some Anthropic tokens "down the OAuth path by prefix" (auth.py:203-209).
_OAUTH_PREFIXES = ("sk-ant-oat",)


@dataclass(frozen=True)
class Provider:
    """One API-key provider an engine profile may select."""

    family: str  # "anthropic" or another vendor
    credential_env: tuple[str, ...]  # names that may carry its API key


@dataclass(frozen=True)
class Engine:
    engine: str
    providers: Mapping[str, Provider]
    # Provider ids that are Claude-subscription routes (refused with a specific cause).
    subscription_providers: tuple[str, ...]
    subscription_auth: tuple[str, ...] = ("oauth", "subscription", "claude_login", "setup_token", "stored")


_API = {
    "anthropic": Provider("anthropic", ("ANTHROPIC_API_KEY",)),
    "openai": Provider("openai", ("OPENAI_API_KEY",)),
}

ENGINES: Mapping[str, Engine] = MappingProxyType(
    {
        "pi": Engine("pi", MappingProxyType(dict(_API)), ("anthropic-oauth", "claude-pro", "claude-max")),
        "hermes": Engine("hermes", MappingProxyType(dict(_API)), ("anthropic-oauth", "claude-code")),
        # OpenCode's Claude Agent provider id is unverified ("claude-agent" is the PR's name for it);
        # every id not on the API list refuses anyway.
        "opencode": Engine("opencode", MappingProxyType(dict(_API)), ("claude-agent", "claude-code", "claude-agent-sdk")),
    }
)


@dataclass(frozen=True)
class RouteDecision:
    engine: str
    provider: str
    credential_env: str
    flags: tuple[str, ...] = field(default=())  # allowed, but worth a line in the run record


def check_route(
    engine: str,
    profile: Mapping[str, object],
    child_env: Mapping[str, str],
    *,
    route_home: str,
    parent_env: Mapping[str, str] | None = None,
    operator_home: str | None = None,
) -> RouteDecision:
    """Validate one Pi/Hermes/OpenCode profile; raise ``SubscriptionRouteRefused`` or return the decision.

    ``profile`` keys: ``provider`` (id), ``auth`` (must be ``"api_key"``), ``credential_env`` (the variable
    that carries the key) and optionally ``model`` (``"<provider>/<model>"``). ``child_env`` is the exact
    environment the core will pass; ``route_home`` its HOME.
    """

    spec = ENGINES.get(engine) if isinstance(engine, str) else None
    if spec is None:
        raise SubscriptionRouteRefused(engine, "unknown_engine")
    if not isinstance(profile, Mapping):
        raise SubscriptionRouteRefused(engine, "profile_malformed")
    unknown = sorted(set(profile) - {"provider", "auth", "credential_env", "model"})
    if unknown:
        raise SubscriptionRouteRefused(engine, "profile_unknown_field", ",".join(unknown))
    provider, auth, credential = profile.get("provider"), profile.get("auth"), profile.get("credential_env")
    model = profile.get("model")
    for name, value in (("provider", provider), ("auth", auth), ("credential_env", credential)):
        if not isinstance(value, str) or not value.strip():
            raise SubscriptionRouteRefused(engine, f"{name}_missing")
    provider = provider.strip().lower()
    if provider in spec.subscription_providers or _looks_like_claude_login(provider):
        raise SubscriptionRouteRefused(engine, "claude_subscription_provider", provider)
    if auth.strip().lower() in spec.subscription_auth:
        raise SubscriptionRouteRefused(engine, "claude_subscription_auth" if provider == "anthropic" else "subscription_auth", auth)
    if auth != "api_key":
        raise SubscriptionRouteRefused(engine, "auth_not_api_key", auth)
    if provider not in spec.providers:
        raise SubscriptionRouteRefused(engine, "provider_not_allowed", provider)
    if model is not None:
        if not isinstance(model, str) or "/" not in model:
            raise SubscriptionRouteRefused(engine, "model_malformed")
        model_provider = model.split("/", 1)[0].strip().lower()
        if model_provider in spec.subscription_providers or _looks_like_claude_login(model_provider):
            raise SubscriptionRouteRefused(engine, "claude_subscription_provider", model_provider)
        if model_provider != provider:
            raise SubscriptionRouteRefused(engine, "model_provider_mismatch", model_provider)
    if credential not in spec.providers[provider].credential_env:
        raise SubscriptionRouteRefused(engine, "credential_env_not_allowed", credential)

    env = dict(child_env)
    present = sorted(name for name in CLAUDE_SUBSCRIPTION_ENV if name in env)
    if present:
        raise SubscriptionRouteRefused(engine, "claude_subscription_env", ",".join(present))
    stores = sorted(name for name in CLAUDE_STORE_ENV if name in env)
    if stores:
        raise SubscriptionRouteRefused(engine, "claude_store_env", ",".join(stores))
    if not env.get(credential):
        raise SubscriptionRouteRefused(engine, "credential_missing", credential)
    if env[credential].startswith(_OAUTH_PREFIXES):
        raise SubscriptionRouteRefused(engine, "claude_oauth_token_in_api_key_env", credential)
    others = sorted(
        name for name in env if name != credential and any(name in p.credential_env for p in spec.providers.values())
    )
    if others:  # one credential per route: a second key could be picked by the engine's own precedence
        raise SubscriptionRouteRefused(engine, "extra_credential_env", ",".join(others))
    selectors = sorted(name for name in ENGINE_SELECTOR_ENV if name in env)
    if selectors:
        raise SubscriptionRouteRefused(engine, "engine_selector_env", ",".join(selectors))
    extra = sorted(name for name in env if name != credential and name not in SAFE_ENV)
    if extra:  # fail closed: the records do not establish which other names an engine reads to pick a login
        raise SubscriptionRouteRefused(engine, "child_env_not_allowed", ",".join(extra))

    _check_home(engine, env, route_home, parent_env, operator_home)
    flags = tuple(f"parent_env_has:{name}" for name in CLAUDE_SUBSCRIPTION_ENV if name in (parent_env or {}))
    return RouteDecision(engine, provider, credential, flags)


def guard_then_spawn(
    launcher: Callable[..., object],
    argv: list[str],
    engine: str,
    profile: Mapping[str, object],
    child_env: Mapping[str, str],
    *,
    route_home: str,
    parent_env: Mapping[str, str] | None = None,
    operator_home: str | None = None,
    **launch_kwargs: object,
) -> tuple[RouteDecision, object]:
    """Run ``check_route`` and only then call ``launcher(argv, env=child_env, ...)``. A refusal never reaches it.

    ``launcher`` and ``argv`` are trusted, core-owned inputs: argv is not parsed, so it must not carry a
    provider, config or login flag the profile did not choose. ``launch_kwargs`` may not replace the env
    or widen the launch (shell, executable, preexec_fn, fds, user or group); those refuse before any check.
    """

    bad = sorted(_FORBIDDEN_LAUNCH.intersection(launch_kwargs))
    if bad:
        raise SubscriptionRouteRefused(engine if isinstance(engine, str) else None, "launch_option_denied", ",".join(bad))
    if isinstance(argv, (str, bytes)) or not argv or not all(isinstance(item, str) for item in argv):
        raise SubscriptionRouteRefused(engine if isinstance(engine, str) else None, "argv_malformed")
    decision = check_route(
        engine, profile, child_env, route_home=route_home, parent_env=parent_env, operator_home=operator_home
    )
    return decision, launcher(list(argv), **launch_kwargs, env=dict(child_env), shell=False)


def _looks_like_claude_login(provider: str) -> bool:
    # Fail closed on spellings of a Claude login provider not on the list (an engine update can rename it).
    squashed = provider.replace("_", "-")
    return squashed.startswith("claude") or squashed.endswith(("-oauth", "-subscription")) and "anthropic" in squashed


def _check_home(
    engine: str,
    env: Mapping[str, str],
    route_home: str,
    parent_env: Mapping[str, str] | None,
    operator_home: str | None,
) -> None:
    """The child's home is route-owned, private, not the operator's, and holds no store or engine state entry."""

    if os.name == "nt":
        raise SubscriptionRouteRefused(engine, "platform_unsupported")
    if not isinstance(route_home, str) or not os.path.isabs(route_home):
        raise SubscriptionRouteRefused(engine, "route_home_missing")
    if env.get("HOME") != route_home:
        raise SubscriptionRouteRefused(engine, "child_home_not_route_home")
    real = os.path.realpath(route_home)
    operator = {os.path.realpath(path) for path in _operator_homes(parent_env, operator_home)}
    if real in operator or any(real == home or real.startswith(home.rstrip("/") + "/") for home in operator):
        raise SubscriptionRouteRefused(engine, "route_home_is_operator_home")
    try:
        st = os.lstat(route_home)
    except OSError:
        raise SubscriptionRouteRefused(engine, "route_home_missing") from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise SubscriptionRouteRefused(engine, "route_home_not_private")
    for entry in CLAUDE_STORE_ENTRIES:
        if os.path.lexists(os.path.join(route_home, entry)):  # presence only; never opened or read
            raise SubscriptionRouteRefused(engine, "claude_store_in_route_home", entry)
    for entry in ENGINE_STORE_ENTRIES:
        if os.path.lexists(os.path.join(route_home, entry)):  # presence only; never opened or read
            raise SubscriptionRouteRefused(engine, "engine_store_in_route_home", entry)


def _operator_homes(parent_env: Mapping[str, str] | None, operator_home: str | None) -> list[str]:
    homes = [path for path in (operator_home, (parent_env or {}).get("HOME")) if path]
    try:
        homes.append(pwd.getpwuid(os.geteuid()).pw_dir)  # the passwd entry's home path, no file in it is read
    except KeyError:
        pass
    return [path for path in homes if os.path.isabs(path)]
