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

1. a line that is not one JSON object with a string ``type``, or a second ``end``: ``protocol_error``;
2. the process ended on a signal (a negative return code, 130 or 143): ``engine_exited``;
3. an ``error`` line: ``short_fault`` when its text sounds like a quota (the shared rule,
   ``contract.sounds_like_quota``, decided by Lucas as grill Q23), else ``engine_error``. Never a plan
   limit: text never trips a connection;
4. an ``end`` line whose stop reason is ``end_turn``, with exit 0: success (code null);
5. an ``end`` line with any other stop reason, or after ``max_turns_reached``: ``engine_error``, the
   stop reason in the detail; an ``end_turn`` with a non-zero exit is ``engine_error`` too;
6. no ``end`` and no ``error``: ``engine_error`` for exit 1, else ``engine_exited``.

Usage is never 0 because it was not known (event.v1, ``token_count``): a per-response ``usage`` line
whose five counts are all 0 is unknown, not free; the turn's counts are a lower bound when
``usage_is_incomplete`` is set; the cost is known only from ``total_cost_usd_ticks`` or
``total_cost_usd`` when neither withholding flag is set. A per-response line covers one model call
(span ``call``); the totals on ``end`` or ``error`` cover the turn (span ``turn``). Neither names the
model, except the totals when ``modelUsage`` names exactly one.

A ``tool.call`` carries ``input_digest``, the SHA-256 of the input as RFC 8785 writes it, taken with
the core's own writer (``control.methods.canonical_text``). An input with no RFC 8785 form (an
integer beyond 2**53, a lone surrogate) gets no ``tool.call`` draft; its raw line is still in
``engine.output`` and the case is counted in ``OneShotParser.ignored``.

Byte offsets. ``engine.output`` names a span of the output file by byte offsets. Pass each line
exactly as it stands in that file, with its newline; the parser counts UTF-8 bytes from
``start_offset``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence

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
# drive but is not claimed before a driver and a test show it.
DECLARATION = {
    "engine": ENGINE,
    "driver": 2,
    "flags": {
        "multi_turn": False,
        "mid_turn_input": False,
        "interrupt": False,
        "resume": False,
        "fork": False,
        "permission_bridge": False,
        "typed_limits": False,
        "usage": True,
    },
    "input_ack": False,
}

# Stop reasons the headless guide names for ``end`` (14-headless-mode.md:239-243).
STOP_END_TURN = "end_turn"
STOP_MAX_TURN_REQUESTS = "max_turn_requests"

_TERMINAL_TOOL_STATUS = {"completed": "ok", "failed": "error"}
_SIGNAL_EXITS = {130: 2, 143: 15}   # 128 + SIGINT, 128 + SIGTERM (headless guide, "Exit Codes")
_TICKS_PER_USD = 10 ** 10
_CONNECTION = re.compile(r"(subscription|api_key|passthrough):.+")
_ONE_LINE = re.compile(r"[^\r\n]+")

