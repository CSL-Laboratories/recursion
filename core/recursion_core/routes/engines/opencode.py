"""OpenCode engine driver, first slice: the parser of ``opencode run --format json`` (lane ENGINE-E3).

OpenCode is one of the engines the product names (``routes.engines.CATALOGUE``). It is used only when the operator
has installed and configured it: Recursion never installs it, never signs it in and never reads its login store.
This module is the first slice of its driver, the PARSER. It turns the lines that ``opencode run --format json``
prints into driver.v2 event drafts, each payload exactly event.v1's, and the end of a run into the run's result.
It starts no process, reads no file and registers nothing; no daemon loads it yet.

THE TWO PACKAGE LINES. OpenCode ships as two npm packages with the same binary name, ``opencode``: ``opencode-ai``
1.19.x (tag v1.19.5, anomalyco/opencode 810c0eba) and ``@opencode/cli`` 2.x (2.2.6, dc2ffcf8), the one OpenCode's
own docs now install (vault records 20261107-csl712-engine-clis-grounding-01 V20 and
20261109-csl816-opencode-session-resume-docs-01 F5). Both print one JSON object per line, ``{"type", "timestamp",
"sessionID", ...}``, of the types ``step_start``, ``text``, ``reasoning``, ``tool_use``, ``step_finish`` and
``error`` (1.19.5 packages/opencode/src/cli/cmd/run.ts:668-681 and :710-781; 2.2.6
packages/cli/src/run/noninteractive.ts:363-366 and :457-505); 2.x adds ``retry`` and ``background_wait``. The two
lines differ in two shapes, and the parser tells them apart on each line from the shape alone:

    tool_use  1.x: ``part.callID`` is the call's id (``part.id`` is OpenCode's own part id)
              2.x: ``part.id`` is the call's id (``part.partID`` is OpenCode's own part id)
    error     1.x: ``{"name", "data": {"message", "statusCode"?, "isRetryable", "responseHeaders"?, ...}}``
                   (packages/core/src/v1/session.ts:55-62, serialized by NamedError.toObject)
              2.x: ``{"type", "message", "status"?, "response"?, "retryAfterMs"?}``
                   (packages/core/src/session/to-session-error.ts:132-143)

Every fixture beside this module is SYNTHETIC: written by hand from those source reads, not captured from a run.

WHAT A LINE GIVES. Every line gives one ``engine.output`` draft first: the line exactly as printed, with its byte
offsets in the run's stdout, so a reader that restarts resumes without losing or repeating a line (an empty chunk
is not a line and gives nothing). Then:

    tool_use      ``tool.call`` and ``tool.result``: OpenCode prints a call once, after it completed or failed
    step_finish   ``usage``, span ``call``: one step is one model call
    error         ``limit``, when the error is a typed 429 (below); otherwise nothing typed
    text          nothing typed: the text is kept as the run's answer (model payload, never classified)
    anything else nothing typed: step_start, reasoning, retry, background_wait, a type not known here, and a line
                  that is not one JSON object (it is counted in ``anomalies``)

THE TYPED LIMIT. Only a typed status of 429 is a plan limit: on 1.x ``error.data.statusCode``, on 2.x
``error.status``, each an integer. OpenCode retries a rate limit itself, up to five times, before it prints the
error (1.19.5 packages/opencode/src/session/retry.ts:31, :85-98), so the line is the outcome and not one attempt.
``reset_at`` comes from the wait the vendor asked for: on 1.x the response header ``retry-after-ms`` when it holds a
number (then nothing else is read, as in OpenCode), else ``retry-after`` as seconds, else as an HTTP date with a
named zone and a real calendar day and time (retry.ts:47-75); on 2.x ``retryAfterMs``. The wait is counted from the moment the line is parsed, on the clock given, so a reset is never
earlier than the vendor's. No wait, or one that is not a positive finite number, leaves ``reset_at`` null and the
breaker uses its own cooldown. The limit is on the connection, never on another one, and names no model or bucket:
OpenCode's stream names neither. Any other error is not a limit: free text in an error envelope that sounds like a
quota makes the result a ``short_fault`` (``contract.sounds_like_quota``, grill Q23), and nothing else does.

USAGE. ``step_finish`` carries ``tokens`` ``{input, output, reasoning, cache: {read, write}}`` and ``cost``. Both
lines compute them the same way: ``input`` excludes the cached tokens, ``output`` excludes reasoning, and a count
the provider did not report is written as 0 (1.19.5 packages/opencode/src/session/session.ts:338-372; 2.2.6
packages/core/src/session/usage.ts:9-19). So a 0 cannot be told from "not reported": it is drafted ``unknown``,
never 0. A positive count is ``known``, ``observed``. event.v1 counts reasoning inside ``output_tokens``, so
``output_tokens`` is OpenCode's output plus its reasoning when both are positive, and a ``lower_bound`` (the one that
is positive) when the other is not known. The
cost is OpenCode's own figure, from its price table or from what the endpoint reported, and the stream does not say
which: a positive cost is ``known``, ``modelled``; 0 is ``unknown``. A step of a sub-agent session is not in the
stream (1.19.5 run.ts:712 prints only the run's own session), so the calls drafted here are each exact and their
sum is not the run's whole spend.

TOOL CALLS. ``call_id`` is the engine's id for the call and ``name`` the tool's. ``input_digest`` is the SHA-256 of
the input as RFC 8785 writes it (``control.methods.canonical_text``, the form event.v1 names ``rfc8785/v1``);
``input`` is carried only when it is a JSON object. An input that has no RFC 8785 text (a whole number outside the
exact range, nesting deeper than 64, half a surrogate pair), or a call whose ``input`` field is absent, cannot be
digested, so neither draft is made and the line is counted in ``anomalies``; its ``engine.output`` still carries it.
A completed call's result is ``ok`` with its output; a failed one is ``error`` with its error text; ``truncated`` is
true when OpenCode marked the output shortened (``state.metadata.truncated``, 1.19.5 tool/tool.ts:131-142) or gave
none. Nothing here reads a permission denial out of that text.

TEXT A CONSUMER WRITES AS UTF-8. A JSON escape can carry half a surrogate pair, which Python keeps and UTF-8
cannot write. In the answer and in a tool's output each one becomes U+FFFD; an id holding one (a session id, a
call id, a tool name) is refused into ``anomalies``.

THE RESULT, at ``end(exit_code)``: ``plan_limit`` after a typed 429; else ``auth_failed`` after a typed sign-in
error (1.x ``ProviderAuthError``, 2.x ``provider.auth``); else, after any other error, ``short_fault`` when its
envelope's text sounds like a quota and ``engine_error`` otherwise; else ``ok`` (code null) on exit 0, and
``engine_exited`` on any other exit or none. ``detail`` names the typed error only when it is one of the error
names OpenCode's source defines (``KNOWN_ERRORS``), and says ``error`` otherwise: never the engine's own words.

What is NOT here: the driver itself (start, send, interrupt, resume), the ``serve`` worker, the permission bridge,
and any registration (later slices; a registration will state ``default_executable='opencode'``). The guard and the
child environment are other lanes' files.
"""

