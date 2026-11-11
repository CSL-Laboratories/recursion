"""Own-loop subscription routes for Copilot and Nous (Lucas, 2026-11-08; CSL-786, candidate).

PRODUCT.md at csl-research #1074 head 055cc7a5 (section 3.3):

* **Copilot: "Copilot's own sign-in".** The own loop reaches a Copilot subscription through GitHub's
  Copilot SDK on the user's existing ``copilot`` CLI sign-in. Recursion registers no GitHub OAuth App
  and holds no GitHub or Copilot token. The SDK's authentication order puts an explicit token,
  ``GITHUB_COPILOT_API_TOKEN``, ``COPILOT_GITHUB_TOKEN``, ``GH_TOKEN`` and ``GITHUB_TOKEN`` ahead of the
  stored ``copilot`` login, and the ``gh`` CLI's login after it. So a Copilot own-loop run passes no
  token, removes those variables (and ``COPILOT_API_URL``) from that run's child, and refuses to run when
  no stored ``copilot`` login exists rather than fall back to ``gh``.
* **Nous: "Local proxy".** The own loop reaches a Nous Portal subscription only through the user's own
  Hermes subscription proxy on their own machine, with only the ``nous`` adapter; the ``xai`` adapter
  stays off. Recursion holds no Nous token. "Their own machine only" means a loopback address; doctor
  reports the proxy address, refuses one that is not loopback, and warns when the host has other
  interactive users, since any local user can reach a loopback proxy (the proxy "accepts any bearer").

Both checks run before any spawn or request and fail closed. Nothing here reads a credential, opens a
login store, resolves a host name or touches the network: whether a stored ``copilot`` login exists is
answered by a caller-supplied probe (``has_copilot_login``). That probe stays a callable, not an argv.
With no probe the route refuses. Where a new process group cannot be enforced, it refuses
``probe unenforceable``. Another live thread refuses ``probe threaded`` before ``fork``.

The child environment is not a copy of the parent. After the Copilot or Nous strip, only names on
``child_env``'s base allowlist remain, and a proxy value that carries userinfo or fails the proxy
rule is removed. ``spawn_copilot_own_loop`` and ``spawn_nous_own_loop`` then rebuild that mapping
with ``build_child_env`` and start the process through ``child_env.spawn``.
"""

from __future__ import annotations

import ipaddress
import math
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

from recursion_core.child_env import EnvRefused, build_child_env, spawn
from recursion_core.child_env.policy import (
    BASE_NAMES, VALUE_ALL_PROXY, VALUE_URLS, _ALL_PROXY_SCHEMES, _private_dir, _unsafe_proxy,
)

__all__ = [
    "COPILOT_TOKEN_ENV",
    "COPILOT_STRIP_ENV",
    "NOUS_ADAPTER",
    "OwnLoopDecision",
    "OwnLoopRouteRefused",
    "check_copilot_own_loop",
    "check_nous_own_loop",
    "nous_proxy_doctor",
    "spawn_copilot_own_loop",
    "spawn_nous_own_loop",
]

# ``child_env``'s base allowlist. A name outside it never reaches a Copilot or Nous child, so a
# denylist cannot forget ``GH_PAT``, ``AWS_*`` or ``GIT_CONFIG_*``.
_CHILD_ALLOW = frozenset(BASE_NAMES)
_PROXY_VALUES = frozenset(VALUE_URLS + VALUE_ALL_PROXY)

# The Copilot SDK reads these ahead of the stored ``copilot`` login (PRODUCT.md 055cc7a5, 3.3).
COPILOT_TOKEN_ENV = ("GITHUB_COPILOT_API_TOKEN", "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
# Removed from a Copilot own-loop child: the token variables plus the API URL override.
COPILOT_STRIP_ENV = COPILOT_TOKEN_ENV + ("COPILOT_API_URL",)
# Anything that would let the run fall back to, or be pointed at, the gh CLI login or host.
_GH_FALLBACK_ENV = ("GH_HOST", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_CONFIG_DIR")

NOUS_ADAPTER = "nous"
_NOUS_OFF_ADAPTERS = ("xai",)
_NOUS_SCHEMES = ("http", "https")

_COPILOT_FIELDS = frozenset({"engine", "auth", "method", "model"})
_NOUS_FIELDS = frozenset({"engine", "auth", "method", "base_url", "adapters", "model"})
_TOKEN_HINTS = ("token", "key", "secret", "bearer", "password", "credential", "authorization")
# Proxy settings would send a loopback-bound request to another host (proxy-aware clients honour them).
_PROXY_ENV = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "SOCKS_PROXY", "SOCKS5_PROXY"})
# Credential names the ``*_API_KEY`` / ``*_TOKEN`` suffix misses. ``OPENAI_API_BASE`` is the older
# spelling of ``OPENAI_BASE_URL``. ``GH_PAT`` and ``AWS_ACCESS_KEY_ID`` do not end in those suffixes.
# Git's ``GIT_CONFIG_KEY_*`` / ``GIT_CONFIG_VALUE_*`` channel can carry an HTTP bearer
# (``http.extraheader``). Matched case-insensitively.
_CREDENTIAL_ENV = frozenset({
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "GITHUB_PASSWORD",
    "GITHUB_PAT",
    "GH_PAT",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
})
# A stored-login probe that does not return is not a login. Callers may pass a shorter bound.
LOGIN_PROBE_DEADLINE_S = 5.0
# After SIGTERM, wait at most this long, and never past the original deadline, before SIGKILL.
_PROBE_GRACE_S = 0.2
_MODEL_MAX = 128
# Fixed refusal text. Parser messages and the raw URL can echo a secret (a bad port named "canary").
_PROXY_DETAIL = "proxy address"
_DEFAULT_PORTS = {"http": 80, "https": 443}

