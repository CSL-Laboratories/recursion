"""OpenCode engine driver, first slice: the parser of ``opencode run --format json`` (lane ENGINE-E3).

OpenCode is one of the engines the product names (``routes.engines.CATALOGUE``). It is used only when the operator
has installed and configured it: Recursion never installs it, never signs it in and never reads its login store.
This module is the first slice of its driver, the PARSER. It turns the lines that ``opencode run --format json``
prints into driver.v2 event drafts, each payload exactly event.v1's, and the end of a run into the run's result.
The parser starts no process and reads no file. ``OneShot`` and ``OpenCodeDriver`` (the second slice) drive a
process the caller's launch path started or the ``spawn`` given to them starts; nothing registers either, and no
daemon loads this module yet.

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

WHAT A LINE GIVES. Every line gives one ``engine.output`` draft first: the line as UTF-8 text (a byte that is not
UTF-8 is written U+FFFD, so only then does the text differ from what was printed), with its exact byte offsets in the
run's stdout, so a reader that restarts resumes without losing or repeating a line (an empty chunk is not a line and
gives nothing). Then:

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
number (then nothing else is read, as in OpenCode), else ``retry-after`` as seconds, else as an HTTP date in either
form RFC 9110 has a recipient read with a zone, IMF-fixdate or rfc850-date, with a real calendar day and time
(``_http_date``; retry.ts:47-75); on 2.x ``retryAfterMs``. The wait is counted from the moment the line is parsed, on
the clock given, so a reset is never earlier than the vendor's. No wait, or one that is not a positive finite number,
leaves ``reset_at`` null and the breaker uses its own cooldown. The limit is on the connection, never on another one,
and names no model or bucket: OpenCode's stream names neither. Any other error is not a limit: free text in an error
envelope that sounds like a quota makes the result a ``short_fault`` (``contract.sounds_like_quota``, grill Q23), and
nothing else does.

USAGE. ``step_finish`` carries ``tokens`` ``{input, output, reasoning, cache: {read, write}}`` and ``cost``. Both
lines compute them the same way: ``input`` excludes the cached tokens, ``output`` excludes reasoning, and a count
the provider did not report is written as 0 (1.19.5 packages/opencode/src/session/session.ts:338-372; 2.2.6
packages/core/src/session/usage.ts:9-19). So a 0 cannot be told from "not reported": it is drafted ``unknown``,
never 0. A positive whole count that every JSON reader keeps exact (at most ``MAX_EXACT``) is ``known``,
``observed``; a larger or a fractional one is drafted ``unknown``, as if it had not been reported. event.v1 counts
reasoning inside ``output_tokens``, so ``output_tokens`` is OpenCode's output plus its reasoning when both are
positive and the sum is still a number every JSON reader keeps exact, and otherwise a ``lower_bound``: the larger
part that is known. The cost
is OpenCode's own figure, from its price table or from what the endpoint reported, and the stream does not say
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
``engine_exited`` on any other exit, and with no exit status unless the last step finished with reason ``stop``
(then ``ok``). ``detail`` names the typed error only when it is one of the error
names OpenCode's source defines (``KNOWN_ERRORS``), and says ``error`` otherwise: never the engine's own words.

``ServeDriver`` (the third slice) drives ``opencode serve`` with one ``opencode run --attach`` per turn, against
fakes: interrupt and resume are there. What is NOT here: the permission bridge, the 2.x drive, and any registration
(later slices; a registration will state ``default_executable='opencode'``). The guard and the child environment
are other lanes' files.
"""

from __future__ import annotations

import calendar
import copy
import hashlib
import http.client
import json
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from types import MappingProxyType

from recursion_core.routes import contract as c
from recursion_core.routes.driver import (Declaration, Driver, DriverInputError, DriverStateError, Flags,
                                          check_draft)
from recursion_core.subscription_routes import ENGINES as _GUARD_ENGINES
from recursion_core.subscription_routes import SubscriptionRouteRefused
from recursion_core.subscription_routes.policy import _looks_like_claude_login   # the guard's own test

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
_FIRST_EPOCH = -62135596800.0  # 0001-01-01T00:00:00Z
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_CONNECTION = re.compile(r"(subscription|api_key|passthrough):.+")
# The two HTTP-date forms RFC 9110 (5.6.7) has a recipient read that carry a zone, matched whole and parsed here (not
# by parsedate_tz, which rewrites a year below 100 and skips words it does not know). Fields are separated by spaces
# only; whitespace may surround the value. Names (weekday, month, zone) are read in any case, as Date.parse reads
# them. IMF-fixdate, with its usual leniencies: an optional weekday, a day and an hour of one or two digits, optional
# seconds. A date without its weekday is read only when _http_date is called directly: from a header, a value that
# starts with the day is first read as that many seconds (_js_float, as Number.parseFloat reads it in OpenCode).
_IMF_DATE = re.compile(r"\s*(?:[A-Za-z]+, *)?([0-9]{1,2}) +([A-Za-z]{3}) +([0-9]{4}) +([0-9]{1,2}):([0-9]{2})"
                       r"(?::([0-9]{2}))? +(GMT|UTC|UT|Z|[+-][0-9]{4})\s*", re.IGNORECASE)
# rfc850-date: weekday, DD-Mon-YY, a time with seconds, a zone; its two-digit year is read by RFC 9110's 50-year rule.
_RFC850_DATE = re.compile(r"\s*[A-Za-z]+, *([0-9]{2})-([A-Za-z]{3})-([0-9]{2}) +([0-9]{2}):([0-9]{2}):([0-9]{2})"
                          r" +(GMT|UTC|UT|Z|[+-][0-9]{4})\s*", re.IGNORECASE)
_MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_SURROGATE = re.compile("[" + chr(0xD800) + "-" + chr(0xDFFF) + "]")
REPLACEMENT = chr(0xFFFD)
# Number.parseFloat's grammar. First JavaScript's white space (StrWhiteSpaceChar: tab, line feed, VT, FF, carriage
# return, the BOM, the Zs spaces, U+2028 and U+2029), not Python's \s, which also takes U+001C-U+001F and U+0085
# and not the BOM. Then StrDecimalLiteral: ASCII digits only (\d would take any script's digits), and Infinity in
# that one spelling, signed or not, with nothing read after it.
_JS_SPACE = ("[" + "".join(map(chr, (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680, 0x2028, 0x2029, 0x202F,
                                     0x205F, 0x3000, 0xFEFF))) + chr(0x2000) + "-" + chr(0x200A) + "]")
