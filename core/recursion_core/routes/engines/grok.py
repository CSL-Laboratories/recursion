"""Grok CLI driver, first slice: the parser of the one-shot drive ``grok --output-format streaming-json`` (lane ENGINE-E4).

What this module is. The parser that turns the one-shot drive's standard output into driver.v2 event
drafts (``schemas/control/v1/driver.v2.json``, ``$defs/event_draft``; payloads as in ``event.v1.json``),
the classification of how the run ended, the driver.v2 declaration of this drive, and the argument list
the one-shot driver will hand the launcher. It registers nothing: no engine-table factory, no hook, no
command. The long-lived ACP drive (``grok agent stdio``) waits for lane ENGINE-E5's shared client.

Where the wire facts come from (all read at xAI's public mirror ``xai-org/grok-build`` at
``2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8``, Sep 29, 2026; the shipped build 1.0.77 is newer and was
not run by this lane):

* ``crates/codegen/xai-grok-pager/src/headless/reducer/acp.rs``: the ``streaming-json`` line shapes.
  Every line is one JSON object tagged by ``type``: ``text`` and ``thought`` (``data``), ``tool_call``
  (``toolCallId``, ``title``, ``kind``, ``status``, ``toolName``, ``rawInput``, ``content``,
  ``locations``), ``tool_call_update`` (``toolCallId``, ``status``, ``content``, ``rawOutput``,
  ``locations``), ``usage`` (``messageId``, ``stopReason``, ``usage``, ``signature``, each optional),
  ``plan``, ``available_commands``, ``max_turns_reached``, ``error`` (``message`` plus spend fields),
  lifecycle lines (``auto_compact_*``, ``memory_*``, ``image_compressed``, ...), and the terminal
  ``end`` (``stopReason``, ``sessionId``, ``requestId`` plus spend fields).
* ``.../src/headless.rs:1397-1436`` and ``.../docs/user-guide/14-headless-mode.md:205-247, 559-566``:
  a turn that ends emits ``end`` and the process exits 0 for every stop reason but the max-turns one,
  so EXIT 0 IS NOT SUCCESS; a prompt error emits one ``error`` line and the process exits 1; 130 and
  143 are SIGINT and SIGTERM. After ``end`` the CLI may still run a memory flush, a model call of its
  own whose session updates go through the same emitter (``headless.rs:1441-1516``), and drain
  background work, so text, tool and usage lines can follow ``end``; a failed flush exits non-zero
  with no ``error`` line on standard output.
* ``crates/codegen/xai-grok-shell/src/extensions/notification.rs:247-274, 340-430``: the spend
  fields. ``usage.input_tokens`` is the UNCACHED prompt part on both the per-response ``usage`` line
  and the turn's totals; cache reads and cache writes are separate buckets. ``total_cost_usd`` and
  ``total_cost_usd_ticks`` (1 USD = 10**10 ticks) appear only when the cost is complete;
  ``cost_is_partial`` and ``usage_is_incomplete`` withhold it.
* Vault record ``20261109-csl815-grok-route-outcome-recording-01`` F2 and F5 (the same mirror): the
  one-shot drive flattens the ACP rate-limit error ``-32003`` into the text of an ``error`` line. So
  THIS DRIVE HAS NO TYPED LIMIT: ``typed_limits`` is false and this module drafts no ``limit`` event.

How a run ends (``OneShotParser.finish``), first match wins:

1. a line that is not blank and is not one JSON object with a string ``type`` (or is nested too deeply
   to read, or repeats a key in that object or in a nested object), a tool line whose ``toolCallId``
   is not a string or whose ``status``, when present and not null, is not a string, or a second
   ``end``: ``protocol_error``. The last line counts here only if it ends with its newline;
2. the process ended on a signal (a negative return code, 130 or 143): ``engine_exited``. A kill that
   lands inside a write leaves a last line with no newline; it is a cut line, not a protocol fault;
3. a last line with no newline that is not one typed JSON object, with no signal: ``protocol_error``;
4. an ``error`` line: ``short_fault`` when its text sounds like a quota (the shared rule,
   ``contract.sounds_like_quota``, decided by Lucas as grill Q23), else ``engine_error``. Never a plan
   limit: text never trips a connection;
5. an ``end`` line whose stop reason is ``end_turn``, with exit 0: success (code null);
6. an ``end`` line with any other stop reason, or after ``max_turns_reached``: ``engine_error``, the
   stop reason in the detail, cut to ``MAX_DETAIL_CHARS``; an ``end_turn`` with a non-zero exit is
   ``engine_error`` too;
7. no ``end`` and no ``error``: ``engine_error`` for exit 1, else ``engine_exited``.
Whatever the code, the detail ends with how many tool calls got no draft because their input has no
RFC 8785 form, when there were any, so such a run never looks complete.

Usage is never 0 because it was not known (event.v1, ``token_count``): a per-response ``usage`` line
or turn totals with no count above 0 are unknown, not free; counts that were sent are a lower bound
when the line's ``usage_is_incomplete`` is set (a call line's as a turn's); the cost is known only from ``total_cost_usd_ticks`` or a finite
``total_cost_usd`` when neither withholding flag is set (a flag is set when present and neither
``false`` nor ``null``, so a malformed value withholds). Two things print a ``usage`` line
(``headless/reducer/acp.rs:147-169``): one model response (``ResponseCompleted``, with an optional
``messageId``, ``stopReason`` and ``signature``) and the whole turn summed over every model call
(``TurnUsage``, ``extensions/notification.rs:1188-1190``, with none of them). A line that carries any of
the three is one call (span ``call``). A line with none of them may be either, so it gets no draft (it
is counted in ``ignored``; its raw line is in ``engine.output``): the totals on ``end`` or ``error``
cover the turn (span ``turn``), drafted once per run (a second line with totals is counted, not
drafted); a line that carries no totals (only the incomplete flag, or a ``usage`` object with no count
above 0 and no known cost) drafts the turn as unknown, once, without taking the place of a later line's
totals. Neither names the model, except the totals when
``modelUsage`` names exactly one, and not one whose name held a lone surrogate.

A ``tool.call`` carries ``input_digest``, the SHA-256 of the input as RFC 8785 writes it, taken with
the core's own writer (``control.methods.canonical_text``); the input itself is carried only when it
is an object whose RFC 8785 text is at most ``MAX_TOOL_INPUT_BYTES`` UTF-8 bytes. An input with no
RFC 8785 form (an integer beyond 2**53; a lone surrogate is cleaned first, below) gets no ``tool.call`` draft; its raw line is
still in ``engine.output``, the case is counted in ``OneShotParser.ignored`` and named in the
result's detail. A tool line whose ``toolCallId`` is not a string, or whose ``status`` is present,
not null and not a string, is a protocol fault; a string id that is empty, too long, holds a line
break or held a lone surrogate (cleaning it could make it another call's id) is counted and drafts
nothing. A ``tool.result`` text is the call's text content blocks; else a string ``rawOutput`` as it
stands; else the raw output as RFC 8785 writes it. Raw output with no RFC 8785 form gives an empty
text marked truncated (counted), never another spelling such as ``Infinity``. A completed line that
carries neither text blocks nor a raw output (an empty ``content`` and a null ``rawOutput``, the
shape of a field the emitter left out) keeps the text or raw output an earlier line of that call
carried. A completed line that carries either one uses that line.

What is public. ``OneShotParser`` (``parse``, ``finish``, ``outcome``), ``parse_run``,
``decode_line``, ``GrokProtocolError``, ``DECLARATION`` (read-only) and ``declaration()`` (a plain
copy), ``oneshot_argv`` and the constants above the first class. The parser's attributes (``text``, ``engine_session_id``, ``request_id``,
``stop_reason``, ``offset``, ``ignored``) describe one run in memory; they are not a format to resume
from, and ``start_offset`` restores only the byte count, not what the parser had seen.

Byte offsets. ``engine.output`` names a span of the output file by byte offsets. Pass each line
exactly as it stands in that file, with its newline; the parser counts UTF-8 bytes from
``start_offset`` (a byte kept by ``surrogateescape`` counts as the one byte it was).

Text no UTF-8 writer can write. A lone surrogate (from a JSON escape such as ``\\ud800``, or a byte that
is not UTF-8 kept by the reader) is written as U+FFFD before anything is read from the line, in the
``engine.output`` text and in every string of the decoded line; so a digest is of the cleaned input.
A name that must be exact is not cleaned but refused when it held a lone surrogate: a ``toolCallId``
(above), a ``sessionId`` or a ``requestId`` (not kept), a ``modelUsage`` name (no model is named).
A count or a tick total above 2**53 - 1 has no exact RFC 8785 form and is unknown.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from recursion_core.routes import contract as c

ENGINE = "grok"
# The one command name the registration will state (``default_executable``): argv[0] is the binary
# option when composition gave one, and this name otherwise, and nothing else.
DEFAULT_EXECUTABLE = "grok"
OUTPUT_FORMAT = "streaming-json"
# Asked of the child-environment profile (lane LAUNCH-CHECK); this module sets no environment.
UPDATER_OFF_ENV = (("GROK_DISABLE_AUTOUPDATER", "1"),)

# driver.v2's declaration of the one-shot drive. One prompt per process, nothing written to it after
# the start, no typed limit; usage is read from typed fields. Resume (``--resume <id>``) exists on this
# drive but is not claimed before a driver and a test show it. Read-only (a consumer cannot change it
# for everyone); ``declaration()`` gives a plain copy, for a schema check or a record.
DECLARATION = MappingProxyType({
    "engine": ENGINE,
    "driver": 2,
    "flags": MappingProxyType({
        "multi_turn": False,
        "mid_turn_input": False,
        "interrupt": False,
        "resume": False,
        "fork": False,
        "permission_bridge": False,
        "typed_limits": False,
        "usage": True,
    }),
    "input_ack": False,
})


def declaration() -> dict:
    """``DECLARATION`` as a new plain dict (driver.v2's root record)."""
    return {**DECLARATION, "flags": dict(DECLARATION["flags"])}

# Stop reasons the headless guide names for ``end`` (14-headless-mode.md:239-243).
STOP_END_TURN = "end_turn"
STOP_MAX_TURN_REQUESTS = "max_turn_requests"

_TERMINAL_TOOL_STATUS = {"completed": "ok", "failed": "error"}
_SIGNAL_EXITS = {130: 2, 143: 15}   # 128 + SIGINT, 128 + SIGTERM (headless guide, "Exit Codes")
_TICKS_PER_USD = 10 ** 10
# The largest integer RFC 8785 can write (2**53 - 1): a count or a tick total above it has no exact
# canonical form, so it is unknown, never a known value.
_MAX_EXACT_INT = 2 ** 53 - 1
# common.v1's connection_id: '<kind>:<id>', at most 256 characters, no CR and no LF.
_CONNECTION = re.compile(r"(subscription|api_key|passthrough):[^\r\n]+")
_ONE_LINE = re.compile(r"[^\r\n]+")
# A usage line that carries any of these is one model response (ResponseCompleted).
_RESPONSE_IDENTITY = ("messageId", "stopReason", "signature")

# Caps. An input whose RFC 8785 text is longer than this many UTF-8 bytes is not carried (its digest
# still is); a result text longer than this many characters is cut and marked truncated; an error
# detail is cut to this many characters.
MAX_TOOL_INPUT_BYTES = 64 * 1024
MAX_TOOL_RESULT_CHARS = 16 * 1024
MAX_DETAIL_CHARS = 2000


class GrokProtocolError(ValueError):
    """A line that is not one JSON object with a string ``type``."""


def oneshot_argv(*, prompt_file: str, model: str | None, binary: str | None = None) -> list[str]:
    """The one-shot argument list. argv[0] is ``binary`` when given, else ``DEFAULT_EXECUTABLE``.

    The prompt goes in a file the driver writes, never on the command line: an argument is readable by
    every user of the machine through ``/proc/<pid>/cmdline``, it is capped near 128 KiB, and a prompt
    beginning with ``-`` would be read as a flag. ``--prompt-file`` puts the CLI in headless mode as
    ``-p`` does (headless guide, "Basic Usage"). ``model`` None leaves the engine's default.

    Refusals, each a ``contract.RouteError`` raised before anything is launched: ``launch_refused`` for an
    executable that is empty or holds NUL, CR or LF; ``request_invalid`` for a prompt path or a model that is empty,
    holds NUL, CR or LF, or begins with ``-`` (it would be read as a flag).
    """

    executable = DEFAULT_EXECUTABLE if binary is None else binary
    if not isinstance(executable, str) or not _ONE_LINE.fullmatch(executable) or "\x00" in executable:
        raise c.RouteError(c.LAUNCH_REFUSED, "grok: no executable")
    if (not isinstance(prompt_file, str) or not _ONE_LINE.fullmatch(prompt_file) or "\x00" in prompt_file
            or prompt_file.startswith("-")):
        raise c.RouteError(c.REQUEST_INVALID, "grok: prompt file")
    argv = [executable, "--prompt-file", prompt_file, "--output-format", OUTPUT_FORMAT]
    if model is not None:
        if (not isinstance(model, str) or not _ONE_LINE.fullmatch(model) or "\x00" in model
                or model.startswith("-")):
            raise c.RouteError(c.REQUEST_INVALID, "route.model")
        argv += ["--model", model]
    return argv


def decode_line(line: str) -> dict:
    """One ``streaming-json`` line as a dict with a string ``type``, every lone surrogate written as
    U+FFFD. Raises ``GrokProtocolError``."""

    return _decode(line)[0]


# Fields that name something exactly (a tool call to pair with its result; the conversation to resume).
# Cleaning one would make two different names the same, so a name that held a lone surrogate is refused.
_IDENTITY_KEYS = ("toolCallId", "sessionId", "requestId")


def _decode(line: str) -> tuple[dict, bool, frozenset]:
    """``decode_line``, whether cleaning changed a string, and the names cleaning would make inexact: the
    identity fields it changed, and ``"modelUsage"`` when it changed a model name."""

    try:
        raw = json.loads(line, parse_constant=_refuse_constant,
                         object_pairs_hook=_object_without_duplicate_keys)
    except (ValueError, RecursionError) as err:
        if isinstance(err, ValueError) and str(err) == "duplicate key":
            raise GrokProtocolError("stdout line repeats a JSON key") from None
        raise GrokProtocolError("stdout line is not JSON") from None
    if not isinstance(raw, dict):
        raise GrokProtocolError("stdout line is not a JSON object")
    try:
        message = _clean_value(raw)
        cleaned = message != raw
    except RecursionError:
        raise GrokProtocolError("stdout line is nested too deeply") from None
    kind = message.get("type")
    if not isinstance(kind, str) or not kind:
        raise GrokProtocolError("stdout line has no type")
    altered = {key for key in _IDENTITY_KEYS
               if isinstance(raw.get(key), str) and _clean_text(raw[key]) is not raw[key]}
    model_usage = raw.get("modelUsage")
    if isinstance(model_usage, dict) and any(_clean_text(name) is not name for name in model_usage):
        altered.add("modelUsage")   # a model name that held a lone surrogate is no model name
    return message, cleaned, frozenset(altered)


def _object_without_duplicate_keys(pairs: list) -> dict:
    """One JSON object. A key that appears twice, at this object or in a nested one, is refused."""

    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj


def _refuse_constant(name: str) -> object:
    raise ValueError(name)   # NaN and Infinity are not JSON


def _clean_text(text: str) -> str:
    """``text`` with every lone surrogate written as U+FFFD. A lone surrogate comes from a JSON escape
    such as ``\\ud800`` or from a reader that kept a byte that is not UTF-8 (``surrogateescape``); no
    UTF-8 writer can write it, so no draft may carry one."""

    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return "".join("\ufffd" if 0xD800 <= ord(ch) <= 0xDFFF else ch for ch in text)


def _clean_value(value: object) -> object:
    """A decoded JSON value with ``_clean_text`` applied to every string, keys included."""

    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, list):
        return [_clean_value(item) for item in value]
    if isinstance(value, dict):
        return {_clean_text(key): _clean_value(item) for key, item in value.items()}
    return value


