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
With no probe the route refuses. Where a new process group cannot be enforced, or ``SIGCHLD``
is ignored or marked ``SA_NOCLDWAIT``, it refuses ``probe unenforceable``. Another live thread
refuses ``probe threaded`` before ``fork``.

The child environment is not a copy of the parent. After the Copilot or Nous strip, only names on
``child_env``'s base allowlist remain, and a proxy value that carries userinfo or fails the proxy
rule is removed. ``spawn_copilot_own_loop`` and ``spawn_nous_own_loop`` then rebuild that mapping
with ``build_child_env`` and start the process through ``child_env.spawn``.
"""

from __future__ import annotations

import ctypes
import errno
import ipaddress
import math
import os
import platform
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

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
    if not isinstance(parent_env, Mapping):
        raise OwnLoopRouteRefused("copilot", "parent_env_malformed")
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
    probe_env = _copilot_runtime_env(child, home).to_dict()
    present = _probe_login(has_copilot_login, probe_deadline, probe_env)
    if not present:
        raise OwnLoopRouteRefused("copilot", "no_stored_copilot_login", "run `copilot login`; gh fallback refused")
    return OwnLoopDecision("copilot", "copilot_cli_sign_in", MappingProxyType(child), removed, model=model)


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
    if not isinstance(parent_env, Mapping):
        raise OwnLoopRouteRefused("nous", "parent_env_malformed")
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
    return OwnLoopDecision("nous", "hermes_local_proxy", MappingProxyType(child), removed, url, adapters,
                           _shared_host_warning(current_user, interactive_users), model)


# One status line from the probe child: T, F, K, or E<exception type>. The byte before it is the
# setsid handshake. The child does not run the callable until the parent has recorded that
# process group and written the ack. A descendant that calls setpgid(0, 0) leaves the group.
_PROBE_LINE_MAX = 128
_HANDSHAKE = b"\x01"
_ACK = b"\x01"


def _probe_enforceable() -> bool:
    """True when the leader can be signalled by pidfd and this thread can own the reap.

    ``pidfd_open`` plus ``pidfd_send_signal`` name the leader after its numeric pid would be
    unsafe. ``waitid(P_PIDFD)`` says whether that leader is still unreaped before any
    ``killpg``. The probe blocks ``SIGINT`` and ``SIGCHLD`` across fork and cleanup, so it
    also needs ``pthread_sigmask``. Without those, the probe refuses instead of signalling
    a process-group id.
    """

    return (
        os.name != "nt"
        and hasattr(os, "fork")
        and hasattr(os, "killpg")
        and hasattr(os, "setsid")
        and hasattr(os, "pidfd_open")
        and hasattr(os, "waitid")
        and hasattr(os, "P_PIDFD")
        and hasattr(os, "P_PID")
        and hasattr(os, "WNOWAIT")
        and hasattr(signal, "pthread_sigmask")
        and _pidfd_send_available()
    )


def _other_threads() -> bool:
    """True when some thread other than this one is alive.

    ``threading.enumerate`` misses a thread started with ``_thread``. ``sys._current_frames`` sees
    Python threads, and ``/proc/self/task`` sees native threads. ``fork`` copies only this thread.
    A lock another thread holds is never released in the child, so the probe deadlocks. The
    callable is an arbitrary closure and cannot be sent to a forkserver. When ``/proc/self/task``
    exists but cannot be listed, a native thread may be hidden, so that is treated as another
    thread. The "no native threads" fallback is only for a missing ``/proc``.
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
            return True
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
    that group cannot be enforced, the route refuses ``probe unenforceable``. ``SIGCHLD`` ignored
    or ``SA_NOCLDWAIT`` is the same refusal, before ``fork``: the kernel would auto-reap the
    worker and a later ``killpg`` could name a reused process-group id. ``signal.getsignal``
    does not show ``SA_NOCLDWAIT``; the flags come from ``sigaction``. That read runs only on
    Linux LP64 x86_64 or aarch64 with glibc or musl. Any other ABI refuses before ``libc`` is
    called. Where another thread is
    alive, including a ``_thread`` or native thread, it refuses ``probe threaded`` and does not
    fork. A thread started
    after that check and before ``fork`` is still a race; refusing is what a non-picklable callable
    can do.
    """

    if (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)
            or deadline <= 0 or deadline > LOGIN_PROBE_DEADLINE_S):
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe deadline")
    if not _probe_enforceable():
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe unenforceable")
    if _other_threads():
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe threaded")
    return _probe_login_group(probe, deadline, env)


def _pid_state(pid: int) -> str:
    """``running``, ``zombie``, ``reaped``, or ``unknown``. ``WNOWAIT`` does not reap.

    ``ECHILD`` is the only error that means this process does not own the pid. Any other
    ``waitid`` error is ``unknown``: the caller still signals that pid and the probe refuses.
    """

    while True:
        try:
            info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except InterruptedError:
            continue
        except ChildProcessError:
            return "reaped"
        except OSError as exc:
            if exc.errno == errno.ECHILD:
                return "reaped"
            return "unknown"
        return "zombie" if info is not None else "running"


def _signal_owned_pid(pid: int, sig: int) -> bool:
    """Signal one identified child only while ``waitid`` still says this process owns it.

    Returns true when ``waitid(P_PID)`` failed for a reason other than ``ECHILD``. The pid is
    still signalled in that case. A reaped pid is not signalled: that number can already belong
    to another process. ``SIGCHLD`` is blocked and is not ignored for the whole probe, so a
    child this sees as unreaped cannot be collected between the check and the signal.
    ``KeyboardInterrupt`` is retried.
    """

    if pid <= 0 or pid == os.getpid():
        return False
    state = _pid_state(pid)
    if state == "reaped":
        return False
    for _ in range(8):
        try:
            os.kill(pid, sig)
            return state == "unknown"
        except KeyboardInterrupt:
            continue
        except OSError:
            return state == "unknown"
    try:
        os.kill(pid, sig)
    except OSError:
        pass
    return state == "unknown"


def _reap_owned(pid: int) -> None:
    while True:
        try:
            os.waitpid(pid, 0)
            return
        except InterruptedError:
            continue
        except ChildProcessError:
            return


_SA_NOCLDWAIT = 2
_SIG_IGN_HANDLER = 1
# LP64 Linux (glibc and musl, x86_64 and aarch64): 8-byte handler, 128-byte mask, then flags.
# The buffer is larger than that struct so a short read cannot overflow it.
_SIGACTION_BYTES = 256
_SA_FLAGS_OFFSET = 8 + 128
_SIGACTION_MACHINES = frozenset({"x86_64", "aarch64"})
# The mapped file's basename, not a substring of the path. A notes file named
# libc.musl-notes.txt is not musl. Two architectures at once is ambiguous.
_MUSL_BASENAME = re.compile(rb"^(?:ld-musl-(x86_64|aarch64)|libc\.musl-(x86_64|aarch64))\.so\.1$")


def _sigaction_abi_supported() -> bool:
    """True only for the ``sigaction`` layout this read was checked against.

    Anything else refuses before ``libc`` is called. A 32-bit ``c_ulong`` mask, or a
    machine that puts the flags somewhere else, would otherwise miss ``SA_NOCLDWAIT``.
    """

    if sys.platform != "linux" or ctypes.sizeof(ctypes.c_void_p) != 8:
        return False
    if platform.machine() not in _SIGACTION_MACHINES:
        return False
    return _libc_family() in {"glibc", "musl"}


def _mapped_basenames(blob: bytes) -> list[bytes]:
    """Basenames of file mappings. ``[heap]`` is not a name.

    A path ending in `` (deleted)`` is not evidence. That suffix is what proc adds for a
    mapping that is gone, and it is also a legal live filename, so stripping it would
    accept a file that is not the musl loader.
    """

    names: list[bytes] = []
    for line in blob.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        path = parts[5]
        if not path or path.startswith(b"[") or path.endswith(b" (deleted)"):
            continue
        names.append(path.rsplit(b"/", 1)[-1])
    return names


def _libc_family() -> str | None:
    """``glibc`` or ``musl`` when this process can be told apart from any other C library.

    musl is the basename ``ld-musl-<machine>.so.1`` or ``libc.musl-<machine>.so.1`` for this
    machine only. Any other spelling, or both machines, is not musl.
    """

    try:
        version = os.confstr("CS_GNU_LIBC_VERSION")
    except (ValueError, OSError):
        version = None
    if isinstance(version, str) and version.startswith("glibc"):
        return "glibc"
    try:
        with open("/proc/self/maps", "rb") as handle:
            blob = handle.read(4 * 1024 * 1024)
    except OSError:
        return None
    arches: set[bytes] = set()
    for name in _mapped_basenames(blob):
        match = _MUSL_BASENAME.fullmatch(name)
        if match is not None:
            arches.add(next(group for group in match.groups() if group is not None))
    machine = platform.machine().encode()
    if arches == {machine}:
        return "musl"
    return None


def _sigchld_auto_reaps() -> bool:
    """True when the kernel collects ``SIGCHLD`` children without a ``wait``.

    ``signal.getsignal`` returns ``SIG_DFL`` when ``SA_NOCLDWAIT`` is set through
    ``sigaction``. ``SIG_IGN`` and that flag both drop the zombie, so the process-group
    id can be reused. An ABI this read was not written for, or a failed read, refuses:
    the probe cannot prove it owns the reap. ``libc`` is not called in that case.
    """

    if not _sigaction_abi_supported():
        return True
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.sigaction.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        libc.sigaction.restype = ctypes.c_int
        old = ctypes.create_string_buffer(_SIGACTION_BYTES)
        if libc.sigaction(signal.SIGCHLD, None, old) != 0:
            return True
    except (AttributeError, OSError):
        return True
    raw = old.raw
    handler = int.from_bytes(raw[:8], sys.byteorder)
    flags = int.from_bytes(raw[_SA_FLAGS_OFFSET:_SA_FLAGS_OFFSET + 4], sys.byteorder)
    return handler == _SIG_IGN_HANDLER or bool(flags & _SA_NOCLDWAIT)


def _probe_login_group(
    probe: Callable[[], bool], deadline: float, env: Mapping[str, str] | None,
) -> bool:
    """Run ``probe`` in its own process group and signal that group on every exit path.

    Pipe fds, the identified worker and its pidfd are released on every exit, including
    ``KeyboardInterrupt`` raised after ``fork`` has returned that pid. No other child is
    signalled. ``SIGINT`` and ``SIGCHLD`` are blocked from before ``fork`` until after the
    group is signalled and the worker is reaped, then the caller's mask is restored with
    ``SIG_SETMASK``. A failed restore refuses ``probe unenforceable`` after cleanup.
    ``SIGCHLD`` ignored refuses before ``fork``. The pidfd is opened and
    stored while that mask is held, and it stays open until after the last signal.
    """

    status_r = status_w = ack_r = ack_w = -1
    pid: int | None = None
    pgid: int | None = None
    pidfd: int | None = None
    selector: selectors.BaseSelector | None = None
    outcome: dict[str, object] = {}
    started = time.monotonic()
    old_mask: set[int] | None = None
    wait_bad = False
    try:
        try:
            # Held across fork, pidfd_open and the store of that fd, the group signals and our
            # reap. A handler cannot run in that window. SIG_IGN would still auto-reap, so it
            # refuses before the callable is acked.
            old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGCHLD})
            if _sigchld_auto_reaps():
                raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe unenforceable")
            status_r, status_w = os.pipe()
            ack_r, ack_w = os.pipe()
            for fd in (status_r, status_w, ack_r, ack_w):
                os.set_inheritable(fd, False)
            os.set_blocking(status_r, False)
            if _other_threads():
                raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe threaded")
            pid = os.fork()
            if pid == 0:
                try:
                    signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
                    _probe_child(probe, status_r, status_w, ack_r, ack_w, env)
                except BaseException:
                    os._exit(1)
                os._exit(1)
            try:
                # One store, with SIGINT blocked, so a signal cannot arrive between the open
                # and the name that ``finally`` closes.
                fd = os.pidfd_open(pid, 0)
                pidfd = fd
            except OSError:
                _signal_owned_pid(pid, signal.SIGKILL)
                _reap_owned(pid)
                pid = None
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
            elif _leader_state(pidfd) == "error":
                # waitid(P_PIDFD) is not usable. Do not ack, so the callable never runs.
                outcome["refused"] = OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe unenforceable")
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
            wait_bad = _stop_probe(pid, pgid, started, deadline, pidfd)
        finally:
            if selector is not None:
                selector.close()
            for fd in (status_r, status_w, ack_r, ack_w):
                if fd >= 0:
                    _close_fd(fd)
            if pidfd is not None and pidfd >= 0:
                _close_fd(pidfd)
            if pid is not None:
                wait_bad = _reap_probe(pid) or wait_bad
            if old_mask is not None:
                try:
                    signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
                except OSError:
                    # The caller's SIGINT and SIGCHLD may still be blocked. A T line is not a login.
                    if not isinstance(outcome.get("error"), (KeyboardInterrupt, SystemExit)):
                        wait_bad = True
        if wait_bad and not isinstance(outcome.get("error"), (KeyboardInterrupt, SystemExit)):
            outcome.pop("error", None)
            outcome.pop("line", None)
            outcome["refused"] = OwnLoopRouteRefused(
                "copilot", "copilot_login_unknown", "probe unenforceable",
            )
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


_LIBC = None
_LIBC_PIDFD: bool | None = None


def _select_pidfd_sender(signal_send: object, os_send: object, libc_send: object) -> object:
    """``signal.pidfd_send_signal``, then ``os.pidfd_send_signal``, then libc."""

    if signal_send is not None:
        return signal_send
    if os_send is not None:
        return os_send
    return libc_send


def _libc_pidfd_send_available() -> bool:
    global _LIBC_PIDFD
    if _LIBC_PIDFD is None:
        try:
            import ctypes
            _LIBC_PIDFD = hasattr(ctypes.CDLL(None, use_errno=True), "pidfd_send_signal")
        except OSError:
            _LIBC_PIDFD = False
    return _LIBC_PIDFD


def _pidfd_send_available() -> bool:
    """True when the leader can be signalled by pidfd, not by its numeric pid."""

    return _select_pidfd_sender(
        getattr(signal, "pidfd_send_signal", None),
        getattr(os, "pidfd_send_signal", None),
        _pidfd_send_signal_libc if _libc_pidfd_send_available() else None,
    ) is not None


def _pidfd_send_signal(pidfd: int, sig: int) -> None:
    """Signal the task ``pidfd`` refers to. A reused numeric pid is not a target.

    The stdlib ``signal.pidfd_send_signal`` is preferred. ``os.pidfd_send_signal`` is next.
    The libc function is only the fallback. ``ESRCH`` means that task is already gone.
    ``InterruptedError`` is retried.
    """

    while True:
        try:
            send = _select_pidfd_sender(
                getattr(signal, "pidfd_send_signal", None),
                getattr(os, "pidfd_send_signal", None),
                _pidfd_send_signal_libc if _libc_pidfd_send_available() else None,
            )
            if send is None:
                raise OSError(errno.ENOSYS, "pidfd_send_signal")
            send(pidfd, sig)  # type: ignore[operator]
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


def _leader_state(pidfd: int, bad: list[int] | None = None) -> str:
    """``running``, ``zombie``, ``reaped``, or ``error``. ``WNOWAIT`` does not reap.

    ``ECHILD`` is the only error that means the leader was collected. Any other
    ``waitid`` error is ``error``: the group is still signalled, and ``bad`` records it
    so the probe refuses.
    """

    while True:
        try:
            info = os.waitid(os.P_PIDFD, pidfd, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except InterruptedError:
            continue
        except ChildProcessError:
            return "reaped"
        except OSError as exc:
            if exc.errno == errno.ECHILD:
                return "reaped"
            if bad is not None:
                bad.append(exc.errno or 0)
            return "error"
        return "zombie" if info is not None else "running"


def _killpg_if_unreaped(pidfd: int, pgid: int | None, sig: int, bad: list[int] | None = None) -> None:
    """``killpg`` unless ``waitid`` says the leader was collected (``ECHILD``).

    A reaped leader's process-group id can be reused, so ``reaped`` returns without
    signalling. Any other ``waitid`` error still signals: that failure is not proof the
    leader is gone. ``SIGCHLD`` is blocked by the caller across this check and the signal.
    This process's group is never signalled. ``InterruptedError`` is retried.
    """

    if pgid is None or pgid <= 1 or pgid == os.getpgrp():
        return
    if _leader_state(pidfd, bad) == "reaped":
        return
    while True:
        try:
            os.killpg(pgid, sig)
            return
        except InterruptedError:
            continue
        except ProcessLookupError:
            return


def _deliver(pidfd: int, pgid: int | None, sig: int, bad: list[int] | None = None) -> None:
    """Signal the leader through its pidfd, then the group unless the leader was reaped.

    An ``OSError`` from the pidfd signal is recorded and does not skip ``killpg``. The group
    is still signalled, and the caller refuses.
    """

    try:
        _pidfd_send_signal(pidfd, sig)
    except OSError as exc:
        if bad is not None:
            bad.append(exc.errno or 0)
    _killpg_if_unreaped(pidfd, pgid, sig, bad)


def _deliver_forcing(pidfd: int, pgid: int | None, sig: int, bad: list[int] | None = None) -> None:
    """``_deliver`` that retries ``KeyboardInterrupt`` so the final signal still lands."""

    for _ in range(8):
        try:
            _deliver(pidfd, pgid, sig, bad)
            return
        except KeyboardInterrupt:
            continue
    _deliver(pidfd, pgid, sig, bad)


def _group_alive(pidfd: int, pgid: int | None, bad: list[int] | None = None) -> bool:
    """True when the leader is unreaped and ``killpg(0)`` sees a live member.

    The ``waitid`` check comes first, so a reaped leader is not passed to ``killpg``.
    """

    if _leader_state(pidfd, bad) == "reaped":
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


def _stop_probe(pid: int | None, pgid: int | None, started: float, deadline: float, pidfd: int | None) -> bool:
    """Signal the original process group confirmed by the handshake. The caller reaps after this.

    Returns true when ``waitid(P_PIDFD)`` failed with anything other than ``ECHILD``. The
    group is still signalled in that case; the caller refuses. The caller blocked SIGINT and
    SIGCHLD before ``fork`` and restores that mask with ``SIG_SETMASK`` after this returns.
    This function does not unblock either signal. The leader is signalled with
    ``pidfd_send_signal``. ``killpg`` runs unless ``waitid`` reports ``ECHILD``. Without a
    pidfd, the identified pid is signalled only when ``waitid(P_PID)`` still says this
    process owns it. SIGTERM, then SIGKILL if a live member is still in that process group
    when the grace runs out. A descendant that calls setpgid(0, 0) leaves the process group
    and is not signalled. This is not session cleanup: that descendant stays in the session.
    The grace is at most 20% of the deadline, and never more than ``_PROBE_GRACE_S`` or past
    ``started + deadline``. A zombie or reaped leader skips the grace. The direct child is
    not reaped here. SIGKILL still runs when SIGTERM raises ``KeyboardInterrupt``.
    """

    if pid is None or pid <= 0 or pid == os.getpid():
        return False
    if pidfd is None:
        return _signal_owned_pid(pid, signal.SIGKILL)
    bad: list[int] = []
    pending = False
    try:
        _deliver(pidfd, pgid, signal.SIGTERM, bad)
    except KeyboardInterrupt:
        pending = True
    finally:
        if not pending and _leader_state(pidfd, bad) == "running" and _group_alive(pidfd, pgid, bad):
            try:
                remaining = (started + deadline) - time.monotonic()
                grace = min(_kill_grace(deadline), max(0.0, remaining))
                stop = time.monotonic() + grace
                while (time.monotonic() < stop and _leader_state(pidfd, bad) == "running"
                       and _group_alive(pidfd, pgid, bad)):
                    time.sleep(0.01)
            except KeyboardInterrupt:
                pending = True
        _deliver_forcing(pidfd, pgid, signal.SIGKILL, bad)
    if pending:
        raise KeyboardInterrupt
    return bool(bad)


def _reap_probe(pid: int | None) -> bool:
    """Reap ``pid``. True when the timeout path saw a non-``ECHILD`` ``waitid`` error.

    That unknown state still receives ``SIGKILL``. The caller refuses: a successful probe
    line does not outrank it.
    """

    if pid is None or pid <= 0:
        return False
    end = time.monotonic() + 0.5
    while True:
        try:
            got, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return False
        if got == pid:
            return False
        if time.monotonic() >= end:
            unknown = _signal_owned_pid(pid, signal.SIGKILL)
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                return unknown
            return unknown
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
    leaves no directory behind. A home containing NUL or a surrogate code point
    (U+D800–U+DFFF, including the surrogateescape form of a real byte) is ``home_unsafe``,
    the same typed refusal as any other unsafe home. The directory is created mode ``0700`` when
    it is absent. An
    existing entry, including a symlink, is left in place and must already be a private directory.
    """

    if not _private_dir(home):
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
