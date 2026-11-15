# Written by Claude Opus 5.5 (claude-opus-5-5), builder lane ENGINE-E8.
"""Pi (engine E8): the reader of what ``pi`` writes, and the RPC lines the driver writes to it.

Lane ENGINE-E8, first slice: fixtures and parser. Nothing here starts a process, is registered in the
engine table, or is loaded by a daemon. The one-shot driver and the RPC worker are later slices; this
module is what both will read Pi's output with.

SOURCES, read and not run (no Pi binary was installed or started): Pi's own protocol pages and code at
earendil-works/pi@bb65e3e49710fbd14f10c7792d0eaa6e2b2c5923, the commit the vault's verified code map
(20261028-recursion-frontier-map-pi-02) cites: ``packages/coding-agent/docs/json.md``, ``rpc.md``,
``rpc-commands.md``, ``message-types.md``; ``src/modes/json-event.ts`` and ``src/modes/print-mode.ts``.
The vault records 20261028-recursion-frontier-b3-stdio-rpc-02 (section 2.2) and
20261107-csl712-engine-clis-grounding-01 (V48 to V52) agree with them where they overlap. The package
the user installs is ``@earendil-works/pi-coding-agent`` (V48); its pin is measured from his install, not
here. Every fixture in ``fixtures/pi/`` is SYNTHETIC: written by hand from those pages.

WHAT PI WRITES

* ``pi -p --mode json ... <prompt>`` (one shot): the session header first when the session has one
  (``{"type":"session","version":3,"id",...}``, print-mode.ts:122-127), then one session event per
  line, then it exits. Its exit code is 0 even when the model call failed; the outcome is in the stream.
* ``pi --mode rpc`` (long-lived): commands on stdin, each with an optional ``id``; on stdout a
  ``{"type":"response","id","command","success","data"|"error"}`` for each command and the same session
  events as JSON mode. No header: the session id is read with ``get_state``.
* Framing (json.md, rpc.md): one JSON object per LF; an optional CR before the LF is stripped; U+2028
  and U+2029 are characters inside strings, never record boundaries (so never ``str.splitlines``).

WHAT THE PARSER DRAFTS (driver.v2 ``$defs/event_draft``; every payload has exactly event.v1's keys)

* ``usage``: one per completed assistant message (``message_end``), span ``call``. Pi gives every
  assistant message a usage object; when every count in it is 0 the provider reported nothing, and
  every measurement is ``unknown`` (never 0). ``cost_usd`` is Pi's own figure from its price table, so
  its provenance is ``modelled``. ``reasoning`` is optional in Pi and is already inside ``output``.
* ``tool.call``: one per ``toolCall`` block of a completed assistant message (the engine asking), and
  one for a ``tool_execution_start`` whose id no block named. ``input_digest`` is SHA-256 over the
  input as RFC 8785 writes it (``rfc8785/v1``); an input outside that form's strict profile is a
  protocol error, never a guessed digest. An input whose text is larger than ``INPUT_CARRY_MAX`` is
  withheld (``input`` null) and still digested.
* ``tool.result``: one per ``tool_execution_end``: ``ok`` or ``error`` (Pi has no approval of its own
  and its result does not say "cancelled", so ``denied`` and ``cancelled`` are never drafted).
* ``result``: JSON mode only, at ``agent_settled``, from the last completed assistant message.
* ``input.accepted``: RPC mode, only from Pi's ``response`` to a ``prompt`` this stream wrote, matched
  by the token this stream minted (``started`` is ``new_turn``, ``queued`` is ``mid_turn``); JSON mode,
  only from Pi's replay of the argv prompt this stream registered, with the same text.

It drafts no ``engine.output``: that is a span of the run's output file with byte offsets, which a line
parser is not given; lane EVENTS' tailer writes each line the parser drafts nothing for (a streamed
``message_update``, for one) as one untrusted ``engine.output`` with its offsets (EVENTS brief, step 6:
"each line through the driver's parse(line), else one engine.output event"). It drafts no ``limit``:
no typed limit signal is known for Pi (no oracle). Free
error text that sounds like a quota makes the one-shot ``result`` a ``short_fault`` (grill Q23), never
a ``plan_limit``. Facts that are not events (the session id, a refused prompt, a dialog Pi waits on, a
settled run) are kept as ``signals`` for the driver.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass

from recursion_core.routes.contract import ENGINE_ERROR, SHORT_FAULT, sounds_like_quota

ENGINE = "pi"
# The one command name this engine's registration will state (``default_executable``): the adapter hands
# the launcher, as argv[0], the binary option composition gave it, and otherwise this, and nothing else.
DEFAULT_EXECUTABLE = "pi"
MODES = ("json", "rpc")

MAX_INT = 2**53 - 1          # the largest whole number every binary64 reader keeps exact (rfc8785/v1 profile)
MAX_DEPTH = 64               # of a tool's input
INPUT_CARRY_MAX = 262144     # UTF-8 bytes of a tool input's text carried in a tool.call; larger is withheld
TOOL_TEXT_MAX = 65536        # characters of a tool result's text carried in a tool.result; more is cut
DETAIL_MAX = 500             # characters of engine error text carried in a result's detail
DIALOGS = ("select", "confirm", "input", "editor")   # extension UI requests Pi waits on (rpc-types.ts)

_CONNECTION = re.compile(r"(subscription|api_key|passthrough):[^\r\n]{1,240}")
_TURN = re.compile(r"turn-[A-Za-z0-9_-]{1,59}")
_DELIVERY = re.compile(r"dlv-[A-Za-z0-9_-]{1,60}")
_STOP_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")   # a stopReason quoted in a result's detail
_UNKNOWN_COUNT = {"value": None, "quality": "unknown", "provenance": "unavailable"}


class PiProtocolError(ValueError):
    """What Pi wrote is not its protocol. ``code`` says which rule it broke. The run is fenced."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