class OneShotParser:
    """Parse one run of the one-shot drive, line by line, into driver.v2 event drafts.

    ``parse(line)`` gives the drafts of one stdout line: always one ``engine.output`` (the line, with any
    lone surrogate written as U+FFFD, and its byte span in the file), then any ``tool.call``,
    ``tool.result`` and ``usage`` it carries. ``line`` must be a ``str`` (``TypeError`` otherwise; after
``finish``, any ``parse`` raises ``RuntimeError`` first). ``finish(code)``
    gives the run's ``result`` draft from what was seen and the process's return code. Not shared
    between threads: one parser per run, and ``finish`` is called by the thread that calls ``parse``,
    after the last line (a Stop or a watchdog on another thread ends the process, not the parser).
    """

    def __init__(self, connection_id: str, *, start_offset: int = 0) -> None:
        if (not isinstance(connection_id, str) or len(connection_id) > 256 or not _CONNECTION.fullmatch(connection_id)
                or _clean_text(connection_id) is not connection_id):
            raise ValueError("connection_id")
        if isinstance(start_offset, bool) or not isinstance(start_offset, int) or start_offset < 0:
            raise ValueError("start_offset")
        self.connection_id = connection_id
        self.offset = start_offset
        self.text_parts: list[str] = []          # the answer, chunk by chunk (model payload: never classified)
        self.engine_session_id: str | None = None
        self.request_id: str | None = None
        self.stop_reason: str | None = None      # end.stopReason, verbatim
        self.ignored: dict[str, int] = {}        # what was seen and drafted nothing, by reason
        self._ended = False
        self._max_turns = False
        self._error: str | None = None           # the first error line's message
        self._protocol: str | None = None        # the first protocol fault
        self._cut_line: str | None = None        # why a last line with no newline did not decode
        self._undrafted_calls = 0                # tool calls whose input has no RFC 8785 form
        self._altered: frozenset = frozenset()   # names in the current line that held a lone surrogate
        self._turn_spend_drafted = False         # the turn's totals are drafted once
        self._turn_unknown_drafted = False       # and an incomplete line with none, once, beside them
        self._open_calls: set[str] = set()       # tool calls drafted and not yet given a result
        self._done_calls: set[str] = set()
        self._streamed_output: dict[str, tuple] = {}  # call id -> (content, rawOutput) last carried
        self._finished = False

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    # -- one line -----------------------------------------------------------------------------------
    def parse(self, line: str) -> list[dict]:
        if self._finished:
            raise RuntimeError("parse after finish")
        if not isinstance(line, str):
            raise TypeError("line")
        if self._cut_line is not None:
            # A line with no newline was not the last after all: it is a fault like any other.
            self._fault(self._cut_line)
            self._cut_line = None
        start = self.offset
        self.offset += _byte_length(line)   # the file's own bytes, before any cleaning
        text = _clean_text(line)
        if text is not line:
            self._count("text_not_utf8")
        drafts = [_draft("engine.output", {"stream": "stdout", "text": text,
                                           "offset_start": start, "offset_end": self.offset})]
        if not line.strip():
            self._count("blank_line")
            return drafts
        try:
            message, cleaned, self._altered = _decode(line)
        except GrokProtocolError as err:
            if line.endswith("\n"):
                self._fault(str(err))
            else:
                self._cut_line = str(err)   # a write cut by a kill, or a fault: finish decides
            return drafts
        if cleaned and text is line:
            self._count("text_not_utf8")
        kind = message["type"]
        if self._ended and kind == "end":
            self._fault("a second end line")
            return drafts
        handler = _HANDLERS.get(kind)
        if handler is None:
            self._count(f"type:{kind[:40]}")   # the guide calls the list non-exhaustive
        else:
            handler(self, message, drafts)
        return drafts

    # -- the end of the run ------------------------------------------------------------------------
    def finish(self, returncode: int) -> list[dict]:
        """The run's ``result`` draft. ``returncode`` as ``subprocess`` gives it (negative: a signal)."""

        if self._finished:
            raise RuntimeError("finish twice")
        code, detail = self.outcome(returncode)
        self._finished = True
        return [_draft("result", {"code": code, "detail": detail})]

    def outcome(self, returncode: int) -> tuple[str | None, str]:
        """``(code, detail)`` for the run: the module docstring's table, first match wins. It may be
        asked before ``finish``; it changes nothing."""

        if isinstance(returncode, bool) or not isinstance(returncode, int):
            raise TypeError("returncode")
        code, detail = self._outcome(returncode)
        if self._undrafted_calls:
            detail += (f"; {self._undrafted_calls} tool call(s) not drafted: their input has no RFC 8785 form")
        return code, detail

    def _outcome(self, returncode: int) -> tuple[str | None, str]:
        if self._protocol is not None:
            return c.PROTOCOL_ERROR, self._protocol
        signal = -returncode if returncode < 0 else _SIGNAL_EXITS.get(returncode)
        if signal is not None:
            cut = "; its last line was cut" if self._cut_line is not None else ""
            return c.ENGINE_EXITED, f"ended by signal {signal} (exit {returncode}){cut}"
        if self._cut_line is not None:
            return c.PROTOCOL_ERROR, f"{self._cut_line} (the last line, with no newline)"
        if self._error is not None:
            code = c.SHORT_FAULT if c.sounds_like_quota(self._error) else c.ENGINE_ERROR
            return code, _cut(self._error, MAX_DETAIL_CHARS)
        if self._ended:
            if self._max_turns:
                return c.ENGINE_ERROR, _cut(
                    f"max turns reached (stop_reason {self.stop_reason})", MAX_DETAIL_CHARS)
            if self.stop_reason != STOP_END_TURN:
                return c.ENGINE_ERROR, _cut(f"stop_reason {self.stop_reason}", MAX_DETAIL_CHARS)
            if returncode != 0:
                return c.ENGINE_ERROR, f"exit {returncode} after end_turn"
            return None, f"stop_reason {STOP_END_TURN}"
        if returncode == 1:
            return c.ENGINE_ERROR, "exit 1 without an error line"
        return c.ENGINE_EXITED, f"exit {returncode} without an end line"

    # -- handlers -----------------------------------------------------------------------------------
    def _text(self, message: dict, drafts: list) -> None:
        data = message.get("data")
        if not isinstance(data, str):
            self._count("text_without_data")
        elif self._ended:
            self._count("text_after_end")   # a memory flush's own output, not the turn's answer
        else:
            self.text_parts.append(data)

    def _tool_call(self, message: dict, drafts: list) -> None:
        call_id = message.get("toolCallId")
        if not self._well_typed_tool_line(message) or not self._identity_is_exact():
            return
        if not _is_one_line(call_id, 128):
            self._count("tool_call_without_id")
            return
        name = message.get("toolName")
        if not _is_one_line(name, 256):
            name = message.get("title")
        if not _is_one_line(name, 256):
            self._count("tool_call_without_name")
            return
        if call_id in self._open_calls or call_id in self._done_calls:
            self._count("tool_call_repeated")
            return
        tool_input = message.get("rawInput")
        try:
            encoded = _canonical_text(tool_input).encode("utf-8")
        except (ValueError, RecursionError):
            self._count("tool_input_without_rfc8785_form")
            self._undrafted_calls += 1
            return
        carried = tool_input if isinstance(tool_input, dict) and len(encoded) <= MAX_TOOL_INPUT_BYTES else None
        drafts.append(_draft("tool.call", {
            "call_id": call_id,
            "name": name,
            "input_digest": hashlib.sha256(encoded).hexdigest(),
            "input": carried,
        }))
        self._open_calls.add(call_id)
        self._tool_result(message, drafts)   # a call can be reported already finished

    def _tool_update(self, message: dict, drafts: list) -> None:
        if self._well_typed_tool_line(message) and self._identity_is_exact():
            self._tool_result(message, drafts)

    def _identity_is_exact(self) -> bool:
        """A ``toolCallId`` that held a lone surrogate is refused (counted), not cleaned: cleaning could make
        it the id of another call, and a result would then be paired with the wrong call."""

        if "toolCallId" in self._altered:
            self._count("tool_id_not_utf8")
            return False
        return True

    def _well_typed_tool_line(self, message: dict) -> bool:
        """A tool line's ``toolCallId`` must be a string and its ``status``, when present and not null,
        a string: anything else is a malformed record, a protocol fault."""

        if not isinstance(message.get("toolCallId"), str):
            self._fault(f"{message['type']} line whose toolCallId is not a string")
            return False
        if message.get("status") is not None and not isinstance(message["status"], str):
            self._fault(f"{message['type']} line whose status is not a string")
            return False
        return True

    def _note_tool_output(self, message: dict) -> None:
        """Remember text or raw output a non-empty update carried, so a later line can keep it."""

        call_id = message.get("toolCallId")
        content, raw = message.get("content"), message.get("rawOutput")
        if isinstance(call_id, str) and _tool_output_present(content, raw):
            self._streamed_output[call_id] = (content, raw)

    def _output_for_result(self, message: dict) -> tuple:
        """The completion's own output, or the earlier output when the completion omits both."""

        content, raw = message.get("content"), message.get("rawOutput")
        if _tool_output_present(content, raw):
            return content, raw
        call_id = message.get("toolCallId")
        if isinstance(call_id, str) and call_id in self._streamed_output:
            return self._streamed_output[call_id]
        return content, raw

    def _tool_result(self, message: dict, drafts: list) -> None:
        self._note_tool_output(message)
        status = _TERMINAL_TOOL_STATUS.get(message.get("status"))
        if status is None:
            return   # pending, in progress, or a token this module does not know: no result yet
        call_id = message.get("toolCallId")
        if call_id not in self._open_calls:
            self._count("tool_result_without_call")
            return
        content, raw = self._output_for_result(message)
        text, truncated, written = _result_text(content, raw)
        if not written:
            self._count("tool_output_without_rfc8785_form")
        drafts.append(_draft("tool.result", {
            "call_id": call_id, "status": status, "text": text, "output_ref": None, "truncated": truncated,
        }))
        self._open_calls.discard(call_id)
        self._done_calls.add(call_id)
        self._streamed_output.pop(call_id, None)

    def _usage_line(self, message: dict, drafts: list) -> None:
        usage = message.get("usage")
        if not isinstance(usage, Mapping):
            self._count("usage_line_without_usage")
            return
        if not any(isinstance(message.get(key), str) and message[key] for key in _RESPONSE_IDENTITY):
            # The turn's sum (TurnUsage) prints the same line with none of these; so may a response on a
            # backend that reports none of them. Not one call, as far as can be told: no draft.
            self._count("usage_line_without_response_identity")
            return
        counts = [_count_of(usage.get(key)) for key in _RESPONSE_KEYS]
        if _nothing_reported(counts):
            known = None   # no bucket above 0: the backend reported nothing, which is not "free"
        else:
            known = counts
        drafts.append(_usage_draft(self.connection_id, None, "call", known,
                                   lower_bound=_flag_set(message, "usage_is_incomplete"), cost=None))

    def _max_turns_line(self, message: dict, drafts: list) -> None:
        self._max_turns = True

    def _end(self, message: dict, drafts: list) -> None:
        self._ended = True
        stop = message.get("stopReason")
        self.stop_reason = stop if isinstance(stop, str) else None
        # Both ids obey one rule: one line of at most 256 characters, never cleaned; anything else that is
        # present is counted and not kept.
        session = message.get("sessionId")
        if "sessionId" in self._altered:
            self._count("session_id_not_utf8")   # a cleaned id would resume another conversation, or none
        elif _is_one_line(session, 256):
            self.engine_session_id = session
        elif session is not None:
            self._count("session_id_unusable")
        request = message.get("requestId")
        if "requestId" in self._altered:
            self._count("request_id_not_utf8")   # a cleaned id could match another request's
        elif _is_one_line(request, 256):
            self.request_id = request
        elif request is not None:
            self._count("request_id_unusable")
        self._spend(message, drafts)

    def _error_line(self, message: dict, drafts: list) -> None:
        text = message.get("message")
        text = text if isinstance(text, str) and text else "error line without a message"
        if self._error is None:
            self._error = text
        else:
            self._count("second_error_line")
        self._spend(message, drafts)

    def _spend(self, message: dict, drafts: list) -> None:
        """The turn's totals on ``end`` or ``error``, when the engine sent any, drafted once: a second
        line that carries totals (a second ``error``, or ``end`` after ``error``) is counted, so a
        consumer that sums span ``turn`` never counts the turn twice. A line that carries no totals (no
        ``usage`` object but the incomplete flag, or a ``usage`` object with no count above 0 and no
        known cost) drafts the turn as unknown, once, and does not take the place of totals a later line
        carries: the consumer then sees one unknown and at most one number."""

        usage = message.get("usage")
        incomplete = _flag_set(message, "usage_is_incomplete")
        if not isinstance(usage, Mapping) and not incomplete:
            return
        counts = None
        cost = None
        if isinstance(usage, Mapping):
            counts = [_count_of(usage.get(key)) for key in _RESPONSE_KEYS]
            if _nothing_reported(counts):
                counts = None   # no bucket above 0: nothing was reported, which is not "free" (as for a call)
            if not (incomplete or _flag_set(message, "cost_is_partial")):
                cost = _cost(message)
        if counts is None and cost is None:
            # No totals: say unknown, never 0, once, and leave the place for a line that has them.
            if self._turn_spend_drafted or self._turn_unknown_drafted:
                self._count("turn_usage_already_drafted")
                return
            self._turn_unknown_drafted = True
            drafts.append(_usage_draft(self.connection_id, None, "turn", None, lower_bound=False, cost=None))
            return
        if self._turn_spend_drafted:
            self._count("turn_usage_already_drafted")
            return
        self._turn_spend_drafted = True
        model = None if "modelUsage" in self._altered else _single_model(message.get("modelUsage"))
        drafts.append(_usage_draft(self.connection_id, model, "turn", counts, lower_bound=incomplete, cost=cost))

    # -- bookkeeping --------------------------------------------------------------------------------
    def _fault(self, reason: str) -> None:
        if self._protocol is None:
            self._protocol = reason
        self._count("protocol_fault")

    def _count(self, reason: str) -> None:
        self.ignored[reason] = self.ignored.get(reason, 0) + 1