# Dropped even if ``child_env``'s base allowlist later grows to include them. ``NODE_EXTRA_CA_CERTS``,
# ``PATH``, ``HOME`` and the locale names are not in these families.
_SECRET_PREFIXES = ("AWS_", "AZURE_", "ARM_", "GOOGLE_", "CLOUDSDK_", "GCLOUD_", "DYLD_", "GIT_CONFIG")
_SECRET_SUFFIXES = ("_SECRET", "_PASSWORD", "_KEY", "_PAT", "_CREDENTIALS")
_SECRET_NAMES = frozenset({
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GH_PAT",
    "GITHUB_COPILOT_API_TOKEN",
    "COPILOT_GITHUB_TOKEN",
    "COPILOT_API_URL",
    "GITHUB_API_URL",
    "SSH_AUTH_SOCK",
    "SSH_ASKPASS",
    "GIT_ASKPASS",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "KUBECONFIG",
    "DOCKER_CONFIG",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "NODE_OPTIONS",
    "NODE_PATH",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "BASH_ENV",
    "ENV",
})


def _ambient_secret(name: str) -> bool:
    """True for parent names a Copilot or Nous child must not see, even if an allowlist grows."""

    upper = name.upper()
    if upper.startswith(_SECRET_PREFIXES) or upper.endswith(_SECRET_SUFFIXES):
        return True
    return upper in _SECRET_NAMES


def _proxy_value_refused(name: str, value: object) -> bool:
    """A proxy value ``child_env`` would refuse (userinfo, a bad host, a bad scheme) does not launch."""

    if name not in _PROXY_VALUES or not isinstance(value, str):
        return False
    schemes = _ALL_PROXY_SCHEMES if name in VALUE_ALL_PROXY else ("http", "https")
    return _unsafe_proxy(value, schemes) is not None


def _project_child(parent: Mapping[str, str], blocked: Callable[[str], bool]) -> tuple[dict[str, str], tuple[str, ...]]:
    """The parent names that may reach the child, and the names that were removed."""

    removed: list[str] = []
    child: dict[str, str] = {}
    for name, value in parent.items():
        if blocked(name) or name not in _CHILD_ALLOW or _ambient_secret(name) or _proxy_value_refused(name, value):
            removed.append(name)
        else:
            child[name] = value
    return child, tuple(removed)


def _model(engine: str, value: object) -> str:
    """Optional model id: printable, no spaces, at most 128 characters. ``""`` when absent."""
    if value is None:
        return ""
    if not isinstance(value, str) or not value or len(value) > _MODEL_MAX or \
            any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
        raise OwnLoopRouteRefused(engine, "model_malformed")
    return value


class OwnLoopRouteRefused(Exception):
    code = "own_loop_route_refused"

    def __init__(self, engine: str, cause: str, detail: str = "") -> None:
        super().__init__(f"{engine}: {cause}" + (f" ({detail})" if detail else ""))
        self.engine, self.cause, self.detail = engine, cause, detail


@dataclass(frozen=True)
class OwnLoopDecision:
    engine: str
    method: str
    child_env: Mapping[str, str]
    removed_env: tuple[str, ...] = ()
    base_url: str = ""
    adapters: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)
    model: str = ""  # checked here; "" means the engine's default


def _norm(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "_".join(value.strip().lower().replace("-", " ").replace("_", " ").split())


def _common(engine: str, route: object, allowed: frozenset[str], method: str) -> Mapping[str, object]:
    if not isinstance(route, Mapping):
        raise OwnLoopRouteRefused(engine, "route_malformed")
    held = sorted(k for k in route if isinstance(k, str) and any(h in k.lower() for h in _TOKEN_HINTS))
    if held:
        raise OwnLoopRouteRefused(engine, "recursion_holds_no_token", ",".join(held))
    unknown = sorted(str(k) for k in set(route) - allowed)
    if unknown:
        raise OwnLoopRouteRefused(engine, "route_unknown_field", ",".join(unknown))
    if _norm(route.get("engine")) != engine:
        raise OwnLoopRouteRefused(engine, "engine_mismatch", str(route.get("engine")))
    if _norm(route.get("auth")) != "subscription":
        raise OwnLoopRouteRefused(engine, "auth_not_subscription", str(route.get("auth")))
    got = _norm(route.get("method"))
    if got != method:
        raise OwnLoopRouteRefused(engine, "method_not_allowed", got or "missing")
    return route


def check_copilot_own_loop(
    route: Mapping[str, object],
    parent_env: Mapping[str, str],
    *,
    has_copilot_login: Callable[[], bool] | None = None,
    probe_deadline: float = LOGIN_PROBE_DEADLINE_S,
    home: str | None = None,
) -> OwnLoopDecision:
    """Decide one Copilot own-loop run. ``route``: engine="copilot", auth="subscription",
    method="copilot_cli_sign_in" (optional ``model``). Returns the child env with every token variable
    and ``COPILOT_API_URL`` removed, or raises ``OwnLoopRouteRefused``.

    The login probe is the caller's callable, not an argv. It runs with a deadline
    (``probe_deadline``, at most ``LOGIN_PROBE_DEADLINE_S``). A probe that raises, including
    ``SystemExit``, or that does not return in time, is not a login.     Where a new process group can
    be made, every exit signals that group (SIGTERM, then SIGKILL inside the deadline). A descendant
    that calls ``setpgid`` leaves the group and is not signalled. Where the group cannot be made,
    or another thread is already running (including one started with ``_thread``), the route refuses
    and does not fork. ``home`` is required: without it the probe would see the ambient account.

    The probe runs with the environment the child will receive, including ``HOME`` and
    ``COPILOT_HOME`` (``<home>/.copilot``). Copilot CLI >= 1.0.1 uses ``COPILOT_HOME`` and ignores
    ``XDG_CONFIG_HOME``, so the probe and the child see one account.
    """

    _common("copilot", route, _COPILOT_FIELDS, "copilot_cli_sign_in")
    parent_env = _screen_parent_env("copilot", parent_env)
    if has_copilot_login is None:
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "no stored-login probe; gh fallback refused")
    model = _model("copilot", route.get("model"))
    strip = COPILOT_STRIP_ENV + _GH_FALLBACK_ENV
    # Any other provider credential (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...) is not this run's either.
    # The allowlist then drops whatever the strip does not name (GH_PAT, AWS_*, GIT_CONFIG_*).
    # A missing home would run the probe in the ambient account and approve a different child account.
    if not isinstance(home, str) or not home:
        raise OwnLoopRouteRefused("copilot", "route_home_missing")
    # Before the env is built, so a platform that cannot enforce the probe never creates the home,
    # and a live thread never reaches fork.
    if not _probe_enforceable():
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe unenforceable")
    if _other_threads():
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe threaded")
    child, removed = _project_child(parent_env, lambda name: name in strip or _is_provider_credential(name))
    _screen_kept_values("copilot", child)
    probe_env = _copilot_runtime_env(child, home).to_dict()
    present = _probe_login(has_copilot_login, probe_deadline, probe_env)
    if not present:
        raise OwnLoopRouteRefused("copilot", "no_stored_copilot_login", "run `copilot login`; gh fallback refused")
    return OwnLoopDecision("copilot", "copilot_cli_sign_in", MappingProxyType(child), removed, model=model)


