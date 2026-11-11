"""Refuse Claude-subscription routes in Pi, Hermes and OpenCode profiles (CSL-786, candidate).

Q12 as widened 2026-11-08 (PRODUCT.md 055cc7a5, CSLR-D-011, sections 2 and 3.3; this replaces the 09d4e762
"only inside Claude Code" reading): a subscription may run through another harness's own sign-in only where
that provider's pages allow that harness; Claude-subscription sign-ins in other harnesses stay refused until
an Anthropic approval for that harness is found, and an Anthropic API key still works. Three harnesses offer a
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
login entry. No credential file is opened or read. The one file read is a Hermes
``.hermes/config.yaml`` that is already exactly the size of the adopt switch Recursion writes there
(37 bytes), to confirm it is that switch; a config of any other size refuses unread.

``guard_then_spawn`` takes the launcher and argv as trusted, core-owned inputs: it does not parse
argv, so an engine flag that selects a provider or config is the caller's responsibility. On a
pinned route ``argv[0]`` must be the pinned path. Launch options that would replace the env or
widen the launch refuse, ``cwd`` is forwarded, and any other option refuses unless the caller
lists it in ``RouteRequirements.extra_launch_options``.

Configured requirements (2026-11-09, Lucas; PRODUCT.md 055cc7a5 Q20, O44, H-06) come in as explicit
inputs: a ``RouteRequirements`` value and ``full_access_acknowledged``. Nothing here reads a config
file, the environment, a keyring or a prompt to find them, and nothing here renders a notice: the
driver owns prompts, saved configuration and the full-access danger notice.

2026-11-08 briefing (Lucas): allow a subscription sign-in in these harnesses only where the provider names
that harness. ChatGPT: OpenCode and Pi, through their registered Sign in with ChatGPT method only. Grok:
OpenCode and Hermes. Claude and Google sign-ins stay refused in all three; anything unconfirmed is refused; an API key
still works. PRODUCT.md 055cc7a5 section 2 (E9): "the E9 profile sets Hermes's
``auth.adopt_external_logins: false`` so it never reads Claude Code's stored login". Every Hermes
profile must carry that key, under Hermes's own name and set to ``False``, and the decision records it in
``engine_config`` for the driver to pass to Hermes. Moving HOME does not hide a login kept in the macOS
Keychain, so this is required on every platform, macOS included; a Hermes profile without it refuses. A profile
picks one with ``auth: "subscription"`` plus a ``method``; the method is checked, not just the provider
label (``SUBSCRIPTION_SIGN_INS``), and a ``model`` (``"<provider>/<model>"``) is required so the engine's
own default cannot pick a Claude model. The sign-in stays in the engine's own login store in the route home:
Recursion holds, copies or ships no token, and a subscription child env carries no credential at all.
That store is allowed by presence only and never read, so whether it also holds an adopted Claude or
Codex login is not known here: the decision says so with an ``own_login_store_not_inspected:<entry>``
flag. Whether to read or refuse those stores is open for Lucas (PRODUCT.md 055cc7a5 section 2).

Candidate code: no engine, provider or network is used, and nothing here claims a running core
calls it yet. Provider ids marked unverified below were not read from the engines' source.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

try:
    import pwd
except ImportError:  # Windows has no pwd; _check_home raises platform_unsupported instead
    pwd = None  # type: ignore[assignment]

from recursion_core.subscription_routes.wrapper_pin import (
    AntigravityPin,
    WrapperInstall,
    WrapperPinRefused,
    check_link_requirements,
    verify_antigravity_pin,
    verify_package_pin,
)

__all__ = [
    "CLAUDE_SUBSCRIPTION_ENV",
    "SUBSCRIPTION_SIGN_INS",
    "ENGINES",
    "ENGINE_SELECTOR_ENV",
    "ENGINE_STORE_ENTRIES",
    "HERMES_ADOPT_EXTERNAL_LOGINS",
    "FORWARDED_LAUNCH_OPTIONS",
    "RouteDecision",
    "RouteRequirements",
    "SAFE_ENV",
    "SubscriptionRouteRefused",
    "check_antigravity_route",
    "check_cursor_route",
    "check_route",
    "guard_antigravity_then_spawn",
    "guard_cursor_then_spawn",
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
# Names the guard itself passes to the launcher; a caller may not pass or configure them.
_RESERVED_LAUNCH = frozenset({"engine_config", "model"})
# Launch options forwarded to the launcher by default. Anything else that is not forbidden refuses
# ``launch_option_unknown`` unless the caller lists it in ``RouteRequirements.extra_launch_options``.
# The full-access profile adds none.
FORWARDED_LAUNCH_OPTIONS = frozenset({"cwd"})
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


# Subscription sign-ins allowed per harness (2026-11-08 briefing): (provider label, sign-in method), both in
# Recursion's own normalised names. The engines' native provider and method ids are not in repository
# evidence, so no other spelling or alias is accepted; the injected argv/driver maps these to the engine.
# "grok_sign_in" is Recursion's label for the harness's own Grok account sign-in: the briefing allows Grok
# in OpenCode and Hermes without naming the method, and the engine-native method id is unconfirmed.
SUBSCRIPTION_SIGN_INS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "opencode": MappingProxyType({"chatgpt": "sign_in_with_chatgpt", "grok": "grok_sign_in"}),
        "pi": MappingProxyType({"chatgpt": "sign_in_with_chatgpt"}),
        "hermes": MappingProxyType({"grok": "grok_sign_in"}),
    }
)
# Each engine's own login store, which a subscription route home may hold (presence only; never read).
# Every other engine's store, and every Claude Code store, still refuses.
OWN_STORE_ENTRIES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {"pi": (".pi",), "hermes": (".hermes",), "opencode": (".local/share/opencode", ".config/opencode")}
)
# Hermes's own config setting that stops it adopting Claude Code's (and other) stored logins
# (PRODUCT.md 055cc7a5 section 2, E9). Required, exactly False, in every Hermes profile.
HERMES_ADOPT_EXTERNAL_LOGINS = "auth.adopt_external_logins"
# Google consumer and subscription labels: refused in all three harnesses.
_GOOGLE_LABELS = ("google", "gemini", "antigravity", "google_one", "google_ai")


@dataclass(frozen=True)
class RouteRequirements:
    """Requirements a user or plugin configured, handed in by the driver. The defaults are the secure ones.

    The guard only compares these values. It never loads them from a file, the environment, a keyring
    or a prompt; the driver owns prompts and saved configuration.

    * ``allowed_link_targets``: absolute, normalised real paths a package pin may follow a symlink to
      when the link leaves the install root (the path itself or anything under it). Default: none,
      so such a link refuses ``package_link_outside_root``.
    * ``extra_launch_options``: launch option names forwarded to the launcher besides
      ``FORWARDED_LAUNCH_OPTIONS``. A forbidden or reserved name here refuses
      ``requirements_forbidden_launch_option``.
    * ``owned_hermes_home``: the Hermes home the user says they own. When set it must resolve to the
      route home, or the Hermes route refuses ``hermes_home_not_owned``; ``.hermes/config.yaml`` is
      written or reused only there. Default ``None``: the route home Recursion created.
    """

    allowed_link_targets: tuple[str, ...] | frozenset[str] = ()
    extra_launch_options: tuple[str, ...] | frozenset[str] = ()
    owned_hermes_home: str | None = None


@dataclass(frozen=True)
class RouteDecision:
    engine: str
    provider: str
    credential_env: str  # "" on a subscription sign-in: no credential is in the child env
    flags: tuple[str, ...] = field(default=())  # allowed, but worth a line in the run record
    auth: str = "api_key"
    method: str = ""
    # Engine-native settings the driver must pass to the engine, e.g. Hermes's
    # (("auth.adopt_external_logins", False),). guard_then_spawn also writes Hermes's
    # config.yaml (or reuses its own exact copy) so a raw launch cannot keep the default (adoption on).
    engine_config: tuple[tuple[str, object], ...] = ()
    model: str = ""
    # "full_access_profile_acknowledged" is one of the flags when the run used the full-access profile.
    # True when a Pi, Hermes, Cursor or Antigravity wrapper matched its pin (2026-11-09).
    # OpenCode is not a pinned wrapper here, so its decisions stay False.
    beta: bool = False
    # The exact env check_route accepted. guard_* launches this copy, never a second read.
    # Left out of repr: on an API-key route it holds the key.
    child_env: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    pin_version: str = ""  # Antigravity pin only; empty for every other route


def check_route(
    engine: str,
    profile: Mapping[str, object],
    child_env: Mapping[str, str],
    *,
    route_home: str,
    parent_env: Mapping[str, str] | None = None,
    operator_home: str | None = None,
    wrapper: WrapperInstall | None = None,
    requirements: RouteRequirements | None = None,
    full_access_acknowledged: bool = False,
) -> RouteDecision:
    """Validate one Pi/Hermes/OpenCode profile; raise ``SubscriptionRouteRefused`` or return the decision.

    ``profile`` keys: ``provider`` (id), ``auth`` (must be ``"api_key"``), ``credential_env`` (the variable
    that carries the key) and optionally ``model`` (``"<provider>/<model>"``; required on a subscription
    route). ``borrow_claude_code_login`` is not a profile field: Hermes's own setting is
    ``auth.adopt_external_logins``, required below, and any other key refuses ``profile_unknown_field``.
    A Hermes profile needs ``"auth.adopt_external_logins": False``. ``child_env`` is the exact
    environment the core will pass; ``route_home`` its HOME. Pi and Hermes also need ``wrapper``: the
    launcher script and installed package files must match that pin, and the decision is then beta.
    ``requirements`` and ``full_access_acknowledged`` are the configured inputs (``RouteRequirements``);
    full access lets a package link leave the install root and relaxes nothing else here.
    """

    spec = ENGINES.get(engine) if isinstance(engine, str) else None
    if spec is None:
        raise SubscriptionRouteRefused(engine, "unknown_engine")
    checked = _checked_requirements(engine, requirements, full_access_acknowledged)
    if not isinstance(profile, Mapping):
        raise SubscriptionRouteRefused(engine, "profile_malformed")
    # borrow_claude_code_login is not accepted. Hermes uses auth.adopt_external_logins (below).
    known = {"provider", "auth", "credential_env", "model", "method"}
    if engine == "hermes":
        known.add(HERMES_ADOPT_EXTERNAL_LOGINS)
    unknown = sorted(str(key) for key in set(profile) - known)
    if unknown:
        raise SubscriptionRouteRefused(engine, "profile_unknown_field", ",".join(unknown))
    engine_config: tuple[tuple[str, object], ...] = ()
    if engine == "hermes":
        # Hermes "will use Claude Code's credential store directly when available", and on macOS that store
        # is the login Keychain, which a route home does not hide: the override is required everywhere.
        if HERMES_ADOPT_EXTERNAL_LOGINS not in profile:
            raise SubscriptionRouteRefused(engine, "hermes_adopt_external_logins_not_off", "missing")
        if profile[HERMES_ADOPT_EXTERNAL_LOGINS] is not False:
            raise SubscriptionRouteRefused(engine, "hermes_adopt_external_logins_not_off",
                                           repr(profile[HERMES_ADOPT_EXTERNAL_LOGINS])[:32])
        engine_config = ((HERMES_ADOPT_EXTERNAL_LOGINS, False),)
    if isinstance(profile.get("auth"), str) and _norm(profile["auth"]) == "subscription":
        return _check_subscription(engine, spec, profile, child_env, route_home, parent_env, operator_home,
                                   engine_config, wrapper, checked)
    if "method" in profile:
        raise SubscriptionRouteRefused(engine, "method_without_subscription")
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
    stored_model = ""
    if model is not None:
        stored_model = _require_model_name(engine, model)
        model_provider = stored_model.split("/", 1)[0].strip().lower()
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
    _check_hermes_home(engine, route_home, checked)
    flags = tuple(f"parent_env_has:{name}" for name in CLAUDE_SUBSCRIPTION_ENV if name in (parent_env or {}))
    return _finish(engine, provider, credential, flags, "api_key", "", engine_config, stored_model, env, wrapper,
                   checked)


def _norm(label: str) -> str:
    """Case, spaces, hyphens and underscores are spelling, not identity: "Sign in with ChatGPT" matches."""

    return "_".join(label.strip().lower().replace("-", " ").replace("_", " ").split())


def _require_model_name(engine: str, model: object) -> str:
    """``"<provider>/<model>"`` with both segments non-empty. ``"anthropic/"`` refuses."""

    if not isinstance(model, str) or "/" not in model:
        raise SubscriptionRouteRefused(engine, "model_malformed")
    provider, name = model.split("/", 1)
    if not provider.strip() or not name.strip():
        raise SubscriptionRouteRefused(engine, "model_name_empty")
    return model


def _finish(
    engine: str,
    provider: str,
    credential: str,
    flags: tuple[str, ...],
    auth: str,
    method: str,
    engine_config: tuple[tuple[str, object], ...],
    model: str,
    env: Mapping[str, str],
    wrapper: WrapperInstall | None,
    checked: _Checked,
    pin_version: str = "",
) -> RouteDecision:
    """Pi and Hermes match a package pin and are beta. Any other engine refuses a pin."""

    beta = False
    if engine in ("pi", "hermes"):
        if wrapper is None:
            raise SubscriptionRouteRefused(engine, "wrapper_pin_missing")
        _verify_wrapper(engine, wrapper, checked)
        beta = True
    elif wrapper is not None:
        raise SubscriptionRouteRefused(engine, "wrapper_pin_not_applicable")
    return RouteDecision(
        engine, provider, credential, flags + checked.flags, auth, method, engine_config, model, beta,
        tuple(sorted(env.items())), pin_version,
    )


def _check_subscription(
    engine: str,
    spec: Engine,
    profile: Mapping[str, object],
    child_env: Mapping[str, str],
    route_home: str,
    parent_env: Mapping[str, str] | None,
    operator_home: str | None,
    engine_config: tuple[tuple[str, object], ...],
    wrapper: WrapperInstall | None,
    checked: _Checked,
) -> RouteDecision:
    """A subscription sign-in: allowed only for a (provider, method) pair on ``SUBSCRIPTION_SIGN_INS``."""

    provider, method, model = profile.get("provider"), profile.get("method"), profile.get("model")
    if not isinstance(provider, str) or not provider.strip():
        raise SubscriptionRouteRefused(engine, "provider_missing")
    raw = provider.strip().lower()
    label = _norm(provider)
    if raw in spec.subscription_providers or _looks_like_claude_login(raw) or label.startswith("anthropic"):
        raise SubscriptionRouteRefused(engine, "claude_subscription_auth" if label == "anthropic"
                                       else "claude_subscription_provider", raw)
    if label.startswith(_GOOGLE_LABELS):
        raise SubscriptionRouteRefused(engine, "google_subscription_refused", raw)
    allowed = SUBSCRIPTION_SIGN_INS[engine]
    if label not in allowed:
        raise SubscriptionRouteRefused(engine, "subscription_unconfirmed", label)
    if not isinstance(method, str) or not method.strip():
        raise SubscriptionRouteRefused(engine, "method_missing")
    if _norm(method) != allowed[label]:
        raise SubscriptionRouteRefused(engine, "subscription_method_not_registered", _norm(method))
    if "credential_env" in profile:
        raise SubscriptionRouteRefused(engine, "credential_env_on_subscription")
    if model is None:  # the engine's own default could be a Claude model (e.g. OpenCode's anthropic/...)
        raise SubscriptionRouteRefused(engine, "model_missing")
    model = _require_model_name(engine, model)
    model_provider = _norm(model.split("/", 1)[0])
    if model_provider in spec.subscription_providers or _looks_like_claude_login(model_provider.replace("_", "-")):
        raise SubscriptionRouteRefused(engine, "claude_subscription_provider", model_provider)
    if model_provider != label:
        raise SubscriptionRouteRefused(engine, "model_provider_mismatch", model_provider)

    env = dict(child_env)
    present = sorted(name for name in CLAUDE_SUBSCRIPTION_ENV if name in env)
    if present:
        raise SubscriptionRouteRefused(engine, "claude_subscription_env", ",".join(present))
    stores = sorted(name for name in CLAUDE_STORE_ENV if name in env)
    if stores:
        raise SubscriptionRouteRefused(engine, "claude_store_env", ",".join(stores))
    keys = sorted(name for name in env if any(name in p.credential_env for p in spec.providers.values()))
    if keys:  # a key in the env could outrank the sign-in; one route is one method
        raise SubscriptionRouteRefused(engine, "credential_env_on_subscription", ",".join(keys))
    selectors = sorted(name for name in ENGINE_SELECTOR_ENV if name in env)
    if selectors:
        raise SubscriptionRouteRefused(engine, "engine_selector_env", ",".join(selectors))
    extra = sorted(name for name in env if name not in SAFE_ENV)
    if extra:  # no token of any kind: Recursion holds no ChatGPT or Grok token
        raise SubscriptionRouteRefused(engine, "child_env_not_allowed", ",".join(extra))

    own = OWN_STORE_ENTRIES[engine]
    _check_home(engine, env, route_home, parent_env, operator_home, own=own)
    _check_hermes_home(engine, route_home, checked)
    flags = tuple(f"parent_env_has:{name}" for name in CLAUDE_SUBSCRIPTION_ENV if name in (parent_env or {}))
    # The engine's own store was allowed by presence only; whether it holds an adopted Claude or Codex
    # login, or a Claude provider plugin, was not inspected (open for Lucas). A .hermes that holds only
    # Recursion's own adopt switch has nothing uninspected in it and gets no flag.
    flags += tuple(f"own_login_store_not_inspected:{entry}" for entry in own
                   if os.path.lexists(os.path.join(route_home, entry))
                   and not (engine == "hermes" and entry == ".hermes" and _hermes_dir_is_only_ours(engine, route_home)))
    return _finish(engine, label, "", flags, "subscription", allowed[label], engine_config, model, env, wrapper,
                   checked)


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
    wrapper: WrapperInstall | None = None,
    requirements: RouteRequirements | None = None,
    full_access_acknowledged: bool = False,
    **launch_kwargs: object,
) -> tuple[RouteDecision, object]:
    """Run ``check_route`` and only then call the launcher with the env that check accepted.

    A later change to ``child_env`` is not what launches. The launcher receives that checked
    snapshot rebuilt by ``child_env.build_child_env`` (a private temp root when a temp name is
    present, and the one checked credential under its own name). It is never ``os.environ``.
    A subscription route's ``model`` binds only before ``--``, as ``--model X`` or ``-m X``.
    ``opencode``, ``hermes``, ``copilot`` and ``nous`` also accept ``--model=X``, ``-m=X`` and an
    attached ``-mX``. Pi's ``--system-prompt`` and ``--append-system-prompt`` consume the next element,
    so a flag-shaped value there is not a binding. ``argv[0]`` and a bare element
    (``opencode run <model>``) do not bind. A second flag with a different value refuses
    ``model_not_bound``.
    On Pi and Hermes ``argv[0]`` must be an absolute path that resolves to
    the pinned ``wrapper.launcher_path`` (``argv0_not_pinned``); OpenCode has no pin and no such check.
    Hermes's ``auth.adopt_external_logins: false`` is written into the route home's
    ``.hermes/config.yaml`` before the launcher runs; a config already there is reused only when it is
    exactly that file. A missing or empty ``.hermes`` is built in a ``.hermes.recursion-*`` staging
    directory and renamed into place; a ``.hermes`` that already holds entries gets the file by a
    hard link that never overwrites. ``launch_kwargs`` may not replace the env or widen the launch:
    ``cwd`` and the names in ``requirements.extra_launch_options`` are forwarded, forbidden names
    refuse ``launch_option_denied`` and any other name refuses ``launch_option_unknown``.
    """

    name = engine if isinstance(engine, str) else None
    forward = _reject_launch_shape(name, argv, launch_kwargs, requirements, full_access_acknowledged)
    decision = check_route(
        engine, profile, child_env, route_home=route_home, parent_env=parent_env,
        operator_home=operator_home, wrapper=wrapper, requirements=requirements,
        full_access_acknowledged=full_access_acknowledged,
    )
    pinned = (wrapper.launcher_path,) if decision.beta and wrapper is not None else ()
    return _launch_checked(launcher, list(argv), decision, route_home, pinned, forward)


def _looks_like_claude_login(provider: str) -> bool:
    # Fail closed on spellings of a Claude login provider not on the list (an engine update can rename it).
    squashed = provider.replace("_", "-")
    return squashed.startswith("claude") or squashed.endswith(("-oauth", "-subscription")) and "anthropic" in squashed


def _surrogate(value: object) -> bool:
    """True when ``value`` holds a code point in U+D800–U+DFFF.

    U+DCFF is the surrogateescape form of the byte 0xff. Checked before any filesystem call.
    """

    return isinstance(value, str) and any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _check_home(
    engine: str,
    env: Mapping[str, str],
    route_home: str,
    parent_env: Mapping[str, str] | None,
    operator_home: str | None,
    own: tuple[str, ...] = (),
) -> None:
    """The child's home is route-owned, private, not the operator's, and holds no store or engine state entry.

    ``own`` lists the engine's own login store entries a subscription sign-in may keep there.
    A surrogate in the route home, the child HOME, an operator home, or the passwd home is
    ``home_unsafe`` before ``realpath`` or ``lstat``.
    """

    if os.name == "nt":
        raise SubscriptionRouteRefused(engine, "platform_unsupported")
    parent_home = parent_env.get("HOME") if parent_env is not None else None
    if any(_surrogate(item) for item in (route_home, env.get("HOME"), operator_home, parent_home)):
        raise SubscriptionRouteRefused(engine, "home_unsafe")
    if not isinstance(route_home, str) or not os.path.isabs(route_home):
        raise SubscriptionRouteRefused(engine, "route_home_missing")
    if env.get("HOME") != route_home:
        raise SubscriptionRouteRefused(engine, "child_home_not_route_home")
    homes = _operator_homes(parent_env, operator_home)
    if any(_surrogate(path) for path in homes):
        raise SubscriptionRouteRefused(engine, "home_unsafe")
    real = os.path.realpath(route_home)
    operator = {os.path.realpath(path) for path in homes}
    # A home of "/" must not use the prefix "/": that matches every absolute path, so a
    # container whose passwd home is missing (HOME=/) could launch no route.
    if any(_operator_contains(real, home) for home in operator):
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
        if entry in own:
            _check_own_store(engine, route_home, real, entry)
            continue
        if engine == "hermes" and entry == ".hermes" and _hermes_dir_is_only_ours(engine, route_home):
            continue  # the adopt switch an earlier launch in this home wrote, and nothing else
        if engine == "hermes" and entry == ".hermes" and _hermes_dir_is_empty(route_home):
            continue  # an empty real .hermes counts as absent on an API-key Hermes route only
        if os.path.lexists(os.path.join(route_home, entry)):  # presence only; never opened or read
            raise SubscriptionRouteRefused(engine, "engine_store_in_route_home", entry)


_STORE_WALK_CAP = 10_000


def _check_own_store(engine: str, route_home: str, real_home: str, entry: str) -> None:
    """The engine's own store must live inside the route home: no link on the way to it, and no link
    inside it that resolves outside the route home (it could reach the operator's store). lstat only."""

    path, parts = route_home, entry.split("/")
    for part in parts:
        path = os.path.join(path, part)
        if not os.path.lexists(path):
            return  # absent: nothing to check
        if os.path.islink(path):
            raise SubscriptionRouteRefused(engine, "own_store_is_link", entry)
    if not os.path.isdir(path):
        return
    # os.walk does not enter a directory symlink. A link that stays inside the route home can
    # point at another directory whose own link leaves the home; that second link has to be checked.
    pending = [path]
    seen_dirs: set[str] = set()
    seen = 0
    while pending:
        current = pending.pop()
        real_current = os.path.realpath(current)
        if real_current in seen_dirs:
            continue
        if not _within(real_current, real_home):
            raise SubscriptionRouteRefused(engine, "own_store_links_outside_route_home", entry)
        seen_dirs.add(real_current)
        for top, dirs, files in os.walk(current, followlinks=False):
            linked_dirs: list[str] = []
            for name in list(dirs):
                item = os.path.join(top, name)
                if os.path.islink(item):
                    dirs.remove(name)
                    linked_dirs.append(item)
            for name in files:
                seen += 1
                if seen > _STORE_WALK_CAP:
                    raise SubscriptionRouteRefused(engine, "own_store_too_large", entry)
                item = os.path.join(top, name)
                if os.path.islink(item) and not _within(os.path.realpath(item), real_home):
                    raise SubscriptionRouteRefused(engine, "own_store_links_outside_route_home", entry)
            for item in linked_dirs:
                seen += 1
                if seen > _STORE_WALK_CAP:
                    raise SubscriptionRouteRefused(engine, "own_store_too_large", entry)
                target = os.path.realpath(item)
                if not _within(target, real_home):
                    raise SubscriptionRouteRefused(engine, "own_store_links_outside_route_home", entry)
                if os.path.isdir(target):
                    pending.append(target)


def _operator_homes(parent_env: Mapping[str, str] | None, operator_home: str | None) -> list[str]:
    homes = [path for path in (operator_home, (parent_env or {}).get("HOME")) if path]
    if pwd is not None:
        try:
            homes.append(pwd.getpwuid(os.geteuid()).pw_dir)  # the passwd entry's home path, no file in it is read
        except KeyError:
            pass
    return [path for path in homes if isinstance(path, str) and os.path.isabs(path)]


def _operator_contains(real: str, home: str) -> bool:
    """True when ``real`` is ``home`` or a child of it. ``home`` of ``/`` matches only ``/``."""

    if home == "/":
        return real == "/"
    base = home.rstrip("/")
    return real == base or real.startswith(base + "/")


def _within(real: str, root: str) -> bool:
    """True when ``real`` is inside ``root``, including when ``root`` is ``/``."""

    if root == "/":
        return real.startswith("/")
    base = root.rstrip("/")
    return real == base or real.startswith(base + "/")


_MODEL_FLAGS = ("--model", "-m")
# Hermes reads auth.adopt_external_logins from ~/.hermes/config.yaml and defaults it to true
# (NousResearch/hermes-agent, auth.adopt_external_logins). The route home is the child HOME.
_HERMES_ADOPT_YAML = b"auth:\n  adopt_external_logins: false\n"  # 37 bytes
# Staging names the adopt-switch write makes in the route home. Not a store entry: a leftover one
# (a crash between mkdir and rename) is never read as Hermes state.
_HERMES_STAGING_PREFIX = ".hermes.recursion-"
_DIR_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class _Checked:
    """``RouteRequirements`` plus the full-access acknowledgment, after validation."""

    allowed_link_targets: tuple[str, ...] = ()
    extra_launch_options: frozenset[str] = frozenset()
    owned_hermes_home: str | None = None
    full_access: bool = False

    @property
    def flags(self) -> tuple[str, ...]:
        return ("full_access_profile_acknowledged",) if self.full_access else ()


def _checked_requirements(engine: str | None, requirements: object, full_access_acknowledged: object) -> _Checked:
    """Validate the configured inputs, fail closed. Only the bool ``True`` turns full access on."""

    if not isinstance(full_access_acknowledged, bool):  # "true", 1 and the like are not an acknowledgment
        raise SubscriptionRouteRefused(engine, "route_full_access_malformed")
    if requirements is None:
        requirements = RouteRequirements()
    if not isinstance(requirements, RouteRequirements):
        raise SubscriptionRouteRefused(engine, "requirements_malformed", "requirements")
    try:
        targets = check_link_requirements(requirements.allowed_link_targets, full_access_acknowledged)
    except WrapperPinRefused as exc:
        raise SubscriptionRouteRefused(engine, exc.cause, exc.detail) from None
    options = requirements.extra_launch_options
    if not isinstance(options, (tuple, frozenset)) or not all(
        isinstance(option, str) and option.isidentifier() for option in options
    ):
        raise SubscriptionRouteRefused(engine, "requirements_malformed", "extra_launch_options")
    denied = sorted((_FORBIDDEN_LAUNCH | _RESERVED_LAUNCH).intersection(options))
    if denied:  # full access does not change this
        raise SubscriptionRouteRefused(engine, "requirements_forbidden_launch_option", ",".join(denied))
    owned = requirements.owned_hermes_home
    if owned is not None and (not isinstance(owned, str) or "\x00" in owned or not os.path.isabs(owned)):
        raise SubscriptionRouteRefused(engine, "requirements_malformed", "owned_hermes_home")
    # A surrogate is requirements_malformed in _check_hermes_home, before realpath.
    return _Checked(targets, frozenset(options), owned, full_access_acknowledged is True)


def _reject_launch_shape(
    engine: str | None,
    argv: object,
    launch_kwargs: Mapping[str, object],
    requirements: object = None,
    full_access_acknowledged: object = False,
) -> dict[str, object]:
    """Refuse a bad launch shape; return the launch options to forward to the launcher."""

    bad = sorted((_FORBIDDEN_LAUNCH | _RESERVED_LAUNCH).intersection(launch_kwargs))
    if bad:
        raise SubscriptionRouteRefused(engine, "launch_option_denied", ",".join(bad))
    if isinstance(argv, (str, bytes)) or not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise SubscriptionRouteRefused(engine, "argv_malformed")
    checked = _checked_requirements(engine, requirements, full_access_acknowledged)
    unknown = sorted(set(launch_kwargs) - FORWARDED_LAUNCH_OPTIONS - checked.extra_launch_options)
    if unknown:  # full access adds no launch option
        raise SubscriptionRouteRefused(engine, "launch_option_unknown", ",".join(unknown))
    return dict(launch_kwargs)


def _verify_wrapper(engine: str, wrapper: WrapperInstall, checked: _Checked) -> None:
    try:
        verify_package_pin(engine, wrapper, allowed_link_targets=checked.allowed_link_targets,
                           full_access_acknowledged=checked.full_access)
    except WrapperPinRefused as exc:
        raise SubscriptionRouteRefused(engine, exc.cause, exc.detail) from None


def _argv0_is_pinned(argv: list[str], pinned: tuple[str, ...]) -> bool:
    """``argv[0]`` is an absolute path that resolves to one of the pinned paths.

    A bare name such as ``cursor`` would be looked up on PATH by the launcher, so it is not the
    pinned file however it resolves here.
    """

    first = argv[0]
    if "\x00" in first or not os.path.isabs(first):
        return False
    real = os.path.realpath(first)
    return any(isinstance(path, str) and os.path.isabs(path) and os.path.realpath(path) == real for path in pinned)


# Options that take the next argv element as a value, so a following ``--model`` is that value and
# not a model flag. Pi's ``--system-prompt`` is one case. ``--session`` takes a value on each of
# these engines. A consumed value is never read again as a flag.
_VALUE_OPTIONS = {
    "pi": frozenset({"--system-prompt", "--append-system-prompt", "--session"}),
    "hermes": frozenset({"--session"}),
    "opencode": frozenset({"--session"}),
    "copilot": frozenset({"--session"}),
    "nous": frozenset({"--session"}),
}
# Engines whose CLIs accept ``--model=VALUE``, ``-m=VALUE`` and an attached ``-mVALUE``.
_EQUALS_MODEL = frozenset({"opencode", "hermes", "copilot", "nous"})


def _scan_argv(argv: Sequence[str], engine: str) -> tuple[list[str], list[str], list[str], bool]:
    """One left-to-right pass. Each flag's value is consumed once, even when it looks like a flag.

    Returns ``(model values, base-url values, adapter values, saw a model flag)``. ``argv[0]`` is
    the program. A token after ``--`` is not a flag. ``--model``, ``-m``, ``--base-url`` and
    ``--adapter`` take the next element. ``opencode``, ``hermes``, ``copilot`` and ``nous`` also
    accept ``--model=X``, ``-m=X`` and an attached ``-mX``. Engines not listed in
    ``_VALUE_OPTIONS`` keep that equals form and have no value-taking options. A value option such
    as ``--session`` or Pi's ``--system-prompt`` consumes the next element, so that element is not
    a later flag.
    """

    value_options = _VALUE_OPTIONS.get(engine, frozenset())
    equals = engine in _EQUALS_MODEL or engine not in _VALUE_OPTIONS
    models: list[str] = []
    base_urls: list[str] = []
    adapters: list[str] = []
    saw_model = False
    index = 1
    while index < len(argv):
        item = argv[index]
        if item == "--":
            break
        if item in _MODEL_FLAGS:
            saw_model = True
            if index + 1 >= len(argv):
                models.append("")
                break
            models.append(argv[index + 1])
            index += 2
            continue
        if item == "--base-url":
            if index + 1 >= len(argv):
                base_urls.append("")
                break
            base_urls.append(argv[index + 1])
            index += 2
            continue
        if item == "--adapter":
            if index + 1 >= len(argv):
                adapters.append("")
                break
            adapters.append(argv[index + 1])
            index += 2
            continue
        if equals and item.startswith("--model="):
            saw_model = True
            models.append(item[len("--model="):])
            index += 1
            continue
        if equals and item.startswith("-m=") and len(item) > 3:
            saw_model = True
            models.append(item[len("-m="):])
            index += 1
            continue
        # Attached short form (-mchatgpt/test-model). ``-m`` alone is the flag with no value.
        if equals and item.startswith("-m") and len(item) > 2:
            saw_model = True
            models.append(item[2:])
            index += 1
            continue
        if item.startswith("--base-url="):
            base_urls.append(item[len("--base-url="):])
            index += 1
            continue
        if item.startswith("--adapter="):
            adapters.append(item[len("--adapter="):])
            index += 1
            continue
        if item in value_options:
            index += 2
            continue
        index += 1
    return models, base_urls, adapters, saw_model


def _argv_binds_model(argv: list[str], model: str, engine: str = "opencode") -> bool:
    """True when every model flag before ``--`` names ``model`` for ``engine``.

    Accepted forms are ``--model X`` and ``-m X``. ``opencode``, ``hermes``, ``copilot`` and ``nous``
    also accept ``--model=X``, ``-m=X`` and an attached ``-mX``. ``argv[0]`` is the program. A bare
    element, including ``opencode run <model>``, does not bind. A flag after ``--`` does not bind.
    A flag with no value, an empty ``--model=``, or two flags that disagree, does not bind.

    Value-taking options (Pi's ``--system-prompt`` and ``--append-system-prompt``, and ``--session``
    on Pi, OpenCode, Hermes, Copilot and Nous) consume the next element, even when that element is
    spelled like a model flag. The scan is the one in ``_scan_argv``.
    """

    if not model:
        return False
    models, _base_urls, _adapters, _saw_model = _scan_argv(argv, engine)
    if not models or any(not value for value in models):
        return False
    return all(value == model for value in models)


def _refuse_hermes_config(engine: str, detail: str) -> SubscriptionRouteRefused:
    return SubscriptionRouteRefused(engine, "hermes_config_blocks_adopt_override", detail)


def _hermes_config_is_ours(engine: str, route_home: str) -> bool:
    """False when ``.hermes/config.yaml`` is absent, True when it is exactly Recursion's adopt switch.

    Anything else refuses: a link, a non-regular file, another owner or any other content. A file
    of any other size is refused from ``lstat`` alone and never opened, so a config that holds more
    than the switch (it might hold a credential) is not read. A file of exactly that size (37 bytes)
    is opened relative to a ``.hermes`` opened as a real directory, never as a controlling terminal,
    and its type, size and owner are checked again on the opened file before the read.
    """

    parent = os.path.join(route_home, ".hermes")
    dest = os.path.join(parent, "config.yaml")
    try:
        parent_mode = os.lstat(parent).st_mode
    except OSError:
        return False
    if not stat.S_ISDIR(parent_mode):
        raise _refuse_hermes_config(engine, ".hermes")
    try:
        st = os.lstat(dest)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_size != len(_HERMES_ADOPT_YAML) or st.st_uid != os.geteuid():
        raise _refuse_hermes_config(engine, "config.yaml")
    try:
        dir_fd = os.open(parent, _DIR_OPEN_FLAGS)
    except OSError:
        raise _refuse_hermes_config(engine, ".hermes") from None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
        try:
            fd = os.open("config.yaml", flags, dir_fd=dir_fd)
        except OSError:
            raise _refuse_hermes_config(engine, "config.yaml") from None
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_size != len(_HERMES_ADOPT_YAML)
                    or opened.st_uid != os.geteuid()):
                raise _refuse_hermes_config(engine, "config.yaml")
            data = os.read(fd, len(_HERMES_ADOPT_YAML) + 1)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)
    if data != _HERMES_ADOPT_YAML:
        raise _refuse_hermes_config(engine, "config.yaml")
    return True


