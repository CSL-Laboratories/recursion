# Written by Claude Opus 5.5 (claude-opus-5-5), builder lane ENGINE-E6, for Lucas.
"""Cursor CLI (engine ``cursor``): the stream-json parser, the drives' argv, and the unattended switch.

Lane ENGINE-E6, first slice (the Liaison's start note of Nov 14, 2026): fixtures, parser, and the switch
written as data that is off. Nothing here starts a process, reads a file or the environment, reads a
login store, or is registered anywhere: no factory is in the engine table, no daemon loads this module.

SOURCES. Cursor publishes no schema for its stream. The shapes below are from its own pages, retrieved
2026-11-15 about 01:05 UTC (Nov 14, about 5:05 PM PST): https://cursor.com/docs/cli/reference/output-format
(the stream-json events), https://cursor.com/docs/cli/reference/parameters (the options) and
https://cursor.com/docs/cli/acp (``agent acp``); and from the vault record
20261107-csl712-engine-clis-grounding-01, V36 to V41 (``--disable-auto-update`` is a per-invocation flag
named in Cursor's changelog of January 2026, and the only switch for the background updater). No signed-in
run has been captured: every fixture of this lane is synthetic and says so.

THE ONE-SHOT STREAM (``agent -p --output-format stream-json``): one JSON object per line, each with a
``type`` and the conversation's ``session_id``::

    system/init        apiKeySource (env | flag | login), cwd, session_id, model, permissionMode
    user               the prompt replayed: message.role "user", message.content [{type: text, text}]
    assistant          model text (message.content); never inspected here
    tool_call/started  call_id, tool_call: {<name>ToolCall: {args}} or {function: {name, arguments}}
    tool_call/completed  the same, with result: {success: {...}} or {error: {...}}
    result/success     is_error, result (the full answer text), duration_ms, duration_api_ms, request_id?

Cursor documents no usage line, no limit line and no error line: on a failure the process writes to
standard error, exits non-zero and may end the stream with no ``result`` at all. So this parser drafts no
``usage`` and no ``limit``: there is NO ORACLE for a Cursor limit (none is known on either drive), and the
driver's flags ``typed_limits`` and ``usage`` are false. Free text is never read for a limit.

WHAT ``StreamParser.parse(line)`` DRAFTS (driver.v2 ``event_draft``; each payload has exactly event.v1's
keys): ``tool.call`` from tool_call/started, ``tool.result`` from tool_call/completed, ``result`` from the
terminal line, and ``input.accepted`` from the replay of the one input this worker was given. It drafts
no ``engine.output``: lane EVENTS' tailer writes a line the driver drafts nothing for as one untrusted
``engine.output`` event with its byte offsets, which only the tailer knows. A line that is not one strict
JSON object, belongs to another conversation, has a type or shape this parser does not know, or repeats
something already drafted, gives no draft and is counted in ``StreamParser.counts``. A forged event.v1
line is such a line: its ``type`` is none of Cursor's. Model text (assistant content, the answer) is
model payload and is never inspected.

The parser serves ONE worker and one reader of its output, in order, and holds no lock.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from types import MappingProxyType

# The core's RFC 8785 writer (lane SPINE's, in the method table's module): the form event.v1 names
# ``rfc8785/v1`` for ``input_digest``. The control package does not import ``routes``.
from recursion_core.control.methods import NoCanonicalText, canonical_text

ENGINE = "cursor"
# The command a registration of this engine will state (``default_executable``, SPINE's contract as the
# Liaison accepted it at 4:19 PM, Nov 14). Cursor's pages name the CLI ``agent``; composition hands the
# pinned launcher's absolute path as the ``binary`` option, and argv[0] is that path or this name, nothing else.
DEFAULT_EXECUTABLE = "agent"
# Background updates are off only by this per-invocation flag (V38); it goes on every spawn, ``agent acp``
# included. Whether ``agent acp`` honours it is not known (V38, the record's question 7).
NO_UPDATE = "--disable-auto-update"
# Options that carry a credential in argv (``--api-key``, ``--auth-token``) or can (a custom request
# header). A key reaches Cursor only as CURSOR_API_KEY in the child environment, never in argv.
CREDENTIAL_OPTIONS = ("--api-key", "--auth-token", "--header")
CREDENTIAL_SHORT_OPTIONS = ("-H",)
LAUNCH_REFUSED = "launch_refused"   # routes.contract's code; the detail says which rule

# The bounds of what a draft carries (this lane's choice, not a published limit). An input over the first
# is not carried (``input`` null; its digest is still taken); a result over the second is cut, ``truncated``.
MAX_INPUT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 64 * 1024

# common.v1's patterns (fullmatch, so a trailing newline never passes).
_TURN = re.compile(r"turn-[A-Za-z0-9_-]{1,59}")
_DELIVERY = re.compile(r"dlv-[A-Za-z0-9_-]{1,60}")
_CODE_WORD = re.compile(r"[a-z][a-z0-9_]{0,63}")
_API_KEY_SOURCES = ("env", "flag", "login")

# What the parser counts instead of drafting. Every name is always present.
COUNTS = (
    "not_json",            # not one strict JSON object (also: duplicate keys, NaN, too deep)
    "no_session",          # no session_id, or one that is not a string of 1 to 256 characters
    "other_session",       # a session_id other than the first this worker reported
    "unknown_type",        # a type this parser does not read (assistant lines are not counted)
    "unknown_subtype",     # a system or tool_call line with a subtype this parser does not read
    "init_repeated",       # a second system/init
    "user_unmatched",      # a user line that is not the replay of the input this worker was given
    "call_invalid",        # a call_id or tool name event.v1 refuses
    "tool_shape_unknown",  # a tool_call whose shape is not one of the two documented ones
    "call_repeated",       # a second started, or a second completed, for one call_id
    "call_undigestible",   # a tool input that has no RFC 8785 text, so no input_digest
    "input_withheld",      # a tool input over MAX_INPUT_BYTES: drafted with input null
    "result_unpaired",     # a completed whose started was not drafted
    "text_invalid",        # a tool result whose text cannot be written as UTF-8 (a lone surrogate)
    "result_repeated",     # a second terminal result line
)


class StreamStateError(RuntimeError):
    """A call the parser's state does not allow (for example a second input on the one-shot drive)."""