_HANDLERS = {
    "text": OneShotParser._text,
    "tool_call": OneShotParser._tool_call,
    "tool_call_update": OneShotParser._tool_update,
    "usage": OneShotParser._usage_line,
    "max_turns_reached": OneShotParser._max_turns_line,
    "end": OneShotParser._end,
    "error": OneShotParser._error_line,
}

# The usage keys, in the order of event.v1's five token fields.
_RESPONSE_KEYS = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens")
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens")


def parse_run(connection_id: str, lines: Sequence[str], returncode: int) -> tuple[list[dict], OneShotParser]:
    """Every draft of one finished run, in order, and the parser that made them. ``lines`` is the lines,
    in order, not the file: a ``str``, ``bytes``, a mapping or a set is refused (``TypeError``) rather than
    read a character, a key or an unordered member at a time."""

    if isinstance(lines, (str, bytes, bytearray, Mapping, set, frozenset)):
        raise TypeError("lines: pass the output's lines in order, not one string, a mapping or a set")
    parser = OneShotParser(connection_id)
    drafts: list[dict] = []
    for line in lines:
        drafts += parser.parse(line)
    drafts += parser.finish(returncode)
    return drafts, parser


# -- helpers -----------------------------------------------------------------------------------------
def _draft(kind: str, payload: dict) -> dict:
    return {"type": kind, "payload": payload}


