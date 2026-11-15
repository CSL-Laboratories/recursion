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
no ``engine.output``, whose byte offsets only the reader of the output file knows; that a line the driver
drafts nothing for (assistant text among them) is still kept as one untrusted ``engine.output`` event is to
be agreed and tested with lane EVENTS before anything is wired (the lane's questions, Q15). ``parse`` takes
text (anything else is a ``TypeError``). A line that is not one strict JSON object, has a type this parser
does not know, belongs to another conversation, has a shape this parser does not know, or repeats something
already drafted, gives no draft and is counted in ``StreamParser.counts``. A forged event.v1 line is such a
line: its ``type`` is none of Cursor's. The one exception is the terminal line: a result line whose outcome
the parser cannot read still ends the turn, as a ``result`` draft with code ``protocol_error``, and is not
counted. The conversation's id is set by the first line that is neither of an unknown type nor assistant
text, so neither can bind the parser to another conversation. Model text (assistant content, the answer) is
model payload and is never inspected; the answer is only checked to be text.

THE RESULT IS TERMINAL. Cursor documents the result line as the stream's last: after the first one, a
second result of this conversation is counted ``result_repeated`` and any other line of it
``after_result``, and neither drafts anything. A later line that is not JSON, has an unknown type, or has
no or another conversation's id is counted as that, as before the result. A success must carry its answer
as text that UTF-8 can write; any other success line is a ``protocol_error``, never an empty success.

The parser serves ONE worker and one reader of its output, in order, and holds no lock. ``parse`` is not
atomic: an exception that escapes it (a ``MemoryError``, an interrupt) may leave its line half recorded,
so a driver that catches one discards the parser with the turn.
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
# Options that widen what a run may do without asking (https://cursor.com/docs/cli/reference/parameters:
# ``-f``/``--force`` "Force allow commands unless explicitly denied", ``--yolo`` its alias, ``--approve-mcps``
# "Automatically approve all MCP servers", ``--sandbox disabled``). This lane's builders never add one; an argv
# carrying one is refused unless full access was acknowledged for the run, the rule the existing launch
# guards keep (``full_access_acknowledged`` in subscription_routes). The last two are not on Cursor's page;
# the break-it read of #388 named them, and they are refused in case a build takes them.
FULL_ACCESS_OPTIONS = ("--force", "--yolo", "--approve-mcps", "--dangerously-skip-permissions", "--auto-approve")
FULL_ACCESS_SHORT_OPTIONS = ("-f",)
# Linux takes at most 131072 bytes, its terminating NUL included, for one argument (MAX_ARG_STRLEN).
MAX_ARG_BYTES = 128 * 1024 - 1
# Linux also bounds argv and the environment together (ARG_MAX: a quarter of the stack limit, 2 MiB under the
# usual 8 MiB stack). This lane holds the whole argv, each argument with its terminating NUL, to half of that,
# leaving the rest for the child's environment (an allowlist) and the pointers. A stack limit under 4 MiB
# lowers ARG_MAX below this bound, and execve then refuses the launch itself.
MAX_ARGV_BYTES = 1024 * 1024
# The two drives, as each builder writes it right after the updater flag.
DRIVES = ("-p", "acp")
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
_TYPES = ("system", "user", "assistant", "tool_call", "result")   # the documented line types

# What the parser counts instead of drafting. Every name is always present.
COUNTS = (
    "not_json",            # not one strict JSON object (also: duplicate keys, NaN, too deep)
    "unknown_type",        # a type this parser does not read; it never sets the conversation's id
    "no_session",          # no session_id, or one that is not a string of 1 to 256 characters
    "other_session",       # a session_id other than the first a known line reported
    "after_result",        # any other line of this conversation after the terminal result
    "result_repeated",     # a second terminal result line
    "unknown_subtype",     # a system or tool_call line with a subtype this parser does not read
    "init_repeated",       # a second system/init
    "user_unmatched",      # a user line that is not the replay of the input this worker was given
    "call_invalid",        # a call_id or tool name event.v1 refuses
    "tool_shape_unknown",  # a tool_call whose shape is not one of the two documented ones
    "call_repeated",       # a second started, or a second completed, for one call_id
    "call_undigestible",   # a tool input that has no RFC 8785 text, so no input_digest
    "input_withheld",      # a tool input over MAX_INPUT_BYTES: drafted with input null
    "result_unpaired",     # a completed whose started was not drafted, or names another tool
    "text_invalid",        # a tool result whose text cannot be written as UTF-8 (a lone surrogate)
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
    """argv[0]: the ``binary`` option composition gave, else DEFAULT_EXECUTABLE.

    The option must be a plain absolute path: it starts with ``/``, and no part of it is empty, ``.`` or
    ``..`` (so ``/``, ``/..``, ``//x`` and ``/opt/agent/`` are refused); no NUL, CR, LF or lone surrogate.
    Nothing here looks at the file system, so whether a file is there shows only at launch.
    """
    if binary is None:
        return DEFAULT_EXECUTABLE
    if (not isinstance(binary, str) or not binary.startswith("/") or any(ch in binary for ch in "\x00\r\n")
            or _has_surrogate(binary) or any(part in ("", ".", "..") for part in binary.split("/")[1:])):
        raise ArgvRefused("binary_not_absolute")
    return binary


def oneshot_argv(model: object, *, binary: object = None) -> list[str]:
    """The one-shot drive's options, up to the prompt (the driver adds ``--`` and the prompt).

    The updater flag stands first, right after argv[0], on both drives, and the drive (``-p``) right after
    it. No ``--force``, ``--api-key`` or header is added. A model that is empty, starts with a dash, or holds
    a NUL, CR, LF or lone surrogate is ``model_invalid``.
    """
    if (not isinstance(model, str) or not model or model.startswith("-") or any(ch in model for ch in "\x00\r\n")
            or _has_surrogate(model)):
        raise ArgvRefused("model_invalid")
    return [_executable(binary), NO_UPDATE, "-p", "--output-format", "stream-json", "--model", model]


def acp_argv(*, binary: object = None) -> list[str]:
    """The long-lived drive: ``agent acp``, the updater flag first, as a global option before the command."""
    return [_executable(binary), NO_UPDATE, "acp"]


def check_argv(argv: object, *, binary: object = None, full_access_acknowledged: bool = False) -> None:
    """Refuse an argv that breaks one of this lane's rules. ``ArgvRefused`` names the first broken.

    The options are the arguments before the first ``--``; what follows it is positional text (the prompt)
    and is never read as an option. In this order:

    * ``full_access_malformed``: ``full_access_acknowledged`` is not a bool; only ``True`` acknowledges, as
      in the existing launch guards (``"true"``, ``1`` and the like are not an acknowledgement).
    * ``argv_invalid``: not a non-empty list of text; an empty argument; an argument holding a NUL or a lone
      surrogate, or longer than MAX_ARG_BYTES UTF-8 bytes; an argv longer than MAX_ARGV_BYTES in all, each
      argument counted with its NUL (none of these can be passed to execve); an option holding a CR or LF.
    * ``executable_mismatch``: argv[0] is not the vouched executable.
    * ``credential_in_argv:<option>``: an option carries a credential, in any spelling: ``--api-key K``,
      ``--api-key=K``, ``-H V``, ``-HV``, or ``H`` among bundled short options (``-vH V``).
    * ``full_access_not_acknowledged:<option>``: a full-access option (FULL_ACCESS_OPTIONS, ``-f`` alone or
      bundled, ``--sandbox disabled``) without ``full_access_acknowledged=True``.
    * ``updater_not_disabled``: argv[1] is not the updater flag. It stands first on both drives, so it is a
      global option before any command (the break-it read's F1, in its stricter form: questions.md Q18).
    * ``drive_missing``: argv[2] is not one of DRIVES, ``-p`` (the one-shot drive) or ``acp``, as the two
      builders write them. An argv that names no drive would start Cursor's interactive agent (the fifth
      break-it read of #388, F5-3).
    """
    if not isinstance(full_access_acknowledged, bool):
        raise ArgvRefused("full_access_malformed")
    if (not isinstance(argv, (list, tuple)) or not argv
            or not all(isinstance(a, str) and a and "\x00" not in a and not _has_surrogate(a)
                       and len(a.encode("utf-8")) <= MAX_ARG_BYTES for a in argv)
            or sum(len(a.encode("utf-8")) + 1 for a in argv) > MAX_ARGV_BYTES):
        raise ArgvRefused("argv_invalid")
    if argv[0] != _executable(binary):
        raise ArgvRefused("executable_mismatch")
    rest = list(argv[1:])
    options = rest[:rest.index("--")] if "--" in rest else rest
    if any("\r" in arg or "\n" in arg for arg in options):
        raise ArgvRefused("argv_invalid")
    bundles = [arg[1:] for arg in options if arg.startswith("-") and not arg.startswith("--")]
    for arg in options:
        for option in CREDENTIAL_OPTIONS:
            if arg == option or arg.startswith(option + "="):
                raise ArgvRefused(f"credential_in_argv:{option}")
    for letters in bundles:
        for option in CREDENTIAL_SHORT_OPTIONS:
            if option[1] in letters:
                raise ArgvRefused(f"credential_in_argv:{option}")
    if full_access_acknowledged is not True:
        for i, arg in enumerate(options):
            for option in FULL_ACCESS_OPTIONS:
                if arg == option or arg.startswith(option + "="):
                    raise ArgvRefused(f"full_access_not_acknowledged:{option}")
            if arg == "--sandbox=disabled" or (arg == "--sandbox" and options[i + 1:i + 2] == ["disabled"]):
                raise ArgvRefused("full_access_not_acknowledged:--sandbox")
        for letters in bundles:
            for option in FULL_ACCESS_SHORT_OPTIONS:
                if option[1] in letters:
                    raise ArgvRefused(f"full_access_not_acknowledged:{option}")
    if len(argv) < 2 or argv[1] != NO_UPDATE:
        raise ArgvRefused("updater_not_disabled")
    if len(rest) < 2 or rest[1] not in DRIVES:
        raise ArgvRefused("drive_missing")


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

    Nothing calls this yet. The driver is to refuse such a turn with it before any spawn; who calls it, and
    on which turns, waits on Lucas's answer to what "attended" means and on the turn's origin field (lane
    SESSIONS), neither of which exists yet.
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

    State the driver reads after the stream: ``engine_session_id`` (from the first line of a known type
    other than assistant text),
    ``model`` and ``api_key_source`` (from the first system/init; None when absent or not one of the three
    documented sources), ``result_seen``, ``result_code`` (None on success and before a result),
    ``final_text`` (the answer of a successful result, else None), and ``counts`` (``COUNTS``, every name
    always present).
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
        self._calls: dict[str, tuple[str, str, bool]] = {}   # call_id -> (tool key, tool name, completed)

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
        kind = message.get("type")
        if kind not in _TYPES:
            return self._count("unknown_type")   # before the session: an unknown line never sets it
        session = message.get("session_id")
        if not isinstance(session, str) or not 1 <= len(session) <= 256 or _has_surrogate(session):
            return self._count("no_session")
        if self.engine_session_id is None:
            if kind != "assistant":   # model text never sets the conversation (break-it read, F4)
                self.engine_session_id = session
        elif session != self.engine_session_id:
            return self._count("other_session")
        if self.result_seen:
            return self._count("result_repeated" if kind == "result" else "after_result")
        if kind == "assistant":
            return []   # model payload, never inspected
        reader = {"system": self._system, "user": self._user, "tool_call": self._tool_call,
                  "result": self._result}[kind]
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
        self._calls[call_id] = (key, name, False)
        return [{"type": "tool.call", "payload": {
            "call_id": call_id, "name": name, "input_digest": digest,
            "input": dict(carried) if carried is not None else None}}]

    def _completed(self, call_id: str, key: str, inner: Mapping) -> list:
        known = self._calls.get(call_id)
        # The completion names the same tool as its start: the same typed key, and for a function call the
        # same function name.
        if known is None or known[0] != key or (key == "function" and inner.get("name") != known[1]):
            return self._count("result_unpaired")
        if known[2]:
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
        self._calls[call_id] = (key, known[1], True)
        return [{"type": "tool.result", "payload": {
            "call_id": call_id, "status": "ok" if outcome == "success" else "error", "text": text,
            "output_ref": None, "truncated": cut or body.get("exceededLimit") is True}}]

    def _result(self, message: dict) -> list:
        subtype, is_error = message.get("subtype"), message.get("is_error")
        answer = message.get("result")
        if is_error is False and subtype == "success":
            if isinstance(answer, str) and not _has_surrogate(answer):
                code, detail = None, ""
                self.final_text = answer   # an empty answer is still an answer
            else:
                # Absent, not text, or not writable as UTF-8: the answer is lost, so this is no success.
                code, detail = "protocol_error", "result answer is not text"
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
