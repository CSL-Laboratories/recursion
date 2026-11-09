"""One route contract for every engine adapter (CSL-797, candidate). See README.md.

The contract is plain dicts that round-trip through JSON, so a surface, an adapter or a later Rust
core can speak it without importing Python types. Every refusal and failure carries a typed code
from ``CODES``; nothing downstream reads wording.

Request::

    {"contract": "recursion.route/v0", "request_id": "r1", "tenant": "t1", "prompt": "...",
     "deadline_s": 120.0,
     "route": {"engine": "claude_code" | "codex" | "pi" | "hermes" | "opencode",
               "model": "<model id>",
               "connection": {"kind": "subscription" | "api_key" | "passthrough", "id": "<login or key id>",
                              "credential_env": "<name>",  # api_key, unless key_source is api_key_helper
                              "key_source": "custody" | "api_key_helper",  # claude_code api_key only
                              "method": "<built-in method>",  # passthrough (claude_code only)
                              "metered_per_model": false},
               "bare": false,                             # claude_code: refused on subscription only
               "bare_opt_out": "--no-bare" | null,         # only if reported by this build help
               "bare_default": "absent" | "present" | "unknown",  # claude_code subscription: reported (B20)
               "claude_build": "<major.minor.patch>",     # with help_text: measured-help gate (B20)
               "help_text": "<claude --help text>",       # exact text; one measured hash runs as beta
               "api_key_helper": "absent" | "present",    # claude_code subscription: reported by the surface
               "billing_ack": "api_key_helper",           # the user chose to run billing their API key
               "limits": {"on_limit": "ask" | "wait" | "fallback", "fallback": [<route>, ...]},
               "profile": {...}}}                         # pi/hermes/opencode only (B3)

Result::

    {"contract", "request_id", "status": "ok" | "error", "code": None | <CODES>, "text",
     "limit": None | <limit signal>, "events": [<event>], "detail": str,
     "retry_at": None | float,     # when a BREAKER_OPEN / PROBE_IN_FLIGHT / SHORT_FAULT refusal may clear
     "breaker_effect": "none" | "opened" | "short_fault" | "closed" | "probe_released",
     "limit_notice": None | <limits.notice>}   # shown to the user at a limit; never a silent switch

Event::

    {"kind": "retry" | "usage" | "limit" | "text" | "fallback", "source": "<vendor field it was read from>", "data": {...}}

Limit signal (the only thing that can trip a connection's breaker)::

    {"scope": "connection" | "model", "model": str | None, "reset_at": float | None,
     "source": "<typed vendor field>"}
"""

from __future__ import annotations

import math
from collections.abc import Mapping

CONTRACT_VERSION = "recursion.route/v0"

OK = "ok"
# Typed result codes. Only PLAN_LIMIT fences a connection; SHORT_FAULT fences one model for one tenant
# briefly (grill Q23); every other code stays with its own request.
PLAN_LIMIT = "plan_limit"  # typed vendor limit signal; trips the connection (or vendor model bucket)
SHORT_FAULT = "short_fault"  # vague quota-sounding text outside the model payload (grill Q23)
ENGINE_ERROR = "engine_error"  # typed engine failure that is not a limit
AUTH_FAILED = "auth_failed"
PROTOCOL_ERROR = "protocol_error"  # engine output that does not parse as the protocol
ENGINE_EXITED = "engine_exited"  # stream ended before a result
DEADLINE_EXCEEDED = "deadline_exceeded"  # Recursion's own deadline: never an engine fault (S4)
BREAKER_OPEN = "breaker_open"
PROBE_IN_FLIGHT = "probe_in_flight"
SUBSCRIPTION_ROUTE_REFUSED = "subscription_route_refused"  # B3, from subscription_routes
LAUNCH_REFUSED = "launch_refused"  # e.g. a Codex config that would enable the updater (B11)
REQUEST_INVALID = "request_invalid"
ENGINE_UNKNOWN = "engine_unknown"
ROUTE_CHOICE_REQUIRED = "route_choice_required"  # the user must pick (e.g. apiKeyHelper outranks sign-in)

CODES = frozenset(
    {
        PLAN_LIMIT,
        SHORT_FAULT,
        ENGINE_ERROR,
        AUTH_FAILED,
        PROTOCOL_ERROR,
        ENGINE_EXITED,
        DEADLINE_EXCEEDED,
        BREAKER_OPEN,
        PROBE_IN_FLIGHT,
        SUBSCRIPTION_ROUTE_REFUSED,
        LAUNCH_REFUSED,
        REQUEST_INVALID,
        ENGINE_UNKNOWN,
        ROUTE_CHOICE_REQUIRED,
    }
)
# Codes that come from the engine run itself (as opposed to a refusal before launch).
ENGINE_CODES = frozenset({PLAN_LIMIT, SHORT_FAULT, ENGINE_ERROR, AUTH_FAILED, PROTOCOL_ERROR, ENGINE_EXITED})