def _hermes_dir_is_only_ours(engine: str, route_home: str) -> bool:
    """True when ``.hermes`` is a real directory that holds only Recursion's exact ``config.yaml``."""

    parent = os.path.join(route_home, ".hermes")
    try:
        if not stat.S_ISDIR(os.lstat(parent).st_mode) or os.listdir(parent) != ["config.yaml"]:
            return False
    except OSError:
        return False
    return _hermes_config_is_ours(engine, route_home)


def _hermes_dir_is_empty(route_home: str) -> bool:
    """True when ``.hermes`` is a real directory, owned by the effective user, with nothing in it."""

    parent = os.path.join(route_home, ".hermes")
    try:
        st = os.lstat(parent)
        return stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid() and not os.listdir(parent)
    except OSError:
        return False


def _check_hermes_home(engine: str, route_home: str, checked: _Checked) -> None:
    """Hermes only: the declared owned home is the route home, and a config already there is ours."""

    if engine != "hermes":
        return
    if _surrogate(checked.owned_hermes_home):
        raise SubscriptionRouteRefused(engine, "requirements_malformed", "owned_hermes_home")
    if checked.owned_hermes_home is not None and os.path.realpath(checked.owned_hermes_home) != os.path.realpath(route_home):
        raise SubscriptionRouteRefused(engine, "hermes_home_not_owned")
    _hermes_config_is_ours(engine, route_home)


