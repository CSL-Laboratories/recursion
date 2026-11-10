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

POSIX only. On Windows (``os.name == "nt"``) every call refuses
(``platform_unsupported``) instead of passing values unchecked. The ownership,
symlink-owner and ``0o077`` mode checks run only where ``os.geteuid`` exists.
Where it does not, those helpers refuse (``owner_check_unsupported``, or a false
owned-chain / private-dir result): they never return True and never invent a uid.
A Windows SID/DACL backend is future work.

Candidate code: no engine, provider or network is used, and nothing here claims the
new core's runtime calls it yet.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import stat
import unicodedata
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

    # Windows has no euid and no Unix mode bits these rules read, so the call
    # refuses instead of passing values unchecked. A SID/DACL backend is future
    # work. Helpers that would call os.geteuid refuse on their own if it is missing;
    # they do not run on this path.
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
    # A bypass only matters when a proxy is set. "*" skips every host. An entry that matches an engine
    # endpoint, or the route's own base host, skips the proxy for that host; a CIDR matches an IP it contains.
    if any(env.get(name) for name in VALUE_URLS + VALUE_ALL_PROXY):
        hosts = profile.hosts
        base = route_env.get(profile.base_url_name) if profile.base_url_name else None
        if base is not None:
            hosts = hosts + (_origin(base)[1],)
        for name in [name for name in NO_PROXY_NAMES if name in env]:
            if (cause := _no_proxy_bypass(env[name], hosts)) is not None:
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


def _canonical_ipv4(host: str) -> bool:
    """Four decimal octets, 0-255, no leading zero (``10.1.2.3``, ``0.0.0.0``)."""

    parts = host.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        # Longer than three digits is not an octet. Do not call int() on it: a
        # thousands-digit string raises ValueError at the conversion limit.
        if not part.isdigit() or len(part) > 3 or (len(part) > 1 and part[0] == "0") or int(part) > 255:
            return False
    return True


def _noncanonical_numeric_ipv4(host: str) -> bool:
    """A numeric IPv4 spelling that is not four decimal octets.

    ``socket.inet_aton`` accepts hex (``0x0a010203``), octal (``010.1.2.3``), a single
    integer (``2130706433``) and a short form (``10.1``, ``10.0``). A host that is only
    digits and dots, or a ``0x`` form, and is not canonical, is the same kind of address.
    A name (``gateway.invalid``) is not.
    """

    if _canonical_ipv4(host):
        return False
    if re.fullmatch(r"[0-9.]+", host) or re.fullmatch(r"0x[0-9a-f.]+", host):
        return True
    try:
        socket.inet_aton(host)
    except OSError:
        return False
    return True


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
    # Hex, octal, a single integer and a short dotted form (``10.1``, ``0x0a010203``)
    # are not the four-decimal address some clients parse them into. Refuse them
    # here so a NO_PROXY CIDR for the canonical address cannot be skipped.
    if not match["host"].startswith("[") and _noncanonical_numeric_ipv4(match["host"]):
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
    if name in VALUE_FILES:
        return _unsafe_file(value)
    if name in VALUE_DIRS:
        # OpenSSL skips an empty SSL_CERT_DIR entry (C2-37: "/etc/ssl/certs:" and ":" read no cwd).
        entries = [entry for entry in value.split(os.pathsep) if entry]
        return next((cause for cause in map(_unsafe_dir, entries) if cause), None)
    return None


def _posix_euid() -> int | None:
    """Effective uid where the POSIX owner and mode checks can run, else None.

    Windows has no ``os.geteuid``. None is not a pass and not a stand-in for uid 0.
    Callers refuse the path (``owner_check_unsupported``, or a false owned-chain /
    private-dir result) instead of accepting one the uid, symlink-owner or ``0o077``
    checks would refuse. A Windows SID/DACL backend is future work.
    """

    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return None
    return geteuid()