def _byte_length(line: str) -> int:
    """The line's length in the output file. A reader that decoded with ``surrogateescape`` gets the
    file's own bytes back; any other lone surrogate is counted as ``surrogatepass`` writes it."""

    try:
        return len(line.encode("utf-8", "surrogateescape"))
    except UnicodeEncodeError:
        return len(line.encode("utf-8", "surrogatepass"))


def _is_one_line(value: object, limit: int) -> bool:
    return isinstance(value, str) and len(value) <= limit and bool(_ONE_LINE.fullmatch(value))


def _canonical_text(value: object) -> str:
    """``value`` as RFC 8785 writes it, by the core's own writer. ``ValueError`` when it has none."""

    from recursion_core.control.methods import canonical_text   # local: routes does not load control at import

    return canonical_text(value)


def _flag_set(message: dict, name: str) -> bool:
    """A withholding flag (``usage_is_incomplete``, ``cost_is_partial``) is set when it is present and is
    neither ``false`` nor ``null``: a malformed value such as ``1`` or ``"true"`` withholds, so the
    number is never claimed as more than the engine said."""
    value = message.get(name)
    return value is not None and value is not False


def _nothing_reported(counts: list) -> bool:
    """No bucket above 0 (each is 0 or not a count): the engine reported nothing, which is not "free"."""
    return all(value is None or value == 0 for value in counts)