def _screen_parent_env(engine: str, parent_env: object) -> dict[str, str]:
    """Copy a parent env into a plain dict ``Popen`` can encode.

    The returned dict is the only map the rest of the check reads. A later ``.items()`` on the
    caller's mapping, or a login callback that mutates that mapping, cannot add a null or a new
    name. A non-string used to raise ``AttributeError``. A null or an ``=`` in a name used to
    crash at launch. Value encodability is checked later, and only for names that stay in the
    child: a dropped credential this process cannot encode is not a reason to refuse the run.
    """

    if not isinstance(parent_env, Mapping):
        raise OwnLoopRouteRefused(engine, "parent_env_malformed")
    snapshot: dict[str, str] = {}
    for name, value in parent_env.items():
        if (not isinstance(name, str) or not isinstance(value, str) or "=" in name
                or "\x00" in name or "\x00" in value):
            raise OwnLoopRouteRefused(engine, "parent_env_malformed")
        try:
            os.fsencode(name)
        except UnicodeEncodeError:
            raise OwnLoopRouteRefused(engine, "env_unencodable") from None
        snapshot[name] = value
    return snapshot


def _screen_kept_values(engine: str, child: Mapping[str, str]) -> None:
    """Refuse a kept value this process cannot encode. Dropped names are not checked."""

    for value in child.values():
        try:
            os.fsencode(value)
        except UnicodeEncodeError:
            raise OwnLoopRouteRefused(engine, "env_unencodable") from None


