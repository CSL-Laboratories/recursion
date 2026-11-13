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
(37 bytes), to confirm it is that switch; a config of any other size refuses unread. Immediately
before a Hermes launch the real path is walked from ``/`` with ``openat`` and ``O_NOFOLLOW`` on
each component. Ancestor directories are opened ``O_PATH|O_DIRECTORY`` and checked with ``fstat``,
so the walk needs search permission: ``O_RDONLY`` would refuse a mode ``0111`` directory that path
lookup can still traverse. The final route directory is the one opened for read. That walk runs
once before the adopt-switch write, so a refusal there does not create ``.hermes/config.yaml``.
``.hermes`` is listed through the directory fd held since the home check (``listdir`` on that fd).
At that listing ``config.yaml`` is opened ``O_RDONLY|O_NOFOLLOW`` from the ``.hermes`` fd. The bytes,
the size, ``mtime_ns``, ``ctime_ns`` and link count from that descriptor are the pin. After the walk
that follows the listing, the recheck takes the same observation twice. One observation is ``fstat``
and ``pread`` from offset 0 of that descriptor, ``config.yaml`` stat'd by name through the ``.hermes``
fd (``follow_symlinks=False``) and compared with ``fstat`` of the descriptor, ``.hermes`` stat'd by
name on the held route-home fd and compared with ``fstat`` of the directory fd, then ``listdir`` and
the name rules. ``.env``, ``.op.env`` and, on an API-key route, any other name refuse, and ``.env`` and
``.op.env`` must also be absent (``stat`` returns ``ENOENT``). The config name is stat'd once more
after that listing. The ``fstat`` that follows that name lookup must show link count at least 1 and
the pinned size, ``mtime_ns``, ``ctime_ns`` and link count: a same-tick replacement leaves the open
inode with link count 0 while ``ctime_ns`` does not move. The two observations must be identical:
bytes, size, ``mtime_ns``, ``ctime_ns``, device and inode of ``config.yaml`` and of ``.hermes``, and
the sorted listing. The config bytes, size, ``mtime_ns``, ``ctime_ns``, link count and both inodes
equal the pin. The last operations before the byte read re-verify every name-based fact, each name
lookup followed by a descriptor check: ``.hermes`` is opened by name from the held home fd
(``O_NOFOLLOW|O_DIRECTORY``) and ``fstat`` must be the pinned directory; ``listdir`` on that fd must
match the pinned set (an API-key ``state.db``, or ``.env`` / ``.op.env``, refuses); ``config.yaml``
is stat'd by name (``AT_SYMLINK_NOFOLLOW``) and must be the pinned inode; ``fstat`` of the config fd
must show link count at least 1 and the full stamp unchanged; ``.hermes`` is stat'd by name from the
home fd again and must be the pinned directory. That name verification runs once more, immediately
before the final ``fstat`` and ``pread`` of the config fd. ``pread`` is the function captured at
import, so a later patch of ``os.pread`` cannot change what that read returns. After that ``pread``
returns, the last operations are a seal, all through the functions captured at import: ``HOME`` is
``lstat``'d by path and must still be a real directory (not a symlink) with the pinned device and
inode; ``.hermes`` is opened again by name from the home fd (``O_NOFOLLOW|O_DIRECTORY``) and must be
the pinned directory; ``listdir`` on that fd must match the pinned names; ``config.yaml`` is
``lstat``'d by name and must be the pinned inode with link count at least 1; ``fstat`` of the config
fd must show the full stamp. Any difference refuses and is left in place: this recheck does not
unlink. A same-size ``O_TRUNC`` that keeps ``mtime_ns`` and ``ctime_ns`` still changes the bytes the
captured ``pread`` sees, so it refuses.
``.env`` and ``.op.env`` are not read, and refuse as a file, a symlink
or a directory: Hermes ``load_hermes_dotenv`` loads ``~/.hermes/.env`` over the process environment,
and ``.op.env`` beside it, before ``config.yaml``, so ``HERMES_HOME`` there selects a different config
that defaults ``adopt_external_logins`` to true. An API-key ``.hermes`` may contain only
``config.yaml``. A subscription route may still hold other names. Allowing those extra names is an
owner decision (Lucas, PRODUCT.md 055cc7a5 section 2); they stay ``own_login_store_not_inspected``,
and whether to read them is still open for him. The launcher receives ``route_home_fd``, a dup of
the held directory, valid only during that call. ``env["HOME"]`` stays the path the profile named.
``HOME=/proc/self/fd/N`` is not set: that path would name this process, not a child. A same-uid
change after this final seal and before the child execs or opens by path remains an owner-decision
residual: the child opens ``config.yaml`` and ``HOME`` by path, and that window is not fixed
in-process. Also residual, and not changed here: ``XDG_*`` and ``HERMES_HOME`` selectors;
subscription extra names other than ``.env`` and ``.op.env`` (a subscription ``state.db``); other
``.hermes`` names and the subscription store; and ``PROJECT_ROOT/.env`` in the Hermes install tree,
which ``load_hermes_dotenv`` can load and which is not checked here. Closing ``route_home_fd``
when this call returns can close a reused descriptor number if the launcher already closed it.
On a platform without ``O_PATH`` the ancestor open falls
back to ``O_RDONLY`` and a search-only ancestor still refuses.

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

