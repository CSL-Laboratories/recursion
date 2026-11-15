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
offsets in the run's stdout, so a reader that restarts resumes without losing or repeating a line. Then:

    tool_use      ``tool.call`` and ``tool.result``: OpenCode prints a call once, after it completed or failed
    step_finish   ``usage``, span ``call``: one step is one model call
    error         ``limit``, when the error is a typed 429 (below); otherwise nothing typed
    text          nothing typed: the text is kept as the run's answer (model payload, never classified)
    anything else nothing typed: step_start, reasoning, retry, background_wait, a type not known here, and a line
                  that is not one JSON object (it is counted in ``anomalies``)

THE TYPED LIMIT. Only a typed status of 429 is a plan limit: on 1.x ``error.data.statusCode``, on 2.x
``error.status``, each an integer. OpenCode retries a rate limit itself, up to five times, before it prints the
error (1.19.5 packages/opencode/src/session/retry.ts:31, :85-98), so the line is the outcome and not one attempt.
``reset_at`` comes from the wait the vendor asked for: on 1.x the response headers ``retry-after-ms``, then
``retry-after`` (seconds, else an HTTP date), read the way OpenCode reads them (retry.ts:47-75); on 2.x
``retryAfterMs``. The wait is counted from the moment the line is parsed, on the clock given, so a reset is never
earlier than the vendor's. No wait, or one that is not a positive finite number, leaves ``reset_at`` null and the
breaker uses its own cooldown. The limit is on the connection, never on another one, and names no model or bucket:
OpenCode's stream names neither. Any other error is not a limit: free text in an error envelope that sounds like a
quota makes the result a ``short_fault`` (``contract.sounds_like_quota``, grill Q23), and nothing else does.

USAGE. ``step_finish`` carries ``tokens`` ``{input, output, reasoning, cache: {read, write}}`` and ``cost``. Both
lines compute them the same way: ``input`` excludes the cached tokens, ``output`` excludes reasoning, and a count
the provider did not report is written as 0 (1.19.5 packages/opencode/src/session/session.ts:338-372; 2.2.6
packages/core/src/session/usage.ts:9-19). So a 0 cannot be told from "not reported": it is drafted ``unknown``,
never 0. A positive count is ``known``, ``observed``. event.v1 counts reasoning inside ``output_tokens``, so
``output_tokens`` is OpenCode's output plus its reasoning (``lower_bound`` when only the reasoning is positive). The
cost is OpenCode's own figure, from its price table or from what the endpoint reported, and the stream does not say
which: a positive cost is ``known``, ``modelled``; 0 is ``unknown``. A step of a sub-agent session is not in the
stream (1.19.5 run.ts:712 prints only the run's own session), so the calls drafted here are each exact and their
sum is not the run's whole spend.

TOOL CALLS. ``call_id`` is the engine's id for the call and ``name`` the tool's. ``input_digest`` is the SHA-256 of
the input as RFC 8785 writes it (``control.methods.canonical_text``, the form event.v1 names ``rfc8785/v1``);
``input`` is carried only when it is a JSON object. An input that has no RFC 8785 text (a whole number outside the
exact range, or nesting deeper than 64) cannot be digested, so neither draft is made and the line is counted in
``anomalies``; its ``engine.output`` still carries it. A completed call's result is ``ok`` with its output; a failed
one is ``error`` with its error text. Nothing here reads a permission denial out of that text.

THE RESULT, at ``end(exit_code)``: ``plan_limit`` after a typed 429; else ``auth_failed`` after a typed sign-in
error (1.x ``ProviderAuthError``, 2.x ``provider.auth``); else, after any other error, ``short_fault`` when its
envelope's text sounds like a quota and ``engine_error`` otherwise; else ``ok`` (code null) on exit 0, and
``engine_exited`` on any other exit or none. ``detail`` names the typed error only, never the engine's own text.

What is NOT here: the driver itself (start, send, interrupt, resume), the ``serve`` worker, the permission bridge,
and any registration (later slices; a registration will state ``default_executable='opencode'``). The guard and the
child environment are other lanes' files.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_tz

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
FLAGS = {"multi_turn": False, "mid_turn_input": False, "interrupt": False, "resume": False, "fork": False,
         "permission_bridge": False, "typed_limits": True, "usage": True}

LIMIT_STATUS = 429
MAX_TOOL_ID = 128     # event.v1 tool.call / tool.result call_id
MAX_TOOL_NAME = 256   # event.v1 tool.call name
MAX_SESSION_ID = 256  # driver.v2 engine_session_id
MAX_EXACT = 2 ** 53 - 1
_LAST_EPOCH = 253402300798.0   # 9999-12-31T23:59:58Z: rounded up, still in the year 9999

_CONNECTION = re.compile(r"(subscription|api_key|passthrough):.+")
_TAG = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,63}")
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
    if binary is not None and (not isinstance(binary, str) or not binary or "\x00" in binary):
        raise ValueError("binary")
    if not isinstance(model, str) or not 3 <= len(model) <= 256 or model.startswith("-") or "/" not in model:
        raise ValueError("model")
    if model.split("/", 1)[0] == "" or _CONTROL.search(model) or any(ch.isspace() for ch in model):
        raise ValueError("model")
    return [binary if binary is not None else DEFAULT_EXECUTABLE, "run", "--format", "json", "--model", model]