# -- framing and strict reading ----------------------------------------------------------------------
def records_of(data: bytes) -> list[str]:
    """Split Pi's stdout into records: on LF only, an optional CR before it stripped, UTF-8 strictly.

    A final fragment with no LF is a record, as Pi's own reader treats it (rpc jsonl.ts). Empty records
    are kept, so that ``read_record`` refuses them.
    """
    if not isinstance(data, bytes):
        raise TypeError("records_of takes bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise PiProtocolError("not_utf8") from None
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return [part[:-1] if part.endswith("\r") else part for part in parts]


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise PiProtocolError("duplicate_key", key[:64])
        out[key] = value
    return out


def _no_constant(token):
    raise PiProtocolError("not_a_number", token)


def read_record(line) -> dict:
    """One record: a JSON object with a string ``type``, read strictly, or ``PiProtocolError``.

    ``line`` is text or bytes, with or without its LF. Refused: invalid UTF-8, an LF inside, text that
    is not JSON, ``NaN`` or an infinity, a key named twice at any depth, anything but an object, and an
    object without a non-empty string ``type``. Pi's JSON.stringify writes none of these.
    """
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            raise PiProtocolError("not_utf8") from None
    if not isinstance(line, str):
        raise TypeError("read_record takes text or bytes")
    if line.endswith("\n"):
        line = line[:-1]       # a CR before it is JSON whitespace, which json.loads passes over
    if "\n" in line:
        raise PiProtocolError("lf_inside_record")
    try:
        record = json.loads(line, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except PiProtocolError:
        raise
    except (ValueError, RecursionError):
        raise PiProtocolError("not_json") from None
    if not isinstance(record, dict):
        raise PiProtocolError("not_an_object")
    kind = record.get("type")
    if not isinstance(kind, str) or not kind:
        raise PiProtocolError("no_type")
    return record


# -- rfc8785/v1: the digest of a tool's input ----------------------------------------------------------
def normalised(value, depth: int = 0):
    """``value`` in the strict profile of ``rfc8785/v1``, with every integral number a whole number.

    Refused (``PiProtocolError``): nesting deeper than ``MAX_DEPTH``, half of a surrogate pair in a string
    or key, a number that is not finite, and an integral value outside ``-MAX_INT .. MAX_INT``. So ``2.0``
    becomes ``2`` and minus zero becomes ``0``: the value digested is the value carried.
    """
    if depth > MAX_DEPTH:
        raise PiProtocolError("tool_input_too_deep")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
            raise PiProtocolError("lone_surrogate")
        return value
    if type(value) is int:
        if not -MAX_INT <= value <= MAX_INT:
            raise PiProtocolError("integer_out_of_range")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise PiProtocolError("number_out_of_range")
        if value.is_integer():
            if abs(value) > MAX_INT:
                raise PiProtocolError("integer_out_of_range")
            return int(value)
        return value
    if isinstance(value, list):
        return [normalised(item, depth + 1) for item in value]
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            normalised(key, depth + 1)
            out[key] = normalised(item, depth + 1)
        return out
    raise PiProtocolError("not_a_json_value")


def number_text(number) -> str:
    """A number as ECMAScript writes it (RFC 8785 section 3.2.2.3): ``1.0`` is ``1``, ``1e21`` is ``1e+21``."""
    if type(number) is int:
        return str(number)
    if number == 0:
        return "0"
    sign = "-" if number < 0 else ""
    mantissa, _, exponent = repr(abs(number)).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = (whole + fraction).lstrip("0")
    # the decimal point sits after ``point`` digits of ``digits`` (negative: zeros before them)
    point = len(whole) + int(exponent or 0) - (len(whole + fraction) - len(digits))
    digits = digits.rstrip("0") or "0"
    count = len(digits)
    if count <= point <= 21:
        return sign + digits + "0" * (point - count)
    if 0 < point <= 21:
        return sign + digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * (-point) + digits
    power = point - 1
    tail = "." + digits[1:] if count > 1 else ""
    return f"{sign}{digits[0]}{tail}e{'+' if power >= 0 else '-'}{abs(power)}"


_SHORT_ESCAPES = {0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r", 0x22: '\\"', 0x5C: "\\\\"}


def _string_text(text: str) -> str:
    out = ['"']
    for ch in text:
        point = ord(ch)
        if point in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[point])
        elif point < 0x20:
            out.append("\\u%04x" % point)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def canonical_text(value) -> str:
    """The text of a normalised value as RFC 8785 writes it: no spaces, keys in UTF-16 code-unit order."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) in (int, float):
        return number_text(value)
    if isinstance(value, str):
        return _string_text(value)
    if isinstance(value, list):
        return "[" + ",".join(canonical_text(item) for item in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return "{" + ",".join(_string_text(key) + ":" + canonical_text(value[key]) for key in keys) + "}"
    raise PiProtocolError("not_a_json_value")


def input_digest(tool_input) -> tuple[object, str, int]:
    """``(normalised input, its rfc8785/v1 SHA-256 in lower-case hex, its text's UTF-8 length)``."""
    value = normalised(tool_input)
    data = canonical_text(value).encode("utf-8")
    return value, hashlib.sha256(data).hexdigest(), len(data)


# -- one stream ----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Pending:
    command: str
    delivery_id: str | None = None
    turn_id: str | None = None
    how: str | None = None
    text: str | None = None


def _bounded_id(value, limit: int) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= limit and "\r" not in value and "\n" not in value


def _count(value) -> bool:
    return type(value) is int and value >= 0


def _text_of_user(content) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(isinstance(b, dict) for b in content):
        texts = [b.get("text") for b in content if b.get("type") == "text"]
        if all(isinstance(t, str) for t in texts) and len(texts) == len(content):
            return "".join(texts)
    return None


class PiStream:
    """The reader of one Pi worker's stdout, and the writer of the lines this stream correlates.

    One stream per worker incarnation: the tokens it mints (``e8-1``, ``e8-2``, ...) are known only to it,
    so a response that names a token of an earlier worker matches nothing here.

    ``feed(line)`` reads one record and returns its drafts. ``signals`` collects the facts that are not
    events. Nothing here holds a lock or writes to a process; the driver writes the lines it is given.
    """

    def __init__(self, mode: str, *, connection_id: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode {mode!r}")
        if not isinstance(connection_id, str) or not _CONNECTION.fullmatch(connection_id):
            raise ValueError("connection_id")
        self.mode, self.connection_id = mode, connection_id
        self.engine_session_id: str | None = None
        self.session_file: str | None = None
        self.records = 0
        self.settled = False          # the last run Pi started has settled (agent_settled)
        self.aborted = False          # ... and it settled because it was aborted
        self.will_retry: bool | None = None
        self.final: dict | None = None   # the last completed assistant message, as the result reads it
        self.signals: list[dict] = []
        self.ignored: dict[str, int] = {}
        self.other_types: dict[str, int] = {}
        self._pending: dict[str, _Pending] = {}
        self._minted = 0
        self._argv_registered = False
        self._calls: set[str] = set()
        self._results: set[str] = set()
        self._result_drafted = False

    # -- the lines the driver writes, and what is correlated with them --------------------------------
    def _mint(self) -> str:
        self._minted += 1
        return f"e8-{self._minted}"

    def _line(self, record: dict) -> str:
        return json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"

    def prompt(self, text: str, *, delivery_id: str, turn_id: str, how: str) -> tuple[str, str]:
        """RPC: the ``prompt`` line for one user message, and its token. ``mid_turn`` steers the run.

        The delivery is pending from here: a write that then fails leaves it pending, its outcome
        unknown, and a later typed acceptance of it still counts.
        """
        self._need("rpc")
        if not isinstance(text, str) or not text:
            raise ValueError("text")
        if not isinstance(delivery_id, str) or not _DELIVERY.fullmatch(delivery_id):
            raise ValueError("delivery_id")
        if not isinstance(turn_id, str) or not _TURN.fullmatch(turn_id):
            raise ValueError("turn_id")
        if how not in ("new_turn", "mid_turn"):
            raise ValueError("how")
        token = self._mint()
        record = {"id": token, "type": "prompt", "message": text}
        if how == "mid_turn":
            record["streamingBehavior"] = "steer"
        self._pending[token] = _Pending("prompt", delivery_id, turn_id, how, text)
        return token, self._line(record)

    def command(self, kind: str) -> tuple[str, str]:
        """RPC: an ``abort`` or ``get_state`` line and its token."""
        self._need("rpc")
        if kind not in ("abort", "get_state"):
            raise ValueError(kind)
        token = self._mint()
        self._pending[token] = _Pending(kind)
        return token, self._line({"id": token, "type": kind})

    def cancel_dialog(self, request_id: str) -> str:
        """RPC: the answer that cancels an extension dialog Pi waits on. It gets no response."""
        self._need("rpc")
        if not _bounded_id(request_id, 256):
            raise ValueError("request_id")
        return self._line({"type": "extension_ui_response", "id": request_id, "cancelled": True})

    def argv_prompt(self, text: str, *, delivery_id: str, turn_id: str) -> str:
        """JSON mode: register the one prompt the driver puts in argv; Pi's replay of it is its acceptance."""
        self._need("json")
        if self._argv_registered:
            raise ValueError("a one-shot run takes one prompt")
        if not isinstance(text, str) or not text:
            raise ValueError("text")
        if not isinstance(delivery_id, str) or not _DELIVERY.fullmatch(delivery_id):
            raise ValueError("delivery_id")
        if not isinstance(turn_id, str) or not _TURN.fullmatch(turn_id):
            raise ValueError("turn_id")
        self._argv_registered = True
        self._pending["argv"] = _Pending("argv", delivery_id, turn_id, "new_turn", text)
        return text

    def pending_deliveries(self) -> list[str]:
        """Deliveries written and not (yet) accepted: their outcome is unknown if the worker ends now."""
        return [p.delivery_id for p in self._pending.values() if p.delivery_id is not None]

    def _need(self, mode: str) -> None:
        if self.mode != mode:
            raise ValueError(f"not in {mode} mode")

    # -- reading --------------------------------------------------------------------------------------
    def feed(self, line) -> list[dict]:
        """Read one record of Pi's stdout and return its drafts (possibly none)."""
        record = read_record(line)
        self.records += 1
        kind = record["type"]
        if kind == "session":
            if self.mode != "json" or self.records != 1:
                raise PiProtocolError("session_header", "only the first record of a one-shot run")
            self._set_session(record.get("id"))
            return []
        handler = self._HANDLERS.get(kind)
        if handler is None:
            self.other_types[kind] = self.other_types.get(kind, 0) + 1
            return []
        if kind in self._RPC_ONLY and self.mode != "rpc":
            raise PiProtocolError("rpc_record_in_json_mode", kind)
        return handler(self, record)

    def _ignore(self, reason: str, **fields) -> None:
        self.ignored[reason] = self.ignored.get(reason, 0) + 1
        self.signals.append({"signal": "ignored", "reason": reason, **fields})

    def _set_session(self, value) -> None:
        if not _bounded_id(value, 256):
            raise PiProtocolError("session_id")
        if self.engine_session_id not in (None, value):
            raise PiProtocolError("session_changed")
        if self.engine_session_id is None:
            self.engine_session_id = value
            self.signals.append({"signal": "session", "engine_session_id": value})

    # -- session events -------------------------------------------------------------------------------
    def _message_end(self, record: dict) -> list[dict]:
        message = record.get("message")
        if not isinstance(message, dict):
            raise PiProtocolError("message")
        role = message.get("role")
        if role == "assistant":
            return self._assistant(message)
        if role == "user":
            return self._user(message)
        return []   # tool results come from tool_execution_end; the other roles carry no event

    def _assistant(self, message: dict) -> list[dict]:
        content = message.get("content")
        if not isinstance(content, list):
            raise PiProtocolError("assistant_content")
        texts, calls = [], []
        for block in content:
            if not isinstance(block, dict):
                raise PiProtocolError("content_block")
            if block.get("type") == "text":
                if not isinstance(block.get("text"), str):
                    raise PiProtocolError("text_block")
                texts.append(block["text"])
            elif block.get("type") == "toolCall":
                calls.extend(self._tool_call(block.get("id"), block.get("name"), block.get("arguments")))
        stop = message.get("stopReason")
        error = message.get("errorMessage")
        self.final = {
            "stop_reason": stop if isinstance(stop, str) else None,
            "error_message": error if isinstance(error, str) else None,
            "text": "\n".join(texts),
        }
        return [self._usage(message)] + calls

    def _usage(self, message: dict) -> dict:
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        counts = [usage.get(name) for name in ("input", "output", "cacheRead", "cacheWrite", "totalTokens")]
        reported = any(_count(v) and v > 0 for v in counts)

        def measured(value):
            if reported and _count(value):
                return {"value": value, "quality": "known", "provenance": "observed"}
            return dict(_UNKNOWN_COUNT)

        cost = usage.get("cost").get("total") if isinstance(usage.get("cost"), dict) else None
        if reported and type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
            cost_usd = {"value": cost, "quality": "known", "provenance": "modelled"}
        else:
            cost_usd = dict(_UNKNOWN_COUNT)
        provider, model = message.get("provider"), message.get("model")
        if isinstance(model, str) and model and isinstance(provider, str) and provider:
            model = f"{provider}/{model}"
        elif not (isinstance(model, str) and model):
            model = None
        return {"type": "usage", "payload": {
            "connection_id": self.connection_id,
            "model": model,
            "bucket": None,
            "span": "call",
            "input_tokens": measured(usage.get("input")),
            "output_tokens": measured(usage.get("output")),
            "reasoning_tokens": measured(usage.get("reasoning")),
            "cache_read_tokens": measured(usage.get("cacheRead")),
            "cache_write_tokens": measured(usage.get("cacheWrite")),
            "cost_usd": cost_usd,
        }}

    def _tool_call(self, call_id, name, arguments) -> list[dict]:
        if not _bounded_id(call_id, 128):
            raise PiProtocolError("tool_call_id")
        if not _bounded_id(name, 256):
            raise PiProtocolError("tool_name")
        if not isinstance(arguments, dict):
            raise PiProtocolError("tool_input_not_an_object")
        if call_id in self._calls:
            self._ignore("tool_call_repeated", call_id=call_id)
            return []
        value, digest, size = input_digest(arguments)
        self._calls.add(call_id)
        return [{"type": "tool.call", "payload": {
            "call_id": call_id, "name": name, "input_digest": digest,
            "input": value if size <= INPUT_CARRY_MAX else None,
        }}]

    def _tool_start(self, record: dict) -> list[dict]:
        call_id = record.get("toolCallId")
        if _bounded_id(call_id, 128) and call_id in self._calls:
            return []   # already drafted from the assistant's toolCall block
        return self._tool_call(call_id, record.get("toolName"), record.get("args"))

    def _tool_end(self, record: dict) -> list[dict]:
        call_id = record.get("toolCallId")
        if not _bounded_id(call_id, 128):
            raise PiProtocolError("tool_call_id")
        if call_id in self._results:
            self._ignore("tool_result_repeated", call_id=call_id)
            return []
        if call_id not in self._calls:
            self.signals.append({"signal": "tool_result_without_call", "call_id": call_id})
        self._results.add(call_id)
        result = record.get("result")
        content = result.get("content") if isinstance(result, dict) else None
        truncated = False
        texts = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                else:
                    truncated = True   # an image or another block is not carried as text
        else:
            truncated = True           # nothing readable was given
        text = "\n".join(texts)
        if len(text) > TOOL_TEXT_MAX:
            text, truncated = text[:TOOL_TEXT_MAX], True
        return [{"type": "tool.result", "payload": {
            "call_id": call_id,
            "status": "error" if record.get("isError") is True else "ok",
            "text": text, "output_ref": None, "truncated": truncated,
        }}]

    def _user(self, message: dict) -> list[dict]:
        if self.mode != "json":
            return []   # in RPC mode the typed acceptance is the response to the prompt line
        pending = self._pending.get("argv")
        if pending is None:
            self._ignore("user_replay_unexpected")
            return []
        if _text_of_user(message.get("content")) != pending.text:
            self._ignore("user_replay_differs", delivery_id=pending.delivery_id)
            return []
        del self._pending["argv"]
        return [{"type": "input.accepted", "payload": {
            "delivery_id": pending.delivery_id, "turn_id": pending.turn_id, "how": "new_turn"}}]

    def _agent_start(self, record: dict) -> list[dict]:
        # A low-level run began (a retry begins another): what settles next is this run's outcome. Not
        # reset when a prompt's response arrives, since Pi may write that response after a fast run's events.
        self.settled, self.aborted, self.final, self.will_retry = False, False, None, None
        return []

    def _agent_end(self, record: dict) -> list[dict]:
        self.will_retry = record.get("willRetry") is True
        return []

    def _agent_settled(self, record: dict) -> list[dict]:
        self.settled, self.aborted = True, record.get("aborted") is True
        final = self.final or {}
        self.signals.append({"signal": "settled", "aborted": self.aborted,
                             "stop_reason": final.get("stop_reason"), "text": final.get("text")})
        if self.mode != "json":
            return []
        if self._result_drafted:
            self._ignore("settled_again")
            return []
        self._result_drafted = True
        code, detail = self.result_code()
        return [{"type": "result", "payload": {"code": code, "detail": detail}}]

    def result_code(self) -> tuple[str | None, str]:
        """The typed outcome of the last settled run: ``(None, "")`` when it ended with ``stop``."""
        final = self.final
        if self.aborted:
            return ENGINE_ERROR, "aborted"
        if final is None:
            return ENGINE_ERROR, "no assistant message"
        stop = final["stop_reason"]
        if stop == "stop":
            return None, ""
        if stop == "error":
            message = final["error_message"] or ""
            code = SHORT_FAULT if sounds_like_quota(message) else ENGINE_ERROR
            return code, ("error: " + message[:DETAIL_MAX]) if message else "error"
        word = stop if isinstance(stop, str) and _STOP_WORD.fullmatch(stop) else "unknown"
        return ENGINE_ERROR, f"stopReason {word}"

    # -- RPC records ------------------------------------------------------------------------------------
    def _response(self, record: dict) -> list[dict]:
        token, command, success = record.get("id"), record.get("command"), record.get("success")
        if not isinstance(command, str) or type(success) is not bool:
            raise PiProtocolError("response")
        pending = self._pending.get(token) if isinstance(token, str) else None
        if pending is None or pending.command != command:
            if command == "parse" and token is None:
                self.signals.append({"signal": "parse_error", "error": str(record.get("error"))[:DETAIL_MAX]})
                return []
            self._ignore("response_unmatched", command=command[:64])
            return []
        del self._pending[token]
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        if command == "prompt":
            return self._prompt_answer(pending, success, data, record.get("error"))
        if command == "get_state" and success:
            self._set_session(data.get("sessionId"))
            if isinstance(data.get("sessionFile"), str) and data["sessionFile"]:
                self.session_file = data["sessionFile"]
        self.signals.append({"signal": "response", "command": command, "token": token, "success": success})
        return []

    def _prompt_answer(self, pending: _Pending, success: bool, data: dict, error) -> list[dict]:
        if not success:
            self.signals.append({"signal": "refused", "delivery_id": pending.delivery_id,
                                 "error": str(error)[:DETAIL_MAX]})
            return []
        how = {"started": "new_turn", "queued": "mid_turn"}.get(data.get("disposition"))
        if how is None:
            # "handled": an extension took the text and no run started. Not an acceptance into a turn.
            self.signals.append({"signal": "not_started", "delivery_id": pending.delivery_id,
                                 "disposition": str(data.get("disposition"))[:64]})
            return []
        return [{"type": "input.accepted", "payload": {
            "delivery_id": pending.delivery_id, "turn_id": pending.turn_id, "how": how}}]

    def _dialog(self, record: dict) -> list[dict]:
        request_id, method = record.get("id"), record.get("method")
        if not _bounded_id(request_id, 256) or not isinstance(method, str):
            raise PiProtocolError("extension_ui_request")
        self.signals.append({"signal": "dialog", "id": request_id, "method": method[:64],
                             "waits": method in DIALOGS})
        return []

    _HANDLERS = {
        "message_end": _message_end,
        "tool_execution_start": _tool_start,
        "tool_execution_end": _tool_end,
        "agent_start": _agent_start,
        "agent_end": _agent_end,
        "agent_settled": _agent_settled,
        "response": _response,
        "extension_ui_request": _dialog,
    }
    _RPC_ONLY = frozenset({"response", "extension_ui_request"})
