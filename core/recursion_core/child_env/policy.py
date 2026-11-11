"""Child-environment allowlist for engine children in the new core (CSL-785, candidate).

The old route transport hands every engine child the whole parent environment
(``scripts/v3/route_contract/transport.py:283`` at master ``209b3ddb``,
``"env": os.environ.copy()``). That file is frozen; this module is the new-core
replacement. It builds the child environment from nothing:

* an exact-name allowlist per engine profile (Claude Code, Codex). Nothing outside
  it reaches the child, so credential names are absent by construction (except the
  user's own sign-in token below), and each profile also lists the names it scrubs so a
  test can assert them by name;
* the value rules ported from csl-research candidate records C2-37 and C2-38
  (``submissions/20261102-csl678-value-check-v3-01``, rule N5h, and
  ``submissions/20261103-csl678-envpath-path-noproxy-01``, rule N5i): file, directory,
  proxy, ``TZ``, perspective-path, private temp root, ``PATH`` and per-engine
  ``NO_PROXY`` checks, refused before any spawn;
* the names PRODUCT.md 055cc7a5 section 3.8 says the Nov 5 list dropped (C1-46, csl-research
  ``20261102-csl653-quota-credential-hardening-grounding-01`` section 5.8): ``ALL_PROXY`` /
  ``all_proxy`` and ``XDG_RUNTIME_DIR`` for both engines, and Claude Code's mTLS and cert-store
  names (``CLAUDE_CODE_CLIENT_CERT``, ``_CLIENT_KEY``, ``_CLIENT_KEY_PASSPHRASE``,
  ``CLAUDE_CODE_CERT_STORE``, ``network-config.md:64-105``) for the Claude profile, each under
  the same value rules as its peers. An override cannot set the client certificate, its
  private key or the passphrase; the parent's own values are still carried;
* the user's own ``CLAUDE_CODE_OAUTH_TOKEN`` passes through to a Claude child unchanged
  (PRODUCT.md 055cc7a5 section 3.3, Nov 8 fleet correction: nothing removes a built-in sign-in
  method from every run). It is never checked, logged or settable by an override; absent in the
  parent means absent in the child;
* every call names its auth mode (``subscription``, ``api_key`` or ``cloud_provider``; PRODUCT.md
  055cc7a5 section 3.3, "Every route starts with an explicit auth mode"; CSLR-D-011). An omitted or
  unknown mode refuses. The vendor key, the base URL and the cloud-provider switches are route-owned:
  the parent's values never reach the child in any mode, and the child gets one only from the
  ``ChildRoute`` the caller passes for the route it launched. This module reads no keyring and no key
  file; the caller hands it a key it already fetched, on an ``api_key`` route only;
* a ``subscription`` route's base URL is refused unless its origin is exactly ``https://api.anthropic.com``, the
  caller passes that origin as one the user already trusted, or the run uses the named full-access profile
  (owner call; CSLR-D-011): the ``CLAUDE_CODE_OAUTH_TOKEN`` passed through would go to that base. This module
  stores no trusted list and asks nothing; the driver does both;
* one change beyond N5h/N5i: links are resolved one component at a time and every
  link met on the way, including links inside a link's target, must be root's or
  ours in a directory chain no one else can write, and no step may enter ``/proc``,
  ``/dev/fd`` or ``/dev/std*``. That answers the hidden-inner-link and owned-alias
  refutations (csl-research ledger CX-3770, CX-3771) for the shapes tested here.

The engine is an explicit profile id, never inferred from argv (ledger CX-082: a
first-token guess let wrapped or renamed argv through). An unknown engine refuses.

POSIX only. On Windows the ownership checks the rules rely on do not exist here, so
every call refuses (``platform_unsupported``) instead of passing values unchecked.

Candidate code: no engine, provider or network is used, and nothing here claims the
new core's runtime calls it yet.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import stat
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

__all__ = [
    "BASE_NAMES",
    "ENGINE_PROFILES",
    "EngineProfile",
    "AUTH_MODES",
    "ChildRoute",
    "EnvRefused",
    "ValidatedEnv",
    "build_child_env",
]


class EnvRefused(Exception):
    """The child environment cannot be built safely; nothing may be spawned.

    ``name`` is the variable (never its value) and ``cause`` a stable reason code.
    """

    code = "env_refused"

    def __init__(self, name: str | None, cause: str) -> None:
        super().__init__(f"{name or '-'}: {cause}")
        self.name, self.cause = name, cause


# The 29 names the Nov 5 tree's route allowlist carried (csl-research
# 20261102-csl672-a4-child-env-values-grounding-01 section 5.1: PATH, HOME and 27 more), plus the three
# engine-neutral names PRODUCT.md 055cc7a5 section 3.8 says it dropped (C1-46): ALL_PROXY, all_proxy and
# XDG_RUNTIME_DIR. 32 names.
BASE_NAMES = (
    "PATH",
    "HOME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SHELL",
    "TZ",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "XDG_RUNTIME_DIR",
)
# Claude Code's mTLS and cert-store names (C1-46; network-config.md:64-105, env-vars.md:219-223).
CLAUDE_TLS_NAMES = (
    "CLAUDE_CODE_CLIENT_CERT",
    "CLAUDE_CODE_CLIENT_KEY",
    "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
    "CLAUDE_CODE_CERT_STORE",
)

VALUE_FILES = ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "SHELL",
               "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY")
VALUE_DIRS = ("SSL_CERT_DIR",)
VALUE_DIR_SINGLE = ("XDG_RUNTIME_DIR",)  # one directory, not a list: no separator split
VALUE_URLS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
# ALL_PROXY may name a SOCKS proxy (curl's ALL_PROXY); same shape rule otherwise, and it counts as a proxy for NO_PROXY.
VALUE_ALL_PROXY = ("ALL_PROXY", "all_proxy")
_ALL_PROXY_SCHEMES = ("http", "https", "socks4", "socks4a", "socks5", "socks5h")
# CLAUDE_CODE_CERT_STORE: a comma list of the documented sources (network-config.md:64-66).
_CERT_STORE_SOURCES = ("bundled", "system")
TEMP_NAMES = ("TMPDIR", "TMP", "TEMP")
NO_PROXY_NAMES = ("NO_PROXY", "no_proxy")
# N5i: the names the engines run by bare name. A link for a name outside this list is
# not checked (envpath record, cell path_tool_link_py); PATH_TOOLS=None checks every entry.
PATH_TOOLS: tuple[str, ...] | None = ("bash", "zsh", "sh", "git", "rg", "node", "sleep")
# A name an override may never set, whatever the profile (the Nov 5 tree's _DENIED).
_DENIED = re.compile(r"(API_?KEY|SECRET|TOKEN|PASSWORD|PASSPHRASE|BASE_URL|_PAT$|USE_BEDROCK|USE_VERTEX|USE_FOUNDRY)", re.IGNORECASE)
# Private-key paths the pattern misses. An override must not point the child at a different key
# (Devin r4510867706). The parent's own value still meets the file rule.
_OVERRIDE_DENIED_NAMES = frozenset({"CLAUDE_CODE_CLIENT_KEY"})
# The client certificate is the other half of that identity: with the key carried from the parent, an override
# of the cert could only pair another file (the key file itself included) with the operator's key (review of
# 58e7081f). It is refused where a profile carries the name; elsewhere the name is simply not allowed.
_CARRIED_ONLY_NAMES = frozenset({"CLAUDE_CODE_CLIENT_CERT"})
# Paths that name a different file in the child than in this process (its own cwd, fds, root).
_PERSPECTIVE = re.compile(r"^/(?:proc|dev/fd|dev/std(?:in|out|err))(?:/|$)")
_PROXY_URL = re.compile(
    r"^(?:(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://)?(?P<userinfo>[^@/]*@)?"
    r"(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::(?P<port>[0-9]{1,5}))?/?$"
)
# A route base URL: http(s)://host[:port][/path]. No user name or password, no query, no fragment.
_BASE_URL = re.compile(
    r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<userinfo>[^@/?#]*@)?"
    r"(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::(?P<port>[0-9]{1,5}))?(?P<path>/[A-Za-z0-9._~/-]*)?\Z"
)
# The one base a subscription route may name without the user's say: Anthropic's own API, as (scheme, host, port).
_OFFICIAL_ORIGIN = ("https", "api.anthropic.com", 443)
_DEFAULT_PORTS = {"http": 80, "https": 443}
_MAX_LINKS = 40
# The three routes Recursion owns (PRODUCT.md 055cc7a5 section 3.3; CSLR-D-011). Nothing else is a mode.
AUTH_MODES = ("subscription", "api_key", "cloud_provider")


@dataclass(frozen=True, repr=False)
class ChildRoute:
    """What the caller says about the route it launched. Separate from the parent environment and from overrides.

    ``api_key`` is a user-entered vendor key the caller already fetched (from the OS keyring, or the owner-only
    file on a host with no keyring); it is accepted on an ``api_key`` route only. ``base_url`` is the route's own
    Anthropic-compatible base, if it has one. ``provider_switches`` are the cloud-provider switches the user
    picked, by name.

    The next two matter on a ``subscription`` route with a ``base_url`` and nowhere else. ``trusted_base_urls``
    are the origins (``scheme://host[:port]``) the user already agreed to send their subscription sign-in to;
    the driver keeps that list and fills it in, this module only compares. ``full_access_acknowledged`` says
    this run uses the named full-access profile and the user acknowledged it for this run (PRODUCT.md 055cc7a5
    Q20, O44, H-06); only the bool ``True`` counts. ``repr`` and ``str`` show whether a value is set, a count or
    a flag, never a key or a URL.
    """

    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = field(default=None, repr=False)
    provider_switches: tuple[str, ...] = ()
    trusted_base_urls: tuple[str, ...] = field(default=(), repr=False)
    full_access_acknowledged: bool = False

    def __repr__(self) -> str:
        key, url = ("set" if value is not None else "none" for value in (self.api_key, self.base_url))
        switches, trusted = (len(value) if isinstance(value, (tuple, list, frozenset)) else "?"
                             for value in (self.provider_switches, self.trusted_base_urls))
        full = "yes" if self.full_access_acknowledged is True else "no"
        return (f"ChildRoute(api_key=<{key}>, base_url=<{url}>, provider_switches=<{switches}>, "
                f"trusted_base_urls=<{trusted}>, full_access_acknowledged=<{full}>)")


@dataclass(frozen=True)
class EngineProfile:
    """What one engine child may inherit, and what it must never see."""

    engine: str
    allowed: tuple[str, ...]
    hosts: tuple[str, ...]  # its own endpoints: a NO_PROXY entry matching one refuses while a proxy is set
    scrub: tuple[str, ...]  # credential and route selectors, asserted absent (they are not allowlisted)
    # The user's own sign-in names: copied from the parent unchanged when present, never checked, never set by
    # an override, absent when the parent lacks them (PRODUCT.md 055cc7a5 section 3.3, Nov 8 fleet correction).
    passthrough: tuple[str, ...] = ()
    # Route-owned names. Each is on ``scrub``, so the parent's value and an override never set it; the child
    # gets it only from the caller's ``ChildRoute``, in the mode that allows it. None or empty: the engine has
    # no such route here.
    key_name: str | None = None  # the user-entered vendor key, ``api_key`` mode only
    base_url_name: str | None = None  # the route's own base URL, any mode (``subscription``: see ``_route_env``)
    provider_switches: tuple[str, ...] = ()  # set to "1" when picked, ``cloud_provider`` mode only
    # Apex names. A NO_PROXY entry equal to the apex, or under it, bypasses every host in the family.
    host_suffixes: tuple[str, ...] = ()


_CLAUDE_SCRUB = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_OAUTH_TOKEN",
    "ANTHROPIC_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_SIMPLE",  # PRODUCT.md 055cc7a5 section 3.3 names it in the Claude scrub list
)
_CODEX_SCRUB = ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME")

ENGINE_PROFILES: Mapping[str, EngineProfile] = MappingProxyType(
    {
        # Hosts: Claude Code network-config.md:221-228; Codex 0.165.0 model-provider-info/src/lib.rs:82, :445,
        # login/src/server.rs:77 (as read in the envpath record, section 5).
        "claude_code": EngineProfile(
            "claude_code",
            BASE_NAMES + CLAUDE_TLS_NAMES,
            ("api.anthropic.com", "platform.claude.com", "claude.ai", "mcp-proxy.anthropic.com"),
            _CLAUDE_SCRUB,
            ("CLAUDE_CODE_OAUTH_TOKEN",),
            key_name="ANTHROPIC_API_KEY",
            base_url_name="ANTHROPIC_BASE_URL",
            provider_switches=("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"),
        ),
        "codex": EngineProfile(
            "codex",
            BASE_NAMES,
            ("chatgpt.com", "auth.openai.com", "api.openai.com"),
            _CODEX_SCRUB,
            key_name="CODEX_API_KEY",  # the variable ``codex exec`` reads; no base URL and no cloud switch here
        ),
        # Copilot CLI >= 1.0.1 reads COPILOT_HOME, else ~/.copilot, and does not read XDG_CONFIG_HOME.
        # Hosts are the Copilot service names a NO_PROXY entry must not exempt. OpenAI's hosts are not
        # among them: a Copilot child may exclude api.openai.com.
        "copilot": EngineProfile(
            "copilot",
            BASE_NAMES + ("COPILOT_HOME",),
            (
                "github.com",
                "api.github.com",
                "copilot-proxy.githubusercontent.com",
                "proxy.individual.githubcopilot.com",
                "api.individual.githubcopilot.com",
                "api.business.githubcopilot.com",
                "api.enterprise.githubcopilot.com",
            ),
            (
                "GITHUB_TOKEN",
                "GH_TOKEN",
                "GH_PAT",
                "GITHUB_COPILOT_API_TOKEN",
                "COPILOT_GITHUB_TOKEN",
                "COPILOT_API_URL",
            ),
            host_suffixes=("githubcopilot.com",),
        ),
        # OpenAI API key inside a ValidatedEnv, so spawn's executable checks run. Not the OpenCode harness name.
        "openai": EngineProfile(
            "openai",
            BASE_NAMES,
            ("api.openai.com", "chatgpt.com"),
            ("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"),
            key_name="OPENAI_API_KEY",
        ),
    }
)


class ValidatedEnv(Mapping[str, str]):
    """An environment ``build_child_env`` produced. Only this type is accepted by the launch boundary."""

    __slots__ = ("_env", "engine", "dropped", "auth_mode")
    _seal = object()

    def __init__(self, env: Mapping[str, str], engine: str, dropped: tuple[str, ...], *, auth_mode: str | None = None,
                 _seal: object = None) -> None:
        if _seal is not ValidatedEnv._seal:
            raise TypeError("ValidatedEnv is made only by build_child_env")
        self._env = dict(env)
        self.engine = engine
        self.auth_mode = auth_mode
        self.dropped = dropped  # parent names left out (names only, never values)

    def __getitem__(self, name: str) -> str:
        return self._env[name]

    def __iter__(self):
        return iter(self._env)

    def __len__(self) -> int:
        return len(self._env)

    def __repr__(self) -> str:  # names only: values may be paths an operator considers private
        return f"ValidatedEnv(engine={self.engine!r}, auth_mode={self.auth_mode!r}, names={sorted(self._env)!r})"

    def to_dict(self) -> dict[str, str]:
        return dict(self._env)


def build_child_env(
    engine: str,
    *,
    home: str,
    auth_mode: str | None = None,
    route: ChildRoute | None = None,
    parent: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> ValidatedEnv:
    """Return the environment for one engine child, or raise ``EnvRefused`` before any spawn.

    ``auth_mode`` is required: one of ``AUTH_MODES``. Omitted refuses with ``auth_mode_missing``, anything else
    with ``auth_mode_unknown``. ``route`` carries what the caller says about the route it launched (see
    ``ChildRoute``); the parent's vendor key, base URL and provider switches are dropped in every mode. On a
    ``subscription`` route a base URL that is not Anthropic's own, not in the route's trusted list and not under
    an acknowledged full-access run refuses with ``route_base_url_untrusted``.
    ``home`` is the route-owned private home the child gets as HOME; the parent's HOME is never passed.
    ``overrides`` are values the core itself sets. An override may only set a name already on this engine's
    allowlist (never HOME, PATH, a credential or a scrubbed name), and it is merged before the checks, so it
    meets exactly the value, PATH, NO_PROXY and temp rules an inherited value meets.
    """

    if os.name == "nt":
        raise EnvRefused(None, "platform_unsupported")
    profile = ENGINE_PROFILES.get(engine) if isinstance(engine, str) else None
    if profile is None:
        raise EnvRefused(None, "unknown_engine")
    if auth_mode is None:
        raise EnvRefused(None, "auth_mode_missing")
    if not isinstance(auth_mode, str) or auth_mode not in AUTH_MODES:
        raise EnvRefused(None, "auth_mode_unknown")
    route_env = _route_env(profile, auth_mode, route)  # refuses before anything is read from the parent
    if not _private_dir(home):
        raise EnvRefused("HOME", "home_unsafe")
    source = dict(os.environ if parent is None else parent)
    env = {name: source[name] for name in profile.allowed if name in source and name != "HOME"}
    passed = {name: source[name] for name in profile.passthrough if name in source}
    dropped = tuple(sorted(name for name in source if name not in env and name not in passed and name != "HOME"))

    # Overrides are merged first and then checked by exactly the rules below, like any inherited value.
    # They may only set a name this engine's allowlist already carries: never HOME, PATH or a credential.
    for name, value in (overrides or {}).items():
        if not isinstance(name, str) or not isinstance(value, str) or not name or "=" in name or "\0" in name + value:
            raise EnvRefused(None, "override_malformed")
        if (_DENIED.search(name) or name in _OVERRIDE_DENIED_NAMES or name in profile.scrub
                or name in profile.passthrough or name in ("HOME", "PATH")):
            raise EnvRefused(name, "override_denied")
        if name not in profile.allowed:
            raise EnvRefused(name, "override_not_allowed")
        if name in _CARRIED_ONLY_NAMES:
            raise EnvRefused(name, "override_denied")
        env[name] = value

    for name in [name for name in env if name not in ("PATH", *TEMP_NAMES, *NO_PROXY_NAMES)]:
        cause = _unsafe_value(name, env[name]) if env[name] else None  # empty: no path or URL to check
        if cause is not None:
            raise EnvRefused(name, cause)
    if "PATH" in env and (cause := _unsafe_path(env["PATH"])) is not None:
        raise EnvRefused("PATH", cause)
    # A bypass only matters when a proxy is set: "*" skips it for every host, an engine-host entry for that engine.
    if any(env.get(name) for name in VALUE_URLS + VALUE_ALL_PROXY):
        for name in [name for name in NO_PROXY_NAMES if name in env]:
            if (cause := _no_proxy_bypass(env[name], profile.hosts, profile.host_suffixes)) is not None:
                raise EnvRefused(name, cause)
    # N5h option A: a temp name is replaced by one private route temp root, never checked or passed.
    present = [name for name in TEMP_NAMES if name in env]
    if present:
        env.update(dict.fromkeys(present, _route_tmp()))
    env["HOME"] = home
    env.update(passed)  # after every check: the user's own sign-in value is passed as it is, never read here

    leaked = sorted(name for name in env if name in profile.scrub
                    or (name not in profile.allowed and name not in profile.passthrough and _DENIED.search(name)))
    if leaked:  # unreachable by construction; kept so a later edit to the allowlist fails closed
        raise EnvRefused(leaked[0], "scrub_failed")
    env.update(route_env)  # last: only what this call's route named, never a parent or override value
    return ValidatedEnv(env, engine, dropped, auth_mode=auth_mode, _seal=ValidatedEnv._seal)


def _route_env(profile: EngineProfile, auth_mode: str, route: ChildRoute | None) -> dict[str, str]:
    """The route-owned names for this call, or a refusal. A refusal names the variable, never a value."""

    if route is None:
        route = ChildRoute()
    if type(route) is not ChildRoute:
        raise EnvRefused(None, "route_malformed")
    switches = route.provider_switches
    if not isinstance(switches, (tuple, list, frozenset)) or not all(isinstance(name, str) for name in switches):
        raise EnvRefused(None, "route_malformed")
    # The two subscription base-URL inputs are shape-checked in every mode, so a malformed route never launches;
    # well-formed, they change nothing outside a subscription route that has a base URL.
    trusted = _trusted_origins(route.trusted_base_urls)
    if trusted is None:
        raise EnvRefused(None, "route_trusted_base_urls_malformed")
    if not isinstance(route.full_access_acknowledged, bool):  # "yes", 1 and the like are not an acknowledgment
        raise EnvRefused(None, "route_full_access_malformed")
    if auth_mode == "cloud_provider" and not profile.provider_switches:
        raise EnvRefused(None, "auth_mode_unsupported")  # this engine has no cloud-provider route here
    out: dict[str, str] = {}

    if route.api_key is not None:
        if auth_mode != "api_key":
            raise EnvRefused(profile.key_name, "route_key_not_allowed")
        if not isinstance(route.api_key, str) or not route.api_key or "\0" in route.api_key:
            raise EnvRefused(profile.key_name, "route_key_malformed")
    if auth_mode == "api_key":
        if profile.key_name is None:
            raise EnvRefused(None, "auth_mode_unsupported")
        if route.api_key is None:  # without a key the child would fall back to a sign-in: a different billing class
            raise EnvRefused(profile.key_name, "route_key_missing")
        out[profile.key_name] = route.api_key

    if auth_mode == "cloud_provider":
        if not switches:
            raise EnvRefused(None, "route_provider_missing")
        for name in switches:
            if name not in profile.provider_switches:
                raise EnvRefused(None, "route_provider_unknown")
            out[name] = "1"
    elif switches:
        raise EnvRefused(None, "route_provider_not_allowed")

    if route.base_url is not None:
        if profile.base_url_name is None:
            raise EnvRefused(None, "route_base_url_unsupported")
        if (cause := _unsafe_base_url(route.base_url)) is not None:
            raise EnvRefused(profile.base_url_name, cause)
        # A subscription child carries the user's own CLAUDE_CODE_OAUTH_TOKEN (passed through above), and the
        # engine sends it to this base. So the base must be Anthropic's own origin, an origin the user already
        # trusted (the driver asks once and remembers; nothing is stored or asked here), or the run must use the
        # named full-access profile the user acknowledged for this run (owner call; CSLR-D-011; PRODUCT.md
        # 055cc7a5 Q20, O44, H-06). Origins are compared whole, never by prefix or substring. An api_key or
        # cloud_provider route is not checked: its base URL behaves as before.
        if auth_mode == "subscription" and route.full_access_acknowledged is not True:
            origin = _origin(route.base_url)
            if origin != _OFFICIAL_ORIGIN and origin not in trusted:
                raise EnvRefused(profile.base_url_name, "route_base_url_untrusted")
        out[profile.base_url_name] = route.base_url
    return out


def _origin(value: str) -> tuple[str, str, int]:
    """(scheme, host, port) of a URL that passed ``_unsafe_base_url``: what two base URLs share when one endpoint.

    Scheme and host are lowercased and a missing port is the scheme's own (443 for https, 80 for http); the path
    is not part of it. Nothing else is folded: a host with a trailing dot, and another spelling of the same IP
    address, are different origins, so they are never the official one and need their own trusted entry.
    """

    match = _BASE_URL.match(value)
    scheme = match["scheme"].lower()
    return scheme, match["host"].lower(), int(match["port"]) if match["port"] is not None else _DEFAULT_PORTS[scheme]


def _trusted_origins(value: object) -> frozenset[tuple[str, str, int]] | None:
    """The route's trusted list as origins, or None when it is not a tuple or frozenset of ``scheme://host[:port]``.

    An entry meets the base URL rule and names an origin only: a path beyond ``/`` refuses, since trust is given
    to an endpoint, not to one path on it. One bad entry refuses the whole list.
    """

    if not isinstance(value, (tuple, frozenset)):
        return None
    origins = set()
    for entry in value:
        if _unsafe_base_url(entry) is not None or _BASE_URL.match(entry)["path"] not in (None, "/"):
            return None
        origins.add(_origin(entry))
    return frozenset(origins)


def _bad_bracket_host(host: str) -> bool:
    """A bracketed host that is not an IPv6 address (``[:::]``, ``[1:2:3]``): no engine could reach it.

    The patterns only limit the characters between the brackets, so the address is parsed here
    (Devin, "Invalid IPv6 gateway reaches child"). A zone id never gets this far: the patterns have no ``%``.
    """

    if not host.startswith("["):
        return False
    try:
        ipaddress.IPv6Address(host[1:-1])
    except ValueError:
        return True
    return False


def _unsafe_base_url(value: object) -> str | None:
    """A route base URL is http(s)://host[:port][/path]: no user name or password, no query, no fragment."""

    if not isinstance(value, str):
        return "base_url_malformed"
    match = _BASE_URL.match(value)
    if match is None or match["scheme"].lower() not in ("http", "https"):
        return "base_url_malformed"
    if match["userinfo"] is not None:
        return "base_url_userinfo"
    if _bad_bracket_host(match["host"]):
        return "base_url_malformed"
    if match["port"] is not None and not 0 < int(match["port"]) < 65536:
        return "base_url_malformed"
    if ".." in (match["path"] or "").split("/"):
        return "base_url_malformed"
    return None


def _unsafe_value(name: str, value: str) -> str | None:
    if "\0" in value:
        return "value_malformed"
    if name in VALUE_URLS:
        return _unsafe_proxy(value)
    if name in VALUE_ALL_PROXY:
        return _unsafe_proxy(value, _ALL_PROXY_SCHEMES)
    if name == "CLAUDE_CODE_CERT_STORE":
        sources = [part.strip() for part in value.split(",")]
        return None if all(part in _CERT_STORE_SOURCES for part in sources) else "cert_store_malformed"
    if name in VALUE_DIR_SINGLE:
        return _unsafe_dir(value)
    if name == "TZ":
        path = value[1:] if value.startswith(":") else value
        if path.startswith("/"):
            return _unsafe_file(path)
        return "tz_relative_path" if ".." in path.split("/") else None
    if name == "COPILOT_HOME":
        # Same rule as HOME: the directory itself is not a symlink, and it is private to this user.
        return None if _private_dir(value) else "home_unsafe"
    if name in VALUE_FILES:
        return _unsafe_file(value)
    if name in VALUE_DIRS:
        # OpenSSL skips an empty SSL_CERT_DIR entry (C2-37: "/etc/ssl/certs:" and ":" read no cwd).
        entries = [entry for entry in value.split(os.pathsep) if entry]
        return next((cause for cause in map(_unsafe_dir, entries) if cause), None)
    return None


def _resolve(path: str) -> tuple[str | None, bool, str | None]:
    """Resolve ``path`` one component at a time: (real path, leaf missing, cause).

    Every link met, including links inside another link's target, must be root's or ours and sit in a
    directory whose own chain no one else can write; so must every directory a ``..`` steps back out of,
    in the value or in a link's target; no step may enter a perspective path. A missing
    component ends the walk: the rest cannot be a link yet, and the caller decides whether that is safe.
    """

    if not os.path.isabs(path):
        return None, False, "value_relative"
    if _PERSPECTIVE.match(path):
        return None, False, "value_perspective"
    euid, hops = os.geteuid(), 0
    pending = list(reversed(Path(path).parts[1:]))
    current = "/"
    while pending:
        part = pending.pop()
        if part in ("", "."):
            continue
        if part == "..":
            # The directory ".." leaves drops out of the result, so the final chain check never sees it; the
            # child still walks it. Where someone else could swap it for a link, the value is theirs to redirect.
            if current != "/" and not _owned_chain(current):
                return None, False, "value_chain_unsafe"
            current = os.path.dirname(current)
            continue
        candidate = os.path.join(current, part)
        if _PERSPECTIVE.match(candidate):
            return None, False, "value_perspective"
        try:
            st = os.lstat(candidate)
        except FileNotFoundError:
            rest = [candidate] + [p for p in reversed(pending)]
            return os.path.join(*rest), True, None
        except OSError:
            return None, False, "value_unreadable"
        if not stat.S_ISLNK(st.st_mode):
            current = candidate
            continue
        hops += 1
        if hops > _MAX_LINKS:
            return None, False, "value_link_loop"
        if st.st_uid not in (0, euid) or not _owned_chain(current):
            return None, False, "value_chain_unsafe"
        target = os.readlink(candidate)
        if os.path.isabs(target):
            if _PERSPECTIVE.match(target):
                return None, False, "value_perspective"
            current = "/"
        pending.extend(reversed(Path(target).parts[1:] if os.path.isabs(target) else Path(target).parts))
    return current, False, None


def _unsafe_file(path: str) -> str | None:
    real, missing, cause = _resolve(path)
    if cause is not None:
        return cause
    if missing:  # harmless only where no one else can create it
        return None if _owned_chain(_existing_ancestor(real)) else "value_missing"
    st = os.stat(real)
    if not stat.S_ISREG(st.st_mode) or st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o022:
        return "value_file_unsafe"
    return None if _owned_chain(os.path.dirname(real)) else "value_chain_unsafe"


def _unsafe_dir(path: str) -> str | None:
    """A directory value: no sticky exception (a shared temp dir is never a cert or PATH dir, C2-37 row 5a)."""

    real, missing, cause = _resolve(path)
    if cause is not None:
        return cause
    if missing:
        return None if _owned_chain(_existing_ancestor(real)) else "value_missing"
    if not stat.S_ISDIR(os.stat(real).st_mode):
        return "value_dir_unsafe"
    return None if _owned_chain(real) else "value_chain_unsafe"


def _unsafe_path(value: str) -> str | None:
    """N5i: each PATH entry is a directory check, then each engine tool name a file check on that PATH."""

    if "\0" in value:
        return "value_malformed"
    entries = value.split(os.pathsep)
    for entry in entries:
        if not entry:
            return "path_empty_entry"  # an empty entry is the child's cwd for execvp
        cause = _unsafe_dir(entry)
        if cause is not None:
            return cause
    names = PATH_TOOLS
    if names is None:
        names = sorted({item for entry in entries if os.path.isdir(entry) for item in os.listdir(entry)})
    for name in names:
        located = shutil.which(name, path=value)
        if located is not None and (cause := _unsafe_file(located)) is not None:
            return "path_tool_" + cause
    return None


def _no_proxy_bypass(value: str, hosts: tuple[str, ...], suffixes: tuple[str, ...] = ()) -> str | None:
    """N5i: comma and whitespace split; the union of curl, requests, hyper-util, undici and Codex matchers.

    ``suffixes`` are apexes such as ``githubcopilot.com``. An entry on that apex, or any name under
    it, bypasses the whole family. A wildcard that can match the apex or any name under it refuses,
    not only a pattern that matches the apex or ``api.<apex>``.
    """

    for entry in re.split(r"[,\s]+", value.strip().lower()):
        entry = re.sub(r"^[a-z][a-z0-9+.-]*://", "", entry).strip("[]")
        entry = re.sub(r":[0-9]*$", "", entry).rstrip(".")
        if not entry:
            continue
        if entry.strip("*.") == "":
            return "no_proxy_wildcard"
        if _proxy_entry_hits(entry, hosts, suffixes):
            return "no_proxy_engine_host"
    return None


def _proxy_entry_hits(entry: str, hosts: tuple[str, ...], suffixes: tuple[str, ...]) -> bool:
    if "*" in entry:
        pattern = re.compile(".*".join(re.escape(part) for part in entry.split("*")) + r"\Z")
        if any(pattern.match(host) for host in hosts):
            return True
        return any(_wildcard_intersects_suffix(entry, pattern, suffix) for suffix in suffixes)
    bare = entry.lstrip(".")
    if any(host == bare or host.endswith("." + bare) for host in hosts):
        return True
    return any(bare == suffix or bare.endswith("." + suffix) for suffix in suffixes)


def _unsafe_proxy(value: str, schemes: tuple[str, ...] = ("http", "https")) -> str | None:
    """A proxy URL is [scheme://]host[:port][/]: no user name or password, no path."""

    match = _PROXY_URL.match(value)
    if match is None or match["scheme"] is not None and match["scheme"].lower() not in schemes:
        return "proxy_malformed"
    if match["userinfo"] is not None:
        return "proxy_userinfo"
    if _bad_bracket_host(match["host"]):
        return "proxy_malformed"
    if match["port"] is not None and not 0 < int(match["port"]) < 65536:
        return "proxy_malformed"
    return None


def _owned_chain(path: str) -> bool:
    """Every directory from / to ``path`` is root's or ours, and no one else can rename what is in it.

    A root-owned sticky directory above it (``/tmp``) may be world-writable: there only an entry's owner can
    rename the entry (OpenSSH ``safe_path``). Same rule as the Nov 5 tree's ``transport._owned_chain``.
    """

    euid, parts = os.geteuid(), Path(path).parts
    try:
        for i in range(1, len(parts) + 1):
            st = os.lstat(os.path.join(*parts[:i]))
            if not stat.S_ISDIR(st.st_mode) or st.st_uid not in (0, euid):
                return False
            if st.st_mode & 0o022 and not (i < len(parts) and st.st_uid == 0 and st.st_mode & stat.S_ISVTX):
                return False
    except OSError:
        return False
    return True


def _existing_ancestor(path: str) -> str:
    while not os.path.lexists(path):
        path = os.path.dirname(path)
    return path


def _lexical_dir(path: str) -> str:
    """The directory entry ``lstat`` must see: trailing slashes and ``.`` removed, ``..`` kept.

    ``lstat`` follows a final symlink when the path ends in a slash or a ``.`` component
    (Devin r4508023438), so ``link`` and ``link/`` have to name the same entry. ``Path``
    does that lexically and does not collapse ``..``, which would drop an intermediate symlink.
    A path that still ends in ``..`` names no entry of its own; ``_private_dir`` refuses it.
    """

    return str(Path(path))


def _wildcard_intersects_suffix(entry: str, pattern: re.Pattern[str], suffix: str) -> bool:
    """True when the glob can match ``suffix`` or some name under it.

    ``*`` is any string, including dots, the same as the host match above. A trailing ``*`` can
    always be completed to ``.<suffix>``. Otherwise every match ends with the last literal, so
    that literal and ``.<suffix>`` have to be the same ending.
    """

    if pattern.match(suffix):
        return True
    last = entry.split("*")[-1]
    if last == "":
        return True
    tail = "." + suffix
    return last.endswith(tail) or tail.endswith(last)


def _private_dir(path: str | None) -> bool:
    # NUL reaches ``lstat`` as ValueError. A lone surrogate fails while the path is encoded.
    # Both are an unsafe home, not an exception for the caller to see.
    try:
        return _private_dir_is(path)
    except (ValueError, UnicodeError):
        return False


def _private_dir_is(path: str | None) -> bool:
    if not isinstance(path, str) or not os.path.isabs(path):
        return False
    entry = _lexical_dir(path)  # the home itself may not be a link, whatever the trailing spelling
    if os.path.basename(entry) == "..":  # link/sub/.. names the link's target without naming an entry: refused
        return False
    real, missing, cause = _resolve(entry)
    if cause is not None or missing or real is None:
        return False
    try:
        if not stat.S_ISDIR(os.lstat(entry).st_mode):
            return False
        st = os.lstat(real)
    except OSError:
        return False
    # The home is ours and 0700. Its parents follow the chain rule on the resolved path, so an accepted link
    # above it (macOS /var -> private/var) is fine, and a root sticky /tmp above it is fine.
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid() and not st.st_mode & 0o077 and _owned_chain(real)


_TMP: dict[int, str] = {}
_TMP_LOCK = threading.Lock()


def _tmp_bases() -> list[str]:
    """Where a route temp root may be made: the process temp dir, then /tmp. Each is checked before use."""

    return [tempfile.gettempdir(), "/tmp"]


def _make_route_tmp() -> str:
    # The daemon's own TMPDIR may name a directory someone else can write (Devin r4503135904). Make the root,
    # then keep it only if its whole resolved chain passes the owned-chain rule; otherwise remove it and try
    # the next base. No safe base refuses rather than handing children a root another user could swap.
    for base in _tmp_bases():
        try:
            path = tempfile.mkdtemp(prefix="recursion-child-tmp-", dir=os.path.realpath(base))
        except OSError:
            continue
        if _owned_chain(path):
            return path
        try:
            os.rmdir(path)
        except OSError:
            pass
    raise EnvRefused("TMPDIR", "tmp_root_unsafe")


def _route_tmp() -> str:
    """One private temp root per process for engine children: a fresh random 0700 name, remade if it changed."""

    key = os.getpid()
    with _TMP_LOCK:
        path = _TMP.get(key)
        try:
            st = os.lstat(path) if path else None
        except OSError:
            st = None
        if (st is None or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077
                or not _owned_chain(path)):
            path = _TMP[key] = _make_route_tmp()
        return path