from __future__ import annotations

import calendar
import copy
import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_tz
from types import MappingProxyType

from recursion_core.routes import contract as c

ENGINE = "opencode"
# The command a registration will state as ``default_executable``: argv[0] is the binary option composition gives,
# and this otherwise.
DEFAULT_EXECUTABLE = "opencode"
# What the child environment must hold for OpenCode (asked of lane LAUNCH-CHECK's profile; not set here): the
# updater off (vault 20261107-csl712-engine-clis-grounding-01 V22).
CHILD_ENV = (("OPENCODE_DISABLE_AUTOUPDATE", "1"),)

# driver.v2 flags of the one-shot drive this parser serves. Only reading limits from typed fields and reading
# usage are true; every call that needs a flag is refused before any side effect.
FLAGS = MappingProxyType({"multi_turn": False, "mid_turn_input": False, "interrupt": False, "resume": False,
                          "fork": False, "permission_bridge": False, "typed_limits": True, "usage": True})

# The error names OpenCode's source defines; ``detail`` names one of these, or says "error".
KNOWN_ERRORS = frozenset({
    # 1.19.5 NamedError names (packages/core/src/v1/session.ts:48-67, packages/core/src/util/error.ts)
    "APIError", "ProviderAuthError", "MessageOutputLengthError", "MessageAbortedError", "StructuredOutputError",
    "ContextOverflowError", "ContentFilterError", "UnknownError",
    # 2.2.6 session error types (packages/core/src/session/to-session-error.ts:34-143)
    "provider.rate-limit", "provider.auth", "provider.quota", "provider.chatgpt-limit", "provider.free-limit",
    "provider.budget-limit", "provider.model-budget-limit", "provider.go-limit", "provider.content-filter",
    "provider.transport", "provider.internal", "provider.invalid-output", "provider.invalid-request",
    "provider.unsupported-operation", "provider.unknown", "provider.timeout", "provider.no-route",
    "permission.rejected", "tool.execution", "unknown", "aborted",
})

