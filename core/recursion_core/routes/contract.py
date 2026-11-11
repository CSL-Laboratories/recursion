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

``reset_at`` is an absolute reading of the breaker clock (the same clock as deadlines). A vendor
``resetsAt`` is Unix epoch seconds and is translated with ``vendor_reset`` before it is stored.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping

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
    """The meter a route is billed through: the login or key, never the engine id alone (grill Q8)."""

    conn = route["connection"]
    return f"{conn['kind']}:{conn['id']}"


def breaker_key(route: Mapping) -> str:
    """The credential that is billed, not the engine that carries it.

    An API key is one meter wherever the same custody lookup runs: billing family, env name, and
    key id. Claude Code's ``ANTHROPIC_API_KEY`` id ``k1`` and an OpenCode anthropic profile with
    that same id and env share a slot. Codex's ``OPENAI_API_KEY`` id ``k1`` is a different key.
    An ``api_key_helper`` route ignores ``credential_env``: the helper supplies the key, so the
    meter is ``api_key:anthropic:api_key_helper:<id>`` and does not share the custody slot.
    A subscription login stays on its engine: the same label on Claude and on Codex is not one
    login. On Pi, Hermes and OpenCode the login is the normalised provider and sign-in method, so
    OpenCode ChatGPT and OpenCode Grok with the same connection id are two meters. A passthrough
    method bills that cloud account, not the Anthropic key.
    """

    conn = route["connection"]
    kind, ident = conn["kind"], conn["id"]
    engine = route["engine"]
    if kind == "api_key":
        family, env = _api_key_meter(route)
        return f"api_key:{family}:{env}:{ident}"
    if kind == "passthrough":
        method = conn.get("method") if isinstance(conn.get("method"), str) else ""
        return f"{engine}:passthrough:{method}:{ident}"
    if kind == "subscription" and engine in PROFILE_ENGINES:
        provider, method = _sign_in_identity(route)
        return f"{engine}:subscription:{provider}:{method}:{ident}"
    return f"{engine}:{kind}:{ident}"


def _sign_in_identity(route: Mapping) -> tuple[str, str]:
    """Normalised provider and sign-in method. Same spelling rule as the sign-in allowlist."""

    profile = route.get("profile") if isinstance(route.get("profile"), Mapping) else {}
    provider = profile.get("provider") if isinstance(profile.get("provider"), str) else ""
    method = profile.get("method") if isinstance(profile.get("method"), str) else ""
    from recursion_core.subscription_routes.policy import _norm

    return _norm(provider), _norm(method)


def _api_key_meter(route: Mapping) -> tuple[str, str]:
    """Billing family and the env name custody is asked for."""

    conn = route["connection"]
    engine = route.get("engine")
    profile = route.get("profile") if isinstance(route.get("profile"), Mapping) else {}
    provider = profile.get("provider") if isinstance(profile.get("provider"), str) else ""
    provider = provider.strip().lower()
    env = conn.get("credential_env") if isinstance(conn.get("credential_env"), str) else ""
    if not env and isinstance(profile.get("credential_env"), str):
        env = profile["credential_env"]
    if conn.get("key_source") == "api_key_helper":
        # The adapter does not read credential_env on this route. A caller-supplied name must not
        # move the meter, and the helper must not share a custody key's slot.
        return "anthropic", "api_key_helper"
    if engine == "claude_code":
        return "anthropic", env or "ANTHROPIC_API_KEY"
    if engine == "codex":
        return "openai", env or "OPENAI_API_KEY"
    return _profile_family(engine, provider), env


def _profile_family(engine: object, provider: str) -> str:
    """``Provider.family`` from subscription_routes, or the provider id when it is not listed."""

    if not isinstance(engine, str) or not provider:
        return provider or (engine if isinstance(engine, str) else "")
    from recursion_core.subscription_routes.policy import ENGINES

    spec = ENGINES.get(engine)
    known = spec.providers.get(provider) if spec is not None else None
    return known.family if known is not None else provider