ENGINES = ("claude_code", "codex", "pi", "hermes", "opencode")
PROFILE_ENGINES = ("pi", "hermes", "opencode")
CONNECTION_KINDS = ("subscription", "api_key", "passthrough")


class RouteError(Exception):
    """A typed refusal; ``code`` is one of ``CODES``."""

    def __init__(self, code: str, detail: str = "") -> None:
        assert code in CODES, code
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def validate_request(request: object) -> dict:
    """Return a normalised copy of ``request`` or raise ``RouteError(REQUEST_INVALID)``."""

    if not isinstance(request, Mapping):
        raise RouteError(REQUEST_INVALID, "request is not an object")
    if request.get("contract") != CONTRACT_VERSION:
        raise RouteError(REQUEST_INVALID, "contract")
    for name in ("request_id", "tenant", "prompt"):
        if not isinstance(request.get(name), str) or not request[name]:
            raise RouteError(REQUEST_INVALID, name)
    deadline = request.get("deadline_s")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline <= 0:
        raise RouteError(REQUEST_INVALID, "deadline_s")
    route = request.get("route")
    if not isinstance(route, Mapping):
        raise RouteError(REQUEST_INVALID, "route")
    engine = route.get("engine")
    if engine not in ENGINES:
        raise RouteError(ENGINE_UNKNOWN, str(engine))
    if not isinstance(route.get("model"), str) or not route["model"]:
        raise RouteError(REQUEST_INVALID, "route.model")
    conn = route.get("connection")
    if not isinstance(conn, Mapping) or conn.get("kind") not in CONNECTION_KINDS:
        raise RouteError(REQUEST_INVALID, "route.connection.kind")
    if not isinstance(conn.get("id"), str) or not conn["id"]:
        raise RouteError(REQUEST_INVALID, "route.connection.id")
    key_source = conn.get("key_source", "custody")
    if key_source not in ("custody", "api_key_helper") or (key_source == "api_key_helper" and (
            conn["kind"] != "api_key" or engine != "claude_code")):
        raise RouteError(REQUEST_INVALID, "route.connection.key_source")
    if conn["kind"] == "api_key" and key_source == "custody" and not (
            isinstance(conn.get("credential_env"), str) and conn["credential_env"]):
        raise RouteError(REQUEST_INVALID, "route.connection.credential_env")
    if conn["kind"] == "passthrough" and (engine != "claude_code" or not (
            isinstance(conn.get("method"), str) and conn["method"])):
        raise RouteError(REQUEST_INVALID, "route.connection.method")
    if "limits" in route:
        from recursion_core.routes.limits import validate_limits  # local: limits imports this module

        validate_limits(route["limits"])
    if engine in PROFILE_ENGINES and not isinstance(route.get("profile"), Mapping):
        raise RouteError(REQUEST_INVALID, "route.profile")
    out = dict(request)
    out["route"] = dict(route)
    out["route"]["connection"] = dict(conn)
    return out


def connection_key(route: Mapping) -> str:
    """The meter a route is billed through: the login or key, never the engine id (grill Q8)."""

    conn = route["connection"]
    return f"{conn['kind']}:{conn['id']}"


def limit_signal(scope: str, source: str, *, model: str | None = None, reset_at: float | None = None) -> dict:
    """Build a limit signal. Adapters call this only from typed vendor fields, never from text."""

    if scope not in ("connection", "model") or (scope == "model" and not model):
        raise ValueError(scope)
    if reset_at is not None and (not isinstance(reset_at, (int, float)) or not math.isfinite(reset_at)):
        reset_at = None
    return {"scope": scope, "model": model if scope == "model" else None, "reset_at": reset_at, "source": source}


def event(kind: str, source: str, data: Mapping | None = None) -> dict:
    return {"kind": kind, "source": source, "data": dict(data or {})}


def result(request_id: str, code: str | None, *, text: str = "", limit: dict | None = None,
           events: list | None = None, detail: str = "", retry_at: float | None = None) -> dict:
    if code is not None and code not in CODES:
        raise ValueError(code)
    return {
        "contract": CONTRACT_VERSION,
        "request_id": request_id,
        "status": OK if code is None else "error",
        "code": code,
        "text": text,
        "limit": limit,
        "events": list(events or []),
        "detail": detail,
        "retry_at": retry_at,
        "breaker_effect": "none",
        "limit_notice": None,
    }


# Words that sound like a quota. Seen outside the model payload (a typed error envelope's free text),
# they make at most a SHORT_FAULT on one model for one tenant (grill Q23); they never make a PLAN_LIMIT.
_QUOTA_WORDS = ("quota", "rate limit", "rate-limit", "usage limit", "limit reached", "exhausted", "too many requests")


def sounds_like_quota(text: object) -> bool:
    return isinstance(text, str) and any(word in text.lower() for word in _QUOTA_WORDS)