import contextvars
import errno
import os
import secrets
import stat
from collections.abc import Callable, Mapping
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
_RESERVED_LAUNCH = frozenset({"engine_config", "model", "route_home_fd"})
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
    decision = RouteDecision(
        engine, provider, credential, flags + checked.flags, auth, method, engine_config, model, beta,
        tuple(sorted(env.items())), pin_version,
    )
    _release_route_home_unless_kept()
    return decision


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

    A later change to ``child_env`` is not what launches: the launcher receives ``decision.child_env``.
    A subscription route's ``model`` must be an argv element (or ``--model`` / ``-m``), so the engine is
    not left to its own default. On Pi and Hermes ``argv[0]`` must be an absolute path that resolves to
    the pinned ``wrapper.launcher_path`` (``argv0_not_pinned``); OpenCode has no pin and no such check.
    Hermes's ``auth.adopt_external_logins: false`` is written into the route home's
    ``.hermes/config.yaml`` before the launcher runs; a config already there is reused only when it is
    exactly that file. A missing or empty ``.hermes`` is built in a ``.hermes.recursion-*`` staging
    directory and renamed into place; a ``.hermes`` that already holds entries gets the file by a
    hard link that never overwrites. The Hermes launcher also receives ``route_home_fd``, a dup of
    the held route-home directory, valid only for that call. The child must use that descriptor as
    its home. Re-opening ``env["HOME"]`` by path is not bound to the directory that was checked.
    ``launch_kwargs`` may not replace the env or widen the launch:
    ``cwd`` and the names in ``requirements.extra_launch_options`` are forwarded, forbidden names
    refuse ``launch_option_denied`` and any other name refuses ``launch_option_unknown``.
    """

    def launch() -> tuple[RouteDecision, object]:
        name = engine if isinstance(engine, str) else None
        forward = _reject_launch_shape(name, argv, launch_kwargs, requirements, full_access_acknowledged)
        decision = check_route(
            engine, profile, child_env, route_home=route_home, parent_env=parent_env,
            operator_home=operator_home, wrapper=wrapper, requirements=requirements,
            full_access_acknowledged=full_access_acknowledged,
        )
        pinned = (wrapper.launcher_path,) if decision.beta and wrapper is not None else ()
        return _launch_checked(launcher, list(argv), decision, route_home, pinned, forward)

    return _call_kept(launch)


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
    own: tuple[str, ...] = (),
) -> None:
    """The child's home is route-owned, private, not the operator's, and holds no store or engine state entry.

    ``own`` lists the engine's own login store entries a subscription sign-in may keep there.
    """

    if os.name == "nt":
        raise SubscriptionRouteRefused(engine, "platform_unsupported")
    if not isinstance(route_home, str) or not os.path.isabs(route_home):
        raise SubscriptionRouteRefused(engine, "route_home_missing")
    if env.get("HOME") != route_home:
        raise SubscriptionRouteRefused(engine, "child_home_not_route_home")
    real = os.path.realpath(route_home)
    homes = _operator_homes(parent_env, operator_home)
    operator: set[str] = set()
    operator_ids: list[tuple[int, int]] = []
    for path in homes:
        try:
            operator.add(os.path.realpath(path))
        except OSError:
            pass
        try:
            ost = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISDIR(ost.st_mode):  # the directory itself, so a later rename onto the route path still matches
            operator_ids.append((ost.st_dev, ost.st_ino))
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
    # Stamp and hold the directory on this lstat. Later path swaps are not this inode.
    try:
        home_fd = os.open(route_home, _DIR_OPEN_FLAGS)
    except OSError:
        raise SubscriptionRouteRefused(engine, "route_home_not_private") from None
    try:
        opened = os.fstat(home_fd)
    except OSError:
        os.close(home_fd)
        raise SubscriptionRouteRefused(engine, "route_home_not_private") from None
    if ((opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino)
            or stat.S_IMODE(opened.st_mode) != stat.S_IMODE(st.st_mode)):
        os.close(home_fd)
        raise SubscriptionRouteRefused(engine, "route_home_not_private")
    _set_route_home(_RouteHome(
        route_home, home_fd, st.st_dev, st.st_ino, st.st_uid, st.st_mode,
        tuple(operator), tuple(operator_ids),
    ))
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

        def refuse_unreadable(_err: OSError) -> None:
            # os.walk skips a directory it cannot list. A link inside it would not be seen.
            raise SubscriptionRouteRefused(engine, "own_store_unreadable", entry)

        for top, dirs, files in os.walk(current, followlinks=False, onerror=refuse_unreadable):
            linked_dirs: list[str] = []
            for name in list(dirs):
                seen += 1
                if seen > _STORE_WALK_CAP:
                    raise SubscriptionRouteRefused(engine, "own_store_too_large", entry)
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
# API-key Hermes launches may leave only the adopt switch in ``.hermes``.
# Subscription launches may also leave the engine's own store; that store is not read.
_HERMES_LAUNCH_ALLOW = frozenset({"config.yaml"})
# ``load_hermes_dotenv`` reads these from the Hermes home before ``config.yaml``.
# ``.env`` overrides the process environment. ``.op.env`` is loaded next and can set
# ``HERMES_HOME`` when the child env has not. Neither name is the adopt switch.
_HERMES_DOTENV_NAMES = frozenset({".env", ".op.env"})
# Staging names the adopt-switch write makes in the route home. Not a store entry: a leftover one
# (a crash between mkdir and rename) is never read as Hermes state.
_HERMES_STAGING_PREFIX = ".hermes.recursion-"
_DIR_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
# Ancestors need search, not read. O_RDONLY is 0, so without O_PATH this is _DIR_OPEN_FLAGS.
_ANCESTOR_OPEN_FLAGS = (
    getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_HERMES_OPEN_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                      | getattr(os, "O_NOCTTY", 0))
# Captured at import. The seal after pread calls these, so a patch of os.* at call time does not
# redirect the last look. pread is one of them: os.pread looked up at the call would be free to lie.
_LIBC_OPEN = os.open
_LIBC_STAT = os.stat
_LIBC_FSTAT = os.fstat
_LIBC_LISTDIR = os.listdir
_LIBC_CLOSE = os.close
_LIBC_PREAD = os.pread
_LIBC_LSTAT = os.lstat


class _RouteHome:
    """The route directory held open from the ``lstat`` in ``_check_home`` until launch."""

    __slots__ = ("path", "fd", "dev", "ino", "uid", "mode", "operator_reals", "operator_ids",
                 "config_dev", "config_ino", "config_bytes", "config_size",
                 "config_mtime_ns", "config_ctime_ns", "config_nlink")

    def __init__(
        self,
        path: str,
        fd: int,
        dev: int,
        ino: int,
        uid: int,
        mode: int,
        operator_reals: tuple[str, ...],
        operator_ids: tuple[tuple[int, int], ...],
    ) -> None:
        self.path, self.fd = path, fd
        self.dev, self.ino, self.uid, self.mode = dev, ino, uid, mode
        self.operator_reals, self.operator_ids = operator_reals, operator_ids
        self.config_dev: int | None = None
        self.config_ino: int | None = None
        self.config_bytes: bytes | None = None
        self.config_size: int | None = None
        self.config_mtime_ns: int | None = None
        self.config_ctime_ns: int | None = None
        self.config_nlink: int | None = None

    def close(self) -> None:
        fd, self.fd = self.fd, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


_ROUTE_HOME: contextvars.ContextVar[_RouteHome | None] = contextvars.ContextVar("route_home", default=None)
# guard_* keeps the hold until after launch. check_route alone drops it before returning.
_KEEP_ROUTE_HOME: contextvars.ContextVar[bool] = contextvars.ContextVar("keep_route_home", default=False)


def _set_route_home(hold: _RouteHome) -> None:
    prev = _ROUTE_HOME.get()
    if prev is not None and prev is not hold:
        prev.close()
    _ROUTE_HOME.set(hold)


def _drop_route_home() -> None:
    hold = _ROUTE_HOME.get()
    _ROUTE_HOME.set(None)
    if hold is not None:
        hold.close()


def _release_route_home_unless_kept() -> None:
    if not _KEEP_ROUTE_HOME.get():
        _drop_route_home()


def _call_kept(fn: Callable[[], tuple[RouteDecision, object]]) -> tuple[RouteDecision, object]:
    token = _KEEP_ROUTE_HOME.set(True)
    try:
        return fn()
    finally:
        _drop_route_home()
        _KEEP_ROUTE_HOME.reset(token)


def _route_home_for(engine: str, route_home: str) -> _RouteHome:
    hold = _ROUTE_HOME.get()
    if hold is None or hold.fd < 0 or hold.path != route_home:
        raise SubscriptionRouteRefused(engine, "route_home_not_private")
    return hold


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


def _argv_binds_model(argv: list[str], model: str) -> bool:
    if model in argv:
        return True
    for index, item in enumerate(argv):
        if item == f"--model={model}":
            return True
        if item in _MODEL_FLAGS and index + 1 < len(argv) and argv[index + 1] == model:
            return True
    return False


def _refuse_hermes_config(engine: str, detail: str) -> SubscriptionRouteRefused:
    return SubscriptionRouteRefused(engine, "hermes_config_blocks_adopt_override", detail)


def _is_true_variant(data: bytes) -> bool:
    """Same-length (or any read) rewrite that turns the adopt switch on."""

    return b"adopt_external_logins: true" in data


def _neutralize_true_variant(hermes_fd: int, seen: tuple[int, int]) -> None:
    """The refusal must not leave ``adopt_external_logins: true`` at ``.hermes/config.yaml``.

    Restore the false switch when the opened inode is still the one just read; otherwise remove the name.
    """

    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOCTTY", 0)
    fd = -1
    try:
        fd = os.open("config.yaml", flags, dir_fd=hermes_fd)
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) == seen and stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid():
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            if os.write(fd, _HERMES_ADOPT_YAML) == len(_HERMES_ADOPT_YAML):
                os.fsync(fd)
                return
    except OSError:
        pass
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        os.unlink("config.yaml", dir_fd=hermes_fd)
    except OSError:
        pass


def _remember_switch_inode(route_home: str, opened: os.stat_result) -> None:
    hold = _ROUTE_HOME.get()
    if hold is not None and hold.path == route_home:
        hold.config_dev, hold.config_ino = opened.st_dev, opened.st_ino


def _hermes_config_is_ours(
    engine: str,
    route_home: str,
    *,
    dir_fd: int | None = None,
    scrub_true: bool = False,
) -> bool:
    """False when ``.hermes/config.yaml`` is absent, True when it is exactly Recursion's adopt switch.

    Anything else refuses: a link, a non-regular file, another owner or any other content. A file
    of any other size is refused from ``lstat`` alone and never opened, so a config that holds more
    than the switch (it might hold a credential) is not read. A file of exactly that size (37 bytes)
    is opened relative to a ``.hermes`` opened as a real directory, never as a controlling terminal,
    and its type, size and owner are checked again on the opened file before the read.
    With ``dir_fd``, ``.hermes`` is opened under that route-home descriptor (``O_NOFOLLOW``).
    ``scrub_true`` restores or removes a same-read true switch before refusing, so a race that
    rewrote the file this launch just wrote does not leave adoption on.
    """

    hold = _ROUTE_HOME.get()
    if dir_fd is None and hold is not None and hold.fd >= 0 and hold.path == route_home:
        dir_fd = hold.fd
    if dir_fd is not None:
        return _hermes_config_is_ours_at(engine, route_home, dir_fd, scrub_true)
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
        hermes_fd = os.open(parent, _DIR_OPEN_FLAGS)
    except OSError:
        raise _refuse_hermes_config(engine, ".hermes") from None
    try:
        return _read_adopt_switch(engine, route_home, hermes_fd, scrub_true, st)
    finally:
        os.close(hermes_fd)


def _hermes_config_is_ours_at(engine: str, route_home: str, dir_fd: int, scrub_true: bool) -> bool:
    """The route-home descriptor's ``.hermes``, not whatever the path names now."""

    try:
        parent_st = os.stat(".hermes", dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISDIR(parent_st.st_mode):
        raise _refuse_hermes_config(engine, ".hermes")
    try:
        hermes_fd = os.open(".hermes", _DIR_OPEN_FLAGS, dir_fd=dir_fd)
    except OSError:
        raise _refuse_hermes_config(engine, ".hermes") from None
    try:
        try:
            st = os.stat("config.yaml", dir_fd=hermes_fd, follow_symlinks=False)
        except OSError:
            return False
        if not stat.S_ISREG(st.st_mode) or st.st_size != len(_HERMES_ADOPT_YAML) or st.st_uid != os.geteuid():
            raise _refuse_hermes_config(engine, "config.yaml")
        return _read_adopt_switch(engine, route_home, hermes_fd, scrub_true, st)
    finally:
        os.close(hermes_fd)


def _read_adopt_switch(
    engine: str,
    route_home: str,
    hermes_fd: int,
    scrub_true: bool,
    st: os.stat_result,
) -> bool:
    fd = -1
    try:
        fd = os.open("config.yaml", _HERMES_OPEN_FLAGS, dir_fd=hermes_fd)
    except OSError:
        raise _refuse_hermes_config(engine, "config.yaml") from None
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_size != len(_HERMES_ADOPT_YAML)
                or opened.st_uid != os.geteuid()
                or (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino)):
            raise _refuse_hermes_config(engine, "config.yaml")
        data = os.read(fd, len(_HERMES_ADOPT_YAML) + 1)
        if data != _HERMES_ADOPT_YAML:
            if scrub_true and _is_true_variant(data):
                seen = (opened.st_dev, opened.st_ino)
                os.close(fd)
                fd = -1
                _neutralize_true_variant(hermes_fd, seen)
            raise _refuse_hermes_config(engine, "config.yaml")
        _remember_switch_inode(route_home, opened)
        return True
    finally:
        if fd >= 0:
            os.close(fd)


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
    Every create and rename is relative to the route-home descriptor held in ``_check_home``.
    """

    if os.path.basename(parent) != ".hermes":
        raise _refuse_hermes_config(engine, ".hermes")
    hold = _route_home_for(engine, route_home)
    name = _HERMES_STAGING_PREFIX + secrets.token_hex(8)
    os.mkdir(name, 0o700, dir_fd=hold.fd)
    try:
        staging_fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=hold.fd)
        try:
            _create_adopt_switch("config.yaml", dir_fd=staging_fd)
            _fsync_dir(staging_fd)
        finally:
            os.close(staging_fd)
        try:
            os.rename(name, ".hermes", src_dir_fd=hold.fd, dst_dir_fd=hold.fd)
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                raise _refuse_hermes_config(engine, ".hermes") from None
            raise
    except BaseException:
        cleanup = -1
        try:
            cleanup = os.open(name, _DIR_OPEN_FLAGS, dir_fd=hold.fd)
            os.unlink("config.yaml", dir_fd=cleanup)
        except OSError:
            pass
        finally:
            if cleanup >= 0:
                os.close(cleanup)
        try:
            os.rmdir(name, dir_fd=hold.fd)
        except OSError:
            pass
        raise
    # A swap of the staged file between the write and the rename is not the switch.
    if not _hermes_config_is_ours(engine, route_home, dir_fd=hold.fd, scrub_true=True):
        raise _refuse_hermes_config(engine, "config.yaml")
    _fsync_dir(hold.fd)


def _link_hermes_config(engine: str, route_home: str, parent: str) -> None:
    """``.hermes`` holds Hermes's own entries and no ``config.yaml``: add the switch without replacing.

    The switch is written to a temp file in the route home and hard-linked into ``.hermes``. A link
    never overwrites, so a ``config.yaml`` that appeared meanwhile refuses instead of being replaced.
    Both the temp file and the link are created under the held route-home descriptor.
    """

    if os.path.basename(parent) != ".hermes":
        raise _refuse_hermes_config(engine, ".hermes")
    hold = _route_home_for(engine, route_home)
    name = _HERMES_STAGING_PREFIX + secrets.token_hex(8)
    try:
        _create_adopt_switch(name, dir_fd=hold.fd)
        try:
            parent_fd = os.open(".hermes", _DIR_OPEN_FLAGS, dir_fd=hold.fd)
            try:
                os.link(name, "config.yaml", src_dir_fd=hold.fd, dst_dir_fd=parent_fd)
                _fsync_dir(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            os.unlink(name, dir_fd=hold.fd)
    except OSError:
        raise _refuse_hermes_config(engine, "config.yaml") from None
    # A swap of the temp file between the write and the link is not the switch. The link's
    # success only means the new name exists; launch only when that name is exactly ours.
    if not _hermes_config_is_ours(engine, route_home, dir_fd=hold.fd, scrub_true=True):
        raise _refuse_hermes_config(engine, "config.yaml")
    _fsync_dir(hold.fd)


def _apply_hermes_adopt_override(decision: RouteDecision, route_home: str) -> None:
    """Write Hermes's adopt switch, or reuse the one an earlier launch in this home wrote."""

    if decision.engine != "hermes":
        return
    if decision.engine_config != ((HERMES_ADOPT_EXTERNAL_LOGINS, False),):
        raise SubscriptionRouteRefused(decision.engine, "hermes_adopt_external_logins_not_off")
    # A swap after the caller's recheck still has to miss this write. The descriptor, not the path,
    # is what mkdir and openat use.
    _recheck_route_home(decision.engine, route_home)
    hold = _route_home_for(decision.engine, route_home)
    parent = os.path.join(route_home, ".hermes")
    if _hermes_config_is_ours(decision.engine, route_home, dir_fd=hold.fd):
        return
    if not os.path.lexists(parent) or _hermes_dir_is_empty(route_home):
        _stage_hermes_dir(decision.engine, route_home, parent)
    elif os.path.islink(parent) or not os.path.isdir(parent):
        raise _refuse_hermes_config(decision.engine, ".hermes")
    else:
        _link_hermes_config(decision.engine, route_home, parent)