# Caps. An input larger than this is not carried (its digest still is); a result text longer than this
# is cut and marked truncated; an error detail is cut.
MAX_TOOL_INPUT_CHARS = 64 * 1024
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
    """

    executable = DEFAULT_EXECUTABLE if binary is None else binary
    if not isinstance(executable, str) or not executable or "\x00" in executable:
        raise c.RouteError(c.LAUNCH_REFUSED, "grok: no executable")
    if not isinstance(prompt_file, str) or not prompt_file or "\x00" in prompt_file:
        raise c.RouteError(c.REQUEST_INVALID, "grok: prompt file")
    argv = [executable, "--prompt-file", prompt_file, "--output-format", OUTPUT_FORMAT]
    if model is not None:
        if not isinstance(model, str) or not model or "\x00" in model or model.startswith("-"):
            raise c.RouteError(c.REQUEST_INVALID, "route.model")
        argv += ["--model", model]
    return argv


def decode_line(line: str) -> dict:
    """One ``streaming-json`` line as a dict with a string ``type``. Raises ``GrokProtocolError``."""

    try:
        message = json.loads(line, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        raise GrokProtocolError("stdout line is not JSON") from None
    if not isinstance(message, dict):
        raise GrokProtocolError("stdout line is not a JSON object")
    kind = message.get("type")
    if not isinstance(kind, str) or not kind:
        raise GrokProtocolError("stdout line has no type")
    return message


def _refuse_constant(name: str) -> object:
    raise ValueError(name)   # NaN and Infinity are not JSON


class OneShotParser:
    """Parse one run of the one-shot drive, line by line, into driver.v2 event drafts.

    ``parse(line)`` gives the drafts of one stdout line: always one ``engine.output`` (the raw line and
    its byte span), then any ``tool.call``, ``tool.result`` and ``usage`` it carries. ``finish(code)``
    gives the run's ``result`` draft from what was seen and the process's return code. Not shared
    between threads; one parser per run.
    """

    def __init__(self, connection_id: str, *, start_offset: int = 0) -> None:
        if not isinstance(connection_id, str) or len(connection_id) > 256 or not _CONNECTION.fullmatch(connection_id):
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
        self._open_calls: set[str] = set()       # tool calls drafted and not yet given a result
        self._done_calls: set[str] = set()
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
        start = self.offset
        self.offset += _byte_length(line)
        drafts = [_draft("engine.output", {"stream": "stdout", "text": line,
                                           "offset_start": start, "offset_end": self.offset})]
        if not line.strip():
            self._count("blank_line")
            return drafts
        try:
            message = decode_line(line)
        except GrokProtocolError as err:
            self._fault(str(err))
            return drafts
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
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            raise TypeError("returncode")
        self._finished = True
        code, detail = self.outcome(returncode)
        return [_draft("result", {"code": code, "detail": detail})]

    def outcome(self, returncode: int) -> tuple[str | None, str]:
        """``(code, detail)`` for the run: the module docstring's table, first match wins."""

        if self._protocol is not None:
            return c.PROTOCOL_ERROR, self._protocol
        signal = -returncode if returncode < 0 else _SIGNAL_EXITS.get(returncode)
        if signal is not None:
            return c.ENGINE_EXITED, f"ended by signal {signal} (exit {returncode})"
        if self._error is not None:
            code = c.SHORT_FAULT if c.sounds_like_quota(self._error) else c.ENGINE_ERROR
            return code, _cut(self._error, MAX_DETAIL_CHARS)
        if self._ended:
            if self._max_turns:
                return c.ENGINE_ERROR, f"max turns reached (stop_reason {self.stop_reason})"
            if self.stop_reason != STOP_END_TURN:
                return c.ENGINE_ERROR, f"stop_reason {self.stop_reason}"
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
            text = _canonical_text(tool_input)
        except ValueError:
            self._count("tool_input_without_rfc8785_form")
            return
        carried = tool_input if isinstance(tool_input, dict) and len(text) <= MAX_TOOL_INPUT_CHARS else None
        drafts.append(_draft("tool.call", {
            "call_id": call_id,
            "name": name,
            "input_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "input": carried,
        }))
        self._open_calls.add(call_id)
        self._tool_result(message, drafts)   # a call can be reported already finished

    def _tool_result(self, message: dict, drafts: list) -> None:
        status = _TERMINAL_TOOL_STATUS.get(message.get("status"))
        if status is None:
            return   # pending, in progress, or a token this module does not know: no result yet
        call_id = message.get("toolCallId")
        if call_id not in self._open_calls:
            self._count("tool_result_without_call")
            return
        text, truncated = _result_text(message.get("content"), message.get("rawOutput"))
        drafts.append(_draft("tool.result", {
            "call_id": call_id, "status": status, "text": text, "output_ref": None, "truncated": truncated,
        }))
        self._open_calls.discard(call_id)
        self._done_calls.add(call_id)

    def _usage_line(self, message: dict, drafts: list) -> None:
        usage = message.get("usage")
        if not isinstance(usage, Mapping):
            self._count("usage_line_without_usage")
            return
        counts = [_count_of(usage.get(key)) for key in _RESPONSE_KEYS]
        if all(value == 0 for value in counts):
            known = None   # every bucket 0: the backend reported nothing, which is not "free"
        else:
            known = counts
        drafts.append(_usage_draft(self.connection_id, None, "call", known, lower_bound=False, cost=None))

    def _max_turns_line(self, message: dict, drafts: list) -> None:
        self._max_turns = True

    def _end(self, message: dict, drafts: list) -> None:
        self._ended = True
        stop = message.get("stopReason")
        self.stop_reason = stop if isinstance(stop, str) else None
        session = message.get("sessionId")
        if _is_one_line(session, 256):
            self.engine_session_id = session
        request = message.get("requestId")
        if isinstance(request, str) and request:
            self.request_id = request
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
        """The turn's totals on ``end`` or ``error``, when the engine sent any."""

        usage = message.get("usage")
        incomplete = message.get("usage_is_incomplete") is True
        if not isinstance(usage, Mapping):
            if incomplete:   # incomplete and no tokens recorded: say unknown, never 0
                drafts.append(_usage_draft(self.connection_id, None, "turn", None, lower_bound=False, cost=None))
            return
        counts = [_count_of(usage.get(key)) for key in _RESPONSE_KEYS]
        withheld = incomplete or message.get("cost_is_partial") is True
        drafts.append(_usage_draft(self.connection_id, _single_model(message.get("modelUsage")), "turn",
                                   counts, lower_bound=incomplete, cost=None if withheld else _cost(message)))

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
    "tool_call_update": OneShotParser._tool_result,
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
    """Every draft of one finished run, in order, and the parser that made them."""

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


def _count_of(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
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
    if not isinstance(ticks, bool) and isinstance(ticks, int) and ticks >= 0:
        return ticks / _TICKS_PER_USD
    dollars = message.get("total_cost_usd")
    if not isinstance(dollars, bool) and isinstance(dollars, (int, float)) and math.isfinite(dollars) and dollars >= 0:
        return float(dollars)
    return None


def _single_model(model_usage: object) -> str | None:
    if isinstance(model_usage, Mapping) and len(model_usage) == 1:
        (name,) = model_usage
        if isinstance(name, str) and name and name != "unknown" and _is_one_line(name, 256):
            return name
    return None


def _result_text(content: object, raw_output: object) -> tuple[str, bool]:
    """A tool result as text: its text content blocks, else its raw output written as RFC 8785 JSON."""

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
        except ValueError:
            text = json.dumps(raw_output, sort_keys=True, ensure_ascii=False, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        return text[:MAX_TOOL_RESULT_CHARS], True
    return text, False


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]
