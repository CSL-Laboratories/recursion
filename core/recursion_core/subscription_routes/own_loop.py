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
With no probe the route refuses. Where a new session cannot be enforced, it refuses
``probe unenforceable``.

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
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

from recursion_core.child_env import build_child_env, spawn
from recursion_core.child_env.policy import (
    BASE_NAMES, VALUE_ALL_PROXY, VALUE_URLS, _ALL_PROXY_SCHEMES, _unsafe_proxy,
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
) -> OwnLoopDecision:
    """Decide one Copilot own-loop run. ``route``: engine="copilot", auth="subscription",
    method="copilot_cli_sign_in" (optional ``model``). Returns the child env with every token variable
    and ``COPILOT_API_URL`` removed, or raises ``OwnLoopRouteRefused``.

    The login probe is the caller's callable, not an argv. It runs with a deadline
    (``probe_deadline``, at most ``LOGIN_PROBE_DEADLINE_S``). A probe that raises, including
    ``SystemExit``, or that does not return in time, is not a login. Where a new session can be
    made, every exit signals that session (SIGTERM, then SIGKILL inside the deadline). Where it
    cannot, the route refuses ``probe unenforceable``.
    """

    _common("copilot", route, _COPILOT_FIELDS, "copilot_cli_sign_in")
    if not isinstance(parent_env, Mapping):
        raise OwnLoopRouteRefused("copilot", "parent_env_malformed")
    if has_copilot_login is None:
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "no stored-login probe; gh fallback refused")
    present = _probe_login(has_copilot_login, probe_deadline)
    if not present:
        raise OwnLoopRouteRefused("copilot", "no_stored_copilot_login", "run `copilot login`; gh fallback refused")
    model = _model("copilot", route.get("model"))
    strip = COPILOT_STRIP_ENV + _GH_FALLBACK_ENV
    # Any other provider credential (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...) is not this run's either.
    # The allowlist then drops whatever the strip does not name (GH_PAT, AWS_*, GIT_CONFIG_*).
    child, removed = _project_child(parent_env, lambda name: name in strip or _is_provider_credential(name))
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


def _proxy_origin(base_url: object) -> str:
    """Canonical origin of ``base_url``, or ``""`` when it is not a string or holds a control character."""

    if not isinstance(base_url, str):
        return ""
    raw = base_url.strip()
    if not raw or _has_control_char(raw):
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    return _format_origin(parts)


