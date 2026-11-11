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
answered by a caller-supplied probe (``has_copilot_login``), and with no probe the route refuses.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

__all__ = [
    "COPILOT_TOKEN_ENV",
    "COPILOT_STRIP_ENV",
    "NOUS_ADAPTER",
    "OwnLoopDecision",
    "OwnLoopRouteRefused",
    "check_copilot_own_loop",
    "check_nous_own_loop",
    "nous_proxy_doctor",
]

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
_MODEL_MAX = 128


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
) -> OwnLoopDecision:
    """Decide one Copilot own-loop run. ``route``: engine="copilot", auth="subscription",
    method="copilot_cli_sign_in" (optional ``model``). Returns the child env with every token variable
    and ``COPILOT_API_URL`` removed, or raises ``OwnLoopRouteRefused``.
    """

    _common("copilot", route, _COPILOT_FIELDS, "copilot_cli_sign_in")
    parent_env = _screen_parent_env("copilot", parent_env)
    if has_copilot_login is None:
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "no stored-login probe; gh fallback refused")
    model = _model("copilot", route.get("model"))
    strip = COPILOT_STRIP_ENV + _GH_FALLBACK_ENV
    # Any other provider credential (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...) is not this run's either.
    removed = tuple(n for n in parent_env if n in strip or _is_provider_credential(n))
    child = {k: v for k, v in parent_env.items() if k not in removed}
    _screen_kept_values("copilot", child)
    # The probe sees this snapshot in its own process. Replacing this process's environment
    # would publish the snapshot to every other thread and drop their concurrent writes.
    try:
        present = _probe_in_screened_env(has_copilot_login, child)
    except OwnLoopRouteRefused:
        raise
    except Exception as exc:  # a failing probe is not a login
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", type(exc).__name__) from None
    if not present:
        raise OwnLoopRouteRefused("copilot", "no_stored_copilot_login", "run `copilot login`; gh fallback refused")
    return OwnLoopDecision("copilot", "copilot_cli_sign_in", MappingProxyType(child), removed, model=model)


def _probe_in_screened_env(probe: Callable[[], bool], child: Mapping[str, str]) -> bool:
    """Run the login probe in a forked child whose environment is ``child``.

    The parent environment is left untouched. Applying ``child`` here and restoring it afterwards
    would show the probe's environment to other threads, and the restore would discard any write
    those threads made while the probe ran.
    """

    if not hasattr(os, "fork"):
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe unenforceable")
    read_fd, write_fd = os.pipe()
    try:
        pid = os.fork()
    except OSError:
        os.close(read_fd)
        os.close(write_fd)
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe failed") from None
    if pid == 0:
        try:
            os.close(read_fd)
            try:
                os.environ.clear()
                os.environ.update(child)
                ok = probe() is True
                payload = b"T" if ok else b"F"
            except BaseException as exc:
                payload = b"E" + type(exc).__name__.encode("ascii", "replace")
            _write_all(write_fd, payload[:80])
        except BaseException:
            pass
        os._exit(0)
    os.close(write_fd)
    try:
        status = _read_all(read_fd, 80)
    finally:
        os.close(read_fd)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    if status == b"T":
        return True
    if status == b"F":
        return False
    if status.startswith(b"E"):
        name = status[1:].decode("ascii", "replace") or "Exception"
        if name == "SystemExit":
            raise SystemExit
        if name == "KeyboardInterrupt":
            raise KeyboardInterrupt
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", name)
    raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe failed")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        if written <= 0:
            return
        view = view[written:]