def _has_control_char(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _format_origin(parts: object) -> str:
    """``scheme://host:port`` with no userinfo, path, query or fragment. Empty when it cannot be formed."""

    try:
        host = parts.hostname  # type: ignore[attr-defined]
        port = parts.port  # type: ignore[attr-defined]
        scheme = parts.scheme  # type: ignore[attr-defined]
    except ValueError:
        return ""
    if not scheme or not host:
        return ""
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    host_text = f"[{host}]" if ":" in host else host
    if port is None:
        return f"{scheme}://{host_text}"
    return f"{scheme}://{host_text}:{port}"


def _loopback_url(base_url: object) -> str:
    if not isinstance(base_url, str) or base_url == "":
        raise OwnLoopRouteRefused("nous", "proxy_address_missing")
    # Before stripping, so a leading newline is not accepted as a loopback URL.
    if _has_control_char(base_url):
        raise OwnLoopRouteRefused("nous", "proxy_address_malformed", _PROXY_DETAIL)
    if not base_url.strip():
        raise OwnLoopRouteRefused("nous", "proxy_address_missing")
    raw = base_url.strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        raise OwnLoopRouteRefused("nous", "proxy_address_malformed", _PROXY_DETAIL) from None
    # Before the scheme check, so a secret in userinfo, the query or the fragment is not copied
    # into the refusal. An empty query or fragment (a bare ``?`` or ``#``) is not a credential.
    # ``ftp://user:secret@...`` and ``http://127.0.0.1/?token=...#secret`` both stop here.
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise OwnLoopRouteRefused("nous", "recursion_holds_no_token", "credentials in proxy URL")
    try:
        host = parts.hostname
        _ = parts.port  # raises ValueError when the port is not an integer; the text is not reused
    except ValueError:
        raise OwnLoopRouteRefused("nous", "proxy_address_malformed", _PROXY_DETAIL) from None
    if parts.scheme not in _NOUS_SCHEMES or not host:
        raise OwnLoopRouteRefused("nous", "proxy_address_malformed", _PROXY_DETAIL)
    # An IPv6 zone id (``::1%canary``) is not a loopback literal. The parser may drop it; the raw
    # netloc is what the caller sent, and it must not be copied into the refusal.
    if "%" in parts.netloc or "%" in host:
        raise OwnLoopRouteRefused("nous", "proxy_address_malformed", _PROXY_DETAIL)
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # A name (even "localhost") is not resolved here; only a loopback literal is provably local.
        raise OwnLoopRouteRefused("nous", "proxy_not_loopback", "use 127.0.0.1 or ::1") from None
    if getattr(addr, "ipv4_mapped", None) is not None:
        addr = addr.ipv4_mapped
    if not addr.is_loopback:
        raise OwnLoopRouteRefused("nous", "proxy_not_loopback", "use 127.0.0.1 or ::1")
    return raw


def _adapters(value: object) -> tuple[str, ...]:
    if value is None:
        return (NOUS_ADAPTER,)
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise OwnLoopRouteRefused("nous", "adapters_malformed")
    names = tuple(_norm(v) for v in value)
    off = sorted({n for n in names if n in _NOUS_OFF_ADAPTERS})
    if off:
        raise OwnLoopRouteRefused("nous", "adapter_must_stay_off", ",".join(off))
    if names != (NOUS_ADAPTER,):
        raise OwnLoopRouteRefused("nous", "nous_adapter_only", ",".join(names) or "empty")
    return names


HOST_USERS_UNKNOWN = ("host users unknown: interactive_users was not given, so other local users may be able "
                      "to reach the loopback proxy")


def _other_users(current_user: str | None, interactive_users: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({u for u in interactive_users if u and u != current_user}))


def nous_proxy_doctor(
    base_url: object,
    *,
    current_user: str | None = None,
    interactive_users: Iterable[str] | None = None,
) -> dict:
    """Doctor line for the Hermes proxy: report the address, refuse non-loopback, warn on shared hosts.

    Never raises; returns ``{"ok", "address", "cause", "warnings"}``. ``address`` is the canonical
    origin (``scheme://host:port``) only after the URL is an accepted loopback. A refusal reports
    an empty address: no hostname, rejected scheme, zone id, userinfo, path, query or fragment.
    ``interactive_users`` is the caller's list of interactive accounts on this host; nothing is
    probed here. Without it the doctor warns that the host's users are unknown rather than reporting
    a clean host.
    """

    warnings = _shared_host_warning(current_user, interactive_users)
    try:
        url = _loopback_url(base_url)
    except OwnLoopRouteRefused as exc:
        return {"ok": False, "address": "", "cause": exc.cause, "warnings": warnings}
    return {"ok": True, "address": _format_origin(urlsplit(url)), "cause": "", "warnings": warnings}


def _shared_host_warning(current_user: str | None, interactive_users: Iterable[str] | None) -> tuple[str, ...]:
    if interactive_users is None:  # "not checked" must not read as "nobody else is here"
        return (HOST_USERS_UNKNOWN,)
    others = _other_users(current_user, interactive_users)
    if not others:
        return ()
    return (f"other interactive users on this host can reach the loopback proxy: {', '.join(others)}",)


def check_nous_own_loop(
    route: Mapping[str, object],
    parent_env: Mapping[str, str],
    *,
    current_user: str | None = None,
    interactive_users: Iterable[str] | None = None,
) -> OwnLoopDecision:
    """Decide one Nous own-loop run. ``route``: engine="nous", auth="subscription",
    method="hermes_local_proxy", base_url=<loopback http(s) URL>, adapters=["nous"] (optional).
    The returned child env carries no Nous, xAI or OpenAI credential; Hermes holds the Portal sign-in.
    """

    _common("nous", route, _NOUS_FIELDS, "hermes_local_proxy")
    parent_env = _screen_parent_env("nous", parent_env)
    url = _loopback_url(route.get("base_url"))
    adapters = _adapters(route.get("adapters"))
    model = _model("nous", route.get("model"))
    if "/" in model:  # an adapter-prefixed id must name the nous adapter (xai/grok-3 would leave it)
        prefix = _norm(model.split("/", 1)[0])
        if prefix in _NOUS_OFF_ADAPTERS:
            raise OwnLoopRouteRefused("nous", "adapter_must_stay_off", prefix)
        if prefix != NOUS_ADAPTER:
            raise OwnLoopRouteRefused("nous", "nous_adapter_only", prefix)
    # Provider credentials, and proxy settings that would carry loopback traffic off the host.
    # The allowlist then drops whatever that strip does not name (GH_PAT, AWS_*, GIT_CONFIG_*).
    child, removed = _project_child(
        parent_env, lambda name: _is_provider_credential(name) or name.upper() in _PROXY_ENV,
    )
    _screen_kept_values("nous", child)
    return OwnLoopDecision("nous", "hermes_local_proxy", MappingProxyType(child), removed, url, adapters,
                           _shared_host_warning(current_user, interactive_users), model)


# One status line from the probe child: T, F, K, or E<exception type>. The byte before it is the
# setsid handshake. The child does not run the callable until the parent has recorded that
# process group and written the ack. A descendant that calls setpgid(0, 0) leaves the group.
_PROBE_LINE_MAX = 128
_HANDSHAKE = b"\x01"
_ACK = b"\x01"


def _probe_enforceable() -> bool:
    """True when the leader can be signalled by pidfd and the group only while it is unreaped.

    ``pidfd_open`` plus ``pidfd_send_signal`` name the leader after its numeric pid would be
    unsafe. ``waitid(P_PIDFD)`` says whether that leader is still unreaped before any
    ``killpg``. Without those, the probe refuses instead of signalling a process-group id.
    """

    return (
        os.name != "nt"
        and hasattr(os, "fork")
        and hasattr(os, "killpg")
        and hasattr(os, "setsid")
        and hasattr(os, "pidfd_open")
        and hasattr(os, "waitid")
        and hasattr(os, "P_PIDFD")
        and hasattr(os, "WNOWAIT")
        and _pidfd_send_available()
    )


def _other_threads() -> bool:
    """True when some thread other than this one is alive.

    ``threading.enumerate`` misses a thread started with ``_thread``. ``sys._current_frames`` sees
    Python threads, and ``/proc/self/task`` sees native threads. ``fork`` copies only this thread.
    A lock another thread holds is never released in the child, so the probe deadlocks. The
    callable is an arbitrary closure and cannot be sent to a forkserver.
    """

    current = threading.current_thread()
    if any(thread is not current and thread.is_alive() for thread in threading.enumerate()):
        return True
    if len(sys._current_frames()) > 1:
        return True
    task_dir = "/proc/self/task"
    if os.path.isdir(task_dir):
        try:
            tasks = os.listdir(task_dir)
        except OSError:
            tasks = []
        if len(tasks) > 1:
            return True
    return False


def _probe_login(probe: Callable[[], bool], deadline: float, env: Mapping[str, str] | None = None) -> bool:
    """Run ``probe`` until ``deadline``. ``SystemExit`` and a missed deadline are not a login.

    ``probe`` stays a callable: it is the caller's check, not a command this module can turn into
    argv. On a platform that can ``fork`` and hold a pidfd, the callable runs in a new process
    group. Every exit, including a login that is present, signals that group: SIGTERM, then a short
    grace still inside ``deadline``, then SIGKILL. The group is the one confirmed by the handshake
    byte. A descendant that calls ``setpgid(0, 0)`` leaves the group and is not signalled. Where
    that group cannot be enforced, the route refuses ``probe unenforceable``. Where another thread
    is alive, including a ``_thread`` or native thread, it refuses ``probe threaded`` and does not
    fork. A thread started after that check and before ``fork`` is still a race; refusing is what a
    non-picklable callable can do.
    """

    if (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)
            or deadline <= 0 or deadline > LOGIN_PROBE_DEADLINE_S):
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe deadline")
    if not _probe_enforceable():
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe unenforceable")
    if _other_threads():
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe threaded")
    return _probe_login_group(probe, deadline, env)


