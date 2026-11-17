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

A SELECTED CREDENTIAL NEVER REACHES OUTPUT. An API-key route gives the child its one key, and the child
could echo it. So a launch that carries a key needs an OUTPUT BOUNDARY (``output_boundary``): a launcher,
asked for by the values to keep out, whose capture masks them before anything is persisted. Without one
every such launch is refused before the key is fetched from custody (``antigravity_output_boundary_missing``);
no boundary exists in the tree yet and the factory cannot be given one. With one, the adapter also masks the
key in every result text and detail it returns, and the parser in every draft it makes. The mask (``mask``)
finds the value in every spelling JSON allows (each character as it stands or as any escape of it), not only
the spellings a serializer writes, and replaces what spells it with a fill of the same UTF-8 length, so the
byte offsets of an ``engine.output`` draft still match a capture masked the same way; a decoded line that
still holds the value anywhere (a number that spells it) fails closed.
A route with no key (``oauth-business``, ``agent-platform`` over ``passthrough``)
launches without a boundary; ``agent-platform`` over ``passthrough`` gets no key at all and relies on the
Application Default Credentials the prepared route home holds for that method.
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
import threading
import unicodedata
from array import array
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from recursion_core.routes import contract as c
from recursion_core.routes.base import Deadline, RouteAdapter, _Protocol
from recursion_core.routes.launch import EngineProcess, ReadTimeout, child_env

# The shared client's: a value with no RFC 8785 text, which a ToolCalls digest raises (the digest contract).
from recursion_core.routes.engines.fixtures.antigravity.acp import NoCanonicalText  # noqa: I001

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

    A prompt that is not a str, or cannot be written as UTF-8 (a lone surrogate), is refused
    ``request_invalid``: the line could never carry it to agy's stdin. The adapter refuses it before anything
    starts; this is the same rule for any other caller."""
    if not isinstance(prompt, str):
        raise c.RouteError(c.REQUEST_INVALID, "prompt is not a str")
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

    ``redact`` names the values no draft may carry (the route's selected credential): each line is masked
    before anything is read from it (``mask``, which reads every JSON spelling of them), so no draft, result
    or code detail holds them. A line whose mask leaves no JSON object (a value where a number or a literal
    stands), or whose decoded object still holds one anywhere (a number whose text spells it), is a protocol
    error: it fails closed.
    """

    def __init__(self, connection_id: str, model: str | None, *, offset: int = 0, redact=()) -> None:
        self.connection_id, self.model, self.offset = connection_id, model, offset
        self.conversation_id: str | None = None
        self.result: dict | None = None
        self._tool_steps: set = set()
        self._redact = mask_forms(redact)

    def parse(self, line: str) -> list[dict]:
        line = mask(line, self._redact)
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
        if self._redact and _number_spells(message, self._redact):
            raise OneShotProtocolError("a value to keep out is still in the line once it is read")
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


def mask_forms(values) -> tuple[str, ...]:
    """What ``mask`` looks for: each value, longest first. Every value must be a non-empty str, exactly (a
    subclass is refused: it could answer ``find`` or ``in`` falsely). ``mask`` reads
    every JSON spelling of each, also in JSON written inside a JSON string, so no escaped form is listed."""
    if isinstance(values, (str, bytes)):   # one value given bare would be read as its characters
        raise ValueError("values to mask are a collection of str, not one str")
    forms = set()
    for value in values:
        if type(value) is not str or not value:
            raise ValueError("a value to mask is a non-empty str, exactly")
        forms.add(value)
        # a canonical variant reads as the same text to anyone who normalizes it; a compatibility one (NFKC,
        # NFKD: fullwidth, superscript, ligature forms) is beyond ordinary output and is not listed
        forms.update(unicodedata.normalize(form, value) for form in ("NFC", "NFD"))
    return tuple(sorted(forms, key=len, reverse=True))


# The fill ``mask`` writes: the first of these that is not the one character a value is made of (a value of
# asterisks only is masked with "#"), so a masked stretch never spells a value. None of them needs a JSON
# escape, so a fill inside a JSON string leaves the string valid.
_FILLS = "*#%~=+@^_xXoO0123456789"
# The bound on ``mask``'s work: layers of escapes read per round (JSON inside a JSON string, the layers a
# serializer writes, is two or three) and rounds per text.
MASK_DEPTH = 8
MASK_ROUNDS = 8
# One JSON string escape: a backslash and u with four hex digits, or a backslash and the character after it.
_ESCAPE = re.compile(r"\\(?:u[0-9a-fA-F]{4}|.)", re.S)
_SIMPLE_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def mask(text: str, forms: tuple[str, ...]) -> str:
    """``text`` with every spelling of each form replaced by a fill character (``_FILLS``) of the same UTF-8
    length, so a byte offset into text masked this way is the offset into the text as it was.

    A spelling is any stretch of ``text`` that reads as the form: as it stands, once its JSON string escapes
    are read (a u-escape of any character, an escaped slash, a surrogate pair for a character beyond the
    BMP), or once they are read again for JSON written inside a JSON string, to any depth. Whatever escape
    spelling the agent chose, the value it decodes to is masked, and so are the value's canonical spellings
    (NFC, NFD; ``mask_forms`` lists them). Its compatibility spellings (NFKC, NFKD: fullwidth, superscript or
    ligature forms of its characters) are not read. Text that is not JSON is read the same way. Masking
    repeats until no spelling is left, since a fill could complete a value partly made of the fill
    character.

    THE WORK IS BOUNDED: a round reads the text and at most ``MASK_DEPTH`` layers of escapes beneath it (one
    linear pass each; a further layer is decoded once only to see that it exists), and at most
    ``MASK_ROUNDS`` rounds run. Text that still has an escape to read after the last layer (escapes
    nested deeper than any serializer writes, or built to peel one layer at a time), or that is not masked
    within the rounds, keeps nothing: it becomes fill of its whole UTF-8 length, which no reader takes for
    JSON, so the parser fails closed on it.

    ``text`` is exactly a ``str``: a subclass is refused (``TypeError``), forms or not, since it could answer
    ``find`` or ``in`` falsely and keep a value unmasked."""
    if type(text) is not str:   # a str subclass could answer find() or `in` falsely and keep a value unmasked
        raise TypeError("mask takes exactly a str")
    if not forms:
        return text
    fill = _fill(forms)
    for _round in range(MASK_ROUNDS):
        cuts = []
        level, starts = text, None          # starts: where each character of ``level`` begins in ``text``
        for _layer in range(MASK_DEPTH + 1):
            for form in forms:
                at = level.find(form)
                while at >= 0:
                    end = at + len(form)
                    if starts is None:
                        cuts.append((at, end))
                    else:
                        cuts.append((starts[at], starts[end] if end < len(starts) else len(text)))
                    at = level.find(form, at + 1)
            decoded, inner = _decode(level) if "\\" in level else (level, None)
            if len(decoded) == len(level):  # no escape left to read: every depth has been read
                break
            starts = inner if starts is None else array("q", map(starts.__getitem__, inner))
            level = decoded
        else:                               # escapes deeper than MASK_DEPTH: keep nothing
            return fill * _bytes(text)
        if not cuts:
            return text
        pieces, pos = [], 0
        for start, end in sorted(cuts):
            start = max(start, pos)
            if end > start:
                pieces += [text[pos:start], fill * _bytes(text[start:end])]
                pos = end
        pieces.append(text[pos:])
        masked = "".join(pieces)
        if masked == text:                  # never with a fill no value is made of; were it so, keep nothing
            return fill * _bytes(text)
        text = masked
    return fill * _bytes(text)              # not masked within MASK_ROUNDS rounds: keep nothing