def _resolve(path: str) -> tuple[str | None, bool, str | None]:
    """Resolve ``path`` one component at a time: (real path, leaf missing, cause).

    Every link met, including links inside another link's target, must be root's or ours and sit in a
    directory whose own chain no one else can write; so must every directory a ``..`` steps back out of,
    in the value or in a link's target; no step may enter a perspective path. A missing
    component ends the walk: the rest cannot be a link yet, and the caller decides whether that is safe.

    Without ``os.geteuid`` the symlink-owner check cannot be done, so the walk does not start and the
    path is refused (``owner_check_unsupported``). That does not accept a link the POSIX rule would refuse.
    """

    if not os.path.isabs(path):
        return None, False, "value_relative"
    if _PERSPECTIVE.match(path):
        return None, False, "value_perspective"
    euid = _posix_euid()
    if euid is None:
        return None, False, "owner_check_unsupported"
    hops = 0
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
    euid = _posix_euid()
    if euid is None:
        return "owner_check_unsupported"  # do not read 0o022 as a pass when the mode bits are not Unix
    if not stat.S_ISREG(st.st_mode) or st.st_uid not in (0, euid) or st.st_mode & 0o022:
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


def _no_proxy_host(host: str) -> str:
    """The host a NO_PROXY entry is compared with: lower case, no brackets, no trailing dot."""

    host = host.lower()
    if len(host) >= 2 and host[0] == "[" and host[-1] == "]":
        host = host[1:-1]
    return host.rstrip(".")


