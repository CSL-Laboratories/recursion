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
import math
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit

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
# Credential names the ``*_API_KEY`` / ``*_TOKEN`` suffix misses. ``OPENAI_API_BASE`` is the older
# spelling of ``OPENAI_BASE_URL``. Git's ``GIT_CONFIG_KEY_*`` / ``GIT_CONFIG_VALUE_*`` channel can
# carry an HTTP bearer (``http.extraheader``). Matched case-insensitively.
_CREDENTIAL_ENV = frozenset({
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "GITHUB_PASSWORD",
    "GITHUB_PAT",
    "AWS_SECRET_ACCESS_KEY",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
})
# A stored-login probe that does not return is not a login. Callers may pass a shorter bound.
LOGIN_PROBE_DEADLINE_S = 5.0
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
    probe_deadline: float = LOGIN_PROBE_DEADLINE_S,
) -> OwnLoopDecision:
    """Decide one Copilot own-loop run. ``route``: engine="copilot", auth="subscription",
    method="copilot_cli_sign_in" (optional ``model``). Returns the child env with every token variable
    and ``COPILOT_API_URL`` removed, or raises ``OwnLoopRouteRefused``.

    The login probe runs with a deadline (``probe_deadline``, at most ``LOGIN_PROBE_DEADLINE_S``).
    A probe that raises, including ``SystemExit``, or that does not return in time, is not a login.
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
    removed = tuple(n for n in parent_env if n in strip or _is_provider_credential(n))
    child = {k: v for k, v in parent_env.items() if k not in removed}
    return OwnLoopDecision("copilot", "copilot_cli_sign_in", MappingProxyType(child), removed, model=model)


def _redact_proxy_address(base_url: object) -> str:
    """``base_url`` with userinfo removed. Empty when it is not a string ``urlsplit`` can read."""

    if not isinstance(base_url, str):
        return ""
    raw = base_url.strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.username is None and parts.password is None:
        return raw
    netloc = parts.netloc
    at = netloc.rfind("@")
    if at == -1:
        return ""
    return urlunsplit((parts.scheme, netloc[at + 1:], parts.path, parts.query, parts.fragment))


def _loopback_url(base_url: object) -> str:
    if not isinstance(base_url, str) or not base_url.strip():
        raise OwnLoopRouteRefused("nous", "proxy_address_missing")
    raw = base_url.strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        raise OwnLoopRouteRefused("nous", "proxy_address_malformed", "unparseable") from None
    # Before the scheme check, so an ftp://user:secret@... URL is not copied into the refusal.
    if parts.username is not None or parts.password is not None:
        raise OwnLoopRouteRefused("nous", "recursion_holds_no_token", "credentials in proxy URL")
    try:
        host = parts.hostname
        parts.port  # noqa: B018 - raises on a bad port
    except ValueError as exc:
        raise OwnLoopRouteRefused("nous", "proxy_address_malformed", str(exc)) from None
    if parts.scheme not in _NOUS_SCHEMES or not host:
        raise OwnLoopRouteRefused("nous", "proxy_address_malformed", raw)
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

    Never raises; returns ``{"ok", "address", "cause", "warnings"}``. ``address`` never includes URL
    userinfo, including when the scheme is not http(s). ``interactive_users`` is the caller's list of
    interactive accounts on this host; nothing is probed here. Without it the doctor warns that the
    host's users are unknown rather than reporting a clean host.
    """

    warnings = _shared_host_warning(current_user, interactive_users)
    try:
        url = _loopback_url(base_url)
    except OwnLoopRouteRefused as exc:
        return {"ok": False, "address": _redact_proxy_address(base_url), "cause": exc.cause,
                "warnings": warnings}
    return {"ok": True, "address": url, "cause": "", "warnings": warnings}


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
    strip = tuple(n for n in parent_env if _is_provider_credential(n) or n.upper() in _PROXY_ENV)
    child = {k: v for k, v in parent_env.items() if k not in strip}
    return OwnLoopDecision("nous", "hermes_local_proxy", MappingProxyType(child), strip, url, adapters,
                           _shared_host_warning(current_user, interactive_users), model)


def _probe_login(probe: Callable[[], bool], deadline: float) -> bool:
    """Run ``probe`` until ``deadline``. ``SystemExit`` and a missed deadline are not a login.

    A probe that is still running is not killed (a thread cannot be). It is a daemon, so it does
    not keep the process alive, and its result is ignored.
    """

    if (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)
            or deadline <= 0 or deadline > LOGIN_PROBE_DEADLINE_S):
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe deadline")
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = probe()
        except BaseException as exc:  # SystemExit included; it must not escape the probe thread
            box["exc"] = exc

    thread = threading.Thread(target=run, name="copilot-login-probe", daemon=True)
    thread.start()
    thread.join(deadline)
    if thread.is_alive():
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", "probe deadline")
    exc = box.get("exc")
    if isinstance(exc, KeyboardInterrupt):
        raise exc
    if exc is not None:
        raise OwnLoopRouteRefused("copilot", "copilot_login_unknown", type(exc).__name__) from None
    return box.get("value") is True


def _is_git_config_injection(upper: str) -> bool:
    return upper in ("GIT_CONFIG_KEY", "GIT_CONFIG_VALUE") or upper.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))


def _is_provider_credential(name: str) -> bool:
    upper = name.upper()
    if upper.startswith(("NOUS_", "XAI_", "GROK_", "HERMES_")):
        return True
    if upper.endswith(("_API_KEY", "_TOKEN", "_AUTH_TOKEN")) or upper in _CREDENTIAL_ENV:
        return True
    return _is_git_config_injection(upper)