def _fill(forms) -> str:
    made_of_one = {form[0] for form in forms if len(set(form)) == 1}
    for fill in _FILLS:
        if fill not in made_of_one:
            return fill
    raise ValueError("every fill character is a value to mask")


def _bytes(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def _decode(text: str) -> tuple[str, array]:
    """``text`` with its JSON string escapes read, and for each character of the result the index in ``text``
    where its spelling starts. Two u-escapes of a surrogate pair read as one character, as JSON reads them; an
    escape JSON does not allow reads leniently as the character after the backslash."""
    out: list[str] = []
    starts = array("q")
    pos, high = 0, False      # high: the last character read is a u-escape of a high surrogate
    for match in _ESCAPE.finditer(text):
        if match.start() > pos:
            out.append(text[pos:match.start()])
            starts.extend(range(pos, match.start()))
            high = False
        body = match.group()[1:]
        if len(body) == 5:
            point = int(body[1:], 16)
            if high and 0xDC00 <= point <= 0xDFFF:
                out[-1] = chr(0x10000 + ((ord(out[-1]) - 0xD800) << 10) + (point - 0xDC00))
                high, pos = False, match.end()
                continue
            out.append(chr(point))
            high = 0xD800 <= point <= 0xDBFF
        else:
            out.append(_SIMPLE_ESCAPES.get(body, body))
            high = False
        starts.append(match.start())
        pos = match.end()
    if pos < len(text):
        out.append(text[pos:])
        starts.extend(range(pos, len(text)))
    return "".join(out), starts


def _number_spells(value, forms) -> bool:
    """Whether a float in a decoded JSON value spells a form once read (``1.2345e4`` is ``12345.0``). Strings
    and keys need no check here: ``mask`` has read every spelling of them. An integer has one JSON spelling,
    its own digits, which ``mask`` has already matched as they stand."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack += item.values()
        elif isinstance(item, list):
            stack += item
        elif isinstance(item, float) and any(form in repr(item) for form in forms):
            return True
    return False


def _utf8_length(raw: str, error: type) -> int:
    """The line's length in the stdout file. A line that cannot be written as UTF-8 (a lone surrogate, which a
    reader produces from bytes that were not UTF-8) is the agent's protocol error, not the parser's crash."""
    try:
        return len(raw.encode("utf-8"))
    except UnicodeEncodeError:
        raise error("stdout line is not UTF-8") from None


def _read_length(line: str) -> int:
    """A line's length in bytes as the reader read it: a surrogate a ``surrogateescape`` decode made is the one
    byte it stands for; any other surrogate is counted as ``surrogatepass`` writes it."""
    try:
        return len(line.encode("utf-8", "surrogateescape"))
    except UnicodeEncodeError:
        return len(line.encode("utf-8", "surrogatepass"))


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
ROUTE_HOME_IS_DAEMON_HOME = "antigravity_route_home_is_daemon_home"
DAEMON_HOME_UNKNOWN = "antigravity_daemon_home_unknown"
CREDENTIAL_UNAVAILABLE = "antigravity_credential_unavailable"
# output_boundary(values) -> a launcher (as ``routes.launch``) whose capture of the child's stdout and stderr
# masks each line with ``mask(line, mask_forms(values))`` (every JSON spelling of each value) before anything
# is persisted, so the capture's bytes and the drafts' offsets agree. The launch side's to give; there is
# none in the tree yet. A boundary that raises ``OSError`` while setting up is a refused launch.
OutputBoundary = Callable[[tuple], Callable]
OUTPUT_UNPROTECTED = "antigravity_output_boundary_missing"


class AntigravityOneShotAdapter(RouteAdapter):
    """One prompt through ``agy -p``. argv[0] is the ``binary`` option when given, else ``agy``, nothing else.

    A route that carries a key launches only through ``output_boundary`` (see the module's docstring); the
    run loop is this class's own so that each run's key stays local to that run."""

    engine = ENGINE

    def __init__(self, launcher, clock, credentials, parent_env, *, binary: str | None = None,
                 prepared_home: PreparedHome | None = None, output_boundary: OutputBoundary | None = None,
                 wall_clock=None) -> None:
        if binary is not None and (not isinstance(binary, str) or not binary.startswith("/") or "\x00" in binary):
            raise ValueError("binary must be an absolute path")
        super().__init__(launcher, clock, credentials, parent_env, wall_clock=wall_clock)
        self._binary = binary
        self._prepared_home = prepared_home
        self._output_boundary = output_boundary

    @property
    def executable(self) -> str:
        return self._binary if self._binary is not None else DEFAULT_EXECUTABLE

    def run(self, request: dict, deadline_at: float) -> dict:
        """A request that is not the route contract's shape is refused ``request_invalid``, before anything
        starts, even when the caller did not validate it first: ``contract`` (the contract's version),
        ``request_id``, ``tenant`` and ``prompt`` (non-empty str), ``deadline_s`` (a finite number above 0, not
        a bool), ``route``, ``route.connection.id`` (non-empty str) and ``route.model``, as
        ``contract.validate_request`` has them. Which engine a route names is the dispatcher's check
        (``engine_unknown``), not this adapter's. ``deadline_at`` must be a finite number of the clock's
        seconds, or the request is refused the same way: no child starts with a deadline that cannot end it.
        Whatever escapes once a child has started fences it first."""
        refused = self._refused(request, deadline_at)
        if refused is not None:
            return refused
        rid = request["request_id"]
        # The base class's loop, with one difference: the key selected for this run decides the launcher (the
        # output boundary) and what the parser and the result mask, and it never leaves this call.
        return self._launched(rid, request, deadline_at)

    def _refused(self, request, deadline_at) -> dict | None:
        """The request's shape and ``deadline_at``, as ``run`` describes them: the refusal, or None. A
        ``request_id`` that is not a non-empty str is never echoed (the refusal carries ``""``). Every adapter
        of this module calls it before anything of its own."""
        rid = request.get("request_id") if isinstance(request, Mapping) else None
        rid = rid if isinstance(rid, str) and rid else ""
        if not isinstance(request, Mapping) or not rid:
            return c.result(rid, c.REQUEST_INVALID, detail="request_id")
        if request.get("contract") != c.CONTRACT_VERSION:
            return c.result(rid, c.REQUEST_INVALID, detail="contract")
        for name in ("tenant", "prompt"):
            if not isinstance(request.get(name), str) or not request[name]:
                return c.result(rid, c.REQUEST_INVALID, detail=name)
        deadline_s = request.get("deadline_s")
        if not _finite(deadline_s) or deadline_s <= 0:
            return c.result(rid, c.REQUEST_INVALID, detail="deadline_s")
        if not _finite(deadline_at):
            return c.result(rid, c.REQUEST_INVALID, detail="deadline_at")
        if self._deadline_passed(deadline_at):
            # Already expired: nothing is prepared and nothing is launched.
            return c.result(rid, c.DEADLINE_EXCEEDED, detail="recursion deadline")
        try:
            request["prompt"].encode("utf-8")
        except UnicodeEncodeError:   # a lone surrogate: agy's stdin takes UTF-8, so it could never be sent
            return c.result(rid, c.REQUEST_INVALID, detail="prompt is not UTF-8")
        route = request.get("route")
        if not isinstance(route, Mapping):
            return c.result(rid, c.REQUEST_INVALID, detail="route")
        if (not isinstance(route.get("connection"), Mapping) or not isinstance(route["connection"].get("id"), str)
                or not route["connection"]["id"]):
            return c.result(rid, c.REQUEST_INVALID, detail="route.connection")
        if not isinstance(route.get("model"), str) or not route["model"]:
            return c.result(rid, c.REQUEST_INVALID, detail="route.model")
        return None

    def _launched(self, rid, request, deadline_at) -> dict:
        try:
            argv = self.argv(request)
            env, secret = self._prepare(request)
        except c.RouteError as err:
            return c.result(rid, err.code, detail=err.detail)
        secrets = () if secret is None else (secret,)
        forms = mask_forms(secrets)
        launcher = self._launcher
        if secrets:
            try:
                launcher = self._output_boundary(secrets)
            except c.RouteError as err:
                return c.result(rid, err.code, detail=mask(err.detail, forms))
            except OSError:      # the boundary could not set its capture up: nothing is launched
                return c.result(rid, c.LAUNCH_REFUSED, detail=OUTPUT_UNPROTECTED)
        if self._deadline_passed(deadline_at):
            # Preparation (the route home, custody, the boundary) spent the time that was left.
            return c.result(rid, c.DEADLINE_EXCEEDED, detail="recursion deadline")
        try:
            process = launcher(argv, env=env, shell=False)
        except c.RouteError as err:
            return c.result(rid, err.code, detail=mask(err.detail, forms))
        except OSError as err:
            return c.result(rid, c.ENGINE_ERROR, detail=f"launch failed: {type(err).__name__}")
        events: list[dict] = []
        try:
            return self.drive(process, request, deadline_at, events, redact=secrets)
        except Deadline:
            process.fence("deadline")
            return c.result(rid, c.DEADLINE_EXCEEDED, events=events, detail="recursion deadline")
        except _Protocol as err:
            process.fence("protocol_error")
            return c.result(rid, c.PROTOCOL_ERROR, events=events, detail=str(err))   # fixed texts only
        except OSError as err:
            process.fence("io_error")
            return c.result(rid, c.ENGINE_ERROR, events=events, detail=f"engine io: {type(err).__name__}")
        except BaseException:   # anything else (a caller's callable, an interruption): the child is fenced first
            process.fence("unexpected")
            raise

    def argv(self, request: dict) -> list[str]:
        route_method(request["route"])
        return one_shot_argv(self.executable, request["route"]["model"])

    def env(self, request: dict) -> dict[str, str]:
        return self._prepare(request)[0]

    def _prepare(self, request: dict) -> tuple[dict[str, str], str | None]:
        """The child's environment and the key it carries (None for a route without one)."""
        route = request["route"]
        method = route_method(route)
        if self._prepared_home is None:
            raise c.RouteError(c.LAUNCH_REFUSED, NOT_PREPARED)
        if route["connection"].get("kind") == "api_key" and self._output_boundary is None:
            # Refused before the route home is asked for and before custody is: no key is fetched for a run
            # whose output nothing keeps it out of.
            raise c.RouteError(c.LAUNCH_REFUSED, OUTPUT_UNPROTECTED)
        try:
            home = self._prepared_home(route, method.id)
        except OSError:      # the route-home loader works on the filesystem: its failure is a home not prepared
            raise c.RouteError(c.LAUNCH_REFUSED, NOT_PREPARED) from None
        if not isinstance(home, str) or not home.startswith("/") or "\x00" in home:
            raise c.RouteError(c.LAUNCH_REFUSED, NOT_PREPARED)
        # The daemon's own HOME never reaches the child, however the callable came to name it (a fallback, an
        # alias, a caller override): compared by resolved path, it or any path inside it is refused. The
        # brief has no own-login route ("never reuse a Google login another program holds").
        own = self._daemon_home()
        if own is None:   # the daemon's HOME cannot be told: no route home can be shown not to be it
            raise c.RouteError(c.LAUNCH_REFUSED, DAEMON_HOME_UNKNOWN)
        real = posixpath.realpath(home)
        if real == own or own == "/" or real.startswith(own + "/"):   # "/" holds every path
            raise c.RouteError(c.LAUNCH_REFUSED, ROUTE_HOME_IS_DAEMON_HOME)
        try:
            credential = self.credential(request, method.credential_env) if method.credential_env else None
        except OSError:      # custody (a keychain) could not give the key: refused, typed, nothing launched
            raise c.RouteError(c.LAUNCH_REFUSED, CREDENTIAL_UNAVAILABLE) from None
        if credential is not None and "\x00" in credential[1]:
            raise c.RouteError(c.LAUNCH_REFUSED, f"the credential for {route['connection']['id']} is not usable")
        # The child gets the base names, its own HOME, the updater switch and the method's one key: no
        # other Google name from the daemon's environment can reach it. ``never=("HOME",)`` is not what keeps
        # the daemon's HOME out (the next line overwrites any HOME it copied): that is
        # the overwrite with the route home together with the check above that the route home is not the
        # daemon's HOME.
        env = child_env(self._parent_env, never=("HOME",), credential=credential)
        env["HOME"] = home
        env.update(UPDATER_ENV)
        return env, (credential[1] if credential is not None else None)

    def _daemon_home(self) -> str | None:
        """The daemon's own HOME, resolved, from the parent environment the adapter was given (its one view of
        the daemon; never the process's own environment). None when that environment gives no absolute HOME
        without a NUL: then no launch is made (``antigravity_daemon_home_unknown``), since the daemon's real
        HOME could be anything. "/" (a container with no home) holds every path, so every route home is
        refused then."""
        given = self._parent_env.get("HOME")
        if not isinstance(given, str) or not given.startswith("/") or "\x00" in given:
            return None
        return posixpath.realpath(given)

    def _deadline_passed(self, deadline_at: float) -> bool:
        """True when the clock returns a time at or after ``deadline_at``. A clock that raises is not a
        deadline reading: that failure is left until a child exists, so the child is fenced before it
        propagates (a clock that fails on its first read must not skip the launch)."""
        try:
            now = self._clock()
        except BaseException:
            return False
        return now >= deadline_at

    def drive(self, process: EngineProcess, request: dict, deadline_at: float, events: list[dict], *,
              redact=()) -> dict:
        rid, route = request["request_id"], request["route"]
        if self._clock() >= deadline_at:
            raise Deadline()   # nothing of the prompt is written once the deadline has passed
        process.send(one_shot_stdin(request["prompt"]))
        process.close_stdin()
        parser = OneShotParser(c.connection_key(route), route["model"], redact=redact)
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
            if self._clock() >= deadline_at:   # the line's reading and masking ran past the deadline
                raise Deadline()
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


def _finite(value) -> bool:
    """A finite int or float, not a bool (a float-sized one: ``10**400`` is not)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


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
    a caller cannot replace them (``hooks=`` is refused), so no caller can make one of agy's errors a limit.
    ``drafts=`` (an ``AcpDrafts``) wires its five callbacks, ``on_turn_start`` and ``on_turn_end`` included."""
    if "hooks" in session_options:
        raise TypeError("open_acp: the hooks are Antigravity's own and cannot be passed")
    drafts = session_options.pop("drafts", None)
    if drafts is not None:
        # The drafts take all four callbacks, abandoned turns included: none of them is left to the caller.
        if not isinstance(drafts, AcpDrafts):
            raise TypeError("open_acp: drafts is an AcpDrafts")
        given = {"on_line", "on_update", "on_ask", "on_turn_end", "on_turn_start"} & set(session_options)
        if given:
            raise TypeError(f"open_acp: drafts= takes the callbacks itself; also given: {sorted(given)}")
        # on_turn_start and on_turn_end come along with the bound callbacks (AcpSession's rule)
        session_options.update(on_line=drafts.on_line, on_update=drafts.on_update, on_ask=drafts.on_ask)
    method = route_method(route)
    acp = acp_module()
    session = acp.AcpSession(process, clock, hooks=acp_hooks(), **session_options)
    session.open(method.id, cwd, deadline_at)
    return session


@dataclass(eq=False)
class _DraftTurn:
    """ONE turn's state in ``AcpDrafts``: made when the turn starts, dropped once its result is drafted. Nothing
    of a turn lives anywhere else."""

    key: tuple | None           # (session, turn), or None until the turn is named
    tools: object               # its tool calls (the shared client's ToolCalls)
    closed: bool = False        # its open calls are ended
    resulted: bool = False      # its result is drafted
    announced: bool = False     # the session told its start and has not told its end yet
    thread: int | None = None   # the thread whose session announced it


class AcpDrafts:
    """Event drafts (driver.v2) for ACP turns: one engine.output per stdout line, tool.call and tool.result
    as the agent reports its tool calls, then each turn's result.

    Its session callbacks are ``on_line``, ``on_update``, ``on_ask``, ``turn_start`` (the session's
    ``on_turn_start``) and ``turn_end`` (``on_turn_end``). ``open_acp(..., drafts=d)`` wires all five, and
    wiring any of the first three bound to ``d`` brings the other two along (``AcpSession``'s rule).
    ``ended(outcome, turn_id, delivery_id)`` drafts the ``input.accepted`` of the delivery that began the turn
    (when the agent answered the prompt) and the turn's result; ``abandoned(code, detail)`` drafts the result
    of the turn in progress, whose ``prompt`` raised. The two ids are given together or not at all, each
    exactly a ``str`` of common.v1's form (``turn-...``, ``dlv-...``).

    A TURN'S STATE IS ONE OBJECT (``_DraftTurn``), made when the turn starts (the session's ``turn_start``, the
    driver's ``start_turn``, or the first update or ask of a turn nobody announced) and held under its key
    ``(session, turn)`` (``TurnOutcome.session`` is its connection's serial). Its tool calls, whether they are
    ended and whether its result is drafted live in it and nowhere else, and it is dropped once its result is
    drafted. EVERY TURN THE SESSION OR THE DRIVER ENDS GETS EXACTLY ONE RESULT, whether or not a line of it was
    read. Per session, two numbers outlive a turn: the highest turn number whose result is drafted, so that an
    end of a turn already gone (``ended`` twice, ``ended`` of an earlier turn, ``turn_end`` again, a repeated
    ``turn_start``) drafts nothing and raises nothing; and the highest turn number the session announced, so
    that ``ended`` of a later turn, which has not begun, is refused (``TypeError``) instead of taking that
    turn's result.

    SESSIONS: one ``AcpDrafts`` serves any number of sessions ONE THREAD AT A TIME. A session whose turn starts
    while another session's announced turn is in progress on the same thread (a callback of the first runs
    the second's turn) is NESTED: the first turn is put aside untouched, every callback until the second's
    end belongs to the second (it is the one reading lines on this thread), and the first is taken up again
    at that end. A start, update, ask or line from another thread while an announced turn is in progress is
    refused (``AcpStateError``) and changes nothing: sessions pumped at once on several threads take one
    ``AcpDrafts`` each. Callbacks wrapped so that the session cannot see this object announce nothing, so
    such a caller keeps to one session at a time itself. A lock makes each call one step.

    ONE PLACE ENDS A TURN'S OPEN TOOL CALLS: ``_close``, on the turn's own object, so a second close is
    harmless. A call left open ends ``cancelled`` when the turn was stopped (the caller's cancel, Recursion's
    deadline, or a turn abandoned because handling it raised) and ``error`` otherwise. NO LINE ENDS A TURN BY
    ITSELF: only the session (``turn_end``) or the driver (``ended``, ``abandoned``, ``start_turn``) says a
    turn ended, so a stray or repeated response line ends nothing. Callbacks wrapped so that the session
    cannot see this object get a turn's start and end only from the driver: ``start_turn`` before each prompt,
    ``abandoned`` in its ``except`` and ``ended`` after it; a turn the driver did not start reaches this
    object only through its updates. ``abandoned`` and ``start_turn`` leave a turn the session announced and
    has not ended to the session. A driver that ends a turn with a code outside the route contract's, or a
    detail that is not exactly a str, is refused (``TypeError``) before anything is drafted.

    ``redact`` names the values no draft may carry (the route's selected credential). A value is looked for
    in every spelling ``mask`` reads, AND ACROSS PIECES: in the strings, keys and other scalars of a decoded
    line or update, in the order they are written and with any pieces between skipped (a reader may join
    values only, keys only, or a list's texts), a value spelled by the end of one piece, whole pieces and the
    start of a later one is masked in each piece it touches, so a value split over two content items, two
    input values or two keys reaches no draft. A line (exactly a str)
    is masked in place, so its byte length and every offset stay; an update is masked before a tool call's
    input, its digest, its name or its result text is taken from it. What cannot be masked in place is the
    agent's protocol error, refused before anything of it is drafted (an ``engine.output`` included; the
    refused line's bytes still count in the offset): a number, a bool or null whose text (as JSON or Python
    writes it) spells a value, or a number equal to one (``1.2345e4`` is ``12345``), or such a scalar that
    holds a part of a value spelled across pieces; a tool call id that holds such a part; two keys of one
    object that mask alike. A result's text and detail never keep a lone surrogate, and are masked again after one
    is replaced. A value split over two LINES is not looked for: no draft joins lines.

    A tool_call the agent writes AFTER its answer to a stopped turn is drafted in the next turn: no line on
    the wire names a turn (ACP updates carry only the session), so it reads exactly as the next turn's own
    first call (the test ``test_r2_f4_a_late_call_reads_as_the_next_turns_own`` shows the two streams are
    the same lines). Lines read while an abandoned turn is drained are stray and reach no ``on_update``."""

    def __init__(self, *, offset: int = 0, redact=()) -> None:
        if type(offset) is not int or offset < 0:
            raise TypeError("offset is a count of bytes (an int, 0 or more)")
        self.offset = offset                # of the stream, not of a turn
        self.drafts: list[dict] = []
        self._redact = mask_forms(redact)
        self._turn: _DraftTurn | None = None   # the turn in progress: where lines' and updates' drafts go
        self._turns: dict = {}                 # (session, turn) -> its _DraftTurn, until its result is drafted
        self._done: dict = {}                  # session -> the highest turn number whose result is drafted
        self._begun: dict = {}                 # session -> the highest turn number the session announced
        self._outer: list = []                 # turns put aside by a nested session's turn, innermost last
        self._lock = threading.RLock()

    # -- a turn's state -----------------------------------------------------------------------------------
    def _new_turn(self, key) -> _DraftTurn:
        forms = self._redact
        tools = acp_module().ToolCalls(input_digest, mask=(lambda text: mask(text, forms)) if forms else None)
        turn = _DraftTurn(key, tools)
        if key is not None:
            self._turns[key] = turn
        return turn

    def _current(self) -> _DraftTurn:
        """The turn in progress, or a new one when the last has ended: an update after a turn's end belongs to
        the next turn."""
        turn = self._turn
        if turn is None or turn.closed or turn.resulted:
            turn = self._turn = self._new_turn(None)
        return turn

    def _owned(self) -> None:
        """Refuses a call from another thread while an announced turn is in progress: it changes nothing."""
        turn = self._turn
        if turn is not None and turn.announced and turn.thread != threading.get_ident():
            raise acp_module().AcpStateError("these drafts serve a turn in progress on another thread")

    def _turn_for(self, key) -> _DraftTurn | None:
        """Turn ``key``'s state: its object while it is held; the turn in progress when that one is not named
        yet (it is this turn); a new object for a turn nothing of which reached these drafts; None when its
        result is drafted already. A held turn is never one whose result is drafted."""
        if key in self._turns:
            return self._turns[key]
        if key[1] <= self._done.get(key[0], 0):
            return None
        turn = self._turn
        if turn is not None and turn.key is None:
            turn.key = key
            if turn.resulted:               # abandoned() drafted its result before it was named
                self._record(key)
                return None
            self._turns[key] = turn
            return turn
        return self._new_turn(key)

    def _record(self, key) -> None:
        self._turns.pop(key, None)
        if key[1] > self._done.get(key[0], 0):
            self._done[key[0]] = key[1]

    def _close(self, turn: _DraftTurn, stopped: bool) -> list[dict]:
        """THE place a turn's open tool calls end. Harmless when nothing is open."""
        out = turn.tools.turn_ended(stopped)
        turn.closed = True
        self.drafts += out
        return out

    def _result(self, turn: _DraftTurn, code, detail: str) -> dict:
        text = _writable(mask(detail, self._redact))
        if self._redact and text != detail:
            text = mask(text, self._redact)     # a replaced surrogate can spell a value: masked as it now stands
        draft = {"type": "result", "payload": {"code": code, "detail": text}}
        self.drafts.append(draft)
        turn.resulted = True
        turn.announced = False
        if turn.key is not None:
            self._record(turn.key)
        self._leave(turn)
        return draft

    def _leave(self, turn: _DraftTurn) -> None:
        """A turn's end: a turn a nested session's turn put aside is taken up again."""
        if turn in self._outer:
            self._outer.remove(turn)
        elif turn is self._turn and self._outer:
            self._turn = self._outer.pop()

    # -- the session's callbacks ------------------------------------------------------------------------
    def turn_start(self, key) -> list[dict]:
        """The session's ``on_turn_start``: turn ``key`` began (its prompt is written, nothing of it is read).
        Its state is made here. A turn of the same session still in progress with calls open, which nothing
        ended, has them ended ``cancelled``; those drafts are returned. Another session's announced turn in
        progress is put aside (nested, above). A key already held or already resulted changes nothing."""
        _check_key(key, "turn_start", none=False)
        with self._lock:
            self._owned()
            if key in self._turns or key[1] <= self._done.get(key[0], 0):
                return []
            turn = self._turn
            out = []
            if turn is not None and not turn.closed and not turn.resulted:
                if turn.announced and turn.key[0] != key[0]:
                    self._outer.append(turn)
                else:
                    out = self._close(turn, True)
            self._turn = self._new_turn(key)
            self._turn.announced, self._turn.thread = True, threading.get_ident()
            self._begin(key)
            return out

    def _begin(self, key) -> None:
        if key[1] > self._begun.get(key[0], 0):
            self._begun[key[0]] = key[1]

    def turn_end(self, key, outcome, code: str, detail: str) -> list[dict]:
        """The session's ``on_turn_end``: every exit of ``prompt`` comes here. ``outcome`` is the turn's
        ``TurnOutcome``, or None when ``prompt`` raised: then the turn's open calls end ``cancelled`` and its
        result is drafted here (none when no turn began, ``key`` None). Again for one turn, it drafts nothing.
        A ``key`` that is not the outcome's own turn is refused (``TypeError``) before anything changes."""
        _check_key(key, "turn_end", none=True)
        if outcome is not None:
            if not isinstance(outcome, acp_module().TurnOutcome):
                raise TypeError("turn_end takes the TurnOutcome the session gave, or None")
            if key != (outcome.session, outcome.turn):
                raise TypeError("turn_end takes the key of its outcome's own turn")
        else:
            _check_end(code, detail)
        with self._lock:
            if key is None:
                return []
            self._begin(key)
            turn = self._turn_for(key)
            if turn is None:
                return []
            turn.announced = False
            if outcome is not None:
                out = self._close(turn, _stopped(outcome))
                self._leave(turn)
                return out
            out = self._close(turn, True)
            out.append(self._result(turn, code, detail))
            return out

    def on_line(self, line: str) -> None:
        if type(line) is not str:
            raise TypeError("on_line takes the line as a str")
        with self._lock:
            self._owned()
            try:
                raw, refused = _masked_line(line, self._redact)
                raw += "\n"
                try:
                    length = _utf8_length(raw, acp_module().AcpProtocolError)
                except BaseException:
                    # The line was read, so its bytes count, as the reader decoded them (a lone surrogate is a
                    # byte that was not UTF-8): every later span stays at its true place in the stream. Nothing
                    # is drafted.
                    self.offset += _read_length(line) + 1
                    raise
                if refused is not None:
                    self.offset += length     # its bytes were read: later offsets stay those of the stream
                    raise acp_module().AcpProtocolError(refused)
            except BaseException:
                turn = self._turn           # the turn is abandoned: its open calls end here, not in the next turn
                if turn is not None and not turn.closed and not turn.resulted:
                    self._close(turn, True)
                raise
            start, self.offset = self.offset, self.offset + length
            self.drafts.append({"type": "engine.output", "payload": {
                "stream": "stdout", "text": raw, "offset_start": start, "offset_end": self.offset}})

    def on_update(self, params) -> None:
        if not isinstance(params, Mapping) or not isinstance(params.get("update"), Mapping):
            raise TypeError("on_update takes a session/update's params: a mapping whose update is a mapping")
        with self._lock:
            self._owned()
            turn = self._current()
            try:
                try:
                    masked = _masked_update(params, self._redact)
                except RecursionError:      # nested too deeply to read: the agent's protocol error, never a crash
                    raise acp_module().AcpProtocolError("an update nested too deeply to read") from None
                drafts = turn.tools.update(masked)
            except BaseException:
                self._close(turn, True)     # the turn is abandoned: its open calls end here, not in the next turn
                raise
            self.drafts += drafts

    def on_ask(self, ask) -> None:
        if not isinstance(ask, acp_module().PermissionAsk):
            raise TypeError("on_ask takes the PermissionAsk the session gave")
        with self._lock:
            self._owned()
            self._current().tools.asked(ask)

    # -- the driver's calls ---------------------------------------------------------------------------------
    def start_turn(self) -> list[dict]:
        """Before a prompt: a new turn begins (for callbacks the session cannot see). A turn still in progress
        with calls open has them ended ``cancelled`` first; those drafts are returned. While a turn the session
        announced is in progress it is refused (``AcpStateError``) and changes nothing."""
        with self._lock:
            turn = self._turn
            if turn is not None and turn.announced:
                raise acp_module().AcpStateError("a turn the session announced is in progress on these drafts")
            out = self._close(turn, True) if turn is not None and not turn.closed and not turn.resulted else []
            self._turn = self._new_turn(None)
            return out

    def abandoned(self, code: str, detail: str) -> list[dict]:
        """The turn in progress, whose ``prompt`` raised: its open calls end ``cancelled``, then its result draft
        with ``code`` (a route contract code). Nothing when its result is drafted already, no turn is in
        progress, or the turn in progress is one the session announced and will end itself."""
        _check_end(code, detail)
        with self._lock:
            turn = self._turn
            if turn is None or turn.resulted or turn.announced:
                return []
            out = self._close(turn, True)
            out.append(self._result(turn, code, detail))
            return out

    def ended(self, outcome, turn_id: str | None = None, delivery_id: str | None = None) -> list[dict]:
        if not isinstance(outcome, acp_module().TurnOutcome):
            raise TypeError("ended takes the TurnOutcome the session gave")
        if type(outcome.session) is not int or type(outcome.turn) is not int:
            raise TypeError("ended takes a TurnOutcome whose session and turn are ints")
        if (turn_id is None) != (delivery_id is None):
            raise TypeError("ended takes turn_id and delivery_id together, or neither")
        if turn_id is not None and not (type(turn_id) is str and TURN_ID.fullmatch(turn_id)
                                        and type(delivery_id) is str and DELIVERY_ID.fullmatch(delivery_id)):
            raise TypeError("ended takes a turn_id of the form turn-<id> and a delivery_id of the form dlv-<id>")
        with self._lock:
            if outcome.turn > self._begun.get(outcome.session, outcome.turn):
                raise TypeError("ended takes the outcome of a turn that began: its session announced no such turn")
            turn = self._turn_for((outcome.session, outcome.turn))
            if turn is None:
                return []
            turn.announced = False
            out = self._close(turn, _stopped(outcome))
            if outcome.stop_reason is not None and turn_id is not None:
                accepted = {"type": "input.accepted",
                            "payload": {"delivery_id": delivery_id, "turn_id": turn_id, "how": "new_turn"}}
                self.drafts.append(accepted)
                out.append(accepted)
            out.append(self._result(turn, outcome.code, outcome.detail))
            return out


def _check_key(key, name: str, *, none: bool) -> None:
    if key is None and none:
        return
    if not (type(key) is tuple and len(key) == 2 and all(type(k) is int for k in key)):
        raise TypeError(f"{name} takes the key (session, turn) the session gives" + (", or None" if none else ""))


def _check_end(code, detail) -> None:
    # the types first: an unhashable code is refused by name, never by a failed set lookup
    if type(code) is not str or type(detail) is not str or code not in c.CODES:
        raise TypeError("a turn's end takes a route contract code and a str detail")


def _stopped(outcome) -> bool:
    """Whether a turn was stopped rather than finished: its open calls then end ``cancelled``."""
    return outcome.cancel_requested or outcome.code in (c.DEADLINE_EXCEEDED, CANCELLED)


def _writable(text: str) -> str:
    """``text`` with any surrogate code point (JSON reads a pair as one character, so any left is lone) as
    U+FFFD, so a draft can always be written as UTF-8."""
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return "".join(chr(0xFFFD) if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text)
    return text


# -- masking across pieces ----------------------------------------------------------------------------------
# One JSON token of a line that json.loads reads: a string (with its quotes), a number or a literal. Outside
# strings a JSON text holds only these and structure, so a left-to-right scan finds every token.
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|-?Infinity|NaN|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?'
                    r"|true|false|null", re.S)
_SCALAR_SPELLS = "a value to keep out is still in an update once it is read"
_ACROSS_ROUNDS = 64


def _units(text: str) -> tuple[str, list]:
    """``text`` as UTF-16 code units (a character beyond the BMP as its two surrogates), and for each unit the
    index of its character in ``text``: so a pair split over two pieces (two lone surrogates) reads as the
    character it spells."""
    units, where = [], []
    for i, ch in enumerate(text):
        point = ord(ch)
        if point > 0xFFFF:
            point -= 0x10000
            units += [chr(0xD800 + (point >> 10)), chr(0xDC00 + (point & 0x3FF))]
            where += [i, i]
        else:
            units.append(ch)
            where.append(i)
    return "".join(units), where


def _chain(pieces: list[str], forms) -> list | None:
    """One value spelled ACROSS pieces, in order, any pieces between them skipped (a reader may join values
    only, keys only, or a list's texts): its parts as (piece, start, end) character ranges, or None. A part
    is the end of one piece, whole pieces, then the start of a later piece; a value inside one piece is
    ``mask``'s to find."""
    expanded = [_units(piece) for piece in pieces]
    for form in forms:
        whole = _units(form)[0]
        n = len(whole)
        if n < 2:
            continue
        states: dict = {}       # k -> the parts that spell whole[:k], each ending at the end of its piece
        for i, (units, where) in enumerate(expanded):
            size = len(units)
            new: dict = {}
            for k, parts in states.items():
                rest = whole[k:]
                if units.startswith(rest):
                    parts = parts + [(i, 0, len(rest))]
                    return [(j, expanded[j][1][a], expanded[j][1][b - 1] + 1) for j, a, b in parts]
                if 0 < size < len(rest) and rest.startswith(units):
                    new.setdefault(k + size, parts + [(i, 0, size)])
            for k in range(1, min(n, size + 1)):
                if units.endswith(whole[:k]):
                    new.setdefault(k, [(i, size - k, size)])
            for k, parts in new.items():
                states.setdefault(k, parts)
    return None


def _across(pieces: list[str], forms) -> list:
    """Every part of every value spelled across ``pieces`` (above), found round by round: each round's parts
    are taken out before the next round looks, until none is left. More than ``_ACROSS_ROUNDS`` rounds is the
    agent's protocol error (fail closed)."""
    work, found = list(pieces), []
    fill = _fill(forms)
    for _round in range(_ACROSS_ROUNDS):
        parts = _chain(work, forms)
        if parts is None:
            return found
        for i, a, b in parts:
            work[i] = work[i][:a] + fill * (b - a) + work[i][b:]
            found.append((i, a, b))
    raise acp_module().AcpProtocolError("a value to keep out is spelled across too many pieces to mask")


def _filled(text: str, ranges, fill: str) -> str:
    """``text`` with each character range replaced by ``fill`` of the same UTF-8 length."""
    for a, b in sorted(ranges, reverse=True):
        text = text[:a] + fill * _bytes(text[a:b]) + text[b:]
    return text


def _scalar_spells(value, forms) -> bool:
    """Whether a number, a bool or null spells a value (as JSON writes it, or as Python does) or equals one."""
    return any(form in text for form in forms for text in (json.dumps(value), repr(value))) or _number_is(value, forms)


def _masked_line(line: str, forms) -> tuple[str, str | None]:
    """A stdout line masked in place (same UTF-8 length), and why it is refused, or None. In a line json reads,
    each part of a value spelled across its tokens (strings read with their escapes, numbers and literals as
    written) is masked where it is written; then the whole line is masked as ``mask`` does. A number or
    literal that spells a value, or that holds a part of one, refuses it. A line json cannot read is masked
    as text: nothing in it is decoded by anyone."""
    if not forms:
        return line, None
    try:
        json.loads(line)
    except (ValueError, RecursionError):
        return mask(line, forms), None
    tokens = list(_TOKEN.finditer(line))
    pieces, starts = [], []
    for token in tokens:
        text = token.group()
        if text.startswith('"'):
            decoded, at = _decode(text[1:-1]) if "\\" in text else (text[1:-1], None)
            pieces.append(decoded)
            starts.append(at)
        else:
            if _scalar_spells(json.loads(text), forms):
                return mask(line, forms), "a value to keep out is spelled by a number or literal of a line"
            pieces.append(text)
            starts.append(None)
    raw_cuts: dict = {}
    for i, a, b in _across(pieces, forms):
        token = tokens[i]
        if not token.group().startswith('"'):
            return mask(line, forms), "a value to keep out is spelled across a number or literal of a line"
        at, inner = starts[i], len(token.group()) - 2
        a, b = (a, b) if at is None else (at[a], at[b] if b < len(at) else inner)
        raw_cuts.setdefault(i, []).append((token.start() + 1 + a, token.start() + 1 + b))
    cuts = [cut for i in sorted(raw_cuts) for cut in raw_cuts[i]]
    return mask(_filled(line, cuts, _fill(forms)), forms), None


def _masked_update(params, forms):
    """A decoded session/update's params masked for ``ToolCalls``: each part of a value spelled across its keys,
    strings and other scalars (in the order they are written, any between skipped) is masked, then each
    string as ``mask`` does. The update's ``toolCallId`` stays the agent's own (ToolCalls knows a call by it
    and masks the id it drafts). Refused (``AcpProtocolError``): a scalar that spells a value or holds a part
    of one, a tool call id that holds a part of one, and two keys of one object that mask alike."""
    if not forms:
        return params
    pieces: list = []
    _collect(params, pieces)
    texts = [text for text, _scalar in pieces]
    fill, ranges = _fill(forms), {}
    for i, a, b in _across(texts, forms):
        if pieces[i][1] is not _NOT_SCALAR:
            raise acp_module().AcpProtocolError(_SCALAR_SPELLS)
        ranges.setdefault(i, []).append((a, b))
    masked = []
    for i, (text, scalar) in enumerate(pieces):
        if scalar is not _NOT_SCALAR:
            if _scalar_spells(scalar, forms):
                raise acp_module().AcpProtocolError(_SCALAR_SPELLS)
            masked.append(text)
        else:
            masked.append(mask(_filled(text, ranges.get(i, ()), fill), forms))
    rebuilt = _rebuild(params, iter(masked))
    update = params.get("update")
    call_id = update.get("toolCallId") if isinstance(update, Mapping) else None
    new_update = rebuilt.get("update") if isinstance(rebuilt, dict) else None
    if type(call_id) is str and isinstance(new_update, dict) and "toolCallId" in new_update:
        if new_update["toolCallId"] != mask(call_id, forms):
            raise acp_module().AcpProtocolError("a value to keep out is spelled across a tool call id")
        new_update["toolCallId"] = call_id
    return rebuilt


_NOT_SCALAR = object()


def _collect(value, out: list) -> None:
    """Every key, string and other scalar of a decoded value, in the order it is written: (text, scalar)."""
    if isinstance(value, Mapping):
        for k, v in value.items():
            if type(k) is not str:
                raise TypeError("an update's keys are exactly str")
            out.append((k, _NOT_SCALAR))
            _collect(v, out)
    elif isinstance(value, list):
        for v in value:
            _collect(v, out)
    elif isinstance(value, str):
        if type(value) is not str:
            raise TypeError("mask takes exactly a str")
        out.append((value, _NOT_SCALAR))
    else:
        out.append((json.dumps(value), value))


def _rebuild(value, masked):
    if isinstance(value, Mapping):
        rebuilt = {}
        for _k, v in value.items():
            key = next(masked)
            if key in rebuilt:
                raise acp_module().AcpProtocolError("two keys of one object mask alike")
            rebuilt[key] = _rebuild(v, masked)
        return rebuilt
    if isinstance(value, list):
        return [_rebuild(v, masked) for v in value]
    text = next(masked)
    return text if isinstance(value, str) else value


def _number_is(value, forms) -> bool:
    """Whether a number equals a value written as a number (``1e10`` read back is ``10000000000.0``)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    for form in forms:
        try:
            if float(form) == value:
                return True
        except (ValueError, OverflowError):
            continue
    return False


# common.v1 $defs/turn_id and $defs/delivery_id.
TURN_ID = re.compile(r"turn-[A-Za-z0-9_-]{1,59}")
DELIVERY_ID = re.compile(r"dlv-[A-Za-z0-9_-]{1,60}")



# -- the ACP drive as a route adapter (one turn per run; inert) -------------------------------------------------
NO_CWD = "cwd"


class AntigravityAcpAdapter(AntigravityOneShotAdapter):
    """One turn of ``agy acp`` per run, through the shared client. NOTHING HERE IS REGISTERED OR LOADED.

    It is the one-shot adapter in every STRICT part, unchanged and shared, not copied: the request's shape and
    ``deadline_at`` checks, the prepared route home, the daemon-HOME refusal, the output boundary for a keyed
    route, custody's typed refusals, the updater switch and exactly the method's one key (``_prepare``).
    What differs is the argv (``agy acp``), the drive (initialize, authenticate with the route's one method,
    session/new in the request's ``cwd``, one prompt), and the request's ``cwd``.

    WHAT THE AGENT IS TOLD IS THE CALLER'S FOLDER. A run is refused ``request_invalid`` (``cwd``) unless the
    request names a ``cwd`` that is exactly a ``str`` (not a subclass), absolute and holding no NUL, and that
    folder, with no fallback, is the one the agent is told (``session/new``). Nothing here decides what a
    run's folder is: that is the workspace lane's (#392). The field is read from ``request["cwd"]`` until
    SPINE names it in the route contract (the Liaison's ruling, Nov 15).

    WHERE THE PROCESS STARTS IS NOT THIS ADAPTER'S. It hands the launch boundary ``argv``, ``env`` and
    ``shell`` and no working directory: the launcher contract (``launcher(argv, *, env, shell=False)``) has no
    field for one, and the daemon's launcher refuses any other option (``launch_option_denied``,
    ``daemon/launcher.py`` ``ProductionLauncher.__call__``). At this head no launcher for this engine can be
    built (``ENGINE_PROFILES`` has no ``antigravity``), and the daemon's launcher starts every unit in the
    engine's home under its state folder (``<state>/homes/<engine>``), not in the run's folder. Once #370
    lands, its S4 ``working_directory`` provider will choose the folder from the run's workspace (the engine's
    home when it answers None).
    THIS ADAPTER MUST NOT BE REGISTERED UNTIL THE LAUNCH BOUNDARY STARTS THE CHILD IN THE RUN'S FOLDER, and
    ``acp_factory`` refuses until then. The class itself is registrable (the engine table checks only that a
    factory is callable), and what the table builds from it has no prepared home: it refuses every run, before
    anything starts, with the precondition's own refusal (``launch_refused``, ``NO_RUN_FOLDER``).

    THE REQUEST IS READ ONCE. ``run`` copies it, its route and its connection into plain dicts (each key read
    once), and every check and every use after that reads the copy: one Mapping cannot answer the check one
    way and the use another. A str field it reads (``request_id``, ``tenant``, ``prompt``, ``cwd``,
    ``contract``, ``route.model``, ``route.auth_method``, ``route.connection`` ``kind``, ``id`` and
    ``credential_env``) must be exactly a ``str``.

    The turn's text and a failure's detail are masked (``mask``), never keep a lone surrogate, and are masked
    again once a lone surrogate is replaced (``_final_text``). A run whose input closing or text finishing
    ends at or after its deadline is ``deadline_exceeded``, its child fenced, never a success. Nothing is
    drafted: this adapter returns the turn's text only (``AcpDrafts`` is a driver's, for a drive that
    publishes events).

    Every failure path fences the child: a refusal or failure of the client (``AcpError``) and a turn that
    did not succeed are fenced with their code, and anything else that escapes is fenced by the one-shot
    adapter's ``run`` before it is raised. The turn's text is the agent's message chunks joined, masked
    once joined (``mask``), so a selected value split across chunks never reaches the result."""

    def run(self, request: dict, deadline_at: float) -> dict:
        if isinstance(request, Mapping):
            try:
                request = _request_once(request)
            except _RequestRefused as err:
                return c.result(err.request_id, err.code, detail=err.detail)
        refused = self._refused(request, deadline_at)   # the shared shape and deadline checks come first
        if refused is not None:
            return refused
        rid, cwd = request["request_id"], request.get("cwd")
        # _request_once has refused a str subclass: a str here is exactly one
        if not (isinstance(cwd, str) and cwd.startswith("/") and "\x00" not in cwd):
            return c.result(rid, c.REQUEST_INVALID, detail=NO_CWD)
        if self._prepared_home is None:     # as the engine table builds it: the precondition refuses
            return c.result(rid, c.LAUNCH_REFUSED, detail=NO_RUN_FOLDER)
        return self._launched(rid, request, deadline_at)

    def argv(self, request: dict) -> list[str]:
        route_method(request["route"])
        return acp_argv(self.executable)

    def drive(self, process: EngineProcess, request: dict, deadline_at: float, events: list[dict], *,
              redact=()) -> dict:
        rid = request["request_id"]
        forms = mask_forms(redact)
        acp = acp_module()
        try:
            session = open_acp(process, self._clock, request["route"], request["cwd"], deadline_at)
            outcome = session.prompt(request["prompt"], deadline_at)
        except acp.AcpError as err:
            code = err.code if err.code in c.CODES else c.ENGINE_ERROR
            process.fence(code)
            return c.result(rid, code, events=events, detail=_final_text(err.detail, forms))
        if outcome.code is None:
            session.close()
            text = _final_text(outcome.text, forms)
            if self._clock() >= deadline_at:     # closing the input or finishing the text ran past the deadline
                raise Deadline()
            return c.result(rid, None, text=text, events=events)
        code = c.ENGINE_ERROR if outcome.code == CANCELLED or outcome.code not in c.CODES else outcome.code
        process.fence(code)
        return c.result(rid, code, events=events, detail=_final_text(outcome.detail, forms))


def _final_text(text: str, forms) -> str:
    """A result's text as returned: masked, made writable (a lone surrogate as U+FFFD), then masked again as it
    now stands, since the replacement can spell a value the first mask did not see."""
    return mask(_writable(mask(text, forms)), forms)


_EXACT_STR = ("request_id", "tenant", "prompt", "cwd", "contract")
_EXACT_STR_ROUTE = ("model", "auth_method")
_EXACT_STR_CONNECTION = ("kind", "id", "credential_env")


class _RequestRefused(c.RouteError):
    """A refusal of ``_request_once``: ``request_invalid``, the field named, and the caller's ``request_id``
    when the copy holds one that is exactly a str and can be written as UTF-8 (else ``""``), so the caller can
    match the refusal."""

    def __init__(self, request_id: str, detail: str) -> None:
        super().__init__(c.REQUEST_INVALID, detail)
        self.request_id = request_id


def _request_once(request: Mapping) -> dict:
    """The request, its route and its connection copied into plain dicts, each key read once; a str field this
    adapter reads that is a str subclass is refused ``request_invalid``, named (``_RequestRefused``). The
    ``request_id`` is read first: one holding a surrogate code point cannot be written as UTF-8, so it is
    refused (``request_id``) and never echoed."""
    def copy(mapping, exact, prefix):
        out = dict(mapping.items())
        return out, [prefix + name for name in exact
                     if isinstance(out.get(name), str) and type(out.get(name)) is not str]

    rid, bad = "", []
    try:
        snapshot, bad = copy(request, _EXACT_STR, "")
        value = snapshot.get("request_id")
        if type(value) is str and any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
            raise _RequestRefused("", "request_id")
        if type(value) is str:
            rid = value
        route = snapshot.get("route")
        if not bad and isinstance(route, Mapping):
            route, bad = copy(route, _EXACT_STR_ROUTE, "route.")
            snapshot["route"] = route
            connection = route.get("connection")
            if not bad and isinstance(connection, Mapping):
                route["connection"], bad = copy(connection, _EXACT_STR_CONNECTION, "route.connection.")
    except _RequestRefused:
        raise
    except Exception:   # a Mapping that cannot be read as one
        raise _RequestRefused(rid, "request") from None
    if bad:
        raise _RequestRefused(rid, bad[0])
    return snapshot


NO_RUN_FOLDER = "antigravity_acp_launch_has_no_run_folder"


def acp_factory(launcher, clock, credentials, environ, **options) -> AntigravityAcpAdapter:
    """S3's ``factory(launcher, clock, credentials, environ, **options)``; ``options`` is at most ``binary``.

    IT REFUSES (``RouteError``, ``launch_refused``, ``NO_RUN_FOLDER``), so a composition that registers it
    fails: this adapter must not be registered until the launch boundary starts the child in the run's
    folder. Nothing given to a factory can say that yet (the launcher contract has no working directory, and
    the daemon's launcher refuses any option it does not know), so the refusal is unconditional. It goes in
    the change that gives the launcher contract a working directory and has ``run`` pass the run's folder."""
    unknown = set(options) - {"binary"}
    if unknown:
        raise TypeError(f"unknown options: {sorted(unknown)}")
    raise c.RouteError(c.LAUNCH_REFUSED, NO_RUN_FOLDER)