def vendor_reset(epoch: object, clock: Callable[[], float], wall: Callable[[], float]) -> float | None:
    """Translate a vendor Unix-epoch ``resetsAt`` onto ``clock``.

    ``clock`` is the breaker and deadline clock and may be monotonic. ``wall`` reads Unix epoch
    seconds (``time.time``). The same function for both leaves the number unchanged, which is what
    the fakes do. A non-finite value, or a reset that is already past on ``clock``, becomes None so
    the breaker uses its own cooldown instead of treating a real epoch as eight days away.
    Both clocks are read once: a second read would move a monotonic clock forward and could reject
    a reset that was still in the future at the first read.
    """

    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)) or not math.isfinite(float(epoch)):
        return None
    now_clock, now_wall = clock(), wall()
    translated = now_clock + (float(epoch) - now_wall)
    if not math.isfinite(translated) or translated <= now_clock:
        return None
    return translated


def limit_signal(scope: str, source: str, *, model: str | None = None, reset_at: float | None = None) -> dict:
    """Build a limit signal. Adapters call this only from typed vendor fields, never from text."""

    if scope not in ("connection", "model") or (scope == "model" and not model):
        raise ValueError(scope)
    if reset_at is not None and (not isinstance(reset_at, (int, float)) or not math.isfinite(reset_at)):
        reset_at = None
    return {"scope": scope, "model": model if scope == "model" else None, "reset_at": reset_at, "source": source}


def event(kind: str, source: str, data: Mapping | None = None) -> dict:
    raw = dict(data or {})
    return {"kind": kind, "source": source, "data": {key: redact(value) if isinstance(value, str) else value
                                                     for key, value in raw.items()}}


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
        "detail": redact(detail),
        "retry_at": retry_at,
        "breaker_effect": "none",
        "limit_notice": None,
    }


# Phrases that sound like a quota. Seen outside the model payload (a typed error envelope's free text),
# they make at most a SHORT_FAULT on one model for one tenant (grill Q23); they never make a PLAN_LIMIT.
# Token edges are letters and digits, including non-ASCII letters, so "unexhausted" and
# "éexhaustedé" are not hits. "_" separates tokens, so "quota_exhausted" is. "rate-limited" and
# "rate limiting" are hits. A denial cancels only a recognised predicate, and the words may be
# joined by the same separators as the phrase itself (space, "_" or "-"): "not exhausted",
# "not_exhausted", "not-rate-limited", "isn't exhausted", "quota is not exhausted" and
# "quota_is_not_exhausted". The character before the negator is a token boundary: the start of the
# string, "_" , or any other non-word character, so '"not exhausted"', "(not exhausted)" and a JSON
# value are denials. "quota is not available" stays a hit: "available" is not one of those
# predicates. "no quota" stays a hit.
_SEP = r"[\s_-]"
_QUOTA = re.compile(
    rf"quotas?|rate{_SEP}*limit(?:ed|ing|s)?|usage{_SEP}*limits?|limits?{_SEP}*reached|exhausted|too many requests",
    re.IGNORECASE,
)
_QUOTA_NEGATED = re.compile(
    # ``_`` is a word character for ``\w`` and still a token boundary, same as quotes and parentheses.
    rf"(?:^|[^\w]|_)(?:not|never|(?:is|are|was|were|do|does|did|wo|can|could|should|would)n['’]t)"
    rf"(?:{_SEP}+(?:a|an|the))?{_SEP}+$",
    re.IGNORECASE,
)
_QUOTA_PREDICATE = rf"(?:exhausted|rate{_SEP}*limit(?:ed|ing|s)?|usage{_SEP}*limits?|quotas?)"
_QUOTA_DENIED_AFTER = re.compile(
    rf"^{_SEP}*(?:is|are|was|were)?{_SEP}*(?:not|never|(?:is|are|was|were)n['’]t){_SEP}+"
    rf"(?:(?:a|an|the){_SEP}+)?{_QUOTA_PREDICATE}(?=$|{_SEP}|\b)",
    re.IGNORECASE,
)
# Credential shapes. Word boundaries so "task-12345678" is not an sk- key. Case-insensitive.
# JSON ``\uXXXX`` / ``\/`` and ``%HH`` are peeled in turn, and every intermediate view is searched
# for secret tokens. Userinfo is not peeled that way: one token reader and one state walk decide
# every delimiter, so ``%20`` in a password is never turned into a space before the scheme matches.
# A hyphen written as ``\u002d`` still matches. Decoded text is only a search view.
_SECRET = re.compile(
    r"(?<!\w)eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}(?!\w)"
    r"|(?<!\w)sk-[A-Za-z0-9_-]{8,}(?!\w)"
    r"|(?<!\w)(?:AKIA|ASIA)[A-Z0-9]{16}(?!\w)"
    r"|(?<!\w)bearer\s+[A-Za-z0-9+/=._~-]{8,}"
    r"|(?<!\w)ghp_[A-Za-z0-9]{20,}(?!\w)"
    r"|(?<!\w)github_pat_[A-Za-z0-9_]{20,}(?!\w)"
    r"|(?<!\w)xox[baprs]-[A-Za-z0-9-]{10,}(?!\w)"
    r"|(?<!\w)ya29\.[A-Za-z0-9._-]{10,}(?!\w)",
    re.IGNORECASE,
)
_JSON_U = re.compile(r"\\u([0-9a-fA-F]{4})")
_HEX = frozenset("0123456789abcdefABCDEF")
# End-delimiters, raw or decoded. The walk's state decides which of them actually stop.
_WS = frozenset(" \t\r\n")
_END_CHARS = frozenset("/?#\"',;<>()[]{}") | _WS
# A backslash run escapes these into a single token of that character. ``\\/`` is still a slash.
_ESCAPED = frozenset("/?#:@\\")
_PEEL_LIMIT = 8