def input_digest(tool_input: object) -> str:
    """event.v1's ``input_digest``: SHA-256 of the input as RFC 8785 writes it. ``ValueError`` when it has no text."""
    from recursion_core.control.methods import canonical_text   # local: routes must not load control at import

    return hashlib.sha256(canonical_text(tool_input).encode("utf-8")).hexdigest()


def route_argv(request: dict, *, binary: str | None = None) -> list[str]:
    """``ProfileRouteAdapter``'s argv hook for one route request. A model OpenCode cannot take is a typed refusal."""
    try:
        return build_argv(request["route"]["model"], binary=binary)
    except ValueError as err:
        raise c.RouteError(c.REQUEST_INVALID, f"route.{err}") from None


class OneShot:
    """The one-shot drive on today's route contract: ``ProfileRouteAdapter`` calls it once the guard has passed.

    ``drive(process, request, deadline_at, events) -> result``: the prompt is written to standard input, which is
    then closed (OpenCode reads it to its end as the message), and stdout is read a line at a time until it ends.
    ``clock`` is the deadline and breaker clock; a read that would pass ``deadline_at`` raises ``Deadline``, which
    the adapter fences for this request only. A route whose connection the parser refuses writes nothing.
    """

    def __init__(self, clock: Callable[[], float], *, wall: Callable[[], float] = time.time) -> None:
        self._clock, self._wall = clock, wall

    def __call__(self, process, request: dict, deadline_at: float, events: list) -> dict:
        from recursion_core.routes.base import Deadline       # local: base imports nothing of this module
        from recursion_core.routes.launch import ReadTimeout

        route = request["route"]
        try:
            parser = Parser(connection_id=c.connection_key(route), model=route["model"], wall=self._wall,
                            clock=self._clock)
        except ValueError as err:
            return c.result(request["request_id"], c.REQUEST_INVALID, detail=f"route.{err}")
        process.send(request["prompt"])
        process.close_stdin()
        while True:
            remaining = deadline_at - self._clock()
            if remaining <= 0:
                raise Deadline()
            try:
                line = process.read_line(remaining)
            except ReadTimeout:
                raise Deadline() from None
            if line is None:
                break
            parser.feed(line if line.endswith("\n") else line + "\n")
        parser.end(None)   # today's EngineProcess gives no exit status: the stream's last step decides
        result = parser.route_result(request["request_id"])
        events.extend(result["events"])
        result["events"] = events
        return result


