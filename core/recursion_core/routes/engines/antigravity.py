# Written by Claude Opus 5.5 (claude-opus-5-5), Claude Code builder lane ENGINE-E5, for Lucas.
"""Antigravity CLI (``agy``): its sign-in methods, its driver.v2 declarations, the one-shot parser and adapter.

NOTHING HERE IS REGISTERED OR LOADED. Antigravity has no child profile, no route-home and pin loader and no
import path for its factory yet, so this module is inert: no table names it and no daemon imports it.

WHAT LUCAS DECIDED (PRODUCT.md d84cdd26, grill Q34, 2075-2084 and 3603-3604): Antigravity runs as T3 Code
runs it, Google's official ACP agent with its own sign-in, with four methods and no fallback between them;
"E5 follows T3 Code's methods; personal-account sign-in waits on Lucas's lawyer." Updaters are off by
default (Nov 7) and an engine is beta until measured (Nov 8).

THE METHODS (``METHODS``). Their ids are the ACP ``authenticate`` ids on Google's "Other editors (ACP)"
page (read Nov 14, 2026, through a fetch tool that summarises), the same four T3 Code 09e8de9c names in
``apps/server/src/provider/antigravityAuthSupport.ts``:

    gemini-api-key   a Gemini API key, read by the agent from GEMINI_API_KEY
    oauth-business   Gemini Enterprise: a browser sign-in with a GCP project and location
    agent-platform   Agent Platform / Vertex AI: GOOGLE_API_KEY, or Application Default Credentials with
                     a GCP project and location
    oauth-personal   a personal Google account: HELD. ``route_method`` refuses it unconditionally, before
                     anything is started: no flag, constant, caller, route or configuration is read for it,
                     so turning it on is an edit of that function, on Lucas's word after counsel.

None of the four has been run by this lane: every one is UNVERIFIED until Lucas's attended run.

A route names its method in ``route["auth_method"]``. That key is this lane's proposal: SPINE's route
schema (common.v1 ``route``) has no field for it, and asking for one is in the lane's questions.

THE ONE-SHOT DRIVE: ``agy -p --input-format stream-json --output-format stream-json --model <m>
--disable-slash-commands``, one ``{"event":"user"}`` line on stdin, stdin closed, stdout read until the
``result`` event. Flags and event shapes are from Google's CLI reference and headless pages (read Nov 14,
2026, through the same fetch tool); none was run. That ``-p`` takes no value when the prompt comes on stdin
is NOT verified (the page shows no example of it). ``--disable-slash-commands`` keeps a prompt that starts
with "/" from running a slash command (T3 Code's page: "/logout" by itself signs an instance out).

The one-shot drive selects its method from agy's settings in its HOME, not from argv, and Google's page
says GEMINI_API_KEY "on its own has no effect" there. So a launch needs a route home already prepared for
exactly the route's method (settings written, nothing else in it), or agy would use whatever login that
home holds: a fallback between methods. That home is the guard's and the route-home loader's to make (not
open to this lane yet). The adapter therefore takes a ``prepared_home`` callable and REFUSES every launch
without one; the factory cannot be given one, so a factory-built adapter refuses every launch.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from recursion_core.routes import contract as c
from recursion_core.routes.base import Deadline, RouteAdapter, _Protocol
from recursion_core.routes.launch import EngineProcess, ReadTimeout, child_env

ENGINE = "antigravity"
# The one command name this engine's registration will state (SPINE's default_executable, not built yet).
DEFAULT_EXECUTABLE = "agy"
# Antigravity's background updater, off for every child (troubleshooting page; grounding record V32).
UPDATER_ENV = (("AGY_CLI_DISABLE_AUTO_UPDATE", "true"),)
ONE_SHOT_FLAGS = ("-p", "--input-format", "stream-json", "--output-format", "stream-json")
ACP_ARGS = ("acp",)


# -- sign-in methods --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Method:
    id: str
    label: str
    connection_kinds: tuple   # the route connection kinds that may name this method
    credential_env: tuple     # the key names the agent reads for it; empty for a sign-in


GEMINI_API_KEY_METHOD = Method("gemini-api-key", "Gemini API key", ("api_key",), ("GEMINI_API_KEY",))
ENTERPRISE_METHOD = Method("oauth-business", "Gemini Enterprise", ("subscription",), ())
AGENT_PLATFORM_METHOD = Method("agent-platform", "Agent Platform / Vertex AI", ("api_key", "passthrough"),
                               ("GOOGLE_API_KEY",))
PERSONAL_ACCOUNT_METHOD = Method("oauth-personal", "Google account", ("subscription",), ())
METHODS = {m.id: m for m in (GEMINI_API_KEY_METHOD, ENTERPRISE_METHOD, AGENT_PLATFORM_METHOD,
                             PERSONAL_ACCOUNT_METHOD)}
# Held on Lucas's word (PRODUCT.md 3603-3604): "personal-account sign-in waits on Lucas's lawyer."
PERSONAL_ACCOUNT_OFF = "antigravity_personal_account_off"


def route_method(route: Mapping) -> Method:
    """The one method a route names, checked against its connection. ``RouteError`` before anything starts."""
    if not isinstance(route, Mapping):
        raise c.RouteError(c.REQUEST_INVALID, "route")
    name = route.get("auth_method")
    if not isinstance(name, str) or name not in METHODS:
        raise c.RouteError(c.REQUEST_INVALID, f"route.auth_method: {name!r} is not an Antigravity method")
    method = METHODS[name]
    if method is PERSONAL_ACCOUNT_METHOD:   # held: unconditional, read from nothing
        raise c.RouteError(c.LAUNCH_REFUSED, PERSONAL_ACCOUNT_OFF)
    conn = route.get("connection")
    kind = conn.get("kind") if isinstance(conn, Mapping) else None
    if kind not in method.connection_kinds:
        raise c.RouteError(c.REQUEST_INVALID, f"route.connection.kind {kind!r} cannot carry {name}")
    if kind == "api_key" and conn.get("credential_env") not in method.credential_env:
        raise c.RouteError(c.LAUNCH_REFUSED, f"credential_env {conn.get('credential_env')} is not read by {name}")
    return method


# -- driver.v2 declarations -------------------------------------------------------------------------------
def _declaration(**flags) -> dict:
    names = ("multi_turn", "mid_turn_input", "interrupt", "resume", "fork", "permission_bridge", "typed_limits",
             "usage")
    return {"engine": ENGINE, "driver": 2, "flags": {n: bool(flags.get(n, False)) for n in names},
            "input_ack": False}


# One prompt, standard input closed, read until the result. ``usage``: the result event carries token
# counts. ``typed_limits`` false: a failed run gives only free text (``error``), never a typed limit.
ONE_SHOT_DECLARATION = _declaration(usage=True)
# ``agy acp`` through the shared client. multi_turn, interrupt (session/cancel) and permission_bridge
# (session/request_permission) rest on the ACP protocol and Google's ACP page, not on a run. ``resume`` is
# false until a load or resume is measured; ``typed_limits`` false (no Antigravity limit signal is known:
# PRODUCT.md 822, "not found"); ``usage`` false (stable ACP has no token counts); no input_ack (a prompt is
# acknowledged only when its turn ends, and a turn takes no input while in progress).
ACP_DECLARATION = _declaration(multi_turn=True, interrupt=True, permission_bridge=True)
UNVERIFIED_FLAGS = {"one_shot": ("usage",), "acp": ("multi_turn", "interrupt", "permission_bridge")}


# -- the one-shot drive -----------------------------------------------------------------------------------
def one_shot_argv(executable: str, model: str) -> list[str]:
    if not isinstance(model, str) or not model or model.startswith("-"):
        raise c.RouteError(c.REQUEST_INVALID, "route.model")
    return [executable, *ONE_SHOT_FLAGS, "--model", model, "--disable-slash-commands"]


def one_shot_stdin(prompt: str) -> str:
    """The one stdin line: a ``user`` event whose content is the prompt (headless page, stdin messages).

    A prompt that cannot be written as UTF-8 (a lone surrogate) is refused ``request_invalid``: the line could
    never reach agy's stdin. The adapter refuses it before anything starts; this is the same rule for any
    other caller."""
    line = json.dumps({"event": "user", "message": {"content": prompt}}, separators=(",", ":"), ensure_ascii=False)
    try:
        line.encode("utf-8")
    except UnicodeEncodeError:
        raise c.RouteError(c.REQUEST_INVALID, "prompt is not UTF-8") from None
    return line


# The result statuses Google's headless page lists. SUCCESS is the only success.
STATUSES = ("SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING")
CANCELLED = "cancelled"


class OneShotProtocolError(Exception):
    pass


# The largest step index a tool call's id is made from: 2^63 - 1, so ``step-<index>`` stays within event.v1's
# 128-character call id.
MAX_STEP_INDEX = 2**63 - 1


class OneShotParser:
    """``agy -p --output-format stream-json``: one stdout line in, event drafts (driver.v2) out.

    Every line gives an ``engine.output`` draft of the line as it is in the stdout file (text and byte
    offsets; lines must be fed in order, each without its newline). Then:

    * ``init``: the conversation id is kept (the engine's session id).
    * ``step_update`` of a ``tool`` step in state ``DONE`` with ``tool_info``: a ``tool.call`` and a
      ``tool.result`` draft, once per step. The call id is ``step-<step_index>`` (agy gives a tool call no
      id of its own); a step index that is missing, not an integer, or outside 0 .. 2^63 - 1 is a protocol
      error, so no tool call vanishes and the id always fits event.v1. The input is
      ``tool_info.parameters`` when it is an object, else null, and the digest
      (RFC 8785 then SHA-256) is always of what agy sent (JSON null when it sent none). Parameters with no
      RFC 8785 text (an integer beyond 2^53 - 1, for one) are a protocol error, as is a line nested too
      deeply to read or one that is not UTF-8.
    * ``result``: a ``usage`` draft from ``result.usage`` (span ``turn`` for one turn, else
      ``session_total``, which is never summed) and a ``result`` draft. Per-step usage is not drafted, so
      nothing is counted twice.

    Unknown events and fields are skipped (agy's own rule for its stdin). A line that is not a JSON object
    is a protocol error. Nothing in the agent's text decides a code, except that a failed run's ``error``
    text that sounds like a quota is a short fault (grill Q23), never a plan limit.
    """

    def __init__(self, connection_id: str, model: str | None, *, offset: int = 0) -> None:
        self.connection_id, self.model, self.offset = connection_id, model, offset
        self.conversation_id: str | None = None
        self.result: dict | None = None
        self._tool_steps: set = set()

    def parse(self, line: str) -> list[dict]:
        raw = line + "\n"
        start, self.offset = self.offset, self.offset + _utf8_length(raw, OneShotProtocolError)
        drafts = [{"type": "engine.output",
                   "payload": {"stream": "stdout", "text": raw, "offset_start": start, "offset_end": self.offset}}]
        try:
            message = json.loads(line)
        except (ValueError, RecursionError):
            raise OneShotProtocolError("stdout line is not JSON (or is nested too deeply to read)") from None
        if not isinstance(message, dict):
            raise OneShotProtocolError("stdout line is not a JSON object")
        if self.result is not None:
            raise OneShotProtocolError("a line after the result")
        event = message.get("event")
        if event == "init":
            cid = message.get("conversation_id")
            if isinstance(cid, str) and cid:
                self.conversation_id = cid
        elif event == "step_update":
            step = message.get("step_update")
            if isinstance(step, Mapping):
                drafts += self._tool(step)
        elif event == "result":
            result = message.get("result")
            if not isinstance(result, Mapping) or result.get("status") not in STATUSES:
                raise OneShotProtocolError("result without a known status")
            self.result = dict(result)
            usage = result.get("usage")
            if isinstance(usage, Mapping):
                drafts.append({"type": "usage", "payload": self._usage(usage, result.get("num_turns"))})
            code, detail = self.code()
            drafts.append({"type": "result", "payload": {"code": code, "detail": detail}})
        return drafts

    def code(self) -> tuple[str | None, str]:
        """The result's code and detail: (None, "") for SUCCESS."""
        result = self.result or {}
        status = result.get("status")
        error = result.get("error") if isinstance(result.get("error"), str) else ""
        if status == "SUCCESS":
            return None, ""
        if status in ("CANCELED", "INTERRUPTED"):
            return CANCELLED, f"status {status}"
        if status == "ERROR":
            code = c.SHORT_FAULT if c.sounds_like_quota(error) else c.ENGINE_ERROR
            return code, f"status ERROR: {error[:200]}" if error else "status ERROR"
        return c.ENGINE_ERROR, f"status {status}"

    def _tool(self, step: Mapping) -> list[dict]:
        info = step.get("tool_info")
        index = step.get("step_index")
        if step.get("step_type") != "tool" or step.get("state") != "DONE" or not isinstance(info, Mapping):
            return []
        if type(index) is not int or not 0 <= index <= MAX_STEP_INDEX:
            # The call id is made from it, and event.v1 caps a call id at 128 characters: a step index no
            # 64-bit counter gives (missing, not an integer, or out of range) is the agent's protocol error,
            # never a tool call dropped without a trace or an id the core must refuse.
            raise OneShotProtocolError("step_index is not a 64-bit count")
        if index in self._tool_steps:
            return []
        name = info.get("name") if isinstance(info.get("name"), str) and info["name"] else step.get("tool_name")
        if not isinstance(name, str) or not name or "\n" in name or "\r" in name:
            return []
        call_id = f"step-{index}"
        # The input as agy gave it: an object is carried; any other value is carried as null with ITS digest;
        # none at all is null with the digest of JSON null. Nothing is invented in its place.
        raw_params = info.get("parameters") if "parameters" in info else None
        try:
            digest = input_digest(raw_params)
        except NoCanonicalText:
            raise OneShotProtocolError("tool parameters have no RFC 8785 text") from None
        params = dict(raw_params) if isinstance(raw_params, Mapping) else None
        self._tool_steps.add(index)
        error = info.get("error")
        if isinstance(error, Mapping):
            status = "error"
            text = error.get("message") if isinstance(error.get("message"), str) else ""
        else:
            status = "ok"
            text = info.get("output") if isinstance(info.get("output"), str) else ""
        return [
            {"type": "tool.call", "payload": {"call_id": call_id, "name": name[:256],
                                              "input_digest": digest, "input": params}},
            {"type": "tool.result", "payload": {"call_id": call_id, "status": status, "text": text,
                                                "output_ref": None, "truncated": False}},
        ]

    def _usage(self, usage: Mapping, num_turns: object) -> dict:
        output = usage.get("output_tokens")
        thinking = usage.get("thinking_tokens")
        # agy's total_tokens is input plus output in both of Google's examples, so thinking is part of
        # output, which is what event.v1's reasoning_tokens means. When it is larger it cannot be.
        if not (_count(thinking) and _count(output) and thinking <= output):
            thinking = None
        return {
            "connection_id": self.connection_id,
            "model": self.model,
            "bucket": None,
            "span": "turn" if num_turns == 1 else "session_total",
            "input_tokens": _measure(usage.get("input_tokens")),
            "output_tokens": _measure(output),
            "reasoning_tokens": _measure(thinking),
            "cache_read_tokens": _measure(usage.get("cache_read_tokens")),
            "cache_write_tokens": _measure(None),
            "cost_usd": {"value": None, "quality": "unknown", "provenance": "unavailable"},
        }


def _utf8_length(raw: str, error: type) -> int:
    """The line's length in the stdout file. A line that cannot be written as UTF-8 (a lone surrogate, which a
    reader produces from bytes that were not UTF-8) is the agent's protocol error, not the parser's crash."""
    try:
        return len(raw.encode("utf-8"))
    except UnicodeEncodeError:
        raise error("stdout line is not UTF-8") from None


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _measure(value: object) -> dict:
    if _count(value):
        return {"value": value, "quality": "known", "provenance": "observed"}
    return {"value": None, "quality": "unknown", "provenance": "unavailable"}


# -- RFC 8785, for tool.call's input_digest ----------------------------------------------------------------
# The same rules as SPINE's ``control.methods.canonical_text`` (#369 927769a9, control/methods/__init__.py
# 289-354), copied because ``routes`` must not import ``control``. One shared function is in this lane's
# questions.
_MAX_INT = 2**53 - 1
_SHORT = {0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r", 0x22: '\\"', 0x5C: "\\\\"}


class NoCanonicalText(ValueError):
    pass


def _number(number) -> str:
    if type(number) is int:
        if not -_MAX_INT <= number <= _MAX_INT:
            raise NoCanonicalText("integer")
        return str(number)
    if not math.isfinite(number):
        raise NoCanonicalText("number")
    if number.is_integer() and not -_MAX_INT <= number <= _MAX_INT:
        raise NoCanonicalText("integer")
    if number == 0:
        return "0"
    sign = "-" if number < 0 else ""
    mantissa, _, exponent = repr(abs(number)).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits, point = whole + fraction, len(whole) + int(exponent or 0)
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip("0") or "0"
    count = len(digits)
    if count <= point <= 21:
        return sign + digits + "0" * (point - count)
    if 0 < point <= 21:
        return sign + digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * (-point) + digits
    power = point - 1
    tail = ("." + digits[1:]) if count > 1 else ""
    return f"{sign}{digits[0]}{tail}e{'+' if power >= 0 else '-'}{abs(power)}"


def _string(text: str) -> str:
    out = ['"']
    for ch in text:
        point = ord(ch)
        if 0xD800 <= point <= 0xDFFF:
            raise NoCanonicalText("surrogate")
        out.append(_SHORT.get(point) or ("\\u%04x" % point if point < 0x20 else ch))
    out.append('"')
    return "".join(out)


def canonical_text(value, depth: int = 0) -> str:
    if depth > 64:
        raise NoCanonicalText("depth")
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) in (int, float):
        return _number(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_text(item, depth + 1) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise NoCanonicalText("key")
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(_string(k) + ":" + canonical_text(value[k], depth + 1) for k in keys) + "}"
    raise NoCanonicalText(type(value).__name__)


def input_digest(value) -> str:
    return hashlib.sha256(canonical_text(value).encode("utf-8")).hexdigest()


# -- the one-shot adapter (route contract v0) and its factory -----------------------------------------------
# prepared_home(route, method_id) -> the absolute route home, prepared for exactly that method, or raises
# RouteError. Composition has no way to give one yet.
PreparedHome = Callable[[Mapping, str], str]
NOT_PREPARED = "antigravity_route_home_not_prepared"


class AntigravityOneShotAdapter(RouteAdapter):
    """One prompt through ``agy -p``. argv[0] is the ``binary`` option when given, else ``agy``, nothing else."""

    engine = ENGINE

    def __init__(self, launcher, clock, credentials, parent_env, *, binary: str | None = None,
                 prepared_home: PreparedHome | None = None, wall_clock=None) -> None:
        if binary is not None and (not isinstance(binary, str) or not binary.startswith("/") or "\x00" in binary):
            raise ValueError("binary must be an absolute path")
        super().__init__(launcher, clock, credentials, parent_env, wall_clock=wall_clock)
        self._binary = binary
        self._prepared_home = prepared_home

    @property
    def executable(self) -> str:
        return self._binary if self._binary is not None else DEFAULT_EXECUTABLE

    def run(self, request: dict, deadline_at: float) -> dict:
        """A request that is not the route contract's shape is refused ``request_invalid``, before anything
        starts, even when the caller did not validate it first."""
        rid = request.get("request_id") if isinstance(request, Mapping) else None
        rid = rid if isinstance(rid, str) and rid else ""
        if not isinstance(request, Mapping) or not rid:
            return c.result(rid, c.REQUEST_INVALID, detail="request_id")
        for name in ("prompt",):
            if not isinstance(request.get(name), str) or not request[name]:
                return c.result(rid, c.REQUEST_INVALID, detail=name)
        try:
            request["prompt"].encode("utf-8")
        except UnicodeEncodeError:   # a lone surrogate: agy's stdin takes UTF-8, so it could never be sent
            return c.result(rid, c.REQUEST_INVALID, detail="prompt is not UTF-8")
        route = request.get("route")
        if not isinstance(route, Mapping):
            return c.result(rid, c.REQUEST_INVALID, detail="route")
        if not isinstance(route.get("connection"), Mapping) or not isinstance(route["connection"].get("id"), str):
            return c.result(rid, c.REQUEST_INVALID, detail="route.connection")
        if not isinstance(route.get("model"), str):
            return c.result(rid, c.REQUEST_INVALID, detail="route.model")
        return super().run(request, deadline_at)

    def argv(self, request: dict) -> list[str]:
        route_method(request["route"])
        return one_shot_argv(self.executable, request["route"]["model"])

    def env(self, request: dict) -> dict[str, str]:
        route = request["route"]
        method = route_method(route)
        if self._prepared_home is None:
            raise c.RouteError(c.LAUNCH_REFUSED, NOT_PREPARED)
        home = self._prepared_home(route, method.id)
        if not isinstance(home, str) or not home.startswith("/"):
            raise c.RouteError(c.LAUNCH_REFUSED, NOT_PREPARED)
        credential = self.credential(request, method.credential_env) if method.credential_env else None
        # The child gets the base names, its own HOME, the updater switch and the method's one key: no
        # other Google name from the daemon's environment can reach it.
        env = child_env(self._parent_env, never=("HOME",), credential=credential)
        env["HOME"] = home
        env.update(UPDATER_ENV)
        return env

    def drive(self, process: EngineProcess, request: dict, deadline_at: float, events: list[dict]) -> dict:
        rid, route = request["request_id"], request["route"]
        process.send(one_shot_stdin(request["prompt"]))
        process.close_stdin()
        parser = OneShotParser(c.connection_key(route), route["model"])
        while parser.result is None:
            remaining = deadline_at - self._clock()
            if remaining <= 0:
                raise Deadline()
            try:
                line = process.read_line(remaining)
            except ReadTimeout:
                raise Deadline() from None
            if line is None:
                return c.result(rid, c.ENGINE_EXITED, events=events, detail="stdout ended before the result")
            try:
                drafts = parser.parse(line)
            except OneShotProtocolError as err:
                raise _Protocol(str(err)) from None
            for draft in drafts:
                if draft["type"] == "usage":
                    events.append(c.event("usage", "result.usage", _usage_data(draft["payload"])))
        code, detail = parser.code()
        if code is None:
            response = parser.result.get("response")
            return c.result(rid, None, text=response if isinstance(response, str) else "", events=events)
        if code == CANCELLED:  # not a route contract code: a run agy reports stopped is an engine error here
            code = c.ENGINE_ERROR
        return c.result(rid, code, events=events, detail=detail)


def _usage_data(payload: Mapping) -> dict:
    return {name: payload[name]["value"] for name in ("input_tokens", "output_tokens", "reasoning_tokens",
                                                      "cache_read_tokens") if payload[name]["value"] is not None}


def one_shot_factory(launcher, clock, credentials, environ, **options) -> AntigravityOneShotAdapter:
    """S3's ``factory(launcher, clock, credentials, environ, **options)``. ``options`` is at most ``binary``."""
    unknown = set(options) - {"binary"}
    if unknown:
        raise TypeError(f"unknown options: {sorted(unknown)}")
    return AntigravityOneShotAdapter(launcher, clock, credentials, environ, binary=options.get("binary"))


# -- the ACP drive (agy acp) through the shared client --------------------------------------------------------
def acp_argv(executable: str) -> list[str]:
    """``<executable> acp``. The executable is ``agy`` or an absolute path (the ``binary`` option), never
    empty, a flag or a string with a NUL in it."""
    if (not isinstance(executable, str) or "\x00" in executable
            or (executable != DEFAULT_EXECUTABLE and not executable.startswith("/"))):
        raise ValueError("the ACP executable must be agy or an absolute path")
    return [executable, *ACP_ARGS]


def acp_module():
    """The shared ACP client. It moves to ``routes.acp`` when that path opens; only this import changes."""
    from recursion_core.routes.engines.fixtures.antigravity import acp
    return acp


def acp_hooks():
    """Antigravity's hook for the shared client: no error of agy's is known to be a typed plan limit."""
    return acp_module().NO_HOOKS


def open_acp(process: EngineProcess, clock, route: Mapping, cwd: str, deadline_at: float, **session_options):
    """Check the route's one method, then initialize, authenticate with it and open a session.

    The personal account refuses before anything is written to ``process``. The hooks are Antigravity's own:
    a caller cannot replace them (``hooks=`` is refused), so no caller can make one of agy's errors a limit."""
    if "hooks" in session_options:
        raise TypeError("open_acp: the hooks are Antigravity's own and cannot be passed")
    method = route_method(route)
    acp = acp_module()
    session = acp.AcpSession(process, clock, hooks=acp_hooks(), **session_options)
    session.open(method.id, cwd, deadline_at)
    return session


class AcpDrafts:
    """Event drafts (driver.v2) for an ACP turn: one engine.output per stdout line, then the turn's result.

    ``on_line`` is the session's ``on_line``. ``ended(outcome, turn_id, delivery_id)`` gives the result
    draft and, when the agent answered the prompt (a typed answer to the client's own request id), the
    ``input.accepted`` draft of the delivery that began the turn."""

    def __init__(self, *, offset: int = 0) -> None:
        self.offset = offset
        self.drafts: list[dict] = []

    def on_line(self, line: str) -> None:
        if not isinstance(line, str):
            raise TypeError("on_line takes the line as a str")
        raw = line + "\n"
        start, self.offset = self.offset, self.offset + _utf8_length(raw, acp_module().AcpProtocolError)
        self.drafts.append({"type": "engine.output", "payload": {
            "stream": "stdout", "text": raw, "offset_start": start, "offset_end": self.offset}})

    def ended(self, outcome, turn_id: str | None = None, delivery_id: str | None = None) -> list[dict]:
        if not isinstance(outcome, acp_module().TurnOutcome):
            raise TypeError("ended takes the TurnOutcome the session gave")
        out = []
        if outcome.stop_reason is not None and turn_id is not None and delivery_id is not None:
            out.append({"type": "input.accepted",
                        "payload": {"delivery_id": delivery_id, "turn_id": turn_id, "how": "new_turn"}})
        out.append({"type": "result", "payload": {"code": outcome.code, "detail": outcome.detail}})
        self.drafts += out
        return out