def _direct_child_pids() -> set[int]:
    """Pids whose parent is this process. Empty when ``/proc`` cannot be read."""

    me = os.getpid()
    found: set[int] = set()
    try:
        names = os.listdir("/proc")
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/status", encoding="ascii") as handle:
                text = handle.read()
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("PPid:"):
                if int(line.split()[1]) == me:
                    found.add(int(name))
                break
    return found


def _kill_untracked(before: set[int]) -> None:
    """Kill and reap direct children that appeared after ``before``.

    Used when ``fork`` itself raises after creating the child, so the parent never received the
    pid. SIGKILL is retried if a ``KeyboardInterrupt`` arrives during the signal.
    """

    for child in _direct_child_pids() - before:
        for _ in range(8):
            try:
                os.kill(child, signal.SIGKILL)
                break
            except KeyboardInterrupt:
                continue
            except OSError:
                break
        try:
            os.waitpid(child, 0)
        except ChildProcessError:
            pass
        except KeyboardInterrupt:
            try:
                os.waitpid(child, 0)
            except (ChildProcessError, KeyboardInterrupt):
                pass


def _probe_login_group(
    probe: Callable[[], bool], deadline: float, env: Mapping[str, str] | None,
) -> bool:
    """Run ``probe`` in its own process group and signal that group on every exit path.

    Pipe fds, the worker and its pidfd are released on every exit, including ``KeyboardInterrupt``
    raised by ``fork`` or during the later signal. The pidfd stays open until after the last
    signal so the process-group id cannot be reused if another handler reaps the worker first.
    """

    status_r = status_w = ack_r = ack_w = -1
    pid: int | None = None
    pgid: int | None = None
    pidfd: int | None = None
    selector: selectors.BaseSelector | None = None
    outcome: dict[str, object] = {}
    started = time.monotonic()
    before_fork: set[int] = set()
    try:
        try:
            status_r, status_w = os.pipe()
            ack_r, ack_w = os.pipe()
            for fd in (status_r, status_w, ack_r, ack_w):
                os.set_inheritable(fd, False)
            os.set_blocking(status_r, False)
            if _other_threads():
                raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe threaded")
            before_fork = _direct_child_pids()
            pid = os.fork()
            if pid == 0:
                _probe_child(probe, status_r, status_w, ack_r, ack_w, env)
            try:
                pidfd = os.pidfd_open(pid, 0)
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe unenforceable") from None
            _close_fd(status_w)
            status_w = -1
            _close_fd(ack_r)
            ack_r = -1
            selector = selectors.DefaultSelector()
            selector.register(status_r, selectors.EVENT_READ)
            pgid, reason = _read_handshake(selector, status_r, pid, started, deadline)
            if pgid is None:
                detail = "probe deadline" if reason == "deadline" else "probe failed"
                outcome["refused"] = OwnLoopRouteRefused("copilot", "copilot_login_unknown", detail)
            else:
                # The group id is recorded before the child is allowed to run the callable.
                _write_ack(ack_w)
                line, complete, timed_out = _read_status(selector, status_r, started, deadline)
                if timed_out or not complete:
                    detail = "probe deadline" if timed_out else "probe failed"
                    outcome["refused"] = OwnLoopRouteRefused("copilot", "copilot_login_unknown", detail)
                else:
                    outcome["line"] = line
        except OwnLoopRouteRefused as exc:
            outcome["refused"] = exc
        except BaseException as exc:
            outcome["error"] = exc
    finally:
        try:
            _stop_probe(pid, pgid, started, deadline, pidfd)
        finally:
            if selector is not None:
                selector.close()
            for fd in (status_r, status_w, ack_r, ack_w):
                if fd >= 0:
                    _close_fd(fd)
            if pidfd is not None and pidfd >= 0:
                _close_fd(pidfd)
            if pid is None:
                _kill_untracked(before_fork)
            else:
                _reap_probe(pid)
    return _finish_probe(outcome)