class Parser:
    """One run's stdout, a line at a time. One reader feeds it; it is not shared between threads.

    ``connection_id`` is the route's connection (``<kind>:<id>``, ``contract.connection_key``) and ``model`` its model
    (null for the engine's default): both are copied into ``usage`` and ``limit``. ``wall`` reads Unix seconds, for
    event.v1's ``reset_at``; ``clock`` is the breaker's clock, for the route contract's limit signal.
    """

    def __init__(self, *, connection_id: str, model: str | None, wall: Callable[[], float] = time.time,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(connection_id, str) or len(connection_id) > 256 or not _CONNECTION.fullmatch(connection_id) \
                or _LINE_BREAK.search(connection_id):
            raise ValueError("connection_id")
        if model is not None and (not isinstance(model, str) or not model):
            raise ValueError("model")
        self._connection, self._model, self._wall, self._clock = connection_id, model, wall, clock
        self._offset = 0
        self._lines = 0
        self._text: list[str] = []
        self._errors: list[dict] = []
        self._limit: dict | None = None       # the route contract's limit signal, from the latest typed 429
        self._usage: list[dict] = []          # the route contract's usage events
        self._result: tuple[str | None, str] | None = None
        self._finish: str | None = None       # the reason of the latest step_finish, a typed field
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
        start = self._offset
        self._offset += len(raw)
        self._lines += 1
        drafts = [_draft("engine.output", {"stream": "stdout", "text": raw.decode("utf-8", errors="replace"),
                                           "offset_start": start, "offset_end": self._offset})]
        event = self._load(raw)
        if event is not None:
            drafts.extend(self._typed(event))
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
        code, detail = self._result
        # ``self._limit`` is set only by a typed 429, which always makes the code plan_limit.
        return c.result(request_id, code, text=self.text, limit=self._limit, events=list(self._usage),
                        detail=detail)

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
                self._text.append(part["text"])
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
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_SESSION_ID or _CONTROL.search(value):
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
        tool_input = state.get("input")
        try:
            digest = input_digest(tool_input)
        except ValueError:
            self._anomaly("tool_input_has_no_rfc8785_text")
            return []
        completed = state["status"] == "completed"
        output = state.get("output") if completed else state.get("error")
        if not isinstance(output, str):
            self._anomaly("tool_output_missing")
        return [
            _draft("tool.call", {"call_id": call_id, "name": name, "input_digest": digest,
                                 "input": tool_input if isinstance(tool_input, dict) else None}),
            _draft("tool.result", {"call_id": call_id, "status": "ok" if completed else "error",
                                   "text": output if isinstance(output, str) else "", "output_ref": None,
                                   "truncated": not isinstance(output, str)}),
        ]

    def _step(self, part: object) -> list[dict]:
        if not isinstance(part, dict) or part.get("type") != "step-finish":
            self._anomaly("step_shape")
            return []
        self._finish = part.get("reason") if isinstance(part.get("reason"), str) else None
        tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        output, reasoning = _positive_int(tokens.get("output")), _positive_int(tokens.get("reasoning"))
        if output is not None:
            output_tokens = _known(output + (reasoning or 0), "observed")
        elif reasoning is not None:
            output_tokens = {"value": reasoning, "quality": "lower_bound", "provenance": "observed"}
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
        self._usage.append(c.event("usage", "opencode.step_finish", {
            name: payload[name]["value"] for name in ("input_tokens", "output_tokens", "reasoning_tokens",
                                                      "cache_read_tokens", "cache_write_tokens", "cost_usd")}))
        return [_draft("usage", payload)]

    def _error(self, error: object) -> list[dict]:
        if isinstance(error, dict) and isinstance(error.get("name"), str):        # 1.x NamedError
            data = error.get("data") if isinstance(error.get("data"), dict) else {}
            tag, status, message = error["name"], data.get("statusCode"), data.get("message")
            auth = tag == "ProviderAuthError"
            wait = _retry_after_s(data.get("responseHeaders"), self._wall)
            source = "opencode.error.data.statusCode"
        elif isinstance(error, dict) and isinstance(error.get("type"), str):      # 2.x session error
            tag, status, message = error["type"], error.get("status"), error.get("message")
            auth = tag == "provider.auth"
            wait = _positive_finite(error.get("retryAfterMs"), scale=1000.0)
            source = "opencode.error.status"
        else:
            self._anomaly("error_shape")
            self._errors.append({"tag": "error", "status": None, "auth": False, "quota_text": False})
            return []
        status = status if type(status) is int else None
        self._errors.append({"tag": tag if _TAG.fullmatch(tag) else "error", "status": status, "auth": auth,
                             "quota_text": c.sounds_like_quota(message)})
        if status != LIMIT_STATUS:
            return []
        reset_at = _timestamp(self._wall() + wait) if wait is not None else None
        if reset_at is None:
            wait = None   # a wait too far off to write is not known: the event and the breaker agree
        self._limit = c.limit_signal("connection", source,
                                     reset_at=self._clock() + wait if wait is not None else None)
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
            # No exit status was read (today's EngineProcess gives none): the stream's own end-of-turn sign
            # decides, a last step that finished with reason "stop". OpenCode's loop goes on after "tool-calls".
            if self._finish == "stop":
                return None, ""
            return c.ENGINE_EXITED, "no exit status and no finished step"
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


def _retry_after_s(headers: object, wall: Callable[[], float]) -> float | None:
    """The wait 1.x response headers ask for, in seconds, read the way OpenCode reads them (retry.ts:47-75).

    ``retry-after-ms`` first, then ``retry-after`` as seconds, then as an HTTP date. A value OpenCode would skip is
    skipped; a wait that is not a positive finite number is None. Header names are matched in any case.
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
        if parts is None:
            return None
        # As Date.parse reads it: a date with no zone, or the zone -0000, is UTC (never this machine's zone).
        moment = calendar.timegm(parts[:6] + (0, 0, 0)) - (parts[9] or 0)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    wait = moment - wall()
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
    return isinstance(value, str) and 1 <= len(value) <= limit and not _LINE_BREAK.search(value)


def _unique_keys(pairs: list) -> dict:
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _no_constant(name: str) -> None:
    raise ValueError(f"not JSON: {name}")
