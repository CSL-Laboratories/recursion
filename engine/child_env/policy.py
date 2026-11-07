"""Child-environment allowlist for engine children in the new core (CSL-785, candidate).

The old route transport hands every engine child the whole parent environment
(``scripts/v3/route_contract/transport.py:283`` at master ``209b3ddb``,
``"env": os.environ.copy()``). That file is frozen; this module is the new-core
replacement. It builds the child environment from nothing:

* an exact-name allowlist per engine profile (Claude Code, Codex). Nothing outside
  it reaches the child, so credential names are absent by construction, and each
  profile also lists the names it scrubs so a test can assert them by name;
* the value rules ported from csl-research candidate records C2-37 and C2-38
  (``submissions/20261102-csl678-value-check-v3-01``, rule N5h, and
  ``submissions/20261103-csl678-envpath-path-noproxy-01``, rule N5i): file, directory,
  proxy, ``TZ``, perspective-path, private temp root, ``PATH`` and per-engine
  ``NO_PROXY`` checks, refused before any spawn;
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

import os
import re
import shutil
import stat
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

__all__ = [
    "BASE_NAMES",
    "ENGINE_PROFILES",
    "EngineProfile",
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
# 20261102-csl672-a4-child-env-values-grounding-01 section 5.1: PATH, HOME and 27 more).
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
)

VALUE_FILES = ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "SHELL")
VALUE_DIRS = ("SSL_CERT_DIR",)
VALUE_URLS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
TEMP_NAMES = ("TMPDIR", "TMP", "TEMP")
NO_PROXY_NAMES = ("NO_PROXY", "no_proxy")
# N5i: the names the engines run by bare name. A link for a name outside this list is
# not checked (envpath record, cell path_tool_link_py); PATH_TOOLS=None checks every entry.
PATH_TOOLS: tuple[str, ...] | None = ("bash", "zsh", "sh", "git", "rg", "node", "sleep")
# A name an override may never set, whatever the profile (the Nov 5 tree's _DENIED).
_DENIED = re.compile(r"(API_?KEY|SECRET|TOKEN|PASSWORD|BASE_URL|_PAT$|USE_BEDROCK|USE_VERTEX|USE_FOUNDRY)", re.IGNORECASE)
# Paths that name a different file in the child than in this process (its own cwd, fds, root).
_PERSPECTIVE = re.compile(r"^/(?:proc|dev/fd|dev/std(?:in|out|err))(?:/|$)")
_PROXY_URL = re.compile(
    r"^(?:(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://)?(?P<userinfo>[^@/]*@)?"
    r"(?P<host>\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::(?P<port>[0-9]{1,5}))?/?$"
)
_MAX_LINKS = 40


@dataclass(frozen=True)
class EngineProfile:
    """What one engine child may inherit, and what it must never see."""

    engine: str
    allowed: tuple[str, ...]
    hosts: tuple[str, ...]  # its own endpoints: a NO_PROXY entry matching one refuses while a proxy is set
    scrub: tuple[str, ...]  # credential and route selectors, asserted absent (they are not allowlisted)


_CLAUDE_SCRUB = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
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
)
_CODEX_SCRUB = ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME")

ENGINE_PROFILES: Mapping[str, EngineProfile] = MappingProxyType(
    {
        # Hosts: Claude Code network-config.md:221-228; Codex 0.165.0 model-provider-info/src/lib.rs:82, :445,
        # login/src/server.rs:77 (as read in the envpath record, section 5).
        "claude_code": EngineProfile(
            "claude_code",
            BASE_NAMES,
            ("api.anthropic.com", "platform.claude.com", "claude.ai", "mcp-proxy.anthropic.com"),
            _CLAUDE_SCRUB,
        ),
        "codex": EngineProfile(
            "codex",
            BASE_NAMES,
            ("chatgpt.com", "auth.openai.com", "api.openai.com"),
            _CODEX_SCRUB,
        ),
    }
)


class ValidatedEnv(Mapping[str, str]):
    """An environment ``build_child_env`` produced. Only this type is accepted by the launch boundary."""

    __slots__ = ("_env", "engine", "dropped")
    _seal = object()

    def __init__(self, env: Mapping[str, str], engine: str, dropped: tuple[str, ...], *, _seal: object = None) -> None:
        if _seal is not ValidatedEnv._seal:
            raise TypeError("ValidatedEnv is made only by build_child_env")
        self._env = dict(env)
        self.engine = engine
        self.dropped = dropped  # parent names left out (names only, never values)

    def __getitem__(self, name: str) -> str:
        return self._env[name]

    def __iter__(self):
        return iter(self._env)

    def __len__(self) -> int:
        return len(self._env)

    def __repr__(self) -> str:  # names only: values may be paths an operator considers private
        return f"ValidatedEnv(engine={self.engine!r}, names={sorted(self._env)!r})"

    def to_dict(self) -> dict[str, str]:
        return dict(self._env)


def build_child_env(
    engine: str,
    *,
    home: str,
    parent: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> ValidatedEnv:
    """Return the environment for one engine child, or raise ``EnvRefused`` before any spawn.

    ``home`` is the route-owned private home the child gets as HOME; the parent's HOME is never passed.
    ``overrides`` are names the core itself sets (never credential-shaped, never a scrubbed name).
    """

    if os.name == "nt":
        raise EnvRefused(None, "platform_unsupported")
    profile = ENGINE_PROFILES.get(engine) if isinstance(engine, str) else None
    if profile is None:
        raise EnvRefused(None, "unknown_engine")
    if not _private_dir(home):
        raise EnvRefused("HOME", "home_unsafe")
    source = dict(os.environ if parent is None else parent)
    env = {name: source[name] for name in profile.allowed if name in source and name != "HOME"}
    dropped = tuple(sorted(name for name in source if name not in env and name != "HOME"))

    for name in [name for name in env if name not in ("PATH", *TEMP_NAMES, *NO_PROXY_NAMES)]:
        cause = _unsafe_value(name, env[name]) if env[name] else None  # empty: no path or URL to check
        if cause is not None:
            raise EnvRefused(name, cause)
    if "PATH" in env and (cause := _unsafe_path(env["PATH"])) is not None:
        raise EnvRefused("PATH", cause)
    # A bypass only matters when a proxy is set: "*" skips it for every host, an engine-host entry for that engine.
    if any(env.get(name) for name in VALUE_URLS):
        for name in [name for name in NO_PROXY_NAMES if name in env]:
            if (cause := _no_proxy_bypass(env[name], profile.hosts)) is not None:
                raise EnvRefused(name, cause)
    # N5h option A: a parent temp name is replaced by one private route temp root, never checked or passed.
    present = [name for name in TEMP_NAMES if name in env]
    if present:
        env.update(dict.fromkeys(present, _route_tmp()))
    env["HOME"] = home

    for name, value in (overrides or {}).items():
        if not isinstance(name, str) or not isinstance(value, str) or not name or "=" in name or "\0" in value:
            raise EnvRefused(None, "override_malformed")
        if _DENIED.search(name) or name in profile.scrub or name in ("HOME", "PATH"):
            raise EnvRefused(name, "override_denied")
        env[name] = value
    leaked = sorted(name for name in env if name in profile.scrub or (name not in profile.allowed and _DENIED.search(name)))
    if leaked:  # unreachable by construction; kept so a later edit to the allowlist fails closed
        raise EnvRefused(leaked[0], "scrub_failed")
    return ValidatedEnv(env, engine, dropped, _seal=ValidatedEnv._seal)


def _unsafe_value(name: str, value: str) -> str | None:
    if "\0" in value:
        return "value_malformed"
    if name in VALUE_URLS:
        return _unsafe_proxy(value)
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
    directory whose own chain no one else can write; no step may enter a perspective path. A missing
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


def _no_proxy_bypass(value: str, hosts: tuple[str, ...]) -> str | None:
    """N5i: comma and whitespace split; the union of curl, requests, hyper-util, undici and Codex matchers."""

    for entry in re.split(r"[,\s]+", value.strip().lower()):
        entry = re.sub(r"^[a-z][a-z0-9+.-]*://", "", entry).strip("[]")
        entry = re.sub(r":[0-9]*$", "", entry).rstrip(".")
        if not entry:
            continue
        if entry.strip("*.") == "":
            return "no_proxy_wildcard"
        if "*" in entry:
            pattern = re.compile(".*".join(re.escape(part) for part in entry.split("*")) + r"\Z")
            hit = any(pattern.match(host) for host in hosts)
        else:
            bare = entry.lstrip(".")
            hit = any(host == bare or host.endswith("." + bare) for host in hosts)
        if hit:
            return "no_proxy_engine_host"
    return None


def _unsafe_proxy(value: str) -> str | None:
    """A proxy URL is [http[s]://]host[:port][/]: no user name or password, no path."""

    match = _PROXY_URL.match(value)
    if match is None or match["scheme"] is not None and match["scheme"].lower() not in ("http", "https"):
        return "proxy_malformed"
    if match["userinfo"] is not None:
        return "proxy_userinfo"
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


def _private_dir(path: str | None) -> bool:
    if not isinstance(path, str) or not os.path.isabs(path) or _resolve(path)[2] is not None:
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    # The home itself is ours and 0700; its parents follow the chain rule (a root sticky /tmp above it is fine).
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid() and not st.st_mode & 0o077 and _owned_chain(path)


_TMP: dict[int, str] = {}
_TMP_LOCK = threading.Lock()


def _route_tmp() -> str:
    """One private temp root per process for engine children: a fresh random 0700 name, remade if it changed."""

    key = os.getpid()
    with _TMP_LOCK:
        path = _TMP.get(key)
        try:
            st = os.lstat(path) if path else None
        except OSError:
            st = None
        if st is None or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            path = _TMP[key] = tempfile.mkdtemp(prefix="recursion-child-tmp-")
        return path