def _loopback_url(base_url: object) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise OwnLoopRouteRefused("nous", "proxy_address_missing")
    raw = base_url.strip()
    # Before ``urlsplit``, so a control character cannot reach the parser or the refusal text.
    if _has_control_char(raw):
        raise OwnLoopRouteRefused("nous", "proxy_address_malformed", _PROXY_DETAIL)
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
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # A name (even "localhost") is not resolved here; only a loopback literal is provably local.
        raise OwnLoopRouteRefused("nous", "proxy_not_loopback", f"{host}: use 127.0.0.1 or ::1") from None
    if getattr(addr, "ipv4_mapped", None) is not None:
        addr = addr.ipv4_mapped
    if not addr.is_loopback:
        raise OwnLoopRouteRefused("nous", "proxy_not_loopback", host)
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
    origin (``scheme://host:port``) only: no userinfo, path, query or fragment. A control character
    or a port the parser rejects yields an empty address and the fixed detail, never the raw URL.
    ``interactive_users`` is the caller's list of interactive accounts on this host; nothing is
    probed here. Without it the doctor warns that the host's users are unknown rather than reporting
    a clean host.
    """

    warnings = _shared_host_warning(current_user, interactive_users)
    try:
        _loopback_url(base_url)
    except OwnLoopRouteRefused as exc:
        return {"ok": False, "address": _proxy_origin(base_url), "cause": exc.cause,
                "warnings": warnings}
    return {"ok": True, "address": _proxy_origin(base_url), "cause": "", "warnings": warnings}


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
# setsid handshake. Descendants that leave this session are out of scope.
_PROBE_LINE_MAX = 128
_HANDSHAKE = b"\x01"


def _probe_enforceable() -> bool:
    """True when the probe callable can be put in its own session and that session can be signalled."""

    return (
        os.name != "nt"
        and hasattr(os, "fork")
        and hasattr(os, "killpg")
        and hasattr(os, "setsid")
    )


def _probe_login(probe: Callable[[], bool], deadline: float) -> bool:
    """Run ``probe`` until ``deadline``. ``SystemExit`` and a missed deadline are not a login.

    ``probe`` stays a callable: it is the caller's check, not a command this module can turn into
    argv. On a platform that can ``fork``, the callable runs in a new session. Every exit, including
    a login that is present, signals that session: SIGTERM, then at most ``_PROBE_GRACE_S`` and still
    inside ``deadline``, then SIGKILL. The session is the one confirmed by the handshake byte.
    Descendants that leave it are not signalled. Where that session cannot be enforced, the route
    refuses ``probe unenforceable`` instead of leaving a thread running.
    """

    if (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)
            or deadline <= 0 or deadline > LOGIN_PROBE_DEADLINE_S):
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe deadline")
    if not _probe_enforceable():
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe unenforceable")
    return _probe_login_group(probe, deadline)


def _probe_login_group(probe: Callable[[], bool], deadline: float) -> bool:
    """Run ``probe`` in its own session and signal that session on every exit path."""

    read_fd, write_fd = os.pipe()
    pid: int | None = None
    try:
        os.set_inheritable(read_fd, False)
        os.set_inheritable(write_fd, False)
        os.set_blocking(read_fd, False)
        pid = os.fork()
    except OSError:
        _close_fd(read_fd)
        _close_fd(write_fd)
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe failed") from None
    if pid == 0:
        _probe_child(probe, read_fd, write_fd)
    _close_fd(write_fd)
    selector: selectors.BaseSelector | None = None
    pgid: int | None = None
    outcome: dict[str, object] = {}
    started = time.monotonic()
    try:
        selector = selectors.DefaultSelector()
        selector.register(read_fd, selectors.EVENT_READ)
        pgid, reason = _read_handshake(selector, read_fd, pid, started, deadline)
        if pgid is None:
            detail = "probe deadline" if reason == "deadline" else "probe failed"
            outcome["refused"] = OwnLoopRouteRefused("copilot", "copilot_login_unknown", detail)
        else:
            line, complete, timed_out = _read_status(selector, read_fd, started, deadline)
            if timed_out or not complete:
                detail = "probe deadline" if timed_out else "probe failed"
                outcome["refused"] = OwnLoopRouteRefused("copilot", "copilot_login_unknown", detail)
            else:
                outcome["line"] = line
    except BaseException as exc:
        outcome["error"] = exc
    finally:
        try:
            _stop_probe(pid, pgid, started, deadline)
        finally:
            if selector is not None:
                selector.close()
            _close_fd(read_fd)
            _reap_probe(pid)
    return _finish_probe(outcome)


def _probe_child(probe: Callable[[], bool], read_fd: int, write_fd: int) -> None:
    """Never returns. The handshake byte is written only after ``setsid``."""

    try:
        _close_fd(read_fd)
        os.setsid()
        _write_all(write_fd, _HANDSHAKE)
        _write_all(write_fd, _probe_status(probe))
    except BaseException:
        pass
    os._exit(0)


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


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _result_deadline(started: float, deadline: float) -> float:
    """When the parent stops waiting, leaving the kill grace inside the original deadline."""

    grace = min(_PROBE_GRACE_S, deadline)
    return started + deadline - grace


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
    That id is never this process's group.
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


def _signal_group(pid: int, pgid: int | None, sig: int) -> None:
    """Signal the confirmed group, or ``pid`` alone when the handshake never arrived.

    This process's group is never signalled.
    """

    if pgid is not None and pgid > 1 and pgid != os.getpgrp():
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
        return
    if pid > 0 and pid != os.getpid():
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def _reap_if_zombie(pid: int) -> None:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


def _group_alive(pid: int, pgid: int | None) -> bool:
    if pgid is not None and pgid > 1 and pgid != os.getpgrp():
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _stop_probe(pid: int | None, pgid: int | None, started: float, deadline: float) -> None:
    """SIGTERM the probe session, then SIGKILL if it is still there when the grace runs out.

    The grace is at most ``_PROBE_GRACE_S`` and never extends past ``started + deadline``.
    A session that is already gone (the probe exited and left no child) returns without waiting.
    """

    if pid is None or pid <= 0 or pid == os.getpid():
        return
    try:
        _reap_if_zombie(pid)
        if not _group_alive(pid, pgid):
            return
        remaining = (started + deadline) - time.monotonic()
        grace = min(_PROBE_GRACE_S, max(0.0, remaining))
        _signal_group(pid, pgid, signal.SIGTERM)
        stop = time.monotonic() + grace
        while True:
            _reap_if_zombie(pid)
            if not _group_alive(pid, pgid):
                return
            if time.monotonic() >= stop:
                break
            time.sleep(0.01)
        _signal_group(pid, pgid, signal.SIGKILL)
        _reap_if_zombie(pid)
    except OSError:
        return


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


def _spawn_checked(argv: Sequence[str], decision: OwnLoopDecision, home: str, cwd: str | None) -> subprocess.Popen:
    """Rebuild the checked child through ``build_child_env`` and start it. ``Popen`` is read at the call."""

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
    """``check_copilot_own_loop`` then ``child_env.spawn``. A refused env never reaches ``Popen``."""

    decision = check_copilot_own_loop(route, parent_env, has_copilot_login=has_copilot_login)
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
    """``check_nous_own_loop`` then ``child_env.spawn``. A refused env never reaches ``Popen``."""

    decision = check_nous_own_loop(
        route, parent_env, current_user=current_user, interactive_users=interactive_users,
    )
    return decision, _spawn_checked(argv, decision, home, cwd)