def _recheck_route_home(engine: str, route_home: str) -> None:
    """The path still names the directory ``_check_home`` held, and it is still not an operator home.

    The stamp is that lstat's device, inode, owner and mode. The held descriptor must still be that
    inode. A 0700 replacement, a widened mode, or the operator directory renamed onto the path refuses.
    """

    hold = _ROUTE_HOME.get()
    if hold is None or hold.fd < 0 or hold.path != route_home:
        raise SubscriptionRouteRefused(engine, "route_home_not_private")
    try:
        st = os.lstat(route_home)
        opened = os.fstat(hold.fd)
        real = os.path.realpath(route_home)
    except OSError:
        raise SubscriptionRouteRefused(engine, "route_home_not_private") from None
    if (st.st_dev, st.st_ino) in hold.operator_ids or any(
        _operator_contains(real, home) for home in hold.operator_reals
    ):
        raise SubscriptionRouteRefused(engine, "route_home_is_operator_home")
    if ((opened.st_dev, opened.st_ino) != (hold.dev, hold.ino)
            or (st.st_dev, st.st_ino, st.st_uid, st.st_mode) != (hold.dev, hold.ino, hold.uid, hold.mode)
            or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077):
        raise SubscriptionRouteRefused(engine, "route_home_not_private")


def _reverify_hermes_switch(engine: str, route_home: str) -> tuple[int, int]:
    """Read the switch from the inode this launch accepted, under the held route home.

    ``config.yaml`` is opened ``O_NOFOLLOW`` under that directory. Its device and inode must be
    the ones recorded when the switch was written or reused, and the bytes must still be the false
    switch. A true switch is restored or removed before the refusal. Returns the device and inode
    of the ``.hermes`` directory that read used, so the caller can bind the child's paths to it.
    The path ``lstat`` is not done here: it runs after this returns, immediately before launch.
    """

    hold = _route_home_for(engine, route_home)
    if hold.config_dev is None or hold.config_ino is None:
        raise _refuse_hermes_config(engine, "config.yaml")
    try:
        hermes_fd = os.open(".hermes", _DIR_OPEN_FLAGS, dir_fd=hold.fd)
    except OSError:
        raise _refuse_hermes_config(engine, ".hermes") from None
    fd = -1
    hermes_id: tuple[int, int] | None = None
    try:
        try:
            fd = os.open("config.yaml", _HERMES_OPEN_FLAGS, dir_fd=hermes_fd)
        except OSError:
            raise _refuse_hermes_config(engine, "config.yaml") from None
        opened = os.fstat(fd)
        data = os.read(fd, len(_HERMES_ADOPT_YAML) + 1)
        try:
            hermes_st = os.fstat(hermes_fd)
        except OSError:
            raise _refuse_hermes_config(engine, ".hermes") from None
        if (stat.S_ISREG(opened.st_mode) and opened.st_uid == os.geteuid()
                and (opened.st_dev, opened.st_ino) == (hold.config_dev, hold.config_ino)
                and data == _HERMES_ADOPT_YAML):
            hermes_id = (hermes_st.st_dev, hermes_st.st_ino)
        else:
            if _is_true_variant(data):
                seen = (opened.st_dev, opened.st_ino)
                os.close(fd)
                fd = -1
                _neutralize_true_variant(hermes_fd, seen)
            raise _refuse_hermes_config(engine, "config.yaml")
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(hermes_fd)
    if hermes_id is None:
        raise _refuse_hermes_config(engine, ".hermes")
    return hermes_id


