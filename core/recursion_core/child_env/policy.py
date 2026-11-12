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
* while a proxy is set, a ``NO_PROXY`` entry that matches the route's own base URL host refuses
  (``no_proxy_route_host``, CSL-839; PRODUCT.md 055cc7a5 / CSLR-D-011): the child would reach the route gateway
  around the proxy. A loopback route host (``localhost``, ``127.0.0.0/8``, ``::1``) is exempt. ``NO_PROXY``
  and ``no_proxy`` are read as one combined list. CIDR entries that cover every IPv4 address, alone or as the
  union of that list's blocks (``0.0.0.0/0``, ``0.0.0.0/1,128.0.0.0/1``, or in IPv6 all of ``::ffff:0:0/96``,
  ``64:ff9b::/96`` or ``::/96``), count as ``*`` on any route (``no_proxy_wildcard``). An entry with a ``/``
  that is no CIDR block with a plain decimal prefix length, with a second ``/``, or with a ``%``, refuses on
  any route (``no_proxy_unparseable``). No ``NO_PROXY`` refusal shows any text of an entry: it names the
  variable, the entry's position in it and the reason. Each entry is also read the looser ways real client
  matchers read it (Devin rt343 MEDIUM-2; PRODUCT.md d84cdd26 / CSLR-D-012, section 3.4 row S8 and section 11.5
  on the child environment and N5i's ``NO_PROXY`` rule), and any one of those readings refuses: ``*.host`` is
  ``host`` too (undici), U+FEFF is whitespace (undici, Node), spaces inside an entry are deleted (requests),
  ``a-b`` is an IPv4 range (Node), and names are folded as IDNA would (Go). An entry that cannot be folded
  with confidence refuses as ``no_proxy_unparseable`` (``_no_proxy_readings``);
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
import unicodedata
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

    ``name`` is the variable (never its value) and ``cause`` a stable reason code. ``detail`` and ``position``
    are None except for the four NO_PROXY refusals (``no_proxy_wildcard``, ``no_proxy_engine_host``,
    ``no_proxy_route_host``, ``no_proxy_unparseable``). There ``position`` is the refusing entry's place in that
    variable, counted from 1, and ``detail`` is one line that gives the variable, that position and the reason
    (``entry 2 of NO_PROXY matches the route's base URL host; the entry is not shown``); it follows the cause in
    ``str()``. No text of an entry is ever shown, in whole or in part, by any of them (owner call, 2026-11-10):
    an entry can carry userinfo, a comma splits a password across two entries, and a percent escape hides one.
    """

    code = "env_refused"

    def __init__(self, name: str | None, cause: str, detail: str | None = None, *, position: int | None = None) -> None:
        super().__init__(f"{name or '-'}: {cause}" if detail is None else f"{name or '-'}: {cause}: {detail}")
        self.name, self.cause, self.detail, self.position = name, cause, detail, position


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
    meets exactly the value, PATH, NO_PROXY and temp rules an inherited value meets. While a proxy is set, a
    NO_PROXY entry that matches the route's base URL host refuses with ``no_proxy_route_host`` (CSL-839), unless
    that host is loopback (``localhost``, ``127.0.0.0/8``, ``::1`` or IPv4-mapped loopback); a ``*`` or
    engine-host entry anywhere in either name is reported first, with its own cause, loopback route or not.
    NO_PROXY and no_proxy are read as one combined list, NO_PROXY's entries first, and a refusal names the
    variable that holds the refusing entry and that entry's position, counted from 1 among that variable's own
    entries as ``_no_proxy_entries`` splits them (``EnvRefused.position``, and in ``EnvRefused.detail`` with the
    reason). No NO_PROXY refusal shows any text of an entry. CIDR entries that cover every IPv4 address are a ``*``:
    ``no_proxy_wildcard`` on any route, with or without a base URL. That is one block, or the union of the
    combined list's blocks and bare addresses, covering ``0.0.0.0/0`` or, in IPv6, all of ``::ffff:0:0/96``,
    ``64:ff9b::/96`` or ``::/96``; a union cover is reported at the entry that completes it. An entry with a
    ``/`` that is no CIDR block with a plain decimal prefix length (``0/0``, ``10.0.0.0/33``,
    ``10.0.0.0/255.0.0.0``, ``example.com/``), with more than one ``/`` (``10.1.2.3:8080/path/8``), with a
    ``:port`` before the ``/`` that is not a plain decimal 1-65535 (``1.2.3.4:x/8``), or with a ``%``, refuses
    with ``no_proxy_unparseable`` on any route.
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
    source = _copy_parent(parent)
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

    # A null in a value that is kept (including NO_PROXY with no proxy set) makes Popen raise ValueError.
    # Refuse before any stat or spawn. Temp values are replaced and are not read.
    for name, value in env.items():
        if name not in TEMP_NAMES:
            _require_text(name, value)
    for name, value in passed.items():
        _require_text(name, value)  # the sign-in token is not interpreted; Popen still rejects a null in it
    for name in [name for name in env if name not in ("PATH", *TEMP_NAMES, *NO_PROXY_NAMES)]:
        cause = _unsafe_value(name, env[name]) if env[name] else None  # empty: no path or URL to check
        if cause is not None:
            raise EnvRefused(name, cause)
    if "PATH" in env and (cause := _unsafe_path(env["PATH"])) is not None:
        raise EnvRefused("PATH", cause)
    # A bypass only matters when a proxy is set: "*" or an all-address CIDR block skips it for every host, an
    # engine-host entry for that engine, an entry for the route's base host for the route gateway.
    if any(env.get(name) for name in VALUE_URLS + VALUE_ALL_PROXY):
        # urllib and requests prefer a lowercase name and drop the uppercase one when the lowercase is set but
        # empty (getproxies_environment pops it; undici's ``https_proxy ?? HTTPS_PROXY`` keeps the empty string).
        # HTTPS then goes direct. The same for http and all. Empty uppercase beside a non-empty lowercase still
        # uses the lowercase value, and both empty is no proxy.
        if (cleared := _proxy_cleared(env)) is not None:
            raise EnvRefused(cleared, "proxy_cleared")
        # NO_PROXY and no_proxy are one combined list here (owner call, 2026-11-10): the refusal names the variable
        # that holds the refusing entry and that entry's position in it, never any text of the entry.
        lists = [(name, env[name]) for name in NO_PROXY_NAMES if name in env]
        if (refusal := _no_proxy_bypass(lists, profile.hosts)) is not None:
            raise refusal
        # CSL-839: the route's own base host is an endpoint too; an entry matching it would send the child to the
        # route gateway around the proxy. Checked after both names passed the rule above, so a "*", all-address or
        # engine-host entry keeps its own cause whatever else the lists hold. A loopback route host is exempt from this check
        # only (owner call, 2026-11-10): NO_PROXY may name a gateway on this machine.
        base = route_env.get(profile.base_url_name) if profile.base_url_name else None
        if base is not None and not _loopback_host(host := _BASE_URL.match(base)["host"]):
            port = _BASE_URL.match(base)["port"]
            for name in [name for name in NO_PROXY_NAMES if name in env]:
                if (found := _no_proxy_route_bypass(env[name], host, int(port) if port else None)) is not None:
                    raise _no_proxy_refused(name, *found)
    # N5h option A: a temp name is replaced by one private route temp root, never checked or passed.
    present = [name for name in TEMP_NAMES if name in env]
    if present:
        env.update(dict.fromkeys(present, _route_tmp()))
    env["HOME"] = home
    env.update(passed)  # after every check: the user's own sign-in value is passed as it is, never read here
    for name, value in route_env.items():
        _require_text(name, value)

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


def _copy_parent(parent: Mapping[str, str] | None) -> dict[str, str]:
    """The parent environment as strings. A non-string name or value is not an environment Popen can take."""

    raw = os.environ if parent is None else parent
    if not isinstance(raw, Mapping):
        raise EnvRefused(None, "value_malformed")
    source: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise EnvRefused(name if isinstance(name, str) else None, "value_malformed")
        source[name] = value
    return source


def _require_text(name: str | None, value: object) -> None:
    """Refuse a null or a non-string. ``Popen`` raises ``ValueError`` or ``TypeError`` for one."""

    if not isinstance(value, str) or "\0" in value:
        raise EnvRefused(name, "value_malformed")


def _proxy_cleared(env: Mapping[str, str]) -> str | None:
    """The lowercase proxy name whose empty value drops a non-empty uppercase one, or None.

    urllib ``getproxies_environment`` and requests return no proxy for that scheme (direct). undici keeps the
    empty string via ``https_proxy ?? HTTPS_PROXY``. Empty uppercase beside a set lowercase is not this case.
    """

    for lower, upper in (("http_proxy", "HTTP_PROXY"), ("https_proxy", "HTTPS_PROXY"), ("all_proxy", "ALL_PROXY")):
        if lower in env and env[lower] == "" and env.get(upper):
            return lower
    return None


def _no_proxy_entries(value: str) -> list[str]:
    """A NO_PROXY value as lowercased entries, comma and whitespace split, each without its scheme prefix.

    An entry's position, as a refusal gives it, is its place in this list counted from 1. The value's outer
    whitespace is stripped and any run of commas and whitespace is one separator, so a value that starts with a
    comma has an empty first entry, which counts (``,a%b`` is entry 2).
    """

    low, spans = _no_proxy_spans(value)
    return [_SCHEME_PREFIX.sub("", low[start:end]) for start, end in spans]


_SCHEME_PREFIX = re.compile(r"^[a-z][a-z0-9+.-]*://")
_SEPARATORS = re.compile(r"[,\s]+")
# Python's whitespace (str.isspace) and JavaScript's (\s, String.prototype.trim): the same set but U+FEFF, which
# only JavaScript counts, and U+001C-U+001F and U+0085, which only Python does. Spelled out so str.strip is linear.
_SPACES = ("\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
           "\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff")
# Default_Ignorable_Code_Point (Unicode DerivedCoreProperties). UTS 46 maps the ones it allows to nothing, so Go's
# idna drops them (U+00AD, U+200B, U+FEFF); it disallows the rest, and dropping those too only refuses more.
_IGNORABLE = re.compile("[\xad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e"
                        "\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8\U0001bca0-\U0001bca3"
                        "\U0001d173-\U0001d17a\U000e0000-\U000e0fff]")
# UTS 46 maps these full stops to "."; NFKC maps only U+FF0E there, and U+FF61 to U+3002.
_FULL_STOPS = str.maketrans("\u3002\uff0e\uff61", "...")


def _no_proxy_spans(value: str) -> tuple[str, list[tuple[int, int]]]:
    """The value stripped and lowercased, and where each ``_no_proxy_entries`` entry is in it (scheme still on)."""

    low, spans, start = value.strip().lower(), [], 0
    for separator in _SEPARATORS.finditer(low):
        spans.append((start, separator.start()))
        start = separator.end()
    spans.append((start, len(low)))
    return low, spans


def _idna_fold(text: str, case=str.casefold) -> str | None:
    """A name mapped as UTS 46 maps it for Go's ``idna.Lookup``, or more widely; None when it is not certain.

    The UTS 46 mapping is close to NFKC_Casefold: NFKC, full case folding and default ignorables removed, with
    U+3002, U+FF0E and U+FF61 mapped to ".". This applies those, stdlib only, until the text stops changing.
    Folding a sharp s (U+00DF) to ``ss`` (transitional) and dropping ZWJ (rejected by Go in most places) map
    more names to ASCII than Go does, never fewer; ``case=str.lower`` keeps the sharp s, as Go does. Not
    certain: a text that still changes after four rounds, or one with a code point this Python's Unicode data
    does not assign (Go's tables can be newer, and map some of those to ASCII: U+1CCD6 is ``a`` there).
    """

    if any(unicodedata.category(char) == "Cn" for char in text):
        return None
    for _ in range(4):
        folded = case(unicodedata.normalize("NFKC", text.translate(_FULL_STOPS)))
        folded = _IGNORABLE.sub("", folded).translate(_FULL_STOPS)
        if folded == text:
            return text
        text = folded
    return None


def _ipv4_range(text: str) -> tuple[int, int] | None:
    """Node's built-in matcher reads ``a-b`` as the IPv4 range from ``a`` to ``b``: both ends in, as integers.

    As ``ProxyConfig.shouldUseProxy`` does: split on ``-``, take the first two parts, trim each and require a
    dotted-decimal IPv4 address in both (``net.isIPv4``; ``ipaddress`` takes the same spelling). An empty range
    (``a`` after ``b``) holds nothing.
    """

    first, dash, rest = text.partition("-")
    if not dash:
        return None
    try:
        start, end = (int(ipaddress.IPv4Address(part.strip(_SPACES))) for part in (first, rest.partition("-")[0]))
    except ValueError:
        return None
    return (start, end) if start <= end else None


@dataclass
class _Readings:
    """One entry of ``_no_proxy_entries`` and the other ways client matchers read it (``_no_proxy_readings``)."""

    checked: list[str]  # the entry, then the pieces of its IDNA fold: the "%" and "/" rules apply to these
    matched: list[str] = field(default_factory=list)  # matched by name or address only, with no "*" patterns
    joined: list[str] = field(default_factory=list)  # comma items with their spaces deleted, as requests reads them
    ranges: list[tuple[int, int]] = field(default_factory=list)  # IPv4 ranges, (first, last)
    foreign: bool = False  # a folded piece is still not ASCII: Go would send it as punycode
    unfolded: bool = False  # the fold did not settle


def _no_proxy_readings(value: str) -> list[_Readings]:
    """Devin rt343 MEDIUM-2: each entry of ``_no_proxy_entries(value)``, in order, read every way a client reads it.

    Real matchers normalise entries more loosely than the split here, so an entry refuses when any one of these
    readings refuses (PRODUCT.md d84cdd26 / CSLR-D-012, section 11.5: N5i's per-engine ``NO_PROXY`` rule):

    * Go (``httpproxy`` maps each name through ``idna.Lookup``): the entry folded by ``_idna_fold``, split again
      where the fold made a separator. Fullwidth letters and dots, U+3002, U+FF61 and U+00AD fold to ASCII. The
      ``%`` and ``/`` rules apply to the pieces as to the entry. A fold that does not settle is ``unfolded``; a
      piece still not ASCII is ``foreign``, since Go would compare its punycode, which is not computed here.
    * undici (splits on JavaScript's ``\\s``) and Node's built-in matcher (trims each comma item): the pieces
      between U+FEFF characters, the one JavaScript space Python's ``\\s`` lacks.
    * undici strips one leading ``*.`` or ``.``: every reading that starts with ``*.`` is also read with its
      leading run of ``*`` and ``.`` removed, so ``*.host`` names ``host`` itself.
    * requests (``no_proxy.replace(" ", "").split(",")``): a comma item with spaces inside, joined, on the entry
      the item starts in (``joined``). It is matched like ``matched``, but only its IPv4 blocks join the cover,
      since requests reads CIDR for IPv4 only: ``0.0.0.0/1,1 28.0.0.0/1`` is a cover of every IPv4 address, and
      ``::1 0.0.0.0/0`` stays the cover it was at ``0.0.0.0/0``, not an IPv6 ``::10.0.0.0/0`` one entry earlier.
    * Node's built-in matcher: an ``a-b`` IPv4 range (``_ipv4_range``), read from the entry and from the trimmed
      comma item, on the entry the item starts in.

    Only the entry itself is matched as a ``*`` pattern: no client that reads the other forms has patterns, and
    a ``*`` left in one names no host, so the extra matching stays linear in the value's length.
    """

    low, spans = _no_proxy_spans(value)
    readings = []
    for start, end in spans:
        entry = _SCHEME_PREFIX.sub("", low[start:end])
        reading = _Readings([entry])
        if not entry.isascii():
            folded = _idna_fold(entry)
            if folded is None:
                reading.unfolded = True
            else:
                # Not ASCII, or ASCII only by folding a sharp s (U+00DF) that Go keeps and sends as punycode.
                reading.foreign = not folded.isascii() or not (_idna_fold(entry, str.lower) or "").isascii()
                if folded != entry:
                    reading.checked += [_SCHEME_PREFIX.sub("", piece) for piece in _SEPARATORS.split(folded.strip())]
            if "\ufeff" in entry:
                reading.matched += [_SCHEME_PREFIX.sub("", piece) for piece in entry.split("\ufeff")]
        if (span := _ipv4_range(entry)) is not None:
            reading.ranges.append(span)
        readings.append(reading)
    index = offset = 0
    for item in low.split(","):
        stripped, begin = item.strip(), offset + len(item) - len(item.lstrip())
        offset += len(item) + 1
        if not stripped:
            continue
        while index + 1 < len(spans) and spans[index + 1][0] <= begin:  # the entry the item starts in
            index += 1
        reading = readings[index]
        if " " in stripped and "*" not in (joined := stripped.replace(" ", "")):
            reading.joined.append(_SCHEME_PREFIX.sub("", joined))
        if (span := _ipv4_range(item)) is not None:
            reading.ranges.append(span)
    for reading in readings:
        reading.matched += [text.lstrip("*.") for text in reading.checked + reading.matched if text.startswith("*.")]
    return readings


def _no_proxy_refused(name: str, position: int, cause: str, reason: str) -> EnvRefused:
    """A NO_PROXY refusal: the variable, the entry's position in it and the reason, on one line.

    Owner call, 2026-11-10: no text of the entry goes into any NO_PROXY refusal, whatever its cause. An entry may
    hold userinfo, a comma splits a password across two entries (``http://user:pa,ss@host``) and a percent
    escape hides an ``@``, so nothing is masked or cut here: nothing of the entry is shown at all.
    """

    return EnvRefused(name, cause, f"entry {position} of {name} {reason}; the entry is not shown", position=position)


def _no_proxy_name(entry: str) -> str:
    """The name an entry is matched by: no brackets, no ``:port``, no trailing dot.

    A trailing dot is removed first, so ``host:443.`` is ``host:443`` before the port is dropped.
    A matched ``[host]:port`` pair is unwrapped as a pair: stripping ``[]`` first leaves ``host]``
    when the port follows the brackets (``[gateway.example]:443``), and that name matches nothing.
    A trailing dot inside the brackets is removed last. The port itself is never compared.
    """

    entry = entry.rstrip(".")
    bracketed = re.fullmatch(r"\[([^\]]*)\](?::[0-9]*)?", entry)
    if bracketed:
        entry = bracketed[1]
    else:
        entry = re.sub(r":[0-9]*$", "", entry.strip("[]"))
    return entry.rstrip(".")


def _no_proxy_hit(entry: str, hosts: tuple[str, ...]) -> bool:
    """Whether one entry (after ``_no_proxy_name``) names a host: a ``*`` pattern, the host or a parent domain."""

    if "*" in entry:
        return any(_star_match(entry, host) for host in hosts)
    return _name_hit(entry, hosts)


def _name_hit(entry: str, hosts: tuple[str, ...]) -> bool:
    """Whether a name, read with no ``*`` patterns, is one of the hosts or a parent domain of one."""

    bare = entry.lstrip(".")
    return any(host == bare or host.endswith("." + bare) for host in hosts)


# urllib.parse._splitport: the last ":digits" of the host being requested are a port, so "::1" is host ":".
_SPLIT_PORT = re.compile(r"(.*):([0-9]+)\Z")


def _urllib_hostonly(host: str) -> str:
    """The host ``urllib.request.proxy_bypass_environment`` compares, after its last-colon split."""

    match = _SPLIT_PORT.fullmatch(host)
    return match.group(1) if match else host


def _urllib_request_host(host: str, port: int | None) -> str:
    """``Request.host`` for this route host, as CPython 3.12's ProxyHandler passes it.

    ``host`` is already lowercased and unbracketed. An IPv6 address is wrapped again: the handler compares
    ``NO_PROXY`` to ``[::ffff:10.1.2.3]:443``, not to ``::ffff:10.1.2.3``. The port is included only when the
    URL spells one. ``_splitport`` then leaves the brackets on the host-only part (``[::ffff:10.1.2.3]``).
    """

    shown = f"[{host}]" if ":" in host else host
    return f"{shown}:{port}" if port is not None else shown


def _urllib_environment_hit(entry: str, request_host: str) -> bool:
    """One entry, matched as CPython 3.12 ``proxy_bypass_environment`` matches it against ``request_host``.

    Leading dots are removed, then the name is equal to ``Request.host`` or to its host-only part, or a dotted
    suffix of either (``name = '.' + name`` before ``endswith``). ``2.3]:443`` is a suffix of
    ``[::ffff:10.1.2.3]:443``; ``2.3]`` is a suffix of the host-only ``[::ffff:10.1.2.3]``. The port in the
    entry is part of that string: ``2.3]:80`` does not match port 443.
    """

    name = entry.strip().lstrip(".").lower()
    if not name:
        return False
    hostonly = _urllib_hostonly(request_host)
    if hostonly == name or request_host == name:
        return True
    dotted = "." + name
    return hostonly.endswith(dotted) or request_host.endswith(dotted)


def _client_hit(entry: str, host: str, port: int | None, *, urllib_host: bool = True) -> bool:
    """Whether urllib or requests 2.31 would bypass ``host`` for this one entry.

    urllib compares the entry, with leading dots removed, to the host and to the hostonly of its last-colon
    split (``:`` equals the hostonly of ``::1``; ``2001:db8:`` equals the hostonly of ``2001:db8::1``), and to
    ``Request.host`` (``[2001:db8::1]:8443`` splits to ``[2001:db8::1]``, so ``.[2001:db8::1]`` matches).
    ProxyHandler passes ``Request.host`` with brackets and an explicit port still on it, and the suffix check
    is dotted: ``2.3]:443`` and ``3]:443`` match ``[::ffff:10.1.2.3]:443``, and the same shape matches
    ``[2001:db8::10.1.2.3]:8443``, ``[64:ff9b::10.1.2.3]:443`` and port 80. That comparison is
    ``_urllib_environment_hit`` (CPython 3.12 ``proxy_bypass_environment``). ``urllib_host`` is false only for
    a port-stripped name: stripping ``:80`` from ``2.3]:80`` leaves ``2.3]``, which would match every port.
    requests deletes spaces before this is called and then uses ``hostname.endswith(entry)`` with no dot
    boundary, and the same on ``hostname:port`` when the URL spells a port. Leading dots are removed first so
    ``.2001:db8::1:8443`` is that host:port and ``.2001:db8::1:9`` is not. A ``*``, ``%`` or ``/`` entry is left
    to the rules that already judge those. Nothing here resolves a name.
    """

    if not entry or any(mark in entry for mark in "*/%"):
        return False
    host = host.lower().strip("[]").rstrip(".")
    texts: list[str] = []
    for text in (entry.lower(), entry.lower().lstrip(".")):
        if text and text not in texts:
            texts.append(text)
    bare = [host]
    if port is not None:
        bare.append(f"{host}:{port}")
    equal = list(bare)
    if ":" in host:
        bracketed = f"[{host}]"
        equal.append(f"{bracketed}:{port}" if port is not None else bracketed)
    for form in list(equal):
        only = _urllib_hostonly(form)
        if only not in equal:
            equal.append(only)
    for text in texts:
        if text in equal or any(target.endswith(text) for target in bare):
            return True
    # Raw entry against Request.host. Not applied to a name whose :port was already stripped.
    return urllib_host and _urllib_environment_hit(entry, _urllib_request_host(host, port))


def _ipv6_with_port(entry: str) -> tuple[str, str] | None:
    """An IPv6 address plus a trailing decimal field, after leading dots, or None.

    ``2001:db8::1:8443`` is a different address from ``2001:db8::1`` and also ``host:port`` for requests.
    Parsing it with ``ip_address`` and stopping there misses the port; dropping the field and ignoring the
    port matches a route on every port, including ``:9``. The field is kept and compared as ``host:port``.
    """

    text = entry.lstrip(".")
    if text.count(":") < 2:
        return None
    head, sep, tail = text.rpartition(":")
    if not sep or not tail.isascii() or not tail.isdecimal():
        return None
    try:
        ipaddress.ip_address(head)
    except ValueError:
        return None
    return head, tail


def _idna_hosts(hosts: tuple[str, ...]) -> bool:
    """Whether a host has a punycode (``xn--``) label, the only kind a ``foreign`` reading could match in Go."""

    return any(label.startswith("xn--") for host in hosts for label in host.split("."))


def _star_match(pattern: str, text: str) -> bool:
    """Whether a ``*`` pattern matches the whole of ``text``: ``*`` is any run of characters, the rest literal.

    A ``*`` run may be empty and may hold dots, and case is compared as given. No regex: a compiled ``.*a.*a...``
    backtracks for exponential time on a host that does not match (Devin rt343, MEDIUM-1), and NO_PROXY is the
    parent's to set. This walks both strings once and, on a mismatch, goes back to the last ``*`` only, so the
    worst case is len(pattern) * len(text) steps. The results are those of the regex it replaces
    (``".*".join(map(re.escape, parts)) + r"\\Z"``, matched from the start), which had no DOTALL: a ``*`` never
    matches a newline, so a newline is matched only by a newline in the pattern. No entry and no host holds one
    here (``_no_proxy_entries`` splits on whitespace; ``_BASE_URL`` and the engine hosts have none); the rule is
    kept so that every input gives the result it gave before.
    """

    lines, texts = pattern.split("\n"), text.split("\n")
    return len(lines) == len(texts) and all(map(_star_match_line, lines, texts))


def _star_match_line(pattern: str, text: str) -> bool:
    """``_star_match`` for a pattern and a text with no newline: greedy, going back to the last ``*`` only."""

    p = t = 0
    star, resume = -1, 0  # the last "*" seen in the pattern, and where in the text its run ends for now
    while t < len(text):
        if p < len(pattern) and pattern[p] == "*":
            star, resume = p, t
            p += 1
        elif p < len(pattern) and pattern[p] == text[t]:
            p += 1
            t += 1
        elif star >= 0:
            resume += 1  # the last "*" takes one more character; an earlier "*" taking more could not help
            p, t = star + 1, resume
        else:
            return False
    return pattern.count("*", p) == len(pattern) - p


def _no_proxy_bypass(lists: list[tuple[str, str]], hosts: tuple[str, ...]) -> EnvRefused | None:
    """N5i: comma and whitespace split; the union of curl, requests, hyper-util, undici and Codex matchers.

    ``lists`` is each NO_PROXY name present with its value, in ``NO_PROXY_NAMES`` order. Owner calls, 2026-11-10
    (CSL-839): their entries are judged as one combined list, and the first refusing entry is returned as a
    refusal that gives the variable holding it, the cause, the entry's position and the reason
    (``_no_proxy_refused``), never any text of the entry. The position counts from 1 among that variable's own
    entries (``_no_proxy_entries``): no_proxy's count does not go on from NO_PROXY's. Blocks that cover every
    IPv4 address are a ``*``, alone or as the union of the combined list's blocks (``_all_address_blocks``),
    reported at the entry that completes the cover. An entry is unparseable, and refuses since a matcher may
    read it some other way, when it holds a ``%`` (a zone id, a percent escape) or holds a ``/`` and is no CIDR
    block ``_no_proxy_ip`` reads (``0/0``, ``10.0.0.0/33``, ``10.0.0.0/255.0.0.0``, ``example.com/``, a second
    ``/`` as in ``10.1.2.3:8080/path/8``, a ``:port`` that is no port as in ``1.2.3.4:x/8``). The ``%`` check
    comes before the ``/`` check, so ``user:pw%40host/`` takes the ``%`` reason.

    MEDIUM-2: each entry is judged by all its ``_no_proxy_readings``, and each rule above refuses when any reading
    meets it, in the same order (the ``%`` and ``/`` rules on the entry and its IDNA fold). The cover takes the
    blocks and IPv4 ranges of every reading. A fold that does not settle refuses as unparseable after the ``/``
    rule; a fold still not ASCII refuses so only when a host has an ``xn--`` label, after the host rule.
    """

    where, readings = [], []
    for name, value in lists:
        for position, reading in enumerate(_no_proxy_readings(value), 1):
            where.append((name, position))
            readings.append(reading)
    blocks = [[ip for ip in map(_no_proxy_ip, reading.checked + reading.matched) if ip is not None]
              + [ip for ip in map(_no_proxy_ip, reading.joined) if ip is not None and ip.version == 4]
              for reading in readings]

    def covered(count: int) -> bool:
        return _covers_every_address([ip for ips in blocks[:count] for ip in ips],
                                     [span for reading in readings[:count] for span in reading.ranges])

    # The cover only grows with each entry, so the entry that completes it is found by halving the list.
    completing, last = -1, len(readings) - 1
    if covered(len(readings)):
        completing = 0
        while completing < last:
            middle = (completing + last) // 2
            if covered(middle + 1):
                last = middle
            else:
                completing = middle + 1
    for index, reading in enumerate(readings):
        name, position = where[index]
        for raw in reading.checked:
            if "%" in raw:
                return _no_proxy_refused(name, position, "no_proxy_unparseable", "is unparseable: a '%' (zone id or "
                                         "percent escape) isn't allowed in a NO_PROXY entry")
            if "/" in raw and _no_proxy_ip(raw) is None:
                return _no_proxy_refused(name, position, "no_proxy_unparseable", "is unparseable: a path, a second '/', "
                                         "or a prefix length that isn't plain decimal isn't allowed in a NO_PROXY entry")
        if reading.unfolded:
            return _no_proxy_refused(name, position, "no_proxy_unparseable", _UNFOLDED_WHY)
        entry, *others = map(_no_proxy_name, reading.checked + reading.matched + reading.joined)
        if any(text and text.strip("*.") == "" for text in (entry, *others)):
            return _no_proxy_refused(name, position, "no_proxy_wildcard", "matches every host")
        if index == completing:
            return _no_proxy_refused(name, position, "no_proxy_wildcard", "completes a cover of every IPv4 address"
                                     if reading.ranges else "completes a CIDR cover of every IPv4 address")
        if (entry and _no_proxy_hit(entry, hosts)) or any(text and _name_hit(text, hosts) for text in others):
            return _no_proxy_refused(name, position, "no_proxy_engine_host", "matches one of the engine's own hosts")
        # requests endswith (no dot boundary) and urllib's last-colon host, including host:443 as spelled in a URL.
        raws = [text for text in reading.checked + reading.matched + reading.joined if text]
        # The name form drops brackets and a :port. An IPv6 address-plus-port is left whole: stripping the
        # last field would compare a different address. requests has no dot boundary, so "pic.com" matches
        # "api.anthropic.com" and "443" matches "host:443".
        named = [_no_proxy_name(text) for text in raws if _ipv6_with_port(text) is None]
        if any(_client_hit(text, host, 443) for text in raws + named if text for host in hosts):
            return _no_proxy_refused(name, position, "no_proxy_engine_host", "matches one of the engine's own hosts")
        if reading.foreign and _idna_hosts(hosts):
            return _no_proxy_refused(name, position, "no_proxy_unparseable", _FOREIGN_WHY)
    return None


_UNFOLDED_WHY = "is unparseable: its characters don't fold to a host name with confidence"
_FOREIGN_WHY = "is unparseable: a name that is not ASCII after folding can't be compared with a punycode (xn--) host"


def _ip_forms(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> set:
    """An address and its other family's spelling: ``10.1.2.3`` and ``::ffff:10.1.2.3`` are one endpoint."""

    if address.version == 4:
        return {address, ipaddress.IPv6Address(f"::ffff:{address}")}
    return {address} | ({address.ipv4_mapped} if address.ipv4_mapped is not None else set())