# One round is a JSON peel and then a percent peel. Eight rounds are shared. A value that is still
# changing after that has not reached a fixed point, and the whole field is redacted.
_ESCAPE_PASSES = 8
_PCT = re.compile(r"%([0-9a-fA-F]{2})")


def _decode_layer(value: str) -> tuple[str, list[tuple[int, int]], bool]:
    """Decode one layer of ``\\uXXXX`` and ``\\/``. Each output character maps into ``value``."""

    out: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    length = len(value)
    changed = False
    while index < length:
        if value.startswith("\\/", index):
            out.append("/")
            spans.append((index, index + 2))
            index += 2
            changed = True
            continue
        match = _JSON_U.match(value, index)
        if match is not None:
            out.append(chr(int(match.group(1), 16)))
            spans.append((match.start(), match.end()))
            index = match.end()
            changed = True
            continue
        out.append(value[index])
        spans.append((index, index + 1))
        index += 1
    return "".join(out), spans, changed


def _decode_pct_layer(value: str) -> tuple[str, list[tuple[int, int]], bool]:
    """Decode one layer of ``%HH``. Each output character maps to three source characters, or one."""

    out: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    length = len(value)
    changed = False
    while index < length:
        match = _PCT.match(value, index)
        if match is not None:
            out.append(chr(int(match.group(1), 16)))
            spans.append((match.start(), match.end()))
            index = match.end()
            changed = True
            continue
        out.append(value[index])
        spans.append((index, index + 1))
        index += 1
    return "".join(out), spans, changed


def _hex_at(text: str, index: int) -> int | None:
    if index + 1 >= len(text):
        return None
    pair = text[index:index + 2]
    if pair[0] not in _HEX or pair[1] not in _HEX:
        return None
    return int(pair, 16)


def _hex4_at(text: str, index: int) -> bool:
    if index + 3 >= len(text):
        return False
    return all(char in _HEX for char in text[index:index + 4])