def _bind_hermes_launch_path(engine: str, route_home: str, hermes_dev: int, hermes_ino: int) -> None:
    """After the descriptor read, the paths the child opens still name those inodes.

    ``os.lstat`` does not follow links. ``route_home`` must be the held directory,
    ``route_home/.hermes`` the directory opened under that hold, and
    ``route_home/.hermes/config.yaml`` the switch inode recorded for this launch.

    A same-inode rewrite after the read cannot be bound: the child opens ``config.yaml``
    by path, and ``lstat`` cannot see new bytes in the inode just read. This check does
    not try to close that window. The caller lists ``.hermes`` through the held directory
    fd, records the pinned bytes, and reads them back after the post-listing walk.
    A rewrite or a directory swap after that recheck is still outside this function.
    """

    hold = _route_home_for(engine, route_home)
    try:
        home_st = os.lstat(route_home)
        held = os.fstat(hold.fd)
    except OSError:
        raise SubscriptionRouteRefused(engine, "route_home_not_private") from None
    if ((home_st.st_dev, home_st.st_ino) != (hold.dev, hold.ino)
            or (held.st_dev, held.st_ino) != (hold.dev, hold.ino)
            or not stat.S_ISDIR(home_st.st_mode) or not stat.S_ISDIR(held.st_mode)):
        raise SubscriptionRouteRefused(engine, "route_home_not_private")
    try:
        hermes_st = os.lstat(os.path.join(route_home, ".hermes"))
    except OSError:
        raise _refuse_hermes_config(engine, ".hermes") from None
    if (not stat.S_ISDIR(hermes_st.st_mode)
            or (hermes_st.st_dev, hermes_st.st_ino) != (hermes_dev, hermes_ino)):
        raise _refuse_hermes_config(engine, ".hermes")
    config_dev, config_ino = hold.config_dev, hold.config_ino
    if config_dev is None or config_ino is None:
        raise _refuse_hermes_config(engine, "config.yaml")
    try:
        config_st = os.lstat(os.path.join(route_home, ".hermes", "config.yaml"))
    except OSError:
        raise _refuse_hermes_config(engine, "config.yaml") from None
    if (not stat.S_ISREG(config_st.st_mode)
            or (config_st.st_dev, config_st.st_ino) != (config_dev, config_ino)):
        raise _refuse_hermes_config(engine, "config.yaml")


def _bind_route_home_from_root(engine: str, route_home: str) -> None:
    """The path still names the held directory, walked from ``/`` without following a symlink.

    ``os.lstat`` of the final path follows ancestor symlinks, so a retargeted ancestor can
    still report the held inode until the next look. Each real-path component is opened
    ``O_NOFOLLOW``. Ancestors use ``O_PATH|O_DIRECTORY`` and ``fstat``: path lookup needs
    search permission, and ``O_RDONLY`` refuses a mode ``0111`` directory. The final route
    directory is opened with the read flags. A symlink component, a replaced parent, or a
    final inode other than the held directory refuses. This does not cover a swap that
    happens after it returns.
    """

    hold = _route_home_for(engine, route_home)
    try:
        real = os.path.realpath(route_home)
        named = os.lstat(route_home)
        held = os.fstat(hold.fd)
    except OSError:
        raise SubscriptionRouteRefused(engine, "route_home_not_private") from None
    if ((named.st_dev, named.st_ino) != (hold.dev, hold.ino)
            or (held.st_dev, held.st_ino) != (hold.dev, hold.ino)
            or not stat.S_ISDIR(named.st_mode) or not stat.S_ISDIR(held.st_mode)):
        raise SubscriptionRouteRefused(engine, "route_home_not_private")
    parts = [part for part in real.split(os.sep) if part]
    dir_fd = -1
    opened: tuple[int, int] | None = None
    try:
        dir_fd = os.open(os.sep, _ANCESTOR_OPEN_FLAGS)
        for index, part in enumerate(parts):
            # The route directory itself is readable by its owner. Ancestors may not be.
            flags = _DIR_OPEN_FLAGS if index == len(parts) - 1 else _ANCESTOR_OPEN_FLAGS
            try:
                st = os.stat(part, dir_fd=dir_fd, follow_symlinks=False)
            except OSError:
                raise SubscriptionRouteRefused(engine, "route_home_not_private") from None
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise SubscriptionRouteRefused(engine, "route_home_not_private")
            try:
                next_fd = os.open(part, flags, dir_fd=dir_fd)
            except OSError:
                raise SubscriptionRouteRefused(engine, "route_home_not_private") from None
            os.close(dir_fd)
            dir_fd = next_fd
            checked = os.fstat(dir_fd)
            if stat.S_ISLNK(checked.st_mode) or not stat.S_ISDIR(checked.st_mode):
                raise SubscriptionRouteRefused(engine, "route_home_not_private")
        final = os.fstat(dir_fd)
        opened = (final.st_dev, final.st_ino)
    except SubscriptionRouteRefused:
        raise
    except OSError:
        raise SubscriptionRouteRefused(engine, "route_home_not_private") from None
    finally:
        if dir_fd >= 0:
            os.close(dir_fd)
    if opened != (hold.dev, hold.ino):
        raise SubscriptionRouteRefused(engine, "route_home_not_private")