class ArgvRefused(ValueError):
    """An argv this lane's rules refuse. ``code`` is ``launch_refused``; ``detail`` names the rule."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"{LAUNCH_REFUSED}: {detail}")
        self.code, self.detail = LAUNCH_REFUSED, detail


# ------------------------------------------------------------------------------------ the declaration

def oneshot_declaration() -> dict:
    """driver.v2's root record for the one-shot drive: every flag false (one prompt, read to the result).

    ``resume`` stays false although the CLI has ``--resume [chatId]``: resuming is not built. ``input_ack``
    is false: the one-shot drive takes no input into a turn in progress.
    """
    flags = ("multi_turn", "mid_turn_input", "interrupt", "resume", "fork", "permission_bridge",
             "typed_limits", "usage")
    return {"engine": ENGINE, "driver": 2, "flags": {name: False for name in flags}, "input_ack": False}


# ------------------------------------------------------------------------------------------- argv

def _executable(binary: object) -> str:
    """argv[0]: the ``binary`` option composition gave (an absolute path), else DEFAULT_EXECUTABLE."""
    if binary is None:
        return DEFAULT_EXECUTABLE
    if not isinstance(binary, str) or not binary.startswith("/") or "\x00" in binary or "\n" in binary:
        raise ArgvRefused("binary_not_absolute")
    return binary


def oneshot_argv(model: object, *, binary: object = None) -> list[str]:
    """The one-shot drive's argv up to the prompt.

    How the prompt reaches the process (a positional argument or standard input) is not settled without a
    capture; the driver that adds it is the next slice. No ``--force``, ``--api-key`` or header is added.
    """
    if not isinstance(model, str) or not model or model.startswith("-") or any(c in model for c in "\x00\r\n"):
        raise ArgvRefused("model_invalid")
    return [_executable(binary), "-p", "--output-format", "stream-json", NO_UPDATE, "--model", model]


def acp_argv(*, binary: object = None) -> list[str]:
    """The long-lived drive: ``agent acp``, the updater flag as a global option before the command."""
    return [_executable(binary), NO_UPDATE, "acp"]


def check_argv(argv: object, *, binary: object = None) -> None:
    """Refuse an argv that breaks one of this lane's rules. ``ArgvRefused`` names the first broken.

    argv[0] is the vouched executable (``executable_mismatch``); the updater flag is present
    (``updater_not_disabled``); no option carries a credential (``credential_in_argv:<option>``), in either
    spelling (``--api-key K`` or ``--api-key=K``; ``-H V`` or ``-HV``).
    """
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
        raise ArgvRefused("argv_invalid")
    if argv[0] != _executable(binary):
        raise ArgvRefused("executable_mismatch")
    for arg in argv[1:]:
        for option in CREDENTIAL_OPTIONS:
            if arg == option or arg.startswith(option + "="):
                raise ArgvRefused(f"credential_in_argv:{option}")
        for option in CREDENTIAL_SHORT_OPTIONS:
            if arg.startswith(option):
                raise ArgvRefused(f"credential_in_argv:{option}")
    if NO_UPDATE not in argv[1:]:
        raise ArgvRefused("updater_not_disabled")


# --------------------------------------------------------------------------------- the switch, as data

UNATTENDED_OFF = "cursor_unattended_off"

# Lucas, grill Q35 ("I agree with the first"; CONTRACT 2087-2089): Cursor is built in full, unattended mode
# included; it runs attended; unattended sits behind a switch that stays off until his lawyer clears
# Cursor's acceptable-use question. What "attended" means is HIS open choice (the brief's first question),
# so nothing here decides whether a turn is attended: no function takes an origin or a caller flag, and
# none of the published schemas carries one. The switch is this record and nothing else. Its one state is
# "off"; this code has no way to turn it on, and nothing reads it yet.
_UNATTENDED = MappingProxyType({
    "engine": ENGINE,
    "switch": "unattended",
    "state": "off",
    "refusal": UNATTENDED_OFF,
    "waits_on": "counsel",
})


def unattended_switch() -> dict:
    """The switch's record, for a surface to show on a route row. A copy: changing it changes nothing."""
    return dict(_UNATTENDED)