def _from_decoded(char: str, text: str, index: int, depth: int) -> tuple[str, int, bool, bool]:
    """Finish a peeled ``%`` or ``\\``. Returns char, end, raw, unresolved."""

    if char == "%":
        if depth > _PEEL_LIMIT:
            return ("%", index, False, True)
        byte = _hex_at(text, index)
        if byte is None:
            return ("%", index, False, False)
        return _from_decoded(chr(byte), text, index + 2, depth + 1)
    if char == "\\":
        return _from_backslash(text, index, depth)
    return (char, index, False, False)


def _from_backslash(text: str, index: int, depth: int) -> tuple[str, int, bool, bool]:
    """A backslash run collapses into the escaped character. ``%5C%5C%2F`` is one slash."""

    if depth > _PEEL_LIMIT:
        return ("\\", index, False, True)
    if index < len(text) and text[index] in "uU" and _hex4_at(text, index + 1):
        return _from_decoded(chr(int(text[index + 1:index + 5], 16)), text, index + 5, depth + 1)
    nxt = _read_logical(text, index, depth)
    if nxt is None:
        return ("\\", index, False, False)
    char, end, _raw, bad = nxt
    if char == "\\":
        return _from_backslash(text, end, depth + 1)
    if char in _ESCAPED:
        return (char, end, False, bad)
    return ("\\", index, False, bad)


def _read_logical(text: str, index: int, depth: int = 0) -> tuple[str, int, bool, bool] | None:
    """One logical character at ``index``.

    Percent-encoding, ``%25`` layers, ``\\uXXXX`` and backslash runs are one token. ``raw`` is true
    only for a single source character, so ``%20`` is a space that is not raw whitespace.
    The fourth value is true when the peel limit was hit.
    """

    if index >= len(text):
        return None
    if depth > _PEEL_LIMIT:
        return (text[index], index + 1, True, True)
    if text[index] == "%" and (byte := _hex_at(text, index + 1)) is not None:
        return _from_decoded(chr(byte), text, index + 3, depth + 1)
    if text[index] == "\\":
        return _from_backslash(text, index + 1, depth + 1)
    return (text[index], index + 1, True, False)


def _slash_pair_end(text: str, index: int) -> int:
    """Index after two slash tokens, or 0."""

    first = _read_logical(text, index)
    if first is None or first[0] != "/":
        return 0
    second = _read_logical(text, first[1])
    if second is None or second[0] != "/":
        return 0
    return second[1]


def _scheme_authority_start(text: str, index: int) -> int:
    """Index where the authority starts, or 0 when ``index`` is not an http(s) scheme.

    The colon and the slashes are the same tokens the authority walk uses. An encoded colon may
    sit between the scheme colon and the slashes (``https:%5C%3A%2F%2F``). Nothing else is rewritten.
    """

    if text[index:index + 5].lower() == "https":
        cursor = index + 5
    elif text[index:index + 4].lower() == "http":
        cursor = index + 4
    else:
        return 0
    colon = _read_logical(text, cursor)
    if colon is None or colon[0] != ":":
        return 0
    cursor = colon[1]
    extra = _read_logical(text, cursor)
    if extra is not None and extra[0] == ":":
        slashes = _slash_pair_end(text, extra[1])
        if slashes:
            return slashes
    return _slash_pair_end(text, cursor)