def _fsync_dir(directory: str | int) -> None:
    """Best effort: a directory that cannot be opened or synced does not fail the write."""

    try:
        fd = os.open(directory, _DIR_OPEN_FLAGS) if isinstance(directory, str) else directory
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        if isinstance(directory, str):
            os.close(fd)


def _create_adopt_switch(path: str, dir_fd: int | None = None) -> None:
    """Create ``path`` (it must not exist) mode 0600 holding exactly the adopt switch, and fsync it."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600, dir_fd=dir_fd)
    try:
        if os.write(fd, _HERMES_ADOPT_YAML) != len(_HERMES_ADOPT_YAML):
            raise OSError(errno.EIO, "short write of the Hermes adopt switch")
        os.fsync(fd)
    except BaseException:
        try:
            os.unlink(path, dir_fd=dir_fd)  # the file this call created; O_EXCL means no one else's
        except OSError:
            pass
        raise
    finally:
        os.close(fd)


def _stage_hermes_dir(engine: str, route_home: str, parent: str) -> None:
    """``.hermes`` is absent or empty: build it beside its place and rename it in, switch included.

    The rename replaces only a missing or empty directory. If something appeared in ``.hermes``
    meanwhile (``ENOTEMPTY`` or ``EEXIST``) the launch refuses and the staging directory is removed.
    """

    staging = os.path.join(route_home, _HERMES_STAGING_PREFIX + secrets.token_hex(8))
    os.mkdir(staging, 0o700)
    try:
        staging_fd = os.open(staging, _DIR_OPEN_FLAGS)
        try:
            _create_adopt_switch("config.yaml", dir_fd=staging_fd)
            _fsync_dir(staging_fd)
        finally:
            os.close(staging_fd)
        try:
            os.rename(staging, parent)
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                raise _refuse_hermes_config(engine, ".hermes") from None
            raise
    except BaseException:
        for remove, path in ((os.unlink, os.path.join(staging, "config.yaml")), (os.rmdir, staging)):
            try:
                remove(path)
            except OSError:
                pass
        raise
    _fsync_dir(route_home)


def _link_hermes_config(engine: str, route_home: str, parent: str) -> None:
    """``.hermes`` holds Hermes's own entries and no ``config.yaml``: add the switch without replacing.

    The switch is written to a temp file in the route home and hard-linked into ``.hermes``. A link
    never overwrites and does not follow a source symlink, so a ``config.yaml`` that appeared
    meanwhile refuses instead of being replaced. The caller checks the result is that regular file.
    """

    temp = os.path.join(route_home, _HERMES_STAGING_PREFIX + secrets.token_hex(8))
    try:
        _create_adopt_switch(temp)
        try:
            parent_fd = os.open(parent, _DIR_OPEN_FLAGS)
            try:
                os.link(temp, "config.yaml", dst_dir_fd=parent_fd, follow_symlinks=False)
                _fsync_dir(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            os.unlink(temp)
    except OSError:
        raise _refuse_hermes_config(engine, "config.yaml") from None
    _fsync_dir(route_home)


def _drop_config_symlink(route_home: str) -> None:
    """Remove a ``config.yaml`` symlink the link step just installed. A regular file is left alone."""

    dest = os.path.join(route_home, ".hermes", "config.yaml")
    if os.path.islink(dest):
        try:
            os.unlink(dest)
        except OSError:
            pass


def _confirm_hermes_switch(engine: str, route_home: str) -> None:
    """The switch just written is the regular file we expect. A symlink is removed and the launch refuses."""

    try:
        ok = _hermes_config_is_ours(engine, route_home)
    except SubscriptionRouteRefused:
        _drop_config_symlink(route_home)
        raise
    if not ok:
        _drop_config_symlink(route_home)
        raise _refuse_hermes_config(engine, "config.yaml")


def _apply_hermes_adopt_override(decision: RouteDecision, route_home: str) -> None:
    """Write Hermes's adopt switch, or reuse the one an earlier launch in this home wrote.

    After a write, the switch is read back. Anything that is not that regular file refuses, and a
    symlink left at ``config.yaml`` is removed, before the launcher runs.
    """

    if decision.engine != "hermes":
        return
    if decision.engine_config != ((HERMES_ADOPT_EXTERNAL_LOGINS, False),):
        raise SubscriptionRouteRefused(decision.engine, "hermes_adopt_external_logins_not_off")
    parent = os.path.join(route_home, ".hermes")
    if _hermes_config_is_ours(decision.engine, route_home):
        return
    if not os.path.lexists(parent) or _hermes_dir_is_empty(route_home):
        _stage_hermes_dir(decision.engine, route_home, parent)
    elif os.path.islink(parent) or not os.path.isdir(parent):
        raise _refuse_hermes_config(decision.engine, ".hermes")
    else:
        _link_hermes_config(decision.engine, route_home, parent)
    _confirm_hermes_switch(decision.engine, route_home)


def _launch_checked(
    launcher: Callable[..., object],
    argv: list[str],
    decision: RouteDecision,
    route_home: str,
    pinned: tuple[str, ...],
    forward: Mapping[str, object],
) -> tuple[RouteDecision, object]:
    if pinned and not _argv0_is_pinned(argv, pinned):
        raise SubscriptionRouteRefused(decision.engine, "argv0_not_pinned")
    if decision.auth == "subscription" and not _argv_binds_model(argv, decision.model, decision.engine):
        raise SubscriptionRouteRefused(decision.engine, "model_not_bound", decision.model)
    # Rebuild before the Hermes write, so a refused environment does not leave a config behind.
    # Imported here: validated_launch does not import this module.
    from recursion_core.child_env import EnvRefused
    from recursion_core.subscription_routes.validated_launch import launch_env

    try:
        _validated, launched = launch_env(
            decision.child_env, route_home=route_home, credential_env=decision.credential_env,
        )
    except EnvRefused as exc:
        detail = exc.cause if not exc.name else f"{exc.name}:{exc.cause}"
        raise SubscriptionRouteRefused(decision.engine, "child_env_refused", detail) from None
    _apply_hermes_adopt_override(decision, route_home)
    # ``launched`` is the rebuilt snapshot, not another read of the caller's mapping and not
    # ``os.environ``. ``forward`` holds only the options _reject_launch_shape let through.
    return decision, launcher(
        list(argv), env=launched, shell=False,
        engine_config=decision.engine_config, model=decision.model, **forward,
    )


def _checked_baseline(
    engine: str,
    child_env: Mapping[str, str],
    route_home: str,
    parent_env: Mapping[str, str] | None,
    operator_home: str | None,
) -> tuple[tuple[str, str], ...]:
    """Safe child env only: no Claude subscription name and no credential. Then the home rules."""

    env = dict(child_env)
    present = sorted(name for name in CLAUDE_SUBSCRIPTION_ENV if name in env)
    if present:
        raise SubscriptionRouteRefused(engine, "claude_subscription_env", ",".join(present))
    stores = sorted(name for name in CLAUDE_STORE_ENV if name in env)
    if stores:
        raise SubscriptionRouteRefused(engine, "claude_store_env", ",".join(stores))
    extra = sorted(name for name in env if name not in SAFE_ENV)
    if extra:
        raise SubscriptionRouteRefused(engine, "child_env_not_allowed", ",".join(extra))
    _check_home(engine, env, route_home, parent_env, operator_home)
    return tuple(sorted(env.items()))


def check_cursor_route(
    child_env: Mapping[str, str],
    *,
    route_home: str,
    wrapper: WrapperInstall,
    parent_env: Mapping[str, str] | None = None,
    operator_home: str | None = None,
    requirements: RouteRequirements | None = None,
    full_access_acknowledged: bool = False,
) -> RouteDecision:
    """Cursor runs as beta when its launcher script and package files match ``wrapper``.

    No Cursor subscription provider is confirmed here, so the child env carries no credential.
    A mismatch refuses until the pin is replaced.
    """

    engine = "cursor"
    checked = _checked_requirements(engine, requirements, full_access_acknowledged)
    if wrapper is None:
        raise SubscriptionRouteRefused(engine, "wrapper_pin_missing")
    frozen = _checked_baseline(engine, child_env, route_home, parent_env, operator_home)
    _verify_wrapper(engine, wrapper, checked)
    return RouteDecision(engine, "", "", checked.flags, "wrapper", "", (), "", True, frozen)


def guard_cursor_then_spawn(
    launcher: Callable[..., object],
    argv: list[str],
    child_env: Mapping[str, str],
    *,
    route_home: str,
    wrapper: WrapperInstall,
    parent_env: Mapping[str, str] | None = None,
    operator_home: str | None = None,
    requirements: RouteRequirements | None = None,
    full_access_acknowledged: bool = False,
    **launch_kwargs: object,
) -> tuple[RouteDecision, object]:
    """``check_cursor_route`` then the launcher, with the checked env only.

    ``argv[0]`` must be an absolute path that resolves to the pinned ``wrapper.launcher_path``.
    Launch options follow the same rules as ``guard_then_spawn``.
    """

    forward = _reject_launch_shape("cursor", argv, launch_kwargs, requirements, full_access_acknowledged)
    decision = check_cursor_route(
        child_env, route_home=route_home, wrapper=wrapper, parent_env=parent_env, operator_home=operator_home,
        requirements=requirements, full_access_acknowledged=full_access_acknowledged,
    )
    return _launch_checked(launcher, list(argv), decision, route_home, (wrapper.launcher_path,), forward)


def check_antigravity_route(
    child_env: Mapping[str, str],
    *,
    version: str,
    binary_path: str,
    launcher_path: str,
    pin: AntigravityPin,
    route_home: str,
    parent_env: Mapping[str, str] | None = None,
    operator_home: str | None = None,
    requirements: RouteRequirements | None = None,
    full_access_acknowledged: bool = False,
) -> RouteDecision:
    """Antigravity CLI (``agy``) as a beta wrapper. This is the Google route.

    The pin is the version plus the sha256 of the ``agy`` binary and of its launcher.
    Package files are not hashed. The binary is not executed. The child env carries no credential.
    ``requirements`` and ``full_access_acknowledged`` are validated; neither relaxes this pin.
    """

    engine = "antigravity"
    checked = _checked_requirements(engine, requirements, full_access_acknowledged)
    frozen = _checked_baseline(engine, child_env, route_home, parent_env, operator_home)
    try:
        measured = verify_antigravity_pin(version, binary_path, launcher_path, pin)
    except WrapperPinRefused as exc:
        raise SubscriptionRouteRefused(engine, exc.cause, exc.detail) from None
    return RouteDecision(engine, "antigravity", "", checked.flags, "wrapper", "", (), "", True, frozen,
                         measured.version)


def guard_antigravity_then_spawn(
    launcher: Callable[..., object],
    argv: list[str],
    child_env: Mapping[str, str],
    *,
    version: str,
    binary_path: str,
    launcher_path: str,
    pin: AntigravityPin,
    route_home: str,
    parent_env: Mapping[str, str] | None = None,
    operator_home: str | None = None,
    requirements: RouteRequirements | None = None,
    full_access_acknowledged: bool = False,
    **launch_kwargs: object,
) -> tuple[RouteDecision, object]:
    """``check_antigravity_route`` then the launcher, with the checked env only.

    Both ``binary_path`` and ``launcher_path`` are in the pin, so ``argv[0]`` must be an absolute path
    that resolves to one of those two files. Launch options follow the same rules as ``guard_then_spawn``.
    """

    forward = _reject_launch_shape("antigravity", argv, launch_kwargs, requirements, full_access_acknowledged)
    decision = check_antigravity_route(
        child_env, version=version, binary_path=binary_path, launcher_path=launcher_path, pin=pin,
        route_home=route_home, parent_env=parent_env, operator_home=operator_home,
        requirements=requirements, full_access_acknowledged=full_access_acknowledged,
    )
    return _launch_checked(launcher, list(argv), decision, route_home, (binary_path, launcher_path), forward)