def _probe_child(
    probe: Callable[[], bool], status_r: int, status_w: int, ack_r: int, ack_w: int,
    env: Mapping[str, str] | None,
) -> None:
    """Never returns. The callable runs only after the parent acks the recorded process group."""

    try:
        _close_fd(status_r)
        _close_fd(ack_w)
        os.setsid()
        _write_all(status_w, _HANDSHAKE)
        if not _read_ack(ack_r):
            os._exit(0)
        if env is not None:
            os.environ.clear()
            os.environ.update(env)
        _write_all(status_w, _probe_status(probe))
    except BaseException:
        pass
    os._exit(0)


def _read_ack(fd: int) -> bool:
    """True when the parent wrote the ack. EOF means the parent died first: do not run the callable."""

    try:
        os.set_blocking(fd, True)
    except OSError:
        return False
    while True:
        try:
            chunk = os.read(fd, 1)
        except InterruptedError:
            continue
        except OSError:
            return False
        return chunk == _ACK


def _probe_status(probe: Callable[[], bool]) -> bytes:
    try:
        value = probe()
    except KeyboardInterrupt:
        return b"K\n"
    except BaseException as exc:
        name = type(exc).__name__
        if name.isascii() and name.isidentifier():
            return b"E" + name.encode("ascii") + b"\n"
        return b"EException\n"
    return b"T\n" if value is True else b"F\n"


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            return
        view = view[written:]


def _write_ack(fd: int) -> None:
    try:
        _write_all(fd, _ACK)
    except OSError:
        pass


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _kill_grace(deadline: float) -> float:
    """Leave a read window even when ``deadline`` is at most ``_PROBE_GRACE_S``."""

    return min(_PROBE_GRACE_S, max(0.0, deadline) * 0.2)


def _result_deadline(started: float, deadline: float) -> float:
    """When the parent stops waiting, leaving the kill grace inside the original deadline."""

    return started + deadline - _kill_grace(deadline)


def _wait_readable(selector: selectors.BaseSelector, stop_at: float) -> bool:
    while True:
        remaining = stop_at - time.monotonic()
        if remaining <= 0:
            return False
        try:
            return bool(selector.select(remaining))
        except InterruptedError:
            continue


def _read_handshake(
    selector: selectors.BaseSelector, fd: int, pid: int, started: float, deadline: float,
) -> tuple[int | None, str]:
    """The confirmed process-group id, or ``None`` and ``deadline`` / ``failed``.

    The handshake byte means the child has called ``setsid``, so its process-group id is its pid.
    That id is never this process's group. The callable has not run yet: it waits for the ack.
    """

    stop_at = _result_deadline(started, deadline)
    while True:
        if not _wait_readable(selector, stop_at):
            return None, "deadline"
        try:
            chunk = os.read(fd, 1)
        except BlockingIOError:
            continue
        except InterruptedError:
            continue
        except OSError:
            return None, "failed"
        if not chunk:
            return None, "failed"
        if chunk != _HANDSHAKE or pid <= 1 or pid == os.getpgrp():
            return None, "failed"
        return pid, ""


def _read_status(
    selector: selectors.BaseSelector, fd: int, started: float, deadline: float,
) -> tuple[bytes, bool, bool]:
    """``(line, complete, timed_out)`` from the status line that follows the handshake."""

    stop_at = _result_deadline(started, deadline)
    buf = bytearray()
    while b"\n" not in buf and len(buf) <= _PROBE_LINE_MAX:
        if not _wait_readable(selector, stop_at):
            line, complete = _split_probe_line(buf)
            return line, complete, True
        try:
            chunk = os.read(fd, 256)
        except BlockingIOError:
            continue
        except InterruptedError:
            continue
        except OSError:
            return b"", False, False
        if not chunk:
            line, complete = _split_probe_line(buf)
            return line, complete, False
        buf.extend(chunk)
    line, complete = _split_probe_line(buf)
    return line, complete, False


def _split_probe_line(buf: bytearray) -> tuple[bytes, bool]:
    raw = bytes(buf)
    if b"\n" not in raw:
        return raw, False
    return raw.split(b"\n", 1)[0], True


def _interpret_probe_line(line: bytes) -> bool:
    if line == b"T":
        return True
    if line == b"F":
        return False
    if line == b"K":
        raise KeyboardInterrupt
    if line.startswith(b"E"):
        name = line[1:].decode("ascii", "replace")
        if name.isidentifier():
            raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", name) from None
    raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe failed") from None


def _finish_probe(outcome: dict[str, object]) -> bool:
    error = outcome.get("error")
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        raise error
    if error is not None:
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe failed") from None
    refused = outcome.get("refused")
    if isinstance(refused, OwnLoopRouteRefused):
        raise refused
    line = outcome.get("line")
    if not isinstance(line, bytes):
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe failed") from None
    return _interpret_probe_line(line)


_PIDFD_SEND: bool | None = None
_LIBC = None


def _pidfd_send_available() -> bool:
    """True when the leader can be signalled by pidfd, not by its numeric pid."""

    global _PIDFD_SEND
    if _PIDFD_SEND is None:
        if hasattr(os, "pidfd_send_signal"):
            _PIDFD_SEND = True
        else:
            try:
                import ctypes
                _PIDFD_SEND = hasattr(ctypes.CDLL(None, use_errno=True), "pidfd_send_signal")
            except OSError:
                _PIDFD_SEND = False
    return _PIDFD_SEND


def _pidfd_send_signal(pidfd: int, sig: int) -> None:
    """Signal the task ``pidfd`` refers to. A reused numeric pid is not a target.

    ``os.pidfd_send_signal`` is used when this interpreter provides it. Otherwise the libc
    function of the same name is used. ``ESRCH`` means that task is already gone.
    ``InterruptedError`` is retried.
    """

    while True:
        try:
            send = getattr(os, "pidfd_send_signal", None)
            if send is not None:
                send(pidfd, sig)
            else:
                _pidfd_send_signal_libc(pidfd, sig)
            return
        except InterruptedError:
            continue
        except ProcessLookupError:
            return