LIMIT_STATUS = 429
MAX_TOOL_ID = 128     # event.v1 tool.call / tool.result call_id
MAX_TOOL_NAME = 256   # event.v1 tool.call name
MAX_SESSION_ID = 256  # driver.v2 engine_session_id
MAX_EXACT = 2 ** 53 - 1
_LAST_EPOCH = 253402300798.0   # 9999-12-31T23:59:58Z: rounded up, still in the year 9999

_CONNECTION = re.compile(r"(subscription|api_key|passthrough):.+")
_ZONE = re.compile(r"GMT|UTC|UT|Z|[+-][0-9]{4}", re.IGNORECASE)   # Date.parse reads zone names in any case
_ZONE_AFTER_TIME = re.compile(r"[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?\s+(\S+)\s*$")   # the one word after the time
_SURROGATE = re.compile("[" + chr(0xD800) + "-" + chr(0xDFFF) + "]")
REPLACEMENT = chr(0xFFFD)
_JS_FLOAT = re.compile(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
_LINE_BREAK = re.compile(r"[\r\n]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def declaration() -> dict:
    """driver.v2's root record for the one-shot drive."""
    return {"engine": ENGINE, "driver": 2, "flags": dict(FLAGS), "input_ack": False}


def build_argv(model: str, *, binary: str | None = None) -> list[str]:
    """argv for one turn of ``opencode run``. The prompt goes on standard input, never into argv.

    ``binary`` is the binary option composition gives (argv[0] is it, else ``DEFAULT_EXECUTABLE``). 1.19.5 reads a
    piped standard input as the message (run.ts:417-418), so no prompt can be read as an option.
    """
    if binary is not None and (not isinstance(binary, str) or not binary or _CONTROL.search(binary)):
        raise ValueError("binary")
    if not model_ok(model):
        raise ValueError("model")
    return [binary if binary is not None else DEFAULT_EXECUTABLE, "run", "--format", "json", "--model", model]


def model_ok(model: object) -> bool:
    """The one rule for a model OpenCode is given (``build_argv``) or reports usage for (``Parser``).

    ``<provider>/<model>``: both parts not empty, at most 256 characters in all, no leading dash, and no space,
    control character or half of a surrogate pair. A further ``/`` inside the model part is allowed.
    """
    if not isinstance(model, str) or len(model) > 256 or model.startswith("-") or "/" not in model:
        return False
    if "" in model.split("/", 1) or _CONTROL.search(model) or _SURROGATE.search(model):
        return False
    return not any(ch.isspace() for ch in model)


def input_digest(tool_input: object) -> str:
    """event.v1's ``input_digest``: SHA-256 of the input as RFC 8785 writes it. ``ValueError`` when it has no text."""
    from recursion_core.control.methods import canonical_text   # local: routes must not load control at import

    return hashlib.sha256(canonical_text(tool_input).encode("utf-8")).hexdigest()


class Parser:
    """One run's stdout, a line at a time. One reader feeds it; it is not shared between threads.

    ``connection_id`` is the route's connection (``<kind>:<id>``, ``contract.connection_key``) and ``model`` its model
    (null for the engine's default): both are copied into ``usage`` and ``limit``. ``wall`` reads Unix seconds, for
    event.v1's ``reset_at``; ``clock`` is the breaker's clock, for the route contract's limit signal.
    """

    def __init__(self, *, connection_id: str, model: str | None, wall: Callable[[], float] = time.time,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(connection_id, str) or len(connection_id) > 256 or not _CONNECTION.fullmatch(connection_id) \
                or _LINE_BREAK.search(connection_id) or _SURROGATE.search(connection_id):
            raise ValueError("connection_id")
        if model is not None and not model_ok(model):
            raise ValueError("model")
        if not callable(wall) or not callable(clock):   # refused here, not mid-stream at the first 429
            raise ValueError("clock")
        self._connection, self._model, self._wall, self._clock = connection_id, model, wall, clock
        self._offset = 0
        self._lines = 0
        self._text: list[str] = []
        self._errors: list[dict] = []
        self._limit: dict | None = None       # the route contract's limit signal, from the latest typed 429
        self._usage: list[dict] = []          # the route contract's usage events
        self._result: tuple[str | None, str] | None = None
        self.engine_session_id: str | None = None
        self.anomalies: list[str] = []

    # -- the stream ---------------------------------------------------------------------------------------
    def feed(self, line: bytes | str) -> list[dict]:
        """The drafts for one line of stdout, its line break included (the last line may have none)."""
        if self._result is not None:
            raise RuntimeError("the run has ended")
        if isinstance(line, str):
            raw = line.encode("utf-8", errors="surrogatepass")
        elif isinstance(line, (bytes, bytearray, memoryview)):
            raw = bytes(line)
        else:
            raise TypeError("a line is bytes or str")
        if b"\n" in raw[:-1]:
            raise ValueError("one line at a time")
        if not raw:
            return []
        start, lines = self._offset, self._lines
        session, anomalies = self.engine_session_id, len(self.anomalies)
        self._offset += len(raw)
        self._lines += 1
        drafts = [_draft("engine.output", {"stream": "stdout", "text": raw.decode("utf-8", errors="replace"),
                                           "offset_start": start, "offset_end": self._offset})]
        try:
            event = self._load(raw)
            if event is not None:
                drafts.extend(self._typed(event))
        except BaseException:
            # Only an injected clock can raise here. Everything the line had changed is put back (the offsets, the
            # line count, the session id, its anomalies; an error and a limit are kept only after the clocks were
            # read), so the line counts as not read and feeding it again gives the same drafts at the same offsets.
            self._offset, self._lines, self.engine_session_id = start, lines, session
            del self.anomalies[anomalies:]
            raise
        return drafts

    def end(self, exit_code: int | None) -> list[dict]:
        """The run's ``result`` draft, once its process has exited (``exit_code`` None: no status was read)."""
        if self._result is not None:
            raise RuntimeError("the run has ended")
        if exit_code is not None and (type(exit_code) is not int):
            raise ValueError("exit_code")
        self._result = self._outcome(exit_code)
        code, detail = self._result
        return [_draft("result", {"code": code, "detail": detail})]

    @property
    def text(self) -> str:
        """The run's answer: every ``text`` part, in order. Model payload: shown, never classified."""
        return "".join(self._text)

    def route_result(self, request_id: str) -> dict:
        """The run as the route contract's result, which the breaker books (``ConnectionBreaker.record``)."""
        if self._result is None:
            raise RuntimeError("the run has not ended")
        if not isinstance(request_id, str) or not request_id:   # as contract.validate_request takes it in
            raise ValueError("request_id")
        code, detail = self._result
        # ``self._limit`` is set only by a typed 429, which always makes the code plan_limit. Each result gets its
        # own copies: a caller that annotates one cannot change the parser's record or another caller's result.
        return c.result(request_id, code, text=self.text, limit=copy.deepcopy(self._limit),
                        events=copy.deepcopy(self._usage), detail=detail)

    # -- one line -----------------------------------------------------------------------------------------
    def _load(self, raw: bytes) -> dict | None:
        body = raw.strip()
        if not body:
            return None
        try:
            event = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_no_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            self._anomaly("not_json")
            return None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            self._anomaly("not_an_event")
            return None
        return event

    def _typed(self, event: dict) -> list[dict]:
        self._session(event.get("sessionID"))
        kind = event["type"]
        if kind == "text":
            part = event.get("part")
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                self._text.append(_SURROGATE.sub(REPLACEMENT, part["text"]))
            return []
        if kind == "tool_use":
            return self._tool(event.get("part"))
        if kind == "step_finish":
            return self._step(event.get("part"))
        if kind == "error":
            return self._error(event.get("error"))
        return []

    def _session(self, value: object) -> None:
        if value is None:
            return
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_SESSION_ID or _CONTROL.search(value) \
                or _SURROGATE.search(value):
            self._anomaly("session_id_invalid")
        elif self.engine_session_id is None:
            self.engine_session_id = value
        elif value != self.engine_session_id:
            self._anomaly("second_session_id")

    def _tool(self, part: object) -> list[dict]:
        if not isinstance(part, dict) or part.get("type") != "tool" or not isinstance(part.get("state"), dict):
            self._anomaly("tool_shape")
            return []
        state = part["state"]
        if state.get("status") not in ("completed", "error"):
            self._anomaly("tool_status")
            return []
        call_id = part.get("callID") if "callID" in part else part.get("id") if "partID" in part else None
        name = part.get("tool")
        if not _one_line(call_id, MAX_TOOL_ID) or not _one_line(name, MAX_TOOL_NAME):
            self._anomaly("tool_id")
            return []
        if "input" not in state:   # no input was received: nothing may be digested in its place
            self._anomaly("tool_input_missing")
            return []
        tool_input = state["input"]
        try:
            digest = input_digest(tool_input)
        except ValueError:
            self._anomaly("tool_input_has_no_rfc8785_text")
            return []
        completed = state["status"] == "completed"
        output = state.get("output") if completed else state.get("error")
        if not isinstance(output, str):
            self._anomaly("tool_output_missing")
        metadata = state.get("metadata")
        shortened = isinstance(metadata, dict) and metadata.get("truncated") is True   # OpenCode's own marker
        return [
            _draft("tool.call", {"call_id": call_id, "name": name, "input_digest": digest,
                                 "input": tool_input if isinstance(tool_input, dict) else None}),
            _draft("tool.result", {"call_id": call_id, "status": "ok" if completed else "error",
                                   "text": _SURROGATE.sub(REPLACEMENT, output) if isinstance(output, str) else "",
                                   "output_ref": None, "truncated": shortened or not isinstance(output, str)}),
        ]

    def _step(self, part: object) -> list[dict]:
        if not isinstance(part, dict) or part.get("type") != "step-finish":
            self._anomaly("step_shape")
            return []
        tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        output, reasoning = _positive_int(tokens.get("output")), _positive_int(tokens.get("reasoning"))
        if output is not None and reasoning is not None:
            output_tokens = _known(output + reasoning, "observed")
        elif output is not None or reasoning is not None:   # the other part is not known: at least this one
            output_tokens = {"value": output if output is not None else reasoning, "quality": "lower_bound",
                             "provenance": "observed"}
        else:
            output_tokens = _UNKNOWN
        payload = {
            "connection_id": self._connection, "model": self._model, "bucket": None, "span": "call",
            "input_tokens": _count(tokens.get("input")),
            "output_tokens": dict(output_tokens),
            "reasoning_tokens": _count(tokens.get("reasoning")),
            "cache_read_tokens": _count(cache.get("read")),
            "cache_write_tokens": _count(cache.get("write")),
            "cost_usd": _cost(part.get("cost")),
        }
        # The route contract's events are free-form; each count keeps its quality there too, so a lower bound or an
        # unknown never reads as an exact number to a consumer of route_result().
        self._usage.append(c.event("usage", "opencode.step_finish", {
            name: dict(payload[name]) for name in ("input_tokens", "output_tokens", "reasoning_tokens",
                                                   "cache_read_tokens", "cache_write_tokens", "cost_usd")}))
        return [_draft("usage", payload)]

    def _error(self, error: object) -> list[dict]:
        if isinstance(error, dict) and isinstance(error.get("name"), str):        # 1.x NamedError
            data = error.get("data") if isinstance(error.get("data"), dict) else {}
            tag, status, message = error["name"], data.get("statusCode"), data.get("message")
            auth = tag == "ProviderAuthError"
            wait_of = lambda now: _retry_after_s(data.get("responseHeaders"), now)   # noqa: E731
            source = "opencode.error.data.statusCode"
        elif isinstance(error, dict) and isinstance(error.get("type"), str):      # 2.x session error
            tag, status, message = error["type"], error.get("status"), error.get("message")
            auth = tag == "provider.auth"
            wait_of = lambda now: _positive_finite(error.get("retryAfterMs"), scale=1000.0)   # noqa: E731
            source = "opencode.error.status"
        else:
            self._anomaly("error_shape")
            self._errors.append({"tag": "error", "status": None, "auth": False, "quota_text": False})
            return []
        status = status if type(status) is int else None
        limit = reset_at = None
        if status == LIMIT_STATUS:   # both clocks are read before anything is kept (see ``feed``)
            now = self._wall()     # read once: an HTTP date's wait and the reset share this one reading
            wait = wait_of(now)
            reset_at = _timestamp(now + wait) if wait is not None else None
            if reset_at is None:
                wait = None   # a wait too far off to write is not known: the event and the breaker agree
            limit = c.limit_signal("connection", source, reset_at=self._clock() + wait if wait is not None else None)
        self._errors.append({"tag": tag if tag in KNOWN_ERRORS else "error", "status": status, "auth": auth,
                             "quota_text": c.sounds_like_quota(message)})
        if limit is None:
            return []
        self._limit = limit
        return [_draft("limit", {"connection_id": self._connection, "scope": "connection", "kind": "plan_limit",
                                 "model": None, "bucket": None, "reset_at": reset_at})]

    # -- the end ------------------------------------------------------------------------------------------
    def _outcome(self, exit_code: int | None) -> tuple[str | None, str]:
        if self._limit is not None:
            return c.PLAN_LIMIT, f"status {LIMIT_STATUS}" + (" with a reset" if self._limit["reset_at"] is not None
                                                             else " without a reset")
        auth = next((e for e in self._errors if e["auth"]), None)
        if auth is not None:
            return c.AUTH_FAILED, auth["tag"]
        if self._errors:
            first = self._errors[0]
            named = first["tag"] + (f" status {first['status']}" if first["status"] is not None else "")
            if any(e["quota_text"] for e in self._errors):
                return c.SHORT_FAULT, f"{named}: untyped quota-like text"
            return c.ENGINE_ERROR, named
        if exit_code == 0:
            return None, ""
        if exit_code is None:
            return c.ENGINE_EXITED, "no exit status"
        return c.ENGINE_EXITED, f"exit {exit_code} without a typed error"

    def _anomaly(self, what: str) -> None:
        self.anomalies.append(f"line {self._lines}: {what}")


# -- helpers --------------------------------------------------------------------------------------------------
_UNKNOWN = {"value": None, "quality": "unknown", "provenance": "unavailable"}


def _draft(kind: str, payload: dict) -> dict:
    return {"type": kind, "payload": payload}


def _known(value: int | float, provenance: str) -> dict:
    return {"value": value, "quality": "known", "provenance": provenance}


def _positive_int(value: object) -> int | None:
    """A token count OpenCode printed, when it is a whole number above 0 that every JSON reader keeps exact."""
    if type(value) is float and value.is_integer():
        value = int(value)
    if type(value) is not int or not 0 < value <= MAX_EXACT:
        return None
    return value


def _count(value: object) -> dict:
    number = _positive_int(value)
    return _known(number, "observed") if number is not None else dict(_UNKNOWN)


def _cost(value: object) -> dict:
    number = _positive_finite(value)
    return _known(number, "modelled") if number is not None else dict(_UNKNOWN)


def _positive_finite(value: object, *, scale: float = 1.0) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:   # a whole number too large for a float
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number / scale


def _js_float(text: str) -> float | None:
    """``Number.parseFloat``: the longest number at the start of the text, or None."""
    match = _JS_FLOAT.match(text)
    return float(match.group(1)) if match else None


def _retry_after_s(headers: object, now: float) -> float | None:
    """The wait 1.x response headers ask for, in seconds, read the way OpenCode reads them (retry.ts:47-75).

    ``now`` is the wall clock's reading for this line. ``retry-after-ms`` when it holds a number (then nothing else
    is read, as in OpenCode, even when that number is not a usable wait), else ``retry-after`` as seconds, else as
    an HTTP date. A wait that is not a positive number is None. Header names are matched in any case. An HTTP date
    counts only when the ONE word after its time, and the last word of the value, is the zone ``GMT``, ``UT``,
    ``UTC``, ``Z`` or a numeric offset (``parsedate_tz`` reads an unknown zone, no zone, and a zone with a word
    between it and the time as offset 0; ``Date.parse`` refuses the first and the third and reads the second in the
    machine's own zone), and only with a real calendar day and time (``Date.parse`` refuses "32 Nov" or "25:00",
    which ``calendar.timegm`` would roll forward).
    """
    if not isinstance(headers, Mapping):
        return None
    lower = {k.lower(): v for k, v in headers.items() if isinstance(k, str) and isinstance(v, str)}
    millis = lower.get("retry-after-ms")
    if millis:
        number = _js_float(millis)
        if number is not None:
            return number / 1000.0 if number > 0 else None   # an infinite wait has no timestamp: no reset
    after = lower.get("retry-after")
    if not after:
        return None
    number = _js_float(after)
    if number is not None:
        return number if number > 0 else None
    try:
        parts = parsedate_tz(after)
        zone = _ZONE_AFTER_TIME.search(after)
        if parts is None or zone is None or not _ZONE.fullmatch(zone.group(1)):
            return None
        year, month, day, hour, minute, second = parts[:6]
        # parsedate_tz has already refused a month it does not name.
        if not (1 <= day <= calendar.monthrange(year, month)[1] and 0 <= hour <= 23 and 0 <= minute <= 59
                and 0 <= second <= 59):
            return None
        moment = calendar.timegm(parts[:6] + (0, 0, 0)) - parts[9]
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    wait = moment - now
    return wait if wait > 0 else None


def _timestamp(epoch: float) -> str | None:
    """common.v1's timestamp, rounded UP to the millisecond (a reset is never earlier than asked).

    None when it cannot be written: after the last second of the year 9999, infinite, or not a number.
    """
    if not epoch <= _LAST_EPOCH:
        return None
    millis = math.ceil(epoch * 1000)
    moment = datetime.fromtimestamp(millis // 1000, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{millis % 1000:03d}Z"


def _one_line(value: object, limit: int) -> bool:
    """An id event.v1 can carry: 1 to ``limit`` characters, one line, and writable as UTF-8."""
    return isinstance(value, str) and 1 <= len(value) <= limit and not _LINE_BREAK.search(value) \
        and not _SURROGATE.search(value)


def _unique_keys(pairs: list) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _no_constant(name: str) -> None:
    raise ValueError(f"not JSON: {name}")