def _reject_hermes_dotenv(engine: str, hermes_fd: int, name: str) -> None:
    """``name`` is in the held ``.hermes``. Refuse a file, symlink or directory. Do not follow or read it."""

    flags = getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOCTTY", 0)
    fd = -1
    try:
        # Open and close only. A file, a directory and a symlink all refuse, and the bytes are not read.
        fd = os.open(name, flags, dir_fd=hermes_fd)
    except OSError:
        # O_NOFOLLOW on a symlink is ELOOP where O_PATH is absent. The name was listed.
        raise SubscriptionRouteRefused(engine, "hermes_dotenv_in_home", name) from None
    finally:
        if fd >= 0:
            os.close(fd)
    raise SubscriptionRouteRefused(engine, "hermes_dotenv_in_home", name)


def _pin_hermes_config_fd(engine: str, hermes_fd: int, hold: _RouteHome) -> int:
    """Open ``config.yaml`` under the held ``.hermes`` and require the false adopt switch.

    ``O_RDONLY|O_NOFOLLOW`` (plus the non-blocking, non-controlling flags used for every
    switch read). The bytes, the size, ``mtime_ns``, ``ctime_ns`` and link count are taken
    from this descriptor and recorded on ``hold``. That stamp is the pin the post-listing
    recheck compares. The caller keeps the descriptor open until that recheck has read it
    again with ``pread``.
    """

    fd = -1
    data = b""
    try:
        fd = os.open("config.yaml", _HERMES_OPEN_FLAGS, dir_fd=hermes_fd)
        opened = os.fstat(fd)
        data = os.read(fd, len(_HERMES_ADOPT_YAML) + 1)
    except OSError:
        if fd >= 0:
            os.close(fd)
        raise _refuse_hermes_config(engine, "config.yaml") from None
    if (hold.config_dev is not None and hold.config_ino is not None
            and stat.S_ISREG(opened.st_mode) and opened.st_uid == os.geteuid()
            and opened.st_nlink >= 1
            and opened.st_size == len(_HERMES_ADOPT_YAML)
            and (opened.st_dev, opened.st_ino) == (hold.config_dev, hold.config_ino)
            and data == _HERMES_ADOPT_YAML):
        hold.config_bytes = data
        hold.config_size = opened.st_size
        hold.config_mtime_ns = opened.st_mtime_ns
        hold.config_ctime_ns = opened.st_ctime_ns
        hold.config_nlink = opened.st_nlink
        return fd
    if _is_true_variant(data):
        seen = (opened.st_dev, opened.st_ino)
        os.close(fd)
        _neutralize_true_variant(hermes_fd, seen)
    else:
        os.close(fd)
    raise _refuse_hermes_config(engine, "config.yaml")


def _pinned_config_stamp(
    st: os.stat_result,
    pin_id: tuple[int, int],
    size: int,
    mtime_ns: int,
    ctime_ns: int,
    nlink: int,
) -> bool:
    """True when ``st`` is the pinned regular file and size, mtime, ctime and nlink match.

    Link count is the signal a same-tick replacement does not hide. Unlink drops it to 0
    on the inode the descriptor still holds, including when ``ctime_ns`` stays put.
    """

    return (stat.S_ISREG(st.st_mode)
            and st.st_nlink >= 1
            and (st.st_dev, st.st_ino) == pin_id
            and st.st_size == size
            and st.st_mtime_ns == mtime_ns
            and st.st_ctime_ns == ctime_ns
            and st.st_nlink == nlink)


def _read_pinned_config_fd(
    engine: str,
    config_fd: int,
    pin_id: tuple[int, int],
    size: int,
    mtime_ns: int,
    ctime_ns: int,
    nlink: int,
    recorded: bytes,
) -> tuple[os.stat_result, bytes]:
    """``fstat`` then ``pread`` the pinned descriptor. Both must be the pin."""

    try:
        st = os.fstat(config_fd)
        # One byte past the pin catches an append that lands between this fstat and the read.
        data = os.pread(config_fd, len(recorded) + 1, 0)
    except OSError:
        raise _refuse_hermes_config(engine, "config.yaml") from None
    if not _pinned_config_stamp(st, pin_id, size, mtime_ns, ctime_ns, nlink) or data != recorded:
        raise _refuse_hermes_config(engine, "config.yaml")
    return st, data


def _require_config_name_matches_fd(
    engine: str,
    hermes_fd: int,
    config_fd: int,
    pin_id: tuple[int, int],
    size: int,
    mtime_ns: int,
    ctime_ns: int,
    nlink: int,
) -> None:
    """``config.yaml`` by name through ``hermes_fd`` is the pinned descriptor.

    The name stat is device and inode. The ``fstat`` that follows it must show link
    count at least 1 and the full stamp (size, ``mtime_ns``, ``ctime_ns``, link count).
    A replacement after the name stat unlinks the inode this descriptor holds; on a
    coarse tick ``ctime_ns`` does not move, and link count is what changes.
    """

    try:
        named = os.stat("config.yaml", dir_fd=hermes_fd, follow_symlinks=False)
        opened = os.fstat(config_fd)
    except OSError:
        raise _refuse_hermes_config(engine, "config.yaml") from None
    named_id = (named.st_dev, named.st_ino)
    opened_id = (opened.st_dev, opened.st_ino)
    if (not stat.S_ISREG(named.st_mode) or named_id != pin_id or named_id != opened_id
            or not _pinned_config_stamp(opened, pin_id, size, mtime_ns, ctime_ns, nlink)):
        raise _refuse_hermes_config(engine, "config.yaml")


def _require_hermes_name_matches_fd(
    engine: str,
    home_fd: int,
    hermes_fd: int,
    hermes_dev: int,
    hermes_ino: int,
) -> tuple[int, int]:
    """``.hermes`` by name on the held home fd is the directory ``hermes_fd`` has open.

    The name is stat'd first. ``fstat`` of the fd does not see a replacement that lands
    after that stat returns; the next observation opens the name again.
    """

    try:
        named = os.stat(".hermes", dir_fd=home_fd, follow_symlinks=False)
        opened = os.fstat(hermes_fd)
    except OSError:
        raise _refuse_hermes_config(engine, ".hermes") from None
    opened_id = (opened.st_dev, opened.st_ino)
    if (not stat.S_ISDIR(named.st_mode) or not stat.S_ISDIR(opened.st_mode)
            or (named.st_dev, named.st_ino) != opened_id
            or opened_id != (hermes_dev, hermes_ino)):
        raise _refuse_hermes_config(engine, ".hermes")
    return opened_id


def _hermes_listing_names(engine: str, hermes_fd: int, auth: str) -> tuple[str, ...]:
    """The sorted names in ``hermes_fd``, after the dotenv and API-key rules.

    ``.env`` and ``.op.env`` refuse from the listing and again when ``stat`` does not
    return ``ENOENT``, so a name created after ``listdir`` is still seen. On an API-key
    route any other name refuses from this snapshot. A subscription route may keep other
    names. Allowing those extra names is an owner decision (Lucas, PRODUCT.md 055cc7a5
    section 2); this check does not make it.
    """

    try:
        names = os.listdir(hermes_fd)
    except OSError:
        raise _refuse_hermes_config(engine, ".hermes") from None
    ordered = tuple(sorted(names))
    for name in ordered:
        if name in _HERMES_DOTENV_NAMES:
            _reject_hermes_dotenv(engine, hermes_fd, name)
    if "config.yaml" not in ordered:
        raise _refuse_hermes_config(engine, "config.yaml")
    if auth != "subscription":
        extra = sorted(set(ordered) - _HERMES_LAUNCH_ALLOW)
        if extra:
            raise SubscriptionRouteRefused(engine, "engine_store_in_route_home", extra[0])
    for name in sorted(_HERMES_DOTENV_NAMES):
        try:
            os.stat(name, dir_fd=hermes_fd, follow_symlinks=False)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                continue
            raise SubscriptionRouteRefused(engine, "hermes_dotenv_in_home", name) from None
        raise SubscriptionRouteRefused(engine, "hermes_dotenv_in_home", name)
    return ordered