def _pidfd_send_signal_libc(pidfd: int, sig: int) -> None:
    global _LIBC
    import ctypes
    if _LIBC is None:
        _LIBC = ctypes.CDLL(None, use_errno=True)
        _LIBC.pidfd_send_signal.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
        _LIBC.pidfd_send_signal.restype = ctypes.c_int
    if _LIBC.pidfd_send_signal(pidfd, sig, None, 0) == 0:
        return
    err = ctypes.get_errno()
    raise OSError(err, os.strerror(err))


def _leader_state(pidfd: int) -> str:
    """``running``, ``zombie``, or ``reaped``. ``WNOWAIT`` observes the pidfd and does not reap."""

    while True:
        try:
            info = os.waitid(os.P_PIDFD, pidfd, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except InterruptedError:
            continue
        except ChildProcessError:
            return "reaped"
        except OSError:
            return "reaped"
        return "zombie" if info is not None else "running"


def _killpg_if_unreaped(pidfd: int, pgid: int | None, sig: int) -> None:
    """``killpg`` only while ``waitid`` says the leader is still unreaped.

    A reaped leader's process-group id can be reused, so this returns without signalling.
    ``SIGCHLD`` is blocked by the caller across this check and the signal. This process's
    group is never signalled. ``InterruptedError`` is retried.
    """

    if pgid is None or pgid <= 1 or pgid == os.getpgrp():
        return
    if _leader_state(pidfd) == "reaped":
        return
    while True:
        try:
            os.killpg(pgid, sig)
            return
        except InterruptedError:
            continue
        except ProcessLookupError:
            return


def _deliver(pidfd: int, pgid: int | None, sig: int) -> None:
    """Signal the leader through its pidfd, then the group only if the leader is unreaped."""

    _pidfd_send_signal(pidfd, sig)
    _killpg_if_unreaped(pidfd, pgid, sig)


def _deliver_forcing(pidfd: int, pgid: int | None, sig: int) -> None:
    """``_deliver`` that retries ``KeyboardInterrupt`` so the final signal still lands."""

    for _ in range(8):
        try:
            _deliver(pidfd, pgid, sig)
            return
        except KeyboardInterrupt:
            continue
    _deliver(pidfd, pgid, sig)


def _group_alive(pidfd: int, pgid: int | None) -> bool:
    """True when the leader is unreaped and ``killpg(0)`` sees a live member.

    The ``waitid`` check comes first, so a reaped leader is not passed to ``killpg``.
    """

    if _leader_state(pidfd) == "reaped":
        return False
    if pgid is None or pgid <= 1 or pgid == os.getpgrp():
        return False
    while True:
        try:
            os.killpg(pgid, 0)
            return True
        except InterruptedError:
            continue
        except ProcessLookupError:
            return False
        except OSError:
            return True


def _stop_probe(pid: int | None, pgid: int | None, started: float, deadline: float, pidfd: int | None) -> None:
    """Signal the original process group confirmed by the handshake. The caller reaps after this.

    The leader is signalled with ``pidfd_send_signal``. ``killpg`` runs only after
    ``waitid(P_PIDFD, WNOHANG|WNOWAIT)`` shows that leader is still unreaped. SIGTERM, then
    SIGKILL if a live member is still in that process group when the grace runs out. A
    descendant that calls setpgid(0, 0) leaves the process group and is not signalled. This is
    not session cleanup: that descendant stays in the session. The grace is at most 20% of the
    deadline, and never more than ``_PROBE_GRACE_S`` or past ``started + deadline``. A zombie
    or reaped leader skips the grace. The direct child is not reaped here. SIGKILL still runs
    when SIGTERM raises ``KeyboardInterrupt``. SIGINT and SIGCHLD are blocked for the duration
    so a real interrupt cannot skip that final signal and a handler cannot reap the leader
    between the ``waitid`` check and ``killpg``.
    """

    if pid is None or pid <= 0 or pid == os.getpid():
        return
    if pidfd is None:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        return
    blocked: set[int] = set()
    if hasattr(signal, "pthread_sigmask"):
        try:
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGCHLD})
            blocked = {signal.SIGINT, signal.SIGCHLD}
        except (OSError, ValueError):
            blocked = set()
    pending = False
    try:
        try:
            _deliver(pidfd, pgid, signal.SIGTERM)
        except KeyboardInterrupt:
            pending = True
        finally:
            if not pending and _leader_state(pidfd) == "running" and _group_alive(pidfd, pgid):
                try:
                    remaining = (started + deadline) - time.monotonic()
                    grace = min(_kill_grace(deadline), max(0.0, remaining))
                    stop = time.monotonic() + grace
                    while time.monotonic() < stop and _leader_state(pidfd) == "running" and _group_alive(pidfd, pgid):
                        time.sleep(0.01)
                except KeyboardInterrupt:
                    pending = True
            _deliver_forcing(pidfd, pgid, signal.SIGKILL)
    finally:
        if blocked:
            try:
                signal.pthread_sigmask(signal.SIG_UNBLOCK, blocked)
            except OSError:
                pass
    if pending:
        raise KeyboardInterrupt


def _reap_probe(pid: int | None) -> None:
    if pid is None or pid <= 0:
        return
    end = time.monotonic() + 0.5
    while True:
        try:
            got, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if got == pid:
            return
        if time.monotonic() >= end:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                return
            return
        time.sleep(0.01)


def _is_git_config_injection(upper: str) -> bool:
    return upper in ("GIT_CONFIG_KEY", "GIT_CONFIG_VALUE") or upper.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))