_JS_FLOAT = re.compile(_JS_SPACE + r"*([+-]?(?:Infinity|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?))")
_LINE_BREAK = re.compile(r"[\r\n]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def declaration() -> dict:
    """driver.v2's root record for the one-shot drive."""
    return {"engine": ENGINE, "driver": 2, "flags": dict(FLAGS), "input_ack": False}


def build_argv(model: str | None, *, binary: str | None = None) -> list[str]:
    """argv for one turn of ``opencode run``. The prompt goes on standard input, never into argv.

    ``binary`` is the binary option composition gives (argv[0] is it, else ``DEFAULT_EXECUTABLE``). 1.19.5 reads a
    piped standard input as the message (run.ts:417-418), so no prompt can be read as an option.
    """
    if binary is not None and (not isinstance(binary, str) or not binary.strip() or binary.startswith("-")
                               or _CONTROL.search(binary)):
        raise ValueError("binary")
    if model is None:   # driver.v2's route: null is the engine's own default model
        return [binary if binary is not None else DEFAULT_EXECUTABLE, "run", "--format", "json"]
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


def held_to_the_guard(model: object, profile: Mapping | None) -> str | None:
    """The model that goes into argv (``--model``, or None for OpenCode's own default), refused unless the guard's
    rules accept that very value. The one place a launched model is worked out, for the route path and the driver.

    The guard (``subscription_routes.check_route``) reads ``profile.model``, and argv carries the route's model. So
    when the profile names a model the two must be the same value (``model_differs_from_profile``); a launched model
    whose provider is a Claude-subscription provider id or a spelling of a Claude login is refused whatever its
    source (``claude_subscription_provider``, the guard's own list and test); and on the route path a model the
    profile does not name must be of the profile's provider (``model_provider_mismatch``, the guard's API-key rule).
    A model OpenCode cannot take, or one that is not a plain ``str`` (a subclass could answer ``!=`` as it likes),
    is ``model_malformed``. Each refusal is the guard's own ``SubscriptionRouteRefused``,
    which the profile adapter answers with a typed ``subscription_route_refused`` before the launcher is called.
    ``profile`` is None on the driver path, which has no profile (driver.v2's route record); a profile that is not
    a mapping counts as an empty one. A null model is OpenCode's own default (no ``--model``): this function then
    holds nothing, and the default the engine's own configuration names is what runs. Today's route service refuses
    a null model before any adapter; on the driver path (driver.v2 allows null) the caller vouches for it.
    """
    if model is not None and (type(model) is not str or not model_ok(model)):
        raise SubscriptionRouteRefused(ENGINE, "model_malformed", "route.model")
    if profile is not None and not isinstance(profile, Mapping):
        profile = {}
    named = profile.get("model") if profile is not None else None
    if named is not None and (type(named) is not str or named != model):
        raise SubscriptionRouteRefused(ENGINE, "model_differs_from_profile")
    if model is not None:
        provider = model.split("/", 1)[0].strip().lower()
        if provider in _GUARD_ENGINES[ENGINE].subscription_providers or _looks_like_claude_login(provider):
            raise SubscriptionRouteRefused(ENGINE, "claude_subscription_provider", provider)
        if profile is not None and named is None:
            stated = profile.get("provider")
            if not isinstance(stated, str) or provider != stated.strip().lower():
                raise SubscriptionRouteRefused(ENGINE, "model_provider_mismatch", provider)
    return model


def route_argv(request: dict, *, binary: str | None = None) -> list[str]:
    """``ProfileRouteAdapter``'s argv hook for one route request: ``build_argv`` on ``held_to_the_guard``'s model.

    The adapter calls it before the guard and the launcher, so each refusal comes back as a typed result with
    nothing launched. A request without a route mapping is ``RouteError(request_invalid, "route")`` (the route
    service never sends one).
    """
    route = request.get("route") if isinstance(request, Mapping) else None
    if not isinstance(route, Mapping):
        raise c.RouteError(c.REQUEST_INVALID, "route")
    profile = route.get("profile")
    return build_argv(held_to_the_guard(route.get("model"), profile if isinstance(profile, Mapping) else {}),
                      binary=binary)


class OneShot:
    """The one-shot drive on today's route contract: ``ProfileRouteAdapter`` calls it once the guard has passed.

    ``drive(process, request, deadline_at, events) -> result``: the prompt is written to standard input, which is
    then closed (OpenCode reads it to its end as the message), and stdout is read a line at a time until it ends.
    ``clock`` is the deadline and breaker clock: the deadline is checked before the prompt is written, before every
    read and again after every read, the end of the output included; a read that passes it raises ``Deadline``,
    which the adapter fences for this request only. A chunk from ``read_line`` that is not text, or holds a line
    break (LF or CR) anywhere but in its terminal ``\n`` or ``\r\n``, is a protocol error (``_Protocol``, fenced by
    the adapter). The end of stdout is not the end of the process: before the result is returned the process is
    handed to the cleanup layer (``fence("stdout_ended")``), and a fence that fails leaves as its own exception.
    An ``OSError`` from the process (``send``, ``close_stdin``, ``read_line``) leaves unfenced here: the profile
    adapter fences it (``io_error``) and answers with a typed ``engine_error``; a caller that drives ``OneShot``
    without that adapter fences it itself. Every other way out once the process has started fences it first
    (``driver_error``) and lets the same exception leave, ``BaseException`` included. A route whose connection the parser refuses writes nothing and is fenced
    (``request_invalid``).
    """

    def __init__(self, clock: Callable[[], float], *, wall: Callable[[], float] = time.time) -> None:
        if not callable(clock) or not callable(wall):
            raise ValueError("clock")
        self._clock, self._wall = clock, wall

    def __call__(self, process, request: dict, deadline_at: float, events: list) -> dict:
        from recursion_core.routes.base import Deadline, _Protocol   # local: base imports nothing of this module
        from recursion_core.routes.launch import ReadTimeout

        try:
            route = request["route"]
            parser = Parser(connection_id=c.connection_key(route), model=route["model"], wall=self._wall,
                            clock=self._clock)
        except ValueError as err:
            _fence(process, "request_invalid")
            return c.result(request["request_id"], c.REQUEST_INVALID, detail=f"route.{err}")
        except BaseException:
            _fence(process, "driver_error")
            raise
        try:
            if deadline_at - self._clock() <= 0:   # nothing is written once the deadline has passed
                raise Deadline()
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
                if deadline_at - self._clock() <= 0:   # a read that returned late, the end of output included
                    raise Deadline()
                if line is None:
                    break
                if type(line) is not str:
                    raise _Protocol(f"stdout chunk is {type(line).__name__}, not text")
                body = line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith("\n") else line
                if _LINE_BREAK.search(body):
                    raise _Protocol("stdout chunk holds more than one line")
                parser.feed(line if line.endswith("\n") else line + "\n")
            parser.end(None)   # today's EngineProcess gives no exit status: the stream's last step decides
            result = parser.route_result(request["request_id"])
            process.fence("stdout_ended")   # the end of stdout is not the end of the process
            events.extend(result["events"])
            result["events"] = events
            return result
        except (Deadline, _Protocol, OSError):
            raise   # the profile adapter fences these and answers with a typed result
        except BaseException:
            _fence(process, "driver_error")
            raise


def _drop(items: list, item: object) -> None:
    """Delete ``item`` (by identity) from ``items`` IN PLACE, the first time it is found. The list is never rebuilt:
    a rebuild (read, then assign) drops whatever another thread appends between the two. An append only adds at
    the end, so the index found stays the entry's own."""
    for index, each in enumerate(items):
        if each is item:
            del items[index]
            return


def _refuse_what_is_not_run(request: dict) -> None:
    """A start this driver would not run as asked is refused before anything is spawned (``DriverInputError``): a
    route naming another engine (``route.engine``; the gate checks only its shape), and a workspace
    (``workspace``): ``spawn(argv, env)`` takes no directory, so a run would start in the spawn's own directory, not
    the one asked for. Running in a workspace waits for the launch path to carry it (lane DRIVER-CC's and LAUNCH's)."""
    if request["route"]["engine"] != ENGINE:
        raise DriverInputError("route.engine")
    if request["workspace"] is not None:
        raise DriverInputError("workspace")


def _fence(process, reason: str) -> None:
    """Ask the cleanup layer to end the process's tree. A fence that fails does not hide the error being raised."""
    try:
        process.fence(reason)
    except Exception:
        pass


class OpenCodeDriver(Driver):
    """driver.v2 for the one-shot drive, on the gate every driver shares (``routes.driver.Driver``, lane DRIVER-CC).

    ``spawn(argv, env) -> process`` starts the engine; the launch path behind it (the guard, the child environment,
    the unit) is the caller's. ``start`` spawns ``opencode run``; the first ``user_message`` is the prompt, written
    to standard input, which is then closed; ``parse`` reads stdout; ``finish(handle, exit_code)`` drafts the result
    once the process has exited and ends the turn. A null route model starts OpenCode's own default model
    (``held_to_the_guard``). The flags are ``FLAGS``: resume, a second message, a message
    mid-turn, an interrupt, a fork and an approval answer are refused by the gate before this class is asked, so no
    process is started and no byte written. The one prompt is accepted (``input.accepted``, ``new_turn``) on the
    first well-formed ``step_start`` of the run's own session that OpenCode prints after the prompt was written: the
    engine's own typed sign that it began the turn, read by the parse call that consumed that line (an empty
    parse consumes none). A write that raised records no delivery, so nothing later can accept it (its outcome is
    unknown, and it is never written again). This is ON PURPOSE stricter than ``routes.driver``'s DELIVERY
    paragraph, which lets a later typed acceptance of such a write count: here that delivery stays pending in the
    gate for ever and no draft names it. A close that raised is not recorded: an implicit one is tried again by a
    later close, and an explicit ``close`` that raised opens the gate's input again (the gate closes it before the
    write), so the same ``close`` may be asked again.

    TERMINAL TRANSITIONS. ``finish``, ``release`` and the reading of one line by ``parse`` hold the driver's one
    lock, so a finish waits for a line being read (a 429 in it decides the result) and a release waits for a
    finish. The lock is taken and released in ONE protected region (``_exclusive``, the form of
    ``control.hooks``' OWNERSHIP note, with its ``take_once``): one interruption anywhere in it, a real SIGINT
    included, leaves the lock free, and the interruption is raised once it is free. A message the gate reserved
    after ``finish`` or ``release`` (or while one ran) is refused with ``DriverStateError`` and its turn is ended
    again, so the refusal leaves no live turn. A ``release`` that lands after that check and before the prompt
    write lets the write reach the process, whose end is the caller's (see ``release``).
    """

    declaration = Declaration(ENGINE, Flags(**FLAGS), input_ack=False)

    def __init__(self, spawn: Callable[[list, dict], object], *, binary: str | None = None,
                 wall: Callable[[], float] = time.time, clock: Callable[[], float] = time.monotonic) -> None:
        if not callable(spawn) or not callable(wall) or not callable(clock):
            raise ValueError("callable")
        if binary is not None:
            build_argv(None, binary=binary)   # the binary rule, refused here rather than at the first start
        self._spawn, self._binary, self._wall, self._clock = spawn, binary, wall, clock
        self._workers: dict[int, dict] = {}
        # One finish, release or parsed line at a time; an RLock only so that its owner can be told (take_once).
        self._ending = threading.RLock()

    def _worker(self, handle) -> dict:
        worker = self._workers.get(id(handle))   # a worker holds its handle, so no other handle has its id
        if worker is None:
            raise DriverStateError("no worker for this handle (finished, released or never started)")
        return worker

    def _start(self, handle, request: dict, env: dict) -> None:
        route = request["route"]
        _refuse_what_is_not_run(request)
        model = held_to_the_guard(route["model"], None)
        parser = Parser(connection_id=f"{route['connection']['kind']}:{route['connection']['id']}",
                        model=model, wall=self._wall, clock=self._clock)
        argv = build_argv(model, binary=self._binary)
        process = self._spawn(argv, env)
        self._workers[id(handle)] = {"process": process, "parser": parser, "turn": None, "delivery": None,
                                     "accepted": False, "handle": handle}

    def _send(self, handle, item: dict) -> None:
        worker = self._workers.get(id(handle))
        if worker is None:
            self._unreserve(handle, item)
            raise DriverStateError("no worker for this handle (finished, released or never started)")
        if item["kind"] == "user_message":     # the gate lets exactly one through: multi_turn is false
            worker["turn"] = item["turn_id"]
            if self._workers.get(id(handle)) is not worker:   # a finish or release ran meanwhile: nothing is written
                self._unreserve(handle, item)
                raise DriverStateError("the run ended before its prompt was written")
            worker["process"].send(item["text"])
            worker["delivery"] = item["delivery_id"]   # only a write that returned can be accepted later
        if not worker.get("stdin_closed"):     # the prompt is the whole input; a close only ends it sooner
            try:
                worker["process"].close_stdin()
            except BaseException:
                if item["kind"] == "close":    # the gate closed the input before this close: not done, so open again
                    with handle._lock:
                        handle._closed = False
                raise
            worker["stdin_closed"] = True          # only a close that returned is done; a raised one is tried again

    def _unreserve(self, handle, item: dict) -> None:
        """End the turn the gate reserved for a message no worker will write, so its refusal leaves no live turn."""
        if item["kind"] == "user_message" and handle.live_turn == item["turn_id"]:
            try:
                self.turn_ended(handle, item["turn_id"])
            except DriverStateError:            # ended by a finish or release that ran at the same moment
                pass

    def _parse(self, handle, line: str) -> list[dict]:
        def step() -> list[dict]:
            worker = self._worker(handle)
            parser = worker["parser"]
            fed = parser.lines_read
            drafts = parser.feed(line)
            if parser.engine_session_id is not None and handle.engine_session_id is None:
                handle.set_engine_session_id(parser.engine_session_id)
            if not worker["accepted"] and worker["delivery"] is not None and parser.lines_read != fed \
                    and parser.started:        # a start read by THIS call: an empty parse reads none
                worker["accepted"] = True
                drafts.append(_draft("input.accepted", {"delivery_id": worker["delivery"],
                                                        "turn_id": worker["turn"], "how": "new_turn"}))
            return drafts

        return self._exclusive(step)

    def _exclusive(self, step: Callable[[], object]) -> object:
        """``step()`` run once while this driver's lock is held, and its value; the lock is free again however one
        interruption lands. ONE protected region in the form of ``control.hooks``' OWNERSHIP note: the taking
        (``take_once``), the step and the release are inside one try; an interruption or an error is kept, the
        lock is released, and what was kept is raised (``raise_kept``). An interruption kept before the step was
        called skips it: nothing was done, and the call can be made again. Interrupted inside the step, the step's
        own state says what was done (``finish`` can be called again). What this form cannot cover is that note's:
        the instructions before the region and after it, a second interruption inside the except clause, and on
        3.14 the marker after a conditional jump not taken, where no signal is delivered."""
        from recursion_core.control.hooks import raise_kept, take_once   # local: routes must not load control at import

        kept: list = []
        box: list = []
        while True:
            try:
                if not kept and not box:
                    if not self._ending._is_owned():
                        take_once(self._ending, kept)
                    if not kept:
                        box.append(step())
                if self._ending._is_owned():
                    self._ending.release()
                break
            except BaseException as exc:  # noqa: BLE001 - kept; the lock is released first
                kept.append(exc)
        raise_kept(kept)
        return box[0]

    def finish(self, handle, exit_code: int | None) -> list[dict]:
        """The ``result`` draft once the process has exited; the turn in progress ends with it, and the handle is
        released. One terminal transition per handle: finishers are serialised, exactly one returns the result, and
        any other (or later) caller gets ``DriverStateError``. An ``exit_code`` that is not an int or None is
        ``DriverInputError`` with nothing changed. Interrupted part-way, ``finish`` can be called again and completes
        the same transition: the parser's outcome, once reached, is kept (the first exit code given decides it), the
        turn is ended, and only then is the worker removed, so a live turn never outlives its worker. The one window
        left: an interruption after the worker was removed (the transition complete) loses the result it drafted,
        and a later call is ``DriverStateError``. A finish waits for a line being parsed (``_exclusive``)."""
        def step() -> list[dict]:
            worker = self._worker(handle)
            if exit_code is not None and type(exit_code) is not int:
                raise DriverInputError("exit_code")
            parser = worker["parser"]
            if parser.outcome is None:
                parser.end(exit_code)
            code, detail = parser.outcome
            drafts = [check_draft(_draft("result", {"code": code, "detail": detail}))]
            if worker["turn"] is not None and handle.live_turn == worker["turn"]:
                self.turn_ended(handle, worker["turn"])
            self._workers.pop(id(handle), None)
            return drafts

        return self._exclusive(step)

    def release(self, handle) -> None:
        """Forget a handle whose ``finish`` will never come (a lost run), and end its turn in progress, so the
        handle refuses a later message as ended rather than busy. Its process is the caller's to end: who calls this
        on Stop and shutdown is lane STOP's and DRIVER-CC's to define. A handle this driver does not hold (another
        driver's, a released one, None) is left as it is. It waits for a finish or a parsed line in progress."""
        def step() -> None:
            worker = self._workers.get(id(handle))
            if worker is None:
                return
            if worker["turn"] is not None and handle.live_turn == worker["turn"]:
                self.turn_ended(handle, worker["turn"])
            self._workers.pop(id(handle), None)

        self._exclusive(step)


# -- the serve drive (the brief's step 5), built against fakes: nothing here is registered, wired or run live ------

# driver.v2 flags of the serve drive. A later turn reaches the same conversation through the server, a turn can be
# interrupted, and a conversation is resumed by OpenCode's own session id. No input reaches a turn in progress (a
# turn's message is the whole of its standard input), no fork, and no permission bridge (lane questions Q1).
SERVE_FLAGS = MappingProxyType({"multi_turn": True, "mid_turn_input": False, "interrupt": True, "resume": True,
                                "fork": False, "permission_bridge": False, "typed_limits": True, "usage": True})
LOOPBACK = "127.0.0.1"
# The server's readiness line (1.x ``opencode serve``), and the only server a turn may attach to: this machine's
# loopback address and a port from 1 to 65535, written without a leading zero.
_LISTENING = re.compile(r"opencode server listening on (http://127\.0\.0\.1:[1-9][0-9]{0,4})[ \t]*\r?\n?")
_LOOPBACK_URL = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})")