def _read_all(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        try:
            chunk = os.read(fd, remaining)
        except InterruptedError:
            continue
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


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


_ORIGIN_PORTS = {"http": 80, "https": 443, "ftp": 21}


def _reported_address(base_url: object) -> str:
    """``scheme://host:port`` only. The raw input, userinfo, path, query and fragment are never returned.

    A percent-encoded authority that hides ``user:secret@host`` (``user%3Asecret%40host``) has no
    safe host to report, so the address is empty.
    """

    if not isinstance(base_url, str):
        return ""
    raw = base_url.strip()
    if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    netloc = parts.netloc
    if "%" in netloc and "@" in unquote(netloc):
        return ""
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if not scheme or not host or "%" in host or "@" in host:
        return ""
    if port is None:
        port = _ORIGIN_PORTS.get(scheme)
    host_text = f"[{host}]" if ":" in host else host
    if port is None:
        return f"{scheme}://{host_text}"
    return f"{scheme}://{host_text}:{port}"


def _loopback_url(base_url: object) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise OwnLoopRouteRefused("nous", "proxy_address_missing")
    text = base_url.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise OwnLoopRouteRefused("nous", "proxy_address_malformed")
    try:
        parts = urlsplit(text)
    except ValueError:
        raise OwnLoopRouteRefused("nous", "proxy_address_malformed") from None
    # Before the scheme check, and before any host is copied into a refusal. ``user%3Asecret%40host``
    # is userinfo even though ``urlsplit`` does not set ``username``. A query or fragment can carry
    # a token; an empty ``?`` or ``#`` does not.
    netloc = parts.netloc
    if (parts.username is not None or parts.password is not None
            or ("%" in netloc and "@" in unquote(netloc)) or parts.query or parts.fragment):
        raise OwnLoopRouteRefused("nous", "recursion_holds_no_token", "credentials in proxy URL")
    try:
        host = parts.hostname
        _ = parts.port  # raises ValueError when the port is not an integer; the text is not reused
    except ValueError:
        raise OwnLoopRouteRefused("nous", "proxy_address_malformed") from None
    if parts.scheme not in _NOUS_SCHEMES or not host:
        raise OwnLoopRouteRefused("nous", "proxy_address_malformed")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # A name (even "localhost") is not resolved here; only a loopback literal is provably local.
        detail = host if host and "%" not in host and "@" not in host else ""
        raise OwnLoopRouteRefused("nous", "proxy_not_loopback",
                                  f"{detail}: use 127.0.0.1 or ::1" if detail else "use 127.0.0.1 or ::1") from None
    if getattr(addr, "ipv4_mapped", None) is not None:
        addr = addr.ipv4_mapped
    if not addr.is_loopback:
        detail = host if "%" not in host and "@" not in host else ""
        raise OwnLoopRouteRefused("nous", "proxy_not_loopback",
                                  f"{detail}: use 127.0.0.1 or ::1" if detail else "use 127.0.0.1 or ::1")
    return base_url.strip()


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

    Never raises; returns ``{"ok", "address", "cause", "warnings"}``. ``interactive_users`` is the
    caller's list of interactive accounts on this host; nothing is probed here. Without it the doctor
    warns that the host's users are unknown rather than reporting a clean host.
    """

    warnings = _shared_host_warning(current_user, interactive_users)
    try:
        _loopback_url(base_url)
    except OwnLoopRouteRefused as exc:
        return {"ok": False, "address": _reported_address(base_url), "cause": exc.cause,
                "warnings": warnings}
    return {"ok": True, "address": _reported_address(base_url), "cause": "", "warnings": warnings}


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
    strip = tuple(n for n in parent_env if _is_provider_credential(n) or n.upper() in _PROXY_ENV)
    child = {k: v for k, v in parent_env.items() if k not in strip}
    _screen_kept_values("nous", child)
    return OwnLoopDecision("nous", "hermes_local_proxy", MappingProxyType(child), strip, url, adapters,
                           _shared_host_warning(current_user, interactive_users), model)


def _is_provider_credential(name: str) -> bool:
    upper = name.upper()
    if upper.startswith(("NOUS_", "XAI_", "GROK_", "HERMES_")):
        return True
    return upper.endswith(("_API_KEY", "_TOKEN", "_AUTH_TOKEN")) or upper in ("OPENAI_BASE_URL",)