def _is_provider_credential(name: str) -> bool:
    upper = name.upper()
    if upper.startswith(("NOUS_", "XAI_", "GROK_", "HERMES_")):
        return True
    if upper.endswith(("_API_KEY", "_TOKEN", "_AUTH_TOKEN")) or upper in _CREDENTIAL_ENV:
        return True
    return _is_git_config_injection(upper)


def _copilot_runtime_env(child: Mapping[str, str], home: str):
    """The environment the Copilot child and the login probe both receive.

    ``COPILOT_HOME`` is ``<home>/.copilot``. A parent ``COPILOT_HOME`` or ``XDG_CONFIG_HOME`` does
    not select a different account. Copilot CLI >= 1.0.1 reads ``COPILOT_HOME`` and ignores
    ``XDG_CONFIG_HOME``. ``home`` is validated before ``.copilot`` is created, so an unsafe home
    leaves no directory behind. The directory is created mode ``0700`` when it is absent. An
    existing entry, including a symlink, is left in place and must already be a private directory.
    A null or a character this process cannot encode is refused before ``Path`` or ``lstat`` sees it.
    """

    if not isinstance(home, str) or "\x00" in home:
        raise EnvRefused("HOME", "home_unsafe")
    try:
        os.fsencode(home)
    except UnicodeEncodeError:
        raise EnvRefused("HOME", "value_unencodable") from None
    try:
        private = _private_dir(home)
    except (ValueError, UnicodeEncodeError):
        raise EnvRefused("HOME", "home_unsafe") from None
    if not private:
        raise EnvRefused("HOME", "home_unsafe")
    copilot_home = os.path.join(home, ".copilot")
    if not os.path.lexists(copilot_home):
        try:
            os.mkdir(copilot_home, 0o700)
        except FileExistsError:
            pass
        else:
            try:
                os.chmod(copilot_home, 0o700)
            except OSError:
                pass
    parent = {name: value for name, value in child.items() if name != "HOME"}
    return build_child_env(
        "copilot", home=home, auth_mode="subscription", parent=parent,
        overrides={"COPILOT_HOME": copilot_home},
    )


def _refusal_origin(url: str) -> str:
    """Canonical origin for a refusal. The path, query and fragment are not copied into the text."""

    try:
        origin = _format_origin(urlsplit(url))
    except (TypeError, ValueError):
        return "base url"
    return origin or "base url"


def _require_checked_argv(argv: Sequence[str], decision: OwnLoopDecision) -> None:
    """Refuse caller argv that does not carry the decision's model, base URL and adapters.

    One scan consumes each flag's value, so a model value spelled ``--adapter`` is not also an
    adapter flag. A refused base URL is reported as its origin only.
    """

    from recursion_core.subscription_routes.policy import _scan_argv

    models, base_urls, adapters, saw_model = _scan_argv(argv, decision.engine)
    if decision.model:
        if not models or any(value != decision.model for value in models):
            raise OwnLoopRouteRefused(decision.engine, "model_not_bound", decision.model)
    elif saw_model:
        raise OwnLoopRouteRefused(decision.engine, "model_not_bound")
    if decision.base_url:
        if base_urls != [decision.base_url]:
            raise OwnLoopRouteRefused(decision.engine, "base_url_not_bound", _refusal_origin(decision.base_url))
    if decision.adapters:
        if adapters != list(decision.adapters):
            raise OwnLoopRouteRefused(decision.engine, "adapter_not_bound")


def _spawn_checked(argv: Sequence[str], decision: OwnLoopDecision, home: str, cwd: str | None) -> subprocess.Popen:
    """Rebuild the checked child through ``build_child_env`` and start it. ``Popen`` is read at the call.

    Copilot uses the Copilot profile, so ``NO_PROXY`` is matched against Copilot's hosts and
    ``COPILOT_HOME`` is the route home's ``.copilot``. Nous keeps the Codex profile: its proxies
    were already removed. Caller argv must name the checked model, base URL and adapters.
    """

    _require_checked_argv(argv, decision)
    if decision.engine == "copilot":
        validated = _copilot_runtime_env(decision.child_env, home)
    else:
        parent = {name: value for name, value in decision.child_env.items() if name != "HOME"}
        validated = build_child_env("codex", home=home, auth_mode="subscription", parent=parent)
    return spawn(list(argv), validated, cwd=cwd, popen=subprocess.Popen)


def spawn_copilot_own_loop(
    route: Mapping[str, object],
    parent_env: Mapping[str, str],
    argv: Sequence[str],
    *,
    home: str,
    has_copilot_login: Callable[[], bool] | None = None,
    cwd: str | None = None,
) -> tuple[OwnLoopDecision, subprocess.Popen]:
    """``check_copilot_own_loop`` then ``child_env.spawn``. A refused env never reaches ``Popen``.

    The login probe sees the same ``HOME`` and ``COPILOT_HOME`` as the child. ``argv`` is copied
    once here and that copy is what the check and the launch both see.
    """

    argv = tuple(argv)
    decision = check_copilot_own_loop(
        route, parent_env, has_copilot_login=has_copilot_login, home=home,
    )
    return decision, _spawn_checked(argv, decision, home, cwd)

def spawn_nous_own_loop(
    route: Mapping[str, object],
    parent_env: Mapping[str, str],
    argv: Sequence[str],
    *,
    home: str,
    current_user: str | None = None,
    interactive_users: Iterable[str] | None = None,
    cwd: str | None = None,
) -> tuple[OwnLoopDecision, subprocess.Popen]:
    """``check_nous_own_loop`` then ``child_env.spawn``. A refused env never reaches ``Popen``.

    ``argv`` is copied once here and that copy is what the check and the launch both see.
    """

    argv = tuple(argv)
    decision = check_nous_own_loop(
        route, parent_env, current_user=current_user, interactive_users=interactive_users,
    )
    return decision, _spawn_checked(argv, decision, home, cwd)