def _authority_userinfo(text: str) -> list[tuple[int, int]]:
    """Userinfo spans from one forward walk. The token reader is the only delimiter table.

    Before a colon, any end-delimiter (raw, or ``/`` ``?`` ``#`` and the other host delimiters in
    any encoding) ends the authority and there is no credential. In the password, only a raw
    space, tab, CR or LF ends the run; a quote, bracket or encoded slash stays inside it. The
    separator is the last ``@`` or ``%40`` before the first end-delimiter that follows a separator.
    After that separator, any end-delimiter in any form ends the authority. The span stops before
    the separator, so the host stays.
    """

    found: list[tuple[int, int]] = []
    index = 0
    length = len(text)
    while index < length:
        auth = _scheme_authority_start(text, index)
        if not auth:
            index += 1
            continue
        cursor = auth
        state = "before"
        separators: list[int] = []
        while cursor < length:
            token = _read_logical(text, cursor)
            if token is None:
                break
            char, end, raw, _bad = token
            if end <= cursor:
                end = cursor + 1
            if state == "before":
                if char in _END_CHARS:
                    break
                if char == ":":
                    state = "password"
                cursor = end
                continue
            if state == "password":
                if raw and char in _WS:
                    break
                if char == "@":
                    separators.append(cursor)
                    state = "after"
                cursor = end
                continue
            if char in _END_CHARS:
                break
            if char == "@":
                separators.append(cursor)
            cursor = end
        if separators:
            found.append((auth, separators[-1]))
        index = cursor if cursor > index else index + 1
    return found


def _compose(spans: list[tuple[int, int]], layer: list[tuple[int, int]]) -> list[tuple[int, int]]:
    return [(spans[start][0], spans[end - 1][1]) for start, end in layer]


def _decode_views(value: str) -> list[tuple[str, list[tuple[int, int]]]] | None:
    """Alternate a JSON peel and a percent peel. None means the cap was not a fixed point."""

    text = value
    spans = [(index, index + 1) for index in range(len(value))]
    views = [(text, spans)]
    for _ in range(_ESCAPE_PASSES):
        changed = False
        decoded, layer, json_changed = _decode_layer(text)
        if json_changed:
            spans = _compose(spans, layer)
            text = decoded
            views.append((text, spans))
            changed = True
        decoded, layer, pct_changed = _decode_pct_layer(text)
        if pct_changed:
            spans = _compose(spans, layer)
            text = decoded
            views.append((text, spans))
            changed = True
        if not changed:
            return views
    if _decode_layer(text)[2] or _decode_pct_layer(text)[2]:
        return None
    return views


def _collect_secret_hits(text: str, spans: list[tuple[int, int]], hits: list[tuple[int, int]]) -> None:
    def add(start: int, end: int) -> None:
        if end > start:
            hits.append((spans[start][0], spans[end - 1][1]))

    for start, end in _authority_userinfo(text):
        add(start, end)
    for match in _SECRET.finditer(text):
        add(match.start(), match.end())


def redact(value: str) -> str:
    """Replace credential-shaped substrings. Connection ids and ordinary error codes are left as they are.

    JSON and percent-encoding are peeled in turn, and every intermediate view is searched for secret
    tokens. Userinfo is one state walk on each view: before a colon any end-delimiter means there
    is no credential, the password keeps its delimiters, and the host ends at the first delimiter
    after the last ``@``. A value that is still changing after the shared cap is replaced as a whole.
    """

    if not value:
        return value
    views = _decode_views(value)
    if views is None:
        return "[redacted]"
    hits: list[tuple[int, int]] = []
    for text, spans in views:
        _collect_secret_hits(text, spans, hits)
    if not hits:
        return value
    hits.sort()
    merged: list[tuple[int, int]] = []
    for start, end in hits:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            continue
        merged.append((start, end))
    pieces: list[str] = []
    cursor = 0
    for start, end in merged:
        pieces.append(value[cursor:start])
        pieces.append("[redacted]")
        cursor = end
    pieces.append(value[cursor:])
    return "".join(pieces)


def _token_edge(text: str, index: int) -> bool:
    """A letter or digit binds. ``_`` and other non-letters separate tokens."""

    if index < 0 or index >= len(text):
        return True
    ch = text[index]
    return ch == "_" or not ch.isalnum()


def sounds_like_quota(text: object) -> bool:
    if not isinstance(text, str):
        return False
    for match in _QUOTA.finditer(text):
        if not _token_edge(text, match.start() - 1) or not _token_edge(text, match.end()):
            continue
        if _QUOTA_NEGATED.search(text[:match.start()]):
            continue
        if _QUOTA_DENIED_AFTER.match(text[match.end():]):
            continue
        return True
    return False