def serve_declaration() -> dict:
    """driver.v2's root record for the serve drive."""
    return {"engine": ENGINE, "driver": 2, "flags": dict(SERVE_FLAGS), "input_ack": False}


def _check_binary(binary: object) -> None:
    if binary is not None:
        build_argv(None, binary=binary)   # the one binary rule


def _port_ok(port: object) -> bool:
    return type(port) is int and 1 <= port <= 65535


def _loopback_url_ok(url: object) -> bool:
    match = _LOOPBACK_URL.fullmatch(url) if isinstance(url, str) else None
    return match is not None and int(match.group(1)) <= 65535


def _session_ok(session: object) -> bool:
    """OpenCode's own session id as an argv value and as one segment of the abort's path: what the parser keeps as
    one, never read as an option, and never ``.`` or ``..`` (which a path would read as another place)."""
    return isinstance(session, str) and 1 <= len(session) <= MAX_SESSION_ID and not session.startswith("-") \
        and session not in (".", "..") and not _CONTROL.search(session) and not _SURROGATE.search(session)


def server_url(port: int) -> str:
    if not _port_ok(port):
        raise ValueError("port")
    return f"http://{LOOPBACK}:{port}"


def serve_argv(port: int, *, binary: str | None = None) -> list[str]:
    """argv for ``opencode serve`` on the loopback address and the port the driver picked."""
    _check_binary(binary)
    if not _port_ok(port):
        raise ValueError("port")
    return [binary if binary is not None else DEFAULT_EXECUTABLE, "serve", "--hostname", LOOPBACK, "--port", str(port)]