def unattended_refusal() -> dict:
    """What an unattended turn will carry while the switch is off: a start error (run.record.v1's shape).

    It is refused before any spawn; who calls this, and on which turns, waits on Lucas's answer to what
    "attended" means and on the turn's origin field (lane SESSIONS), neither of which exists yet.
    """
    return {"code": UNATTENDED_OFF,
            "detail": "unattended Cursor turns are off until counsel clears Cursor's acceptable-use terms"}


# ------------------------------------------------------------------------------------------ the parser

class _Duplicate(ValueError):
    pass


def _no_duplicates(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise _Duplicate(key)
        out[key] = value
    return out


def _no_constant(name: str) -> object:
    raise ValueError(name)   # NaN, Infinity, -Infinity: not JSON


def _strict_object(line: str) -> dict | None:
    """One line as one JSON object, read strictly; None when it is not one."""
    try:
        value = json.loads(line, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _has_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def _label(value: object, longest: int) -> bool:
    """A call_id or tool name as event.v1 takes it: 1 to ``longest`` characters, no CR or LF."""
    return (isinstance(value, str) and 1 <= len(value) <= longest and "\r" not in value and "\n" not in value
            and not _has_surrogate(value))


def _cut(text: str, limit: int) -> tuple[str, bool]:
    """``text`` cut to at most ``limit`` UTF-8 bytes on a character boundary, and whether it was cut."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    return data[:limit].decode("utf-8", "ignore"), True


def input_digest(value: object) -> str:
    """event.v1's ``input_digest``: SHA-256 of the RFC 8785 text of the tool's input. NoCanonicalText if none."""
    return hashlib.sha256(canonical_text(value).encode("utf-8")).hexdigest()


class StreamParser:
    """Cursor's one-shot stream-json, one stdout line at a time, to driver.v2 event drafts.

    State the driver reads after the stream: ``engine_session_id``, ``model`` and ``api_key_source`` (from
    system/init), ``final_text`` and ``result_code`` (from the terminal result; None until then), and
    ``counts``.
    """

    def __init__(self) -> None:
        self.engine_session_id: str | None = None
        self.model: str | None = None
        self.api_key_source: str | None = None
        self.final_text: str | None = None
        self.result_code: str | None = None
        self.result_seen = False
        self.counts = dict.fromkeys(COUNTS, 0)
        self._init_seen = False
        self._input: tuple[str, str, str] | None = None   # (delivery_id, turn_id, text), until replayed
        self._wrote = False
        self._calls: dict[str, tuple[str, bool]] = {}     # call_id -> (tool key, completed)

    # -- the one input ---------------------------------------------------------------------------------
    def wrote(self, delivery_id: object, turn_id: object, text: object) -> None:
        """The driver gave the engine its one input. Its replay becomes ``input.accepted`` (``new_turn``).

        The replay must carry exactly this text: the one-shot drive has one input and no protocol token,
        so the text is the only thing that ties a ``user`` line to this write.
        """
        if not isinstance(delivery_id, str) or not _DELIVERY.fullmatch(delivery_id):
            raise ValueError("delivery_id")
        if not isinstance(turn_id, str) or not _TURN.fullmatch(turn_id):
            raise ValueError("turn_id")
        if not isinstance(text, str) or not text:
            raise ValueError("text")
        if self._wrote:
            raise StreamStateError("the one-shot drive takes one input")
        self._wrote = True
        self._input = (delivery_id, turn_id, text)

    # -- one line --------------------------------------------------------------------------------------
    def parse(self, line: object) -> list[dict]:
        if not isinstance(line, str):
            raise TypeError("a line is text")
        message = _strict_object(line)
        if message is None:
            return self._count("not_json")
        session = message.get("session_id")
        if not isinstance(session, str) or not 1 <= len(session) <= 256 or _has_surrogate(session):
            return self._count("no_session")
        if self.engine_session_id is None:
            self.engine_session_id = session
        elif session != self.engine_session_id:
            return self._count("other_session")
        kind = message.get("type")
        if kind == "assistant":
            return []   # model payload; the tailer keeps the line as engine output
        reader = {"system": self._system, "user": self._user, "tool_call": self._tool_call,
                  "result": self._result}.get(kind) if isinstance(kind, str) else None
        if reader is None:
            return self._count("unknown_type")
        return reader(message)

    def _count(self, name: str) -> list:
        self.counts[name] += 1
        return []

    def _system(self, message: dict) -> list:
        if message.get("subtype") != "init":
            return self._count("unknown_subtype")
        if self._init_seen:
            return self._count("init_repeated")
        self._init_seen = True
        model = message.get("model")
        self.model = model if isinstance(model, str) and model else None
        source = message.get("apiKeySource")
        self.api_key_source = source if source in _API_KEY_SOURCES else None
        return []

    def _user(self, message: dict) -> list:
        body = message.get("message")
        content = body.get("content") if isinstance(body, Mapping) and body.get("role") == "user" else None
        parts = content if isinstance(content, list) and content else None
        if parts is None or not all(isinstance(p, Mapping) and p.get("type") == "text"
                                    and isinstance(p.get("text"), str) for p in parts):
            return self._count("user_unmatched")
        text = "".join(p["text"] for p in parts)
        if self._input is None or text != self._input[2]:
            return self._count("user_unmatched")
        delivery_id, turn_id, _ = self._input
        self._input = None
        return [{"type": "input.accepted",
                 "payload": {"delivery_id": delivery_id, "turn_id": turn_id, "how": "new_turn"}}]

    def _tool_call(self, message: dict) -> list:
        subtype = message.get("subtype")
        if subtype not in ("started", "completed"):
            return self._count("unknown_subtype")
        call_id = message.get("call_id")
        if not _label(call_id, 128):
            return self._count("call_invalid")
        call = message.get("tool_call")
        if not isinstance(call, Mapping) or len(call) != 1:
            return self._count("tool_shape_unknown")
        (key, inner), = call.items()
        if not isinstance(inner, Mapping):
            return self._count("tool_shape_unknown")
        if subtype == "started":
            return self._started(call_id, key, inner)
        return self._completed(call_id, key, inner)

    def _started(self, call_id: str, key: str, inner: Mapping) -> list:
        if key == "function":
            name, arguments = inner.get("name"), inner.get("arguments")
            if isinstance(arguments, str):
                decoded = _strict_object(arguments)
                # The input is the arguments object; arguments that are not one object are carried as
                # null and digested as the string the engine gave.
                value, carried = (decoded, decoded) if decoded is not None else (arguments, None)
            elif isinstance(arguments, Mapping):
                value, carried = arguments, arguments
            else:
                return self._count("tool_shape_unknown")
        else:
            name, value = key, inner.get("args")
            if not isinstance(value, Mapping):
                return self._count("tool_shape_unknown")
            carried = value
        if not _label(name, 256):
            return self._count("call_invalid")
        if call_id in self._calls:
            return self._count("call_repeated")
        try:
            text = canonical_text(value)
        except NoCanonicalText:
            return self._count("call_undigestible")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if carried is not None and len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            self.counts["input_withheld"] += 1
            carried = None
        self._calls[call_id] = (key, False)
        return [{"type": "tool.call", "payload": {
            "call_id": call_id, "name": name, "input_digest": digest,
            "input": dict(carried) if carried is not None else None}}]

    def _completed(self, call_id: str, key: str, inner: Mapping) -> list:
        known = self._calls.get(call_id)
        if known is None or known[0] != key:
            return self._count("result_unpaired")
        if known[1]:
            return self._count("call_repeated")
        result = inner.get("result")
        if not isinstance(result, Mapping) or len(result) != 1:
            return self._count("tool_shape_unknown")
        (outcome, body), = result.items()
        if outcome not in ("success", "error") or not isinstance(body, Mapping):
            return self._count("tool_shape_unknown")
        content = body.get("content")
        if isinstance(content, str):
            text = content
        else:
            try:
                text = canonical_text(body)
            except NoCanonicalText:
                return self._count("text_invalid")
        if _has_surrogate(text):
            return self._count("text_invalid")
        text, cut = _cut(text, MAX_RESULT_BYTES)
        self._calls[call_id] = (key, True)
        return [{"type": "tool.result", "payload": {
            "call_id": call_id, "status": "ok" if outcome == "success" else "error", "text": text,
            "output_ref": None, "truncated": cut or body.get("exceededLimit") is True}}]

    def _result(self, message: dict) -> list:
        if self.result_seen:
            return self._count("result_repeated")
        subtype, is_error = message.get("subtype"), message.get("is_error")
        if is_error is False and subtype == "success":
            code, detail = None, ""
            answer = message.get("result")
            self.final_text = answer if isinstance(answer, str) else ""
        elif is_error is True:
            # Cursor documents no error envelope; its subtype word is kept when it is a plain code word,
            # and no text of the line is read (it may be the engine's words).
            code = "engine_error"
            detail = f"subtype:{subtype}" if isinstance(subtype, str) and _CODE_WORD.fullmatch(subtype) else "is_error"
        else:
            # The terminal line came and says neither success nor a typed error: never taken for success.
            code, detail = "protocol_error", "result line not understood"
        self.result_seen = True
        self.result_code = code
        return [{"type": "result", "payload": {"code": code, "detail": detail}}]