def _no_proxy_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _host_addrs(host: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    """The host as an IP, plus the IPv4 address inside an IPv4-mapped IPv6 host.

    ``[::ffff:10.1.2.3]`` is also ``10.1.2.3``, so an IPv4 entry or CIDR can exempt it.
    """

    address = _no_proxy_ip(host)
    if address is None:
        return ()
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return (address, address.ipv4_mapped)
    return (address,)


# requests int() skips this leading whitespace and an optional '+'. curl's strtoul does too,
# and then keeps the leading digits when junk follows (``8x`` is 8). A longer digit run is refused:
# converting it would raise ValueError at the integer limit.
_PREFIX_SPACE = frozenset(" \t\n\r\v\f")


def _prefix_bits(text: str) -> tuple[str, int | None]:
    """``('ok', bits)``, ``('no', None)`` when this is not a prefix, or ``('bad', None)``.

    ``bad`` is a prefix that cannot be judged without raising: a NUL, a thousands-digit
    integer, or no digits where ``int()`` would raise (``²``).
    """

    if "\0" in text:
        return "bad", None
    i = 0
    while i < len(text) and text[i] in _PREFIX_SPACE:
        i += 1
    if i < len(text) and text[i] == "+":
        i += 1
    start = i
    # ASCII digits only. ``²``.isdigit() is true, and int() then raises.
    while i < len(text) and text[i] in "0123456789":
        i += 1
        if i - start > 4:
            return "bad", None
    if i == start:
        if text[i:].strip("".join(_PREFIX_SPACE)):
            return "bad", None
        return "no", None
    bits = int(text[start:i])
    if bits > 128:
        return "no", None
    return "ok", bits


def _permissive_cidr(entry: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | str | None:
    """A CIDR ``ipaddress`` rejected, read the way requests or curl would.

    ``10.0/+8`` and ``10.0.0.0/\\t8`` are ``/8`` (requests). ``10.0.0.0/8x`` is ``/8`` (curl).
    Returns ``'bad'`` when the prefix cannot be read without raising.
    """

    if entry.count("/") != 1:
        return None
    addr, bits_s = entry.split("/", 1)
    if not addr:
        return None
    kind, bits = _prefix_bits(bits_s)
    if kind == "bad":
        return "bad"
    if kind != "ok" or bits is None:
        return None
    if bits <= 32:
        try:
            packed = socket.inet_aton(addr)
        except (OSError, ValueError):
            packed = None
        if packed is not None:
            try:
                return ipaddress.IPv4Network((ipaddress.IPv4Address(packed), bits), strict=False)
            except ValueError:
                return "bad"
    try:
        address = ipaddress.ip_address(addr)
    except ValueError:
        return None
    limit = 32 if address.version == 4 else 128
    if bits > limit:
        return None
    try:
        if address.version == 4:
            return ipaddress.IPv4Network((address, bits), strict=False)
        return ipaddress.IPv6Network((address, bits), strict=False)
    except ValueError:
        return "bad"


def _no_proxy_network(entry: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | str | None:
    """A CIDR entry, None, or ``'bad'`` when reading it would raise.

    Host bits may be set (``10.1.2.3/8``); the prefix is what is matched. A prefix length of 0
    covers that whole address family (``0.0.0.0/0``, ``::/0``). The bare entry ``/0`` is not a
    network; the caller treats it as every IP. ``ipaddress`` runs first. What it rejects is read
    again with the requests/curl prefix rules, including a shorthand address (``10.0/8``).
    """

    if "/" not in entry or entry == "/0":
        return None
    if "\0" in entry:
        return "bad"
    try:
        return ipaddress.ip_network(entry, strict=False)
    except ValueError:
        return _permissive_cidr(entry)


def _ipv4_view_of_mapped(network: ipaddress.IPv6Network) -> ipaddress.IPv4Network | None:
    """Go's ``IPNet.Contains``: an IPv4-mapped network is compared as IPv4.

    ``::ffff:10.0.0.0/104`` is ``10.0.0.0/8``. ``::ffff:0:0/96`` is ``0.0.0.0/0``.
    A network that does not keep the mapped prefix (``::/0``) is not an IPv4 network.
    """

    mapped = network.network_address.ipv4_mapped
    if mapped is None or network.prefixlen < 96:
        return None
    try:
        return ipaddress.IPv4Network((mapped, network.prefixlen - 96), strict=False)
    except ValueError:
        return None


def _network_covers(network: ipaddress.IPv4Network | ipaddress.IPv6Network, hosts: tuple[str, ...]) -> bool:
    v4_view = _ipv4_view_of_mapped(network) if isinstance(network, ipaddress.IPv6Network) else None
    for host in hosts:
        for address in _host_addrs(host):
            if address.version == network.version and address in network:
                return True
            if v4_view is not None and isinstance(address, ipaddress.IPv4Address) and address in v4_view:
                return True
    return False


def _no_proxy_entries(value: str) -> tuple[str, ...]:
    """Every entry either reading would see. A hit in either refuses.

    Commas and whitespace split the curl / hyper-util reading. requests <=2.32.3
    deletes spaces first (``replace(' ', '').split(',')``), so ``gate way.invalid``
    is the one entry ``gateway.invalid``.
    """

    lowered = value.strip().lower()
    spaced = [part for part in re.split(r"[,\s]+", lowered) if part]
    packed = [part for part in lowered.replace(" ", "").split(",") if part]
    seen: list[str] = []
    for part in spaced + packed:
        if part not in seen:
            seen.append(part)
    return tuple(seen)


def _idna2008_name(body: str) -> str:
    """IDNA2008 / UTS46 nontransitional, per label, the way Go's proxy encodes a name.

    ``ß`` stays ``ß`` (``xn--fa-hia``), not ``ss``. ``ẞ`` lowercases to ``ß``, so it is the
    same name. Final sigma ``ς`` stays distinct from ``σ``. This is the Go 1.22
    ``ProxyFromEnvironment`` reading. IDNA2003 (``encode('idna')``) is kept as well.
    """

    labels = []
    for label in body.lower().split("."):
        if not label:
            raise UnicodeError("empty label")
        text = unicodedata.normalize("NFC", label)
        if text.isascii():
            labels.append(text)
        else:
            labels.append("xn--" + text.encode("punycode").decode("ascii"))
    return ".".join(labels)


def _idna_forms(label: str) -> set[str] | None:
    """ASCII, IDNA2003, and IDNA2008 spellings of one host or entry.

    A single leading dot is suffix syntax and is not an empty label. None means a
    non-ASCII label will not encode under IDNA2003. The caller refuses: while a proxy
    is set, an entry that cannot be compared must not be kept.
    """

    raw = label.rstrip(".")
    suffix = raw.startswith(".")
    body = raw[1:] if suffix else raw
    if not body:
        return {raw} if raw else set()
    forms: set[str] = set()
    nonascii = any(ord(ch) > 127 for ch in body)
    try:
        forms.add(body.lower().encode("idna").decode("ascii"))
    except UnicodeError:
        if nonascii:
            return None
    try:
        forms.add(_idna2008_name(body))
    except UnicodeError:
        pass
    forms.add(body.lower())
    if suffix:
        forms |= {"." + form for form in list(forms)}
    return {form for form in forms if form}


def _exact_name(entry: str, host: str) -> bool:
    """True when two names are the same host under either IDNA reading."""

    entry_forms = _idna_forms(entry)
    host_forms = _idna_forms(host)
    if entry_forms is None or host_forms is None:
        return True
    left = {form.lstrip(".") for form in entry_forms if form.lstrip(".")}
    right = {form.lstrip(".") for form in host_forms if form.lstrip(".")}
    return bool(left & right)


def _star_match(pattern: str, text: str) -> bool:
    """Match a ``*``-only pattern in one left-to-right pass.

    Consecutive stars are one star. ``*`` matches across dots (``gate*invalid`` matches
    ``gateway.invalid``). A leading ``*.`` does not by itself match the apex; the caller
    adds that comparison.
    """

    parts = pattern.split("*")
    if not text.startswith(parts[0]):
        return False
    pos = len(parts[0])
    for part in parts[1:-1]:
        if not part:
            continue
        found = text.find(part, pos)
        if found < 0:
            return False
        pos = found + len(part)
    last = parts[-1]
    if not last:
        return True
    start = len(text) - len(last)
    return start >= pos and text.endswith(last)


def _normalize_no_proxy_entry(entry: str) -> str:
    """Scheme already removed. Strip a port from a name or an IPv4 address, and keep every IPv6 hextet.

    ``api.anthropic.com:443`` and ``10.1.2.3:80`` lose their port. A bare or bracketed IPv6 address does not:
    the old ``:[0-9]*$`` strip turned ``::1`` into ``:``.
    """

    bracket = re.fullmatch(r"\[([^\]]*)\](?::[0-9]*)?", entry)
    if bracket:
        return bracket.group(1).rstrip(".")
    if entry.count(":") > 1:
        return entry.rstrip(".")
    return re.sub(r":[0-9]*$", "", entry.strip("[]")).rstrip(".")


def _names_match(entry: str, host: str) -> bool:
    """Host match, including a plain suffix with no dot boundary.

    requests <=2.32.3 uses ``hostname.endswith(entry)`` (and the same with the port
    still attached). ``ateway.invalid`` therefore exempts ``gateway.invalid``. This
    check follows that reading, because a child may still run that requests. A dot
    boundary (``host == bare`` or ``host.endswith('.' + bare)``) is included in it.
    IDNA2003 and IDNA2008 spellings are compared both ways (``bücher.invalid`` and
    ``xn--bcher-kva.invalid``; ``faß.invalid`` and ``xn--fa-hia.invalid``).
    """

    entry_forms = _idna_forms(entry)
    host_forms = _idna_forms(host)
    if entry_forms is None or host_forms is None:
        return True  # cannot compare a non-ASCII label: refuse
    for bare in entry_forms:
        bare = bare.lstrip(".")
        if not bare:
            continue
        address = _no_proxy_ip(bare)
        mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
        for form in host_forms:
            if form == bare or form.endswith(bare):
                return True
            for host_ip in _host_addrs(form):
                if address is not None and host_ip == address:
                    return True
                if mapped is not None and host_ip == mapped:
                    return True
    return False


def _no_proxy_bypass(value: str, hosts: tuple[str, ...]) -> str | None:
    """N5i: the union of curl, requests, hyper-util, undici, Go and Codex matchers.

    ``hosts`` is the engine's own endpoints plus the route base host, when the route has one. A CIDR entry
    matches an IP host it contains, including a requests shorthand (``10.0/8``), a permissive prefix
    (``+8``, a tab, ``8x``), an IPv4-mapped route host, and an IPv4-mapped CIDR compared as IPv4
    (Go: ``::ffff:10.0.0.0/104``). It does not match a domain name: none of those matchers resolve DNS.
    ``/0`` covers every IP of both families. A leading ``*.`` also matches the apex (Undici 7.30.1).
    IDNA2003 and IDNA2008 spellings are compared. A plain suffix matches without a dot boundary, which is
    what requests <=2.32.3 does. A NUL is ``value_malformed``. A CIDR prefix that cannot be read without
    raising is ``no_proxy_malformed``.
    """

    if "\0" in value:
        return "value_malformed"
    checked = tuple(_no_proxy_host(host) for host in hosts)
    for raw in _no_proxy_entries(value):
        entry = re.sub(r"^[a-z][a-z0-9+.-]*://", "", raw)
        if not entry:
            continue
        if "\0" in entry:
            return "value_malformed"
        if entry == "/0":
            if any(_host_addrs(host) for host in checked):
                return "no_proxy_engine_host"
            continue
        network = _no_proxy_network(entry)
        if network == "bad":
            return "no_proxy_malformed"
        if network is not None:
            if network.prefixlen == 0 and any(
                address.version == network.version for host in checked for address in _host_addrs(host)
            ):
                return "no_proxy_engine_host"
            if _network_covers(network, checked):
                return "no_proxy_engine_host"
            continue
        entry = _normalize_no_proxy_entry(entry)
        if not entry:
            continue
        if _idna_forms(entry) is None or any(_idna_forms(host) is None for host in checked):
            return "no_proxy_engine_host"
        if entry.strip("*.") == "":
            return "no_proxy_wildcard"
        if "*" in entry:
            # ``*`` matches across dots (``gate*invalid`` matches ``gateway.invalid``).
            # Undici 7.30.1 also treats a leading ``*.`` as the apex itself.
            forms: list[str] = []
            for host in checked:
                host_forms = _idna_forms(host)
                forms.extend(host_forms or ())
            hit = any(_star_match(entry, host) for host in forms)
            apex = entry[2:] if entry.startswith("*.") else ""
            if apex and "*" not in apex:
                hit = hit or any(_exact_name(apex, host) for host in checked)
        else:
            hit = any(_names_match(entry, host) for host in checked)
        if hit:
            return "no_proxy_engine_host"
    return None


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

    POSIX only. Where ``os.geteuid`` is missing, the uid and ``0o022`` bits this rule reads are not
    available, so the chain is not accepted. False here is a refusal, not a pass: a path the POSIX
    rule would refuse must not be accepted because the check could not be run.
    """

    euid = _posix_euid()
    if euid is None:
        return False
    parts = Path(path).parts
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


def _private_dir(path: str | None) -> bool:
    # No euid: do not treat a directory as private, and do not read 0o077 as a pass.
    if _posix_euid() is None:
        return False
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
    euid = _posix_euid()
    if euid is None:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == euid and not st.st_mode & 0o077 and _owned_chain(real)


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
        euid = _posix_euid()
        # No euid: do not read 0o077 as a pass. The replacement below refuses when no base is owned.
        if (euid is None or st is None or not stat.S_ISDIR(st.st_mode) or st.st_uid != euid or st.st_mode & 0o077
                or not _owned_chain(path)):
            path = _TMP[key] = _make_route_tmp()
        return path