def _observe_pinned_hermes(
    engine: str,
    home_fd: int,
    config_fd: int,
    pin_id: tuple[int, int],
    size: int,
    mtime_ns: int,
    ctime_ns: int,
    nlink: int,
    recorded: bytes,
    hermes_dev: int,
    hermes_ino: int,
    auth: str,
) -> tuple:
    """One full look at the pin, the names, and the directory. Mismatch refuses.

    The result is the bytes, size, ``mtime_ns``, ``ctime_ns`` and device/inode of the
    config descriptor, the device/inode of ``.hermes``, and the sorted listing. The
    config name is stat'd again after the listing. A new inode during the listing
    fails that stat; one that lands after it returns is stat'd by the next observation.
    """

    st, data = _read_pinned_config_fd(
        engine, config_fd, pin_id, size, mtime_ns, ctime_ns, nlink, recorded)
    try:
        hermes_fd = os.open(".hermes", _DIR_OPEN_FLAGS, dir_fd=home_fd)
    except OSError:
        raise _refuse_hermes_config(engine, ".hermes") from None
    try:
        try:
            opened_dir = os.fstat(hermes_fd)
        except OSError:
            raise _refuse_hermes_config(engine, ".hermes") from None
        if (not stat.S_ISDIR(opened_dir.st_mode)
                or (opened_dir.st_dev, opened_dir.st_ino) != (hermes_dev, hermes_ino)):
            raise _refuse_hermes_config(engine, ".hermes")
        _require_config_name_matches_fd(
            engine, hermes_fd, config_fd, pin_id, size, mtime_ns, ctime_ns, nlink)
        hermes_id = _require_hermes_name_matches_fd(
            engine, home_fd, hermes_fd, hermes_dev, hermes_ino)
        names = _hermes_listing_names(engine, hermes_fd, auth)
        # After the names. A new inode during the listing fails here, and the next
        # observation stats the name again.
        _require_config_name_matches_fd(
            engine, hermes_fd, config_fd, pin_id, size, mtime_ns, ctime_ns, nlink)
    finally:
        os.close(hermes_fd)
    return (data, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_dev, st.st_ino,
            hermes_id[0], hermes_id[1], names)


def _require_pinned_listing(
    engine: str,
    names: tuple[str, ...],
    pinned_names: tuple[str, ...],
    auth: str,
) -> None:
    """The directory listing still matches the pin. A new dotenv or API-key name refuses.

    An API-key listing must equal the pinned set, so ``state.db`` created after the
    earlier ``listdir`` refuses. A subscription route may still gain names other than
    ``.env`` and ``.op.env``; that store is an owner residual and this check does not
    refuse it. A pinned name that disappeared refuses on either route.
    """

    ordered = tuple(sorted(names))
    for name in ordered:
        if name in _HERMES_DOTENV_NAMES:
            raise SubscriptionRouteRefused(engine, "hermes_dotenv_in_home", name)
    if "config.yaml" not in ordered:
        raise _refuse_hermes_config(engine, "config.yaml")
    pinned = tuple(pinned_names)
    if auth != "subscription":
        if ordered != pinned:
            extra = sorted(set(ordered) - _HERMES_LAUNCH_ALLOW)
            if extra:
                raise SubscriptionRouteRefused(engine, "engine_store_in_route_home", extra[0])
            raise _refuse_hermes_config(engine, "config.yaml")
        return
    if not set(pinned) <= set(ordered):
        raise _refuse_hermes_config(engine, ".hermes")


def _reverify_pinned_names(
    engine: str,
    home_fd: int,
    config_fd: int,
    pin_id: tuple[int, int],
    hermes_dev: int,
    hermes_ino: int,
    size: int,
    mtime_ns: int,
    ctime_ns: int,
    nlink: int,
    pinned_names: tuple[str, ...],
    auth: str,
    opener: Callable[..., int],
    stater: Callable[..., os.stat_result],
    fstater: Callable[..., os.stat_result],
    lister: Callable[..., list[str]],
    closer: Callable[[int], None],
) -> None:
    """Re-check every name, then the descriptor that would catch a swap of that name.

    ``.hermes`` is opened by name from the home fd (``O_NOFOLLOW|O_DIRECTORY``) and
    ``fstat`` must be the pinned directory. ``listdir`` on that fd must match the pinned
    set. ``config.yaml`` stat'd with ``AT_SYMLINK_NOFOLLOW`` must be the pinned inode,
    and ``fstat`` of the config fd must show link count at least 1 and the full stamp.
    ``.hermes`` stat'd by name from the home fd must be the pinned directory again.
    This function does not unlink.
    """

    fd = -1
    try:
        try:
            fd = opener(".hermes", _DIR_OPEN_FLAGS, dir_fd=home_fd)
        except OSError:
            raise _refuse_hermes_config(engine, ".hermes") from None
        try:
            opened_dir = fstater(fd)
        except OSError:
            raise _refuse_hermes_config(engine, ".hermes") from None
        if (not stat.S_ISDIR(opened_dir.st_mode)
                or (opened_dir.st_dev, opened_dir.st_ino) != (hermes_dev, hermes_ino)):
            raise _refuse_hermes_config(engine, ".hermes")
        try:
            names = tuple(sorted(lister(fd)))
        except OSError:
            raise _refuse_hermes_config(engine, ".hermes") from None
        _require_pinned_listing(engine, names, pinned_names, auth)
        try:
            named = stater("config.yaml", dir_fd=fd, follow_symlinks=False)
        except OSError:
            raise _refuse_hermes_config(engine, "config.yaml") from None
        if not stat.S_ISREG(named.st_mode) or (named.st_dev, named.st_ino) != pin_id:
            raise _refuse_hermes_config(engine, "config.yaml")
        try:
            opened = fstater(config_fd)
        except OSError:
            raise _refuse_hermes_config(engine, "config.yaml") from None
        if not _pinned_config_stamp(opened, pin_id, size, mtime_ns, ctime_ns, nlink):
            raise _refuse_hermes_config(engine, "config.yaml")
        try:
            named_dir = stater(".hermes", dir_fd=home_fd, follow_symlinks=False)
        except OSError:
            raise _refuse_hermes_config(engine, ".hermes") from None
        if (not stat.S_ISDIR(named_dir.st_mode)
                or (named_dir.st_dev, named_dir.st_ino) != (hermes_dev, hermes_ino)):
            raise _refuse_hermes_config(engine, ".hermes")
        # The name stat above does not see a directory swapped in after it returns.
        # Open that name again; fstat of the new descriptor catches the swap.
        again = -1
        try:
            try:
                again = opener(".hermes", _DIR_OPEN_FLAGS, dir_fd=home_fd)
            except OSError:
                raise _refuse_hermes_config(engine, ".hermes") from None
            try:
                again_st = fstater(again)
            except OSError:
                raise _refuse_hermes_config(engine, ".hermes") from None
            if (not stat.S_ISDIR(again_st.st_mode)
                    or (again_st.st_dev, again_st.st_ino) != (hermes_dev, hermes_ino)):
                raise _refuse_hermes_config(engine, ".hermes")
        finally:
            if again >= 0:
                closer(again)
    finally:
        if fd >= 0:
            closer(fd)