def _host_addresses(host: str) -> set:
    """The addresses a base URL host names when it is an IP literal, else none. No name is resolved.

    An IPv4 host is read the way URL parsers read it (``127.1``, ``0x7f.0.0.1``, ``2130706433``), so a short or
    hex spelling of an address a NO_PROXY entry names is still that address.
    """

    try:
        return _ip_forms(ipaddress.ip_address(host))
    except ValueError:
        pass
    # One to four dot-separated numbers, decimal, 0x hex or 0-led octal; the last fills the bytes that remain.
    parts = host.split(".")
    try:
        numbers = [int(part, 16 if part.startswith("0x") else 8 if part.startswith("0") and part != "0" else 10)
                   for part in parts]
        if len(parts) > 4 or not all(re.fullmatch(r"0x[0-9a-f]*|[0-9]+", part) for part in parts):
            return set()
        if any(number > 255 for number in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
            return set()
        packed = sum(number << (8 * (3 - i)) for i, number in enumerate(numbers[:-1])) + numbers[-1]
        return _ip_forms(ipaddress.IPv4Address(packed))
    except ValueError:
        return set()


def _loopback_host(host: str) -> bool:
    """CSL-839: whether a base URL host is loopback, the one kind of route host a NO_PROXY entry may name.

    Loopback is the exact name ``localhost`` (any case, one trailing dot allowed; not a subdomain of it), an
    address in ``127.0.0.0/8`` in any spelling ``_host_addresses`` reads (``127.1``, ``0x7f.0.0.1``,
    ``2130706433``), ``::1`` in any spelling, and IPv4-mapped loopback (``::ffff:127.0.0.1``). Nothing is
    resolved: a name that only looks like one (``localhost.example.com``, ``127.0.0.1.example.com``) is not.
    """

    host = host.lower().strip("[]")
    host = host[:-1] if host.endswith(".") else host
    if host == "localhost":
        return True
    # Every form must be loopback; an IPv4-mapped IPv6 form is judged by the IPv4 address it carries.
    addresses = {address.ipv4_mapped or address if address.version == 6 else address
                 for address in _host_addresses(host)}
    return bool(addresses) and all(address.is_loopback for address in addresses)


def _no_proxy_ip(entry: str):
    """The address or CIDR block (an ``ipaddress`` object) an entry is, or None when it is neither shape.

    Read as ``ip``, ``ip:port``, ``[ip]``, ``[ip]:port`` or any of those with ``/prefix``; the port is dropped,
    as for a name. Host bits may be set in a block: ``10.1.2.3/8``. The prefix is what follows the one ``/``
    and must be a plain decimal length in the family's range: ASCII digits, no sign, no leading zero but ``0``
    itself. So a netmask or hostmask (``10.0.0.0/255.0.0.0``), ``/08`` and ``/0x0`` are no block, and neither is
    any entry with a ``%`` (owner calls, 2026-11-10). An entry with more than one ``/`` is no block either
    (``10.1.2.3:8080/path/8``, ``10.0.0.0/8/8``, ``//8``): the port strip must never drop a path. And in a
    ``/`` entry the ``:port`` dropped from an IPv4 address must itself be a port, plain ASCII decimal 1-65535
    with no leading zero, so the whole address-and-port part is read before any of it is dropped
    (``1.2.3.4:80/8`` is ``1.0.0.0/8``; ``1.2.3.4:x/8``, ``1.2.3.4:0/8`` and ``1.2.3.4:65536/8`` are no block).
    """

    if "%" in entry or entry.count("/") > 1:
        return None
    text, slash, prefix = entry.rpartition("/")
    if not slash:
        text = entry
    elif not re.fullmatch(r"0|[1-9][0-9]*", prefix):
        return None
    bracketed = re.fullmatch(r"\[([^\]]*)\](?::[0-9]*)?", text)
    if bracketed:
        text = bracketed[1]
    elif text.count(":") == 1:  # ip:port; two or more colons are a bare IPv6 address
        text, port = text.split(":")
        if slash and not (re.fullmatch(r"[1-9][0-9]{0,4}", port) and int(port) < 65536):
            return None
    text = text.rstrip(".")
    try:
        return ipaddress.ip_network(f"{text}/{prefix}", strict=False) if slash else ipaddress.ip_address(text)
    except ValueError:
        return None


_NETWORKS = (ipaddress.IPv4Network, ipaddress.IPv6Network)
_ALL_IPV4 = ipaddress.IPv4Network("0.0.0.0/0")
# The IPv6 blocks that each carry every IPv4 address: IPv4-mapped, the NAT64 well-known prefix, IPv4-compatible.
_IPV4_MAPPING_BLOCKS = (ipaddress.IPv6Network("::ffff:0:0/96"), ipaddress.IPv6Network("64:ff9b::/96"),
                        ipaddress.IPv6Network("::/96"))


def _all_address_blocks(entries: list[str], ranges=()) -> bool:
    """Whether the CIDR blocks among these entries skip the proxy for every IPv4 address, as ``*`` does (CSL-839).

    That is one block, or the union of the blocks, covering all of ``0.0.0.0/0`` (``0.0.0.0/0``, ``10.0.0.0/0``,
    ``0.0.0.0/1,128.0.0.0/1``) or, in IPv6, all of one of ``_IPV4_MAPPING_BLOCKS`` (``::ffff:0:0/96``, ``::/64``,
    ``::/0``, ``64:ff9b::/97,64:ff9b::8000:0/97``): a matcher that reads IPv4 through such a block (Go, undici, a
    NAT64 path) would skip the proxy for every IPv4 host. A bare address joins the union as its single-address
    block. The two families are not mixed, and blocks that leave a gap (``0.0.0.0/1``, ``0.0.0.0/1,192.0.0.0/2``,
    ``2000::/3``, ``::ffff:0:0/97``) are not a cover. The shapes are ``_no_proxy_ip``'s. ``ranges`` are IPv4
    ranges (``_ipv4_range``, MEDIUM-2) that join the IPv4 union.
    """

    return _covers_every_address([ip for ip in map(_no_proxy_ip, entries) if ip is not None], ranges)


def _covers_every_address(parsed: list, ranges=()) -> bool:
    """``_all_address_blocks`` over parsed addresses and blocks and IPv4 ``(first, last)`` ranges.

    The IPv4 union is swept as sorted integer intervals, so many ranges cost n log n, not a block per range.
    """

    blocks = [ip if isinstance(ip, _NETWORKS) else ipaddress.ip_network(ip) for ip in parsed]
    spans = sorted([(int(block[0]), int(block[-1])) for block in blocks if block.version == 4] + list(ranges))
    reach = -1  # every IPv4 address up to here is covered
    for first, last in spans:
        if first > reach + 1:
            break
        reach = max(reach, last)
    if reach >= int(_ALL_IPV4[-1]):
        return True
    # collapse_addresses only merges blocks into larger ones, so a full cover of a target is one merged block.
    merged = list(ipaddress.collapse_addresses(block for block in blocks if block.version == 6))
    return any(block.supernet_of(target) for block in merged for target in _IPV4_MAPPING_BLOCKS)


def _no_proxy_ip_hit(entry: str, addresses: set) -> bool | None:
    """Whether an entry is one of these addresses, or a CIDR block that holds one; None when it is neither shape.

    The shapes are ``_no_proxy_ip``'s. Addresses are compared parsed, so ``::1`` and ``0:0:0:0:0:0:0:1`` are one.
    """

    parsed = _no_proxy_ip(entry)
    if parsed is None:
        return None
    if isinstance(parsed, _NETWORKS):
        return any(address.version == parsed.version and address in parsed for address in addresses)
    return bool(_ip_forms(parsed) & addresses)


def _no_proxy_route_bypass(value: str, host: str, port: int | None = None) -> tuple[int, str, str] | None:
    """CSL-839: the first NO_PROXY entry that matches the route's base URL host, by position from 1, or None.

    The same matcher as ``_no_proxy_bypass``, over the one host: the host itself, a parent domain with or
    without the leading dot, a ``*`` pattern, with scheme, brackets, ``:port`` and trailing dot stripped from
    the entry. The entry's port is not compared with the route's: curl and requests ignore it, so another port
    is still a hit. The host is compared lowercased and without its own trailing dot. An IP-literal host is
    also matched as an address: by an entry that is that address, and by a CIDR block that holds it (curl
    7.86+, requests, undici and Go honour CIDR). A CIDR entry never matches a name: no matcher resolves DNS.
    An entry that is an address or a block is decided as one and not also as a name, so the port strip cannot
    turn ``::1:0`` into ``::1``. An entry that is an IPv6 address plus a decimal field (``2001:db8::1:8443``,
    including a leading dot) is not that strip: the field is a port for requests' ``host:port``, so
    ``.2001:db8::1:9`` does not match port 8443. ``_client_hit`` also applies urllib's last-colon split,
    requests' endswith with no dot boundary, and CPython 3.12's dotted suffix of the raw entry against
    ``Request.host`` and its host-only part, brackets included (``2.3]:443`` matches
    ``[::ffff:10.1.2.3]:443``; ``2.3]:80`` does not). A loopback host never gets here: ``build_child_env`` exempts it
    (``_loopback_host``). Neither does an all-address cover, a ``/`` entry that is no CIDR block or a ``%``
    entry: ``_no_proxy_bypass`` refused those first.

    MEDIUM-2: every reading of an entry (``_no_proxy_readings``) is matched the same way, the entry itself as a
    ``*`` pattern and the others by name or address only, and an IPv4 range that holds the host is a hit. The
    result is ``(position, cause, reason)`` for ``_no_proxy_refused``: ``no_proxy_route_host`` for a hit, or
    ``no_proxy_unparseable`` for an entry still not ASCII after folding when the host has an ``xn--`` label.
    """

    host = host.lower().strip("[]").rstrip(".")
    addresses = _host_addresses(host)
    v4 = [int(address) for address in addresses if address.version == 4]
    for position, reading in enumerate(_no_proxy_readings(value), 1):
        for index, entry in enumerate(reading.checked + reading.matched + reading.joined):
            raw = entry
            hit = _no_proxy_ip_hit(entry, addresses)
            # An IPv6 address with a trailing decimal field is also a different address. Keep the field and
            # compare host:port; dropping it would match every port, and stopping at ip_address would match none.
            if hit is None and _ipv6_with_port(entry) is None:
                named = _no_proxy_name(entry)
                matcher = _no_proxy_hit if index == 0 else _name_hit  # only the entry itself is a "*" pattern
                hit = bool(named) and named.strip("*.") != "" and matcher(named, (host,))
            if not hit and _client_hit(raw, host, port):
                hit = True
            # A name's :port is dropped and then matched with no dot boundary ("pic.com" in "epic.com").
            # Two or more colons is an IPv6 spelling: dropping the last field would turn "2001:db8::2" into
            # "2001:db8:", which is urllib's hostonly of the different address "2001:db8::1".
            # urllib_host is off here. "2.3]:80" strips to "2.3]", and that host-only suffix matches
            # Request.host on every port; ProxyHandler only matches the port still written in the entry.
            if not hit and raw.lstrip(".").count(":") < 2:
                named = _no_proxy_name(raw)
                hit = bool(named) and _client_hit(named, host, port, urllib_host=False)
            if hit:
                return position, "no_proxy_route_host", "matches the route's base URL host"
        if any(first <= address <= last for first, last in reading.ranges for address in v4):
            return position, "no_proxy_route_host", "matches the route's base URL host"
        if reading.foreign and _idna_hosts((host,)):
            return position, "no_proxy_unparseable", _FOREIGN_WHY
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


def _private_dir(path: str | None) -> bool:
    # A null is refused before Path or lstat: both raise ValueError ("embedded null byte") instead of EnvRefused.
    if not isinstance(path, str) or "\0" in path or not os.path.isabs(path):
        return False
    try:
        entry = _lexical_dir(path)  # the home itself may not be a link, whatever the trailing spelling
    except (OSError, ValueError):
        return False
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
            # The cached root failed the mode check. Remove it only when a second lstat still sees the same
            # empty directory of ours (st_dev, st_ino): a path replaced in between is left alone. os.rmdir
            # deletes a directory only when it is empty.
            if path and st is not None and stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid():
                try:
                    again = os.lstat(path)
                except OSError:
                    again = None
                if again is not None and (again.st_dev, again.st_ino) == (st.st_dev, st.st_ino):
                    try:
                        os.rmdir(path)
                    except OSError:
                        pass
            path = _TMP[key] = _make_route_tmp()
        return path