def _count_of(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > _MAX_EXACT_INT:
        return None
    return value


def _measure(value: int | float | None, *, lower_bound: bool) -> dict:
    if value is None:
        return {"value": None, "quality": "unknown", "provenance": "unavailable"}
    return {"value": value, "quality": "lower_bound" if lower_bound else "known", "provenance": "observed"}


def _usage_draft(connection_id: str, model: str | None, span: str, counts: list | None, *,
                 lower_bound: bool, cost: float | None) -> dict:
    values = counts if counts is not None else [None] * len(_TOKEN_FIELDS)
    payload = {"connection_id": connection_id, "model": model, "bucket": None, "span": span}
    for name, value in zip(_TOKEN_FIELDS, values):
        payload[name] = _measure(value, lower_bound=lower_bound)
    payload["cost_usd"] = _measure(cost, lower_bound=False)
    return _draft("usage", payload)


def _cost(message: dict) -> float | None:
    ticks = message.get("total_cost_usd_ticks")
    if not isinstance(ticks, bool) and isinstance(ticks, int) and ticks >= 0 and ticks <= _MAX_EXACT_INT:
        return ticks / _TICKS_PER_USD
    dollars = message.get("total_cost_usd")
    if isinstance(dollars, bool):
        return None
    if isinstance(dollars, int):   # an integer of any size: bounded before it becomes a float
        return float(dollars) if 0 <= dollars <= _MAX_EXACT_INT else None
    if isinstance(dollars, float) and math.isfinite(dollars) and dollars >= 0:
        return dollars
    return None


def _single_model(model_usage: object) -> str | None:
    if isinstance(model_usage, Mapping) and len(model_usage) == 1:
        (name,) = model_usage
        if isinstance(name, str) and name and name != "unknown" and _is_one_line(name, 256):
            return name
    return None


def _tool_output_present(content: object, raw_output: object) -> bool:
    """True when a tool line carries text blocks or a raw output. Empty content and null raw output
    is how the emitter writes a field it left out, so that line does not replace an earlier one."""

    if raw_output is not None:
        return True
    return isinstance(content, list) and len(content) > 0


def _result_text(content: object, raw_output: object) -> tuple[str, bool, bool]:
    """``(text, truncated, written)`` for a tool result: its text content blocks; else a string raw
    output as it stands; else the raw output as RFC 8785 writes it. Raw output with no RFC 8785 form
    gives ``("", True, False)``: an empty text that says it is shorter than the output, never a
    spelling RFC 8785 would refuse."""

    parts = []
    if isinstance(content, list):
        for block in content:
            inner = block.get("content") if isinstance(block, Mapping) else None
            if isinstance(inner, Mapping) and inner.get("type") == "text" and isinstance(inner.get("text"), str):
                parts.append(inner["text"])
    if parts:
        text = "\n".join(parts)
    elif isinstance(raw_output, str):
        text = raw_output
    elif raw_output is None:
        text = ""
    else:
        try:
            text = _canonical_text(raw_output)
        except (ValueError, RecursionError):
            return "", True, False
    if len(text) > MAX_TOOL_RESULT_CHARS:
        return text[:MAX_TOOL_RESULT_CHARS], True, True
    return text, False, True


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]