def _seal_pinned_hermes_names(
    engine: str,
    route_home: str,
    home_fd: int,
    home_dev: int,
    home_ino: int,
    config_fd: int,
    pin_id: tuple[int, int],
    hermes_dev: int,
    hermes_ino: int,
    size: int,
    mtime_ns: int,
    ctime_ns: int,
    nlink: int,
    pinned_names: tuple[str, ...],
    auth: str,
) -> None:
    """Last look, after ``pread`` returns. Any mismatch refuses. Does not unlink.

    HOME is ``lstat``'d by path and must still be a real directory with the pinned
    device and inode, not a symlink. ``.hermes`` is opened by name from the home fd
    (``O_NOFOLLOW|O_DIRECTORY``) and ``fstat`` must be the pinned directory; the name
    is ``lstat``'d again so a replacement after that open refuses. ``listdir`` must
    match the pinned names. ``config.yaml`` is ``lstat``'d by name and must be the
    pinned inode with link count at least 1. ``fstat`` of the config fd must show
    the full stamp. Every call is a function captured at import.
    """

    try:
        home_st = _LIBC_LSTAT(route_home)
    except OSError:
        raise SubscriptionRouteRefused(engine, "route_home_not_private") from None
    if (stat.S_ISLNK(home_st.st_mode) or not stat.S_ISDIR(home_st.st_mode)
            or (home_st.st_dev, home_st.st_ino) != (home_dev, home_ino)):
        raise SubscriptionRouteRefused(engine, "route_home_not_private")
    fd = -1
    try:
        try:
            fd = _LIBC_OPEN(".hermes", _DIR_OPEN_FLAGS, dir_fd=home_fd)
        except OSError:
            raise _refuse_hermes_config(engine, ".hermes") from None
        try:
            opened_dir = _LIBC_FSTAT(fd)
        except OSError:
            raise _refuse_hermes_config(engine, ".hermes") from None
        if (not stat.S_ISDIR(opened_dir.st_mode)
                or (opened_dir.st_dev, opened_dir.st_ino) != (hermes_dev, hermes_ino)):
            raise _refuse_hermes_config(engine, ".hermes")
        # The fd above does not see a directory swapped in after open returned.
        try:
            named_dir = _LIBC_LSTAT(".hermes", dir_fd=home_fd)
        except OSError:
            raise _refuse_hermes_config(engine, ".hermes") from None
        if (not stat.S_ISDIR(named_dir.st_mode)
                or (named_dir.st_dev, named_dir.st_ino) != (hermes_dev, hermes_ino)):
            raise _refuse_hermes_config(engine, ".hermes")
        try:
            names = tuple(sorted(_LIBC_LISTDIR(fd)))
        except OSError:
            raise _refuse_hermes_config(engine, ".hermes") from None
        _require_pinned_listing(engine, names, pinned_names, auth)
        try:
            named = _LIBC_LSTAT("config.yaml", dir_fd=fd)
        except OSError:
            raise _refuse_hermes_config(engine, "config.yaml") from None
        if (not stat.S_ISREG(named.st_mode) or named.st_nlink < 1
                or (named.st_dev, named.st_ino) != pin_id):
            raise _refuse_hermes_config(engine, "config.yaml")
        try:
            opened = _LIBC_FSTAT(config_fd)
        except OSError:
            raise _refuse_hermes_config(engine, "config.yaml") from None
        if not _pinned_config_stamp(opened, pin_id, size, mtime_ns, ctime_ns, nlink):
            raise _refuse_hermes_config(engine, "config.yaml")
    finally:
        if fd >= 0:
            _LIBC_CLOSE(fd)


def _recheck_pinned_hermes_config(
    engine: str,
    route_home: str,
    config_fd: int,
    hermes_dev: int,
    hermes_ino: int,
    auth: str,
) -> None:
    """The pinned switch is unchanged, and the names still point at that pin.

    The observation in ``_observe_pinned_hermes`` runs twice. The two results must be
    identical and equal to the pin: bytes, size, ``mtime_ns``, ``ctime_ns``, device and
    inode of ``config.yaml`` and of ``.hermes``, and the sorted listing. A same-size
    ``O_TRUNC`` that keeps ``mtime_ns`` and ``ctime_ns`` still changes the bytes, so the
    second read refuses it. Link count is part of that stamp.

    The last operations before the byte read re-verify every name-based fact, each
    lookup followed by the descriptor check that would catch a swap, and that
    verification runs once more immediately before the final ``fstat`` and ``pread``.
    ``pread`` is the function captured at import. After it returns, the seal
    ``lstat``s HOME by path, re-opens and re-lists ``.hermes``, ``lstat``s
    ``config.yaml`` by name, and ``fstat``s the config fd. Any difference refuses.
    This function does not unlink.

    A same-uid change after that seal, before the child execs or opens by path, remains
    an owner-decision residual. The child opens ``config.yaml`` and ``HOME`` by path,
    and that window is not fixed in-process. Subscription extra names other than
    ``.env`` and ``.op.env`` stay allowed.
    """

    hold = _route_home_for(engine, route_home)
    recorded = hold.config_bytes
    recorded_size = hold.config_size
    recorded_mtime = hold.config_mtime_ns
    recorded_ctime = hold.config_ctime_ns
    recorded_nlink = hold.config_nlink
    if (recorded is None or recorded_size is None or recorded_size != len(recorded)
            or hold.config_dev is None or hold.config_ino is None
            or recorded_mtime is None or recorded_ctime is None
            or recorded_nlink is None or recorded_nlink < 1):
        raise _refuse_hermes_config(engine, "config.yaml")
    pin_id = (hold.config_dev, hold.config_ino)
    first = _observe_pinned_hermes(
        engine, hold.fd, config_fd, pin_id, recorded_size, recorded_mtime, recorded_ctime,
        recorded_nlink, recorded, hermes_dev, hermes_ino, auth)
    second = _observe_pinned_hermes(
        engine, hold.fd, config_fd, pin_id, recorded_size, recorded_mtime, recorded_ctime,
        recorded_nlink, recorded, hermes_dev, hermes_ino, auth)
    if first != second:
        if first[:6] != second[:6]:
            raise _refuse_hermes_config(engine, "config.yaml")
        raise _refuse_hermes_config(engine, ".hermes")
    pinned_names = tuple(second[8])
    # After the second listing. A name that changed during that observation is checked
    # again here, and once more immediately before the final read.
    verify_args = (
        engine, hold.fd, config_fd, pin_id, hermes_dev, hermes_ino, recorded_size,
        recorded_mtime, recorded_ctime, recorded_nlink, pinned_names, auth,
    )
    _reverify_pinned_names(*verify_args, os.open, os.stat, os.fstat, os.listdir, os.close)
    _reverify_pinned_names(
        *verify_args, _LIBC_OPEN, _LIBC_STAT, _LIBC_FSTAT, _LIBC_LISTDIR, _LIBC_CLOSE)
    # The fstat is the descriptor check for the config name. pread is the last byte
    # read, and it is the function captured at import. The seal below is the last
    # look at the names, after this pread returns.
    try:
        last = _LIBC_FSTAT(config_fd)
    except OSError:
        raise _refuse_hermes_config(engine, "config.yaml") from None
    if not _pinned_config_stamp(
            last, pin_id, recorded_size, recorded_mtime, recorded_ctime, recorded_nlink):
        raise _refuse_hermes_config(engine, "config.yaml")
    try:
        last_bytes = _LIBC_PREAD(config_fd, len(recorded) + 1, 0)
    except OSError:
        raise _refuse_hermes_config(engine, "config.yaml") from None
    if last_bytes != recorded:
        raise _refuse_hermes_config(engine, "config.yaml")
    _seal_pinned_hermes_names(
        engine, route_home, hold.fd, hold.dev, hold.ino, config_fd, pin_id, hermes_dev,
        hermes_ino, recorded_size, recorded_mtime, recorded_ctime, recorded_nlink,
        pinned_names, auth,
    )