def listening_url(text: object, port: int) -> str | None:
    """The server's own readiness line, when it names exactly the loopback address and ``port``; otherwise None.
    A ``port`` that is not a port (``server_url``'s rule) raises ``ValueError``."""
    match = _LISTENING.fullmatch(text) if isinstance(text, str) else None
    if match is None or match.group(1) != server_url(port):
        return None
    return match.group(1)


def attach_argv(url: str, model: str | None, *, session: str | None = None, binary: str | None = None) -> list[str]:
    """argv for one turn through the server: ``opencode run --format json [--model m] --attach <url>``, and
    ``--session <id>`` to continue OpenCode's own conversation. Only the loopback server is ever attached."""
    if not _loopback_url_ok(url):
        raise ValueError("url")
    argv = build_argv(model, binary=binary) + ["--attach", url]
    if session is not None:
        if not _session_ok(session):
            raise ValueError("session")
        argv += ["--session", session]
    return argv


def pick_port() -> int:
    """A free loopback port, from the kernel (a bind to port 0). Another process may take it before the server binds
    it; the server then gives no readiness line and ``start`` refuses."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


def http_abort(url: str, session: str, *, timeout: float = 5.0) -> bool:
    """OpenCode's own abort of the session's running turn: ``POST <url>/session/<id>/abort`` on the loopback server,
    never through a proxy and never following a redirect. True when the server answered 2xx; False when it
    answered otherwise or not at all."""
    if not _loopback_url_ok(url):
        raise ValueError("url")
    if not _session_ok(session):
        raise ValueError("session")
    import urllib.parse
    import urllib.request

    request = urllib.request.Request(f"{url}/session/{urllib.parse.quote(session, safe='')}/abort", data=b"",
                                     method="POST")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):   # a 3xx stays an HTTPError: nothing is followed
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout):
            return True   # any answer that is not 2xx raises HTTPError (urllib's HTTPErrorProcessor)
    except (OSError, http.client.HTTPException):   # URLError and HTTPError are OSErrors; a malformed reply is not
        return False


class ServeDriver(Driver):
    """driver.v2 for the serve drive: one ``opencode serve`` per handle on the loopback address, and one
    ``opencode run --attach`` per turn (the 1.x line; 2.x's ``--server`` is not built). Built against fakes.

    ``spawn(argv, env) -> process`` starts a process; the launch path behind it (the guard, the child environment,
    the unit) is the caller's. A process has ``send(text)``, ``close_stdin()``, ``read_line(timeout)`` (a line, or
    None once its output has ended) and ``terminate()``. ``port()`` picks the server's port.

    ``start`` and ``resume`` start the server and read its output, on ``clock``, for up to ``ready_s`` seconds until
    its readiness line names the loopback address and that port. Otherwise the server is terminated and the call
    raises ``DriverStateError``; nothing else was started. The driver owns every process it starts from the
    statement its spawn returns in (OWNERSHIP in ``_start``): whatever stops a start (a read that raises, an
    interruption anywhere) terminates the server, and a termination that fails then is noted on the exception that
    leaves, not raised in its place, and keeps the server owed in the driver's table. ``resume`` continues OpenCode's own session id: every turn of that handle passes ``--session <id>``.

    A turn: the ``user_message`` is spawned as ``run --attach`` with its text on standard input, which is then
    ended; ``parse`` reads that turn's stdout; the first well-formed ``step_start`` of the turn's own session
    accepts the message (``new_turn``) once its write has returned (a write that raised is never accepted);
    ``finish_turn(handle, exit_code)`` drafts the turn's result once its process has exited, and ends the turn. A
    later turn of a handle whose conversation id is still unknown cannot reach the same conversation: it is refused
    (``DriverStateError``), that turn is ended, and nothing is spawned. Whatever stops a turn before its process is
    registered (its preparation, a spawn that raises, an interruption) ends that turn at once and leaves; a process
    the spawn gave back by then is terminated. A session id the turn reports that a later turn could not pass as
    ``--session`` (``attach_argv``'s rule) is not adopted: it is recorded in the turn parser's ``anomalies``
    (``session_id_unusable``). A turn that reports another session id than the conversation's is recorded there too
    (``conversation_changed``).

    ``interrupt`` asks the server to abort the session's running turn (``abort(url, session_id)``, OpenCode's own
    abort); when no id is known yet, or the server does not answer 2xx, or the ask raises, the turn's process is
    terminated instead. An ask cut short by a ``BaseException`` (a Stop, an exit) also terminates the process, and
    then the same exception leaves, a failing termination (``BaseException`` included) noted on it. Either way the turn ends through ``finish_turn``, one finisher per turn.
    ``close`` ends the handle's input: the server is stopped then, or once the turn in progress is finished.

    ``release(handle)`` is terminal: from its first call the handle refuses every later call of this driver on it
    (``DriverStateError``), its turn in progress is ended, and the turn's process and the server are terminated,
    each on its own. A turn whose process is still being spawned when ``release`` runs is terminated as soon as the
    spawn gives it back, and nothing is written to it (its ``send`` raises ``DriverStateError``, with any failed
    termination noted on it). A prompt being written when ``release`` comes is written first (release waits for
    the turn's write lock, at most ten seconds), then its process is terminated. A termination that raises is
    raised from ``release`` after the others were tried, and that process stays owed: a later ``release`` tries it
    again, and only it. The driver keeps the handle until every process it started for it was terminated. One
    interruption anywhere in ``release``, its cleanup or a turn's start loses no process (each line is tested).
    Who calls ``release`` on Stop and shutdown is lane STOP's and DRIVER-CC's to define.
    """

    declaration = Declaration(ENGINE, Flags(**SERVE_FLAGS), input_ack=False)

    def __init__(self, spawn: Callable[[list, dict], object], *, binary: str | None = None,
                 port: Callable[[], int] = pick_port, abort: Callable[[str, str], bool] = http_abort,
                 ready_s: float = 30.0, wall: Callable[[], float] = time.time,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not all(callable(f) for f in (spawn, port, abort, wall, clock)):
            raise ValueError("callable")
        _check_binary(binary)
        if type(ready_s) not in (int, float) or not math.isfinite(ready_s) or ready_s <= 0:
            raise ValueError("ready_s")
        self._spawn, self._binary, self._port, self._abort = spawn, binary, port, abort
        self._ready_s, self._wall, self._clock = ready_s, wall, clock
        self._workers: dict[int, dict] = {}
        # Guards the table and every worker's fields. Never held while a process, the spawn or the abort is called.
        self._lock = threading.Lock()

    def _worker(self, handle) -> dict:
        with self._lock:   # a registered worker holds its handle, so no other handle can have its id
            worker = self._workers.get(id(handle))
            if worker is not None and (worker["released"] or not worker["started"]):
                worker = None   # kept only until its processes are terminated: closed to every call
        if worker is None:
            raise DriverStateError("this handle has no server (released, or never started)")
        return worker

    def _start(self, handle, request: dict, env: dict) -> None:
        route = request["route"]
        _refuse_what_is_not_run(request)   # another engine's route, or a workspace the spawn cannot honour
        connection = f"{route['connection']['kind']}:{route['connection']['id']}"
        # The launched model, and a route the parser refuses, are refused here, before anything is started.
        model = held_to_the_guard(route["model"], None)
        Parser(connection_id=connection, model=model, wall=self._wall, clock=self._clock)
        if request["engine_session_id"] is not None and not _session_ok(request["engine_session_id"]):
            raise DriverInputError("engine_session_id")
        port = self._port()
        argv = serve_argv(port, binary=self._binary)
        # OWNERSHIP: "live" holds every process this driver started for the handle and has not yet terminated. A
        # process enters it in the very statement its spawn returns in, and leaves it only once its terminate()
        # returned, so no transfer between two lists can be lost to an interruption.
        worker = {"handle": handle, "server": None, "url": None, "env": dict(env), "model": model,
                  "connection": connection, "turn": None, "turns": 0, "closed": False, "released": False,
                  "started": False, "spawning": 0, "live": [], "busy": []}
        try:   # the server is in the worker's live list from the statement its spawn returns in
            worker["live"].append(server := self._spawn(argv, dict(env)))
            url = self._await_ready(server, port)
            if url is None:
                raise DriverStateError("opencode serve gave no readiness line for its loopback address")
            with self._lock:
                worker["server"], worker["url"], worker["started"] = server, url, True
                self._workers[id(handle)] = worker
        except BaseException as error:
            with self._lock:
                worker["released"] = worker["closed"] = True
                if self._workers.get(id(handle)) is not worker:   # kept in the table until nothing is live
                    self._workers[id(handle)] = worker
            self._clean(worker, noting=error)   # a termination that fails stays owed, its worker kept in the table
            raise

    def _await_ready(self, server, port: int) -> str | None:
        from recursion_core.routes.launch import ReadTimeout   # local, as in OneShot

        deadline = self._clock() + self._ready_s
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            try:
                line = server.read_line(remaining)
            except ReadTimeout:
                return None
            if line is None:   # its output ended: the server exited
                return None
            url = listening_url(line, port)
            if url is not None:
                return url

    def _send(self, handle, item: dict) -> None:
        # A user_message: the gate reserved it as a new turn (mid_turn_input is false). The turn is published on the
        # worker BEFORE its process is spawned, and the process enters "live" in the statement its spawn returns in:
        # whatever stops this call then (a release, an error, an interruption) ends the turn at the gate and
        # terminates its process. The write is made holding the turn's own write lock, after a last look at the
        # released flag; release sets that flag first and then takes the same lock, so a prompt is either refused
        # unwritten or written before its process is terminated.
        try:   # the first statement: the gate has reserved the turn already, so no line of this call is outside it
            if item["kind"] == "close":
                worker = self._worker(handle)
                with self._lock:
                    worker["closed"] = True
                    idle = worker["turn"] is None
                if idle:
                    self.release(handle)
                return
            turn_id, worker, turn, process, spawning, written = item["turn_id"], None, None, None, False, False
            worker = self._worker(handle)
            session = handle.engine_session_id
            if worker["turns"] and session is None:
                raise DriverStateError("no conversation id to continue: the earlier turns reported none")
            argv = self._turn_argv(worker, session)
            parser = Parser(connection_id=worker["connection"], model=worker["model"], wall=self._wall,
                            clock=self._clock)
            turn = {"id": turn_id, "delivery": None, "process": None, "parser": parser, "accepted": False,
                    "finishing": False, "write": threading.RLock()}   # re-entrant: a release from the writer
            with self._lock:
                if worker["released"]:
                    raise DriverStateError("this handle has no server (released, or never started)")
                worker["turn"], worker["spawning"], spawning = turn, worker["spawning"] + 1, True
            try:
                worker["live"].append(process := self._spawn(argv, dict(worker["env"])))
                turn["process"], worker["turns"] = process, worker["turns"] + 1   # a turn that started a process
            finally:
                if spawning:
                    with self._lock:
                        worker["spawning"], spawning = worker["spawning"] - 1, False
            with turn["write"]:
                with self._lock:
                    released = worker["released"]
                if released:
                    raise DriverStateError("the handle was released before the message was written: nothing was "
                                           "written, and the process was terminated")
                written = True   # from here a failure leaves the turn as it is: the write's outcome is unknown
                process.send(item["text"])
                turn["delivery"] = item["delivery_id"]   # only a write that returned can be accepted later (as #414)
                process.close_stdin()
        except BaseException as error:
            if item["kind"] == "close":
                raise
            here = locals()   # an interruption on the first line leaves the names below unbound
            turn_id, worker, turn, process = item["turn_id"], here.get("worker"), here.get("turn"), here.get("process")
            spawning, written = here.get("spawning", False), here.get("written", False)
            if worker is not None and spawning:
                with self._lock:
                    worker["spawning"] -= 1
            if worker is not None and turn is not None and not written:
                with self._lock:
                    if worker["turn"] is turn:
                        worker["turn"] = None
                self._end_turn(handle, turn_id)
                if process is not None or worker["released"]:
                    self._clean(worker, None if worker["released"] else [process], noting=error)
            elif turn is None:
                self._end_turn(handle, turn_id)
            raise

    def _turn_argv(self, worker: dict, session: str | None) -> list[str]:
        try:
            return attach_argv(worker["url"], worker["model"], session=session, binary=self._binary)
        except ValueError:
            raise DriverStateError("the conversation id cannot be passed to a turn") from None

    def _parse(self, handle, line: str) -> list[dict]:
        worker = self._worker(handle)
        turn = worker["turn"]
        if turn is None:
            raise DriverStateError("no turn in progress")
        parser = turn["parser"]
        fed = parser.lines_read
        drafts = parser.feed(line)
        reported = parser.engine_session_id
        if reported is not None and not _session_ok(reported):
            if not turn.get("unusable"):   # a later turn could not pass it as --session: never adopted
                turn["unusable"] = True
                parser._anomaly("session_id_unusable")
        elif reported is not None and handle.engine_session_id is None:
            handle.set_engine_session_id(reported)
        elif reported is not None and reported != handle.engine_session_id and not turn.get("changed"):
            turn["changed"] = True
            parser._anomaly("conversation_changed")
        if not turn["accepted"] and turn["delivery"] is not None and parser.lines_read != fed and parser.started:
            turn["accepted"] = True        # a start read by THIS call (#414's rule: an empty parse reads none)
            drafts.append(_draft("input.accepted", {"delivery_id": turn["delivery"], "turn_id": turn["id"],
                                                    "how": "new_turn"}))
        return drafts

    def finish_turn(self, handle, exit_code: int | None) -> list[dict]:
        """The turn's ``result`` draft once its process has exited; the turn ends with it. An ``exit_code`` that
        is not an int or None is ``DriverInputError`` with nothing changed. One finisher per turn: a second caller,
        while the first runs or after it, gets ``DriverStateError``. Interrupted part-way, ``finish_turn`` can be
        called again and completes the same turn: the parser's outcome, once reached, is kept (the first exit code
        given decides it), the gate's turn is ended, and only then is the turn taken off the worker. When the
        handle's input was closed, the server is then stopped; a termination that fails there stays owed to
        ``release``."""
        worker = self._worker(handle)
        claimed = None
        try:   # the claim is inside: an interruption anywhere after this line gives it back
            with self._lock:
                turn = worker["turn"]
                if turn is None or turn["finishing"]:
                    raise DriverStateError("no turn in progress")
                if exit_code is not None and type(exit_code) is not int:
                    raise DriverInputError("exit_code")
                turn["finishing"] = True; claimed = turn   # noqa: E702 - one line: one step
            parser = turn["parser"]
            if parser.outcome is None:
                parser.end(exit_code)
            code, detail = parser.outcome
            drafts = [check_draft(_draft("result", {"code": code, "detail": detail}))]
            self._end_turn(handle, turn["id"])   # the gate's transition first: the turn stays reachable until it
            with self._lock:
                if worker["turn"] is turn:
                    worker["turn"] = None
                _drop(worker["live"], turn["process"])   # it has exited
                closed = worker["closed"]
        except BaseException:
            if claimed is not None:
                with self._lock:
                    claimed["finishing"] = False
            raise
        if closed:
            try:
                self.release(handle)
            except Exception:   # the server stays owed: a later release tries it again
                pass
        return drafts

    def _interrupt(self, handle, turn_id: str) -> None:
        worker = self._worker(handle)
        with self._lock:
            turn = worker["turn"]
        if turn is None or turn["id"] != turn_id or turn["process"] is None:
            raise DriverStateError(f"{turn_id} has no process")
        session = handle.engine_session_id
        if session is not None:
            try:
                if self._abort(worker["url"], session) is True:
                    return
            except Exception:   # an ask that fails is no abort: the process is ended instead
                pass
            except BaseException as error:   # an ask cut short: the process is ended, then the SAME exception
                self._clean(worker, [turn["process"]], noting=error)
                raise
        self._clean(worker, [turn["process"]])

    def release(self, handle) -> None:
        """Terminal: the handle refuses every later call of this driver on it, its turn in progress ends, and every
        process started for it (the turn's and the server) is terminated, each on its own. A termination that
        raised is tried again by the next call; a handle this driver does not hold is left as it is. A prompt
        being written when ``release`` comes is written first, then its process is terminated."""
        with self._lock:
            worker = self._workers.get(id(handle))
            if worker is None:
                return
            worker["released"] = worker["closed"] = True   # the one bookkeeping step: nothing moves between lists
            turn = worker["turn"]
        # A write in progress ends first (bounded; taken and given back in one line, so no interruption keeps it).
        if turn is not None and turn["write"].acquire(timeout=10.0): turn["write"].release()   # noqa: E701
        live = handle.live_turn
        if live is not None:
            self._end_turn(handle, live)
        self._clean(worker)

    def _end_turn(self, handle, turn_id: str) -> None:
        """End ``turn_id`` at the gate when it is still the turn in progress (a release may have ended it)."""
        try:
            self.turn_ended(handle, turn_id)
        except DriverStateError:
            pass

    def _clean(self, worker: dict, processes: list | None = None, *, noting: BaseException | None = None) -> None:
        """Terminate ``processes`` (every live one of the worker when None), each on its own. A process leaves
        "live" only once its terminate() returned; one whose termination raises, or is interrupted, stays there and
        a later ``release`` tries it again. A process another cleaner is terminating at this moment is left to it.
        A released worker leaves the table once nothing is live and no spawn is under way. The first termination
        error is raised after all were tried; with ``noting`` (an exception already leaving) each failure, a
        ``BaseException`` included, is noted on it instead and never replaces it."""
        with self._lock:
            todo = [p for p in (worker["live"][::-1] if processes is None else processes)   # the turn's first
                    if any(p is q for q in worker["live"]) and not any(p is q for q in worker["busy"])]
        errors = []
        for process in todo:
            claimed = False
            try:
                with self._lock:   # looked at again: another cleaner may have ended it since the list was made
                    if not any(process is q for q in worker["live"]) or any(process is q for q in worker["busy"]):
                        continue
                    worker["busy"].append(process); claimed = True   # noqa: E702 - one line: one step
                process.terminate()
                with self._lock:
                    _drop(worker["live"], process)
            except Exception as error:
                errors.append(error)
            except BaseException as error:
                if noting is None:
                    raise
                errors.append(error)
            finally:
                if claimed:
                    with self._lock:
                        _drop(worker["busy"], process)
        with self._lock:
            key = id(worker["handle"])
            if worker["released"] and not worker["live"] and not worker["spawning"] \
                    and self._workers.get(key) is worker:
                del self._workers[key]
        if not errors:
            return
        if noting is not None:
            for error in errors:
                noting.add_note(f"terminating a process also failed: {error!r}")
            return
        for error in errors[1:]:
            errors[0].add_note(f"terminating another process also failed: {error!r}")
        raise errors[0]

class Parser:
    """One run's stdout, a line at a time. One reader feeds it; it is not shared between threads.

    ``connection_id`` is the route's connection (``<kind>:<id>``, ``contract.connection_key``) and ``model`` its model
    (null for the engine's default): both are copied into ``usage`` and ``limit``. ``wall`` reads Unix seconds, for
    event.v1's ``reset_at``; ``clock`` is the breaker's clock, for the route contract's limit signal.

    ``started`` is True when the line just fed was a well-formed ``step_start`` of the run's own session (reset by
    every line, put back by ``feed``'s rollback); ``lines_read`` counts the lines fed (an empty feed is none);
    ``outcome`` is the run's ``(code, detail)`` once ``end`` has returned.
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
        self._finish: str | None = None       # the reason of the latest step_finish, a typed field
        self.last_type: str | None = None     # the type of the line just fed, when it was an event
        self.started = False                  # the line just fed was a well-formed step_start of this run's session
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
        body = raw[:-2] if raw.endswith(b"\r\n") else raw[:-1] if raw.endswith(b"\n") else raw
        if b"\n" in body or b"\r" in body:   # a line break (LF or CR) anywhere but at its end: two lines
            raise ValueError("one line at a time")
        if not raw:
            return []
        # What the line may change, taken before anything moves: if reading it raises inside this method (an injected
        # clock, an interruption, a memory error), up to and including the return, all of it is put back, so the line
        # counts as not read and feeding it again gives the same drafts at the same offsets, with no text, usage,
        # error or anomaly counted twice. An interruption that lands in the CALLER after the return finds the line
        # read: its drafts are the caller's to keep or lose. A SECOND interruption that lands inside the rollback
        # below is the one case not put back: no rollback written in Python can guard its own lines.
        start, lines, session, limit = self._offset, self._lines, self.engine_session_id, self._limit
        finish, last_type, started = self._finish, self.last_type, self.started
        kept = [(kept_list, len(kept_list)) for kept_list in (self.anomalies, self._text, self._usage, self._errors)]
        try:
            self._offset += len(raw)
            self._lines += 1
            self.last_type, self.started = None, False
            drafts = [_draft("engine.output", {"stream": "stdout", "text": raw.decode("utf-8", errors="replace"),
                                               "offset_start": start, "offset_end": self._offset})]
            event = self._load(raw)
            if event is not None:
                drafts.extend(self._typed(event))
            # Inside the try: an interruption on the return itself is put back too.
            return drafts
        except BaseException:
            self._offset, self._lines, self.engine_session_id, self._limit = start, lines, session, limit
            self._finish, self.last_type, self.started = finish, last_type, started
            for kept_list, length in kept:
                del kept_list[length:]
            raise

    def end(self, exit_code: int | None) -> list[dict]:
        """The run's ``result`` draft, once its process has exited (``exit_code`` None: no status was read)."""
        if self._result is not None:
            raise RuntimeError("the run has ended")
        if exit_code is not None and (type(exit_code) is not int):
            raise ValueError("exit_code")
        try:
            self._result = self._outcome(exit_code)
            code, detail = self._result
            return [_draft("result", {"code": code, "detail": detail})]
        except BaseException:
            # As in feed: one interruption before end() has returned leaves the run open, so end() can be called
            # again; a second one that lands on the line below is not put back.
            self._result = None
            raise

    @property
    def lines_read(self) -> int:
        """How many lines have been fed (an empty feed is none; a line whose reading raised is not counted)."""
        return self._lines

    @property
    def outcome(self) -> tuple[str | None, str] | None:
        """The run's ``(code, detail)`` once ``end`` has returned, else None."""
        return self._result

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
        kind = self.last_type = event["type"]
        if kind == "text":
            part = event.get("part")
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                self._text.append(_SURROGATE.sub(REPLACEMENT, part["text"]))
            return []
        if kind == "step_start":
            # A later step starts: whatever an earlier step finished with no longer says how the run ended.
            self._finish = None
            part, session = event.get("part"), event.get("sessionID")
            self.started = (isinstance(part, dict) and part.get("type") == "step-start" and isinstance(session, str)
                            and session == self.engine_session_id)
            return []
        if kind == "tool_use":
            return self._tool(event.get("part"))
        if kind == "step_finish":
            return self._step(event.get("part"), event.get("sessionID"))
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

    def _step(self, part: object, session: object = None) -> list[dict]:
        if not isinstance(part, dict) or part.get("type") != "step-finish":
            self._anomaly("step_shape")
            return []
        if session is None or session == self.engine_session_id:
            # Only a step of the run's own session says how the run ended. Another session's, or one with an
            # invalid id (``_session`` counted it), leaves the run's proof as it was.
            self._finish = part.get("reason") if isinstance(part.get("reason"), str) else None
        tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        output, reasoning = _positive_int(tokens.get("output")), _positive_int(tokens.get("reasoning"))
        if output is not None and reasoning is not None and output + reasoning <= MAX_EXACT:
            output_tokens = _known(output + reasoning, "observed")
        elif output is not None or reasoning is not None:
            # One part is not known, or the sum is past what every JSON reader keeps exact: at least the larger part.
            output_tokens = {"value": max(n for n in (output, reasoning) if n is not None), "quality": "lower_bound",
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
    """``Number.parseFloat``: the longest number at the start of the text, or None where it gives NaN.

    The grammar is JavaScript's (``_JS_FLOAT``): its own white space first, ASCII digits only, and ``Infinity``
    (signed or not) is a number, so a ``retry-after-ms`` of ``Infinity`` is a number and nothing else is read.
    """
    match = _JS_FLOAT.match(text)
    return float(match.group(1)) if match else None


def _retry_after_s(headers: object, now: float) -> float | None:
    """The wait 1.x response headers ask for, in seconds, read the way OpenCode reads them (retry.ts:47-75).

    ``now`` is the wall clock's reading for this line. ``retry-after-ms`` when it holds a number (then nothing else
    is read, as in OpenCode, even when that number is not a usable wait), else ``retry-after`` as seconds, else as
    an HTTP date (``_http_date``). A wait that is not a positive number is None. Header names are matched in any case.
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
    moment = _http_date(after, now)
    if moment is None:
        return None
    wait = moment - now
    return wait if wait > 0 else None


def _http_date(text: str, now: float) -> float | None:
    """An HTTP date as Unix seconds, or None.

    Only the two forms with a zone are read, each matched WHOLE (``_IMF_DATE``, ``_RFC850_DATE``): one time and one
    zone ``GMT``, ``UT``, ``UTC``, ``Z`` or a numeric offset (``-2500`` is 25 hours, as ``Date.parse`` reads it), and
    nothing else. An unknown zone, no zone (asctime-date, which ``Date.parse`` reads in the machine's own zone), a word
    or a second time between the time and the zone, a tab or a line break between fields, and an impossible day or
    time ("32 Nov", "25:00", which ``calendar.timegm`` would roll forward) give None. A four-digit year is the year
    written (0068 is the year 68, long past). A two-digit rfc850 year is first put in the century of ``now``; if the
    instant it then names, zone included, is MORE than 50 calendar years after ``now``, it means the same date a
    century earlier (RFC 9110, 5.6.7: "the most recent year in the past that had the same last two digits"). That is
    past, and this returns None for it at once; any other past date is returned as its instant, and ``_retry_after_s``
    gives no wait for it, since the wait is not positive. Exactly 50 years ahead stays ahead. Fifty years after a 29
    February is the 28th.
    """
    match = _IMF_DATE.fullmatch(text) or _RFC850_DATE.fullmatch(text)
    if match is None:
        return None
    day, month_name, year_text, hour, minute, second, zone = match.groups()
    month = _MONTHS.get(month_name.lower())
    if month is None:
        return None
    day, hour, minute, second = int(day), int(hour), int(minute), int(second or 0)
    year = int(year_text)
    offset = 0 if zone[0] not in "+-" else (-1 if zone[0] == "-" else 1) * (int(zone[1:3]) * 3600 + int(zone[3:]) * 60)
    if len(year_text) == 2:
        if not _FIRST_EPOCH <= now <= _LAST_EPOCH:
            return None
        # The whole second of now: timedelta would round a fraction such as .9999998 up into the next second. A date
        # is whole seconds, so it lies past "now plus 50 years" exactly when it lies past this second plus 50 years.
        this = _UNIX_EPOCH + timedelta(seconds=math.floor(now))
        year += this.year // 100 * 100
    # The date is checked before anything is computed from it (timegm refuses the year 0).
    if not (1 <= year and 1 <= day <= calendar.monthrange(year, month)[1] and hour <= 23 and minute <= 59
            and second <= 59):
        return None
    moment = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0)) - offset
    if len(year_text) == 2 and this.year + 50 <= 9999:   # a later cutoff lies past every date that can be written
        # Only a leap year has 29 February, and a leap year plus 50 never is one (50 is not a multiple of 4).
        cutoff_day = 28 if (this.month, this.day) == (2, 29) else this.day
        cutoff = calendar.timegm((this.year + 50, this.month, cutoff_day, this.hour, this.minute, this.second, 0, 0, 0))
        if moment > cutoff:
            return None   # it names the same date a century earlier, which is past: no wait at all
    return moment


def _timestamp(epoch: float) -> str | None:
    """common.v1's timestamp, rounded UP to the millisecond (a reset is never earlier than asked).

    None when it cannot be written: before the first second of the year 1, after 9999-12-31T23:59:58Z
    (``_LAST_EPOCH``, kept a second short of the year's end so that rounding up never reaches the year 10000; the
    last second of the year 9999 is refused too, and only an injected wall clock gets that far), infinite, or not
    a number. The year is always written with four digits.
    """
    if not _FIRST_EPOCH <= epoch <= _LAST_EPOCH:
        return None
    # Fraction keeps the float's exact value. ``epoch * 1000`` can land on the previous millisecond,
    # and ceiling that product would stamp a reset earlier than the wait that was asked for.
    millis = math.ceil(Fraction(epoch) * 1000)
    m = _UNIX_EPOCH + timedelta(seconds=millis // 1000)
    return f"{m.year:04d}-{m.month:02d}-{m.day:02d}T{m.hour:02d}:{m.minute:02d}:{m.second:02d}.{millis % 1000:03d}Z"


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