def _refuse_hermes_dir_entries(engine: str, route_home: str, auth: str, hermes_dev: int, hermes_ino: int) -> int:
    """List the held ``.hermes`` and refuse a dotenv name, or any other name on an API-key route.

    The directory is opened from the route-home descriptor (``O_NOFOLLOW``). ``listdir`` uses
    that fd. A ``.env`` planted after an earlier listing is still in this one. A subscription
    route may keep other names; those are the uninspected own store. ``config.yaml`` is opened
    from this fd and returned still open; the caller closes it after the post-listing recheck.
    A name created after this returns is seen when the caller lists the directory again.
    """

    hold = _route_home_for(engine, route_home)
    try:
        hermes_fd = os.open(".hermes", _DIR_OPEN_FLAGS, dir_fd=hold.fd)
    except OSError:
        raise _refuse_hermes_config(engine, ".hermes") from None
    pinned = -1
    try:
        try:
            hermes_st = os.fstat(hermes_fd)
            names = os.listdir(hermes_fd)
        except OSError:
            raise _refuse_hermes_config(engine, ".hermes") from None
        if (not stat.S_ISDIR(hermes_st.st_mode)
                or (hermes_st.st_dev, hermes_st.st_ino) != (hermes_dev, hermes_ino)):
            raise _refuse_hermes_config(engine, ".hermes")
        for name in sorted(names):
            if name in _HERMES_DOTENV_NAMES:
                _reject_hermes_dotenv(engine, hermes_fd, name)
        if "config.yaml" not in names:
            raise _refuse_hermes_config(engine, "config.yaml")
        try:
            cfg = os.stat("config.yaml", dir_fd=hermes_fd, follow_symlinks=False)
        except OSError:
            raise _refuse_hermes_config(engine, "config.yaml") from None
        if (hold.config_dev is None or hold.config_ino is None or not stat.S_ISREG(cfg.st_mode)
                or (cfg.st_dev, cfg.st_ino) != (hold.config_dev, hold.config_ino)):
            raise _refuse_hermes_config(engine, "config.yaml")
        pinned = _pin_hermes_config_fd(engine, hermes_fd, hold)
        if auth != "subscription":
            extra = sorted(set(names) - _HERMES_LAUNCH_ALLOW)
            if extra:
                raise SubscriptionRouteRefused(engine, "engine_store_in_route_home", extra[0])
        kept, pinned = pinned, -1
        return kept
    finally:
        if pinned >= 0:
            os.close(pinned)
        os.close(hermes_fd)


def _launch_checked(
    launcher: Callable[..., object],
    argv: list[str],
    decision: RouteDecision,
    route_home: str,
    pinned: tuple[str, ...],
    forward: Mapping[str, object],
) -> tuple[RouteDecision, object]:
    # The stamp was taken in _check_home, before the pin check. A swap during argv checks or the
    # pin check must not be the directory the adopt switch is written into, or the HOME that launches.
    if pinned and not _argv0_is_pinned(argv, pinned):
        raise SubscriptionRouteRefused(decision.engine, "argv0_not_pinned")
    if decision.auth == "subscription" and not _argv_binds_model(argv, decision.model):
        raise SubscriptionRouteRefused(decision.engine, "model_not_bound", decision.model)
    _recheck_route_home(decision.engine, route_home)
    if decision.engine == "hermes":
        # Before the adopt write. A refusal here must not create .hermes/config.yaml.
        _bind_route_home_from_root(decision.engine, route_home)
    _apply_hermes_adopt_override(decision, route_home)
    _recheck_route_home(decision.engine, route_home)
    # The checked snapshot, not another read of the caller's mapping. ``forward`` holds only the
    # launch options _reject_launch_shape let through; none can replace env, shell or the model.
    # route_home_fd is a dup of the held directory. It is closed when this call returns, so a
    # real launcher must dup it or spawn before returning. Re-opening env HOME by path is not
    # bound: a same-uid rename after the walk below can make that path a different directory.
    # Residual: if the launcher closed route_home_fd and the process reused that number, the
    # close below closes the new descriptor.
    home_fd = -1
    try:
        forwarded = dict(forward)
        if decision.engine == "hermes":
            # Read the switch through the held dirfd, then lstat the paths, then walk every
            # ancestor from / and list .hermes through the held fd. The listing pins
            # config.yaml and records its bytes, size, mtime_ns, ctime_ns and link count.
            # The walk runs again after the listing. The recheck takes that observation
            # twice and requires the two to match each other and the pin. The last
            # operations re-verify every name (open .hermes, fstat, listdir, the config
            # name, fstat of the config fd including link count, .hermes by name again)
            # and repeat that verification immediately before the final fstat and pread.
            # pread is captured at import. After it returns, the seal lstats HOME by path,
            # re-opens and re-lists .hermes, lstats config.yaml by name, and fstats the
            # config fd. A same-uid change after that seal, before the child execs or
            # opens by path, remains an owner-decision residual (the child opens by path),
            # not fixed in-process. Subscription extra names stay an owner decision
            # (Lucas). XDG_* and HERMES_HOME are unchanged.
            # Also residual: a route_home_fd number reused after the launcher closed it.
            hermes_dev, hermes_ino = _reverify_hermes_switch(decision.engine, route_home)
            _bind_hermes_launch_path(decision.engine, route_home, hermes_dev, hermes_ino)
            _bind_route_home_from_root(decision.engine, route_home)
            config_fd = _refuse_hermes_dir_entries(
                decision.engine, route_home, decision.auth, hermes_dev, hermes_ino)
            try:
                _bind_route_home_from_root(decision.engine, route_home)
                _recheck_pinned_hermes_config(
                    decision.engine, route_home, config_fd, hermes_dev, hermes_ino, decision.auth)
            finally:
                os.close(config_fd)
            try:
                home_fd = os.dup(_route_home_for(decision.engine, route_home).fd)
            except OSError:
                raise SubscriptionRouteRefused(decision.engine, "route_home_not_private") from None
            forwarded["route_home_fd"] = home_fd
        return decision, launcher(
            list(argv), env=dict(decision.child_env), shell=False,
            engine_config=decision.engine_config, model=decision.model, **forwarded,
        )
    finally:
        if home_fd >= 0:
            os.close(home_fd)


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
    decision = RouteDecision(engine, "", "", checked.flags, "wrapper", "", (), "", True, frozen)
    _release_route_home_unless_kept()
    return decision


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

    def launch() -> tuple[RouteDecision, object]:
        forward = _reject_launch_shape("cursor", argv, launch_kwargs, requirements, full_access_acknowledged)
        decision = check_cursor_route(
            child_env, route_home=route_home, wrapper=wrapper, parent_env=parent_env,
            operator_home=operator_home, requirements=requirements,
            full_access_acknowledged=full_access_acknowledged,
        )
        return _launch_checked(launcher, list(argv), decision, route_home, (wrapper.launcher_path,), forward)

    return _call_kept(launch)


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
    decision = RouteDecision(engine, "antigravity", "", checked.flags, "wrapper", "", (), "", True, frozen,
                             measured.version)
    _release_route_home_unless_kept()
    return decision


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

    def launch() -> tuple[RouteDecision, object]:
        forward = _reject_launch_shape(
            "antigravity", argv, launch_kwargs, requirements, full_access_acknowledged,
        )
        decision = check_antigravity_route(
            child_env, version=version, binary_path=binary_path, launcher_path=launcher_path, pin=pin,
            route_home=route_home, parent_env=parent_env, operator_home=operator_home,
            requirements=requirements, full_access_acknowledged=full_access_acknowledged,
        )
        return _launch_checked(launcher, list(argv), decision, route_home, (binary_path, launcher_path), forward)

    return _call_kept(launch)
