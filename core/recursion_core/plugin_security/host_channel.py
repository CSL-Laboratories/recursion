"""The one channel between the daemon and a plugin host (``csl-plugin-host/1``).

The schema is ``schemas/plugin/v1/host.v1.json``: it names every message (``hello``, ``load``,
``call``, ``core``, ``result``, ``refusal``, ``bye``), holds the ONE append-only list of call names,
the reserved names and the refusal codes. This module is the code side of that schema: the names
below are the schema's, and a test holds them equal to it. Frames are those of
``control_transport/framing.py``, reused as they are: ``encode``/``decode`` here only wrap its
``encode_frame``/``decode_body``.

What this module settles, and nothing more (the host program, its wall and the broker's handles are
later work):

* ``check_message(message, receiver=...)``: a message is one object of a known type with exactly
  its fields, sent by a side that may send it. A type the receiver does not know is refused
  ``message_unknown``; a reserved type ``message_reserved``; the channel stays up in both cases
  (the refusal is an answer, never a close).
* ``answer_call(message, handlers)``: the receiving side of a ``call``. A reserved name is refused
  ``call_reserved`` and an unknown one ``call_unknown`` before any handler is looked up, so nothing
  runs; a name on the list whose arguments are not exactly its own is refused ``call_args_invalid``.
  ``call`` and ``result`` travel both ways: the daemon calls a plugin's function by name, and a
  plugin's host calls a broker handle by name (every reserved call name is one of those, so a
  reserved name arriving at the daemon is refused ``call_reserved`` before any other check of it).
* ``DaemonEnd``: the daemon's side of one channel, opened for ONE loaded plugin. The caller of every
  ``core`` message is that plugin, bound once from ``CoreService.client_for``; nothing in a message
  can name another, and a ``core`` message that tries is refused ``caller_forged`` before the core
  sees it. The bytes go to the core's one dispatcher unchanged; every ``csl-core/v0`` op stays the
  core's. ``DaemonEnd.load`` sends a load only for that plugin.
* ``run.request`` (a plugin asks for a run from one of its records): answered at the daemon's end only
  when the channel was given the daemon's start path (``runs``) and the core says the channel's plugin
  holds the ``schedule`` permission; otherwise ``call_not_offered``. Its seven arguments are checked
  before anything starts (``call_args_invalid``); ``tenant``, ``engine`` and ``prompt`` exactly as
  ``run.start`` checks them. The run is started by ``runs.call("run.start", ..., origin=...)``: the one
  admission path of ``run.start``, with the start key derived here from the plugin, the record, its
  revision and the occurrence (never taken from the plugin), so the same plugin, record, revision and
  occurrence start at most one run for one tenant; the same occurrence for another tenant is another run.
  The record gains ``origin``: kind ``trigger``, the plugin the channel was opened for, and the record.
  What the code does today: a plugin granted ``schedule`` can ask for a run of any engine, with any
  prompt, for any tenant, in any number; the record it names is a label the core does not read; the
  permission is granted with no ask-once step; these limits are for the merge to settle and for the
  slices above this one to narrow.
* ``HostEnd``: the host's side of one channel: it answers the daemon's calls and takes a ``result``
  only for an id it called.
* Frame size: a ``load`` frame (daemon to host only) may be as large as the daemon's largest package
  needs (``MAX_LOAD_FRAME_BYTES``, computed from the package sizes in ``limits.py``, which the
  daemon reads); every other
  frame, and every frame a host sends, is at most ``MAX_FRAME_BYTES`` (1 MiB). A package that does
  not fit is refused ``load_too_large`` on the daemon's side, never a frame error.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import json
import re
from typing import Any, Callable, Mapping

from recursion_core.control_transport.errors import FrameError
from recursion_core.control_transport.framing import MAX_FRAME_BYTES, decode_body, encode_frame

from recursion_core.control.api import start_params_problem

from .canonical import canonical, parse
from .limits import ARTIFACT, MAX_ARTIFACT_BYTES, MAX_MANIFEST_BYTES
from .protocol import HOST, PROTOCOL_VERSION
from recursion_core.plugin_memory import words as memory_words

__all__ = [
    "CALL_NAMES", "CHANNEL", "INVENTORY", "MAX_FRAME_BYTES", "MAX_LOAD_FRAME_BYTES", "MESSAGE_TYPES",
    "RESERVED_CALL_NAMES", "RESERVED_MESSAGE_TYPES", "RESERVED_RUN_ORIGINS", "RESERVED_TEXT", "REFUSAL_CODES",
    "MEMORY_CALL_NAMES", "MEMORY_KEY_MAX_BYTES", "MEMORY_PERMISSION", "RUN_ORIGIN_FIELDS", "RUN_ORIGIN_KIND", "RUN_REQUEST_PERMISSION", "RUN_REQUEST_RECORD_KINDS",
    "ChannelRefusal", "DaemonEnd", "HostEnd", "answer_call", "check_message", "decode", "encode", "frame_limit",
    "refusal",
]

CHANNEL = "csl-plugin-host/1"
RESERVED_TEXT = "reserved; not available in this release"

# type -> (the sides that may send it, its exact fields). host.v1.json x-message-types.
MESSAGE_TYPES: dict[str, tuple[frozenset, frozenset]] = {
    "hello": (frozenset({"daemon", "host"}), frozenset({"type", "channel"})),
    "load": (frozenset({"daemon"}), frozenset({"type", "plugin_id", "manifest_b64", "files"})),
    "call": (frozenset({"daemon", "host"}), frozenset({"type", "id", "name", "args"})),
    "core": (frozenset({"daemon", "host"}), frozenset({"type", "id", "bytes_b64"})),
    "result": (frozenset({"daemon", "host"}), frozenset({"type", "id", "value"})),
    "refusal": (frozenset({"daemon", "host"}), frozenset({"type", "id", "code", "detail"})),
    "bye": (frozenset({"daemon", "host"}), frozenset({"type", "reason"})),
}
# The ONE append-only list of call names, each with the arguments it takes, in order. host.v1.json
# x-call-names. A later surface kind is a new name here, never a new message type.
CALL_NAMES: dict[str, tuple[str, ...]] = {
    "registry": (),
    "operator": ("command",),
    "surfaces": (),
    "surface": ("kind", "name"),
    "run.request": ("record_kind", "record_id", "revision", "occurrence", "tenant", "engine", "prompt"),
    "memory.put": ("key", "value_b64", "if_revision"),
    "memory.get": ("key",),
    "memory.list": ("prefix", "after", "limit"),
    "memory.delete": ("key", "if_revision"),
}
# memory.* (host.v1.json x-memory): a plugin's saved data, kept by the daemon for the plugin the channel was
# opened for and its install identity. Offered only when the daemon gave the channel a store (the plugin is in
# the person's row plugin_memory.plugins) AND the core says the plugin holds the permission 'memory'.
MEMORY_CALL_NAMES = ("memory.put", "memory.get", "memory.list", "memory.delete")
MEMORY_PERMISSION = "memory"
MEMORY_KEY_MAX_BYTES = 256  # a form of the protocol, like _RECORD_ID: not a setting
# run.request (host.v1.json x-run-request): the record kinds a run may be asked from (append-only), the
# permission that offers the call, and the origin the daemon writes into the run's record.
RUN_REQUEST_RECORD_KINDS = ("routine.v1",)
RUN_REQUEST_PERMISSION = "schedule"
RUN_ORIGIN_KIND = "trigger"
RUN_ORIGIN_FIELDS = ("kind", "plugin", "record_kind", "record_id", "revision", "occurrence")
# Reserved names (host.v1.json x-reserved): answered as reserved, never built here. When built, each
# reserved call name is sent by a plugin's host to the daemon.
RESERVED_CALL_NAMES = ("computer.open", "computer.act", "computer.observe", "computer.close",
                       "net.request", "net.connect")
RESERVED_MESSAGE_TYPES = ("event", "guard", "surface.update")
RESERVED_RUN_ORIGINS = ("trigger", "agent", "packet")
# Every function the daemon calls on a plugin -> (message, call name). host.v1.json x-inventory.
INVENTORY: dict[str, tuple[str, str | None]] = {
    "registry": ("call", "registry"),
    "operator": ("call", "operator"),
    "bind_core": ("core", None),
    "surfaces": ("call", "surfaces"),
    "surface": ("call", "surface"),
}
REFUSAL_CODES = ("message_invalid", "message_unknown", "message_reserved", "message_wrong_side", "caller_forged",
                 "call_unknown", "call_reserved", "call_args_invalid", "call_not_offered", "call_failed",
                 "load_too_large")

SIDES = ("daemon", "host")
MAX_ID = 2 ** 53 - 1
_TYPE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_CALL_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_ARG_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PLUGIN_ID = re.compile(r"[a-z][a-z0-9_.-]{1,63}")
_PLUGIN_ID_MAX = 64
_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*")
_RECORD_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_OCCURRENCE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
# Fields a core message might use to name a caller. None is ever read: naming one is a forgery.
_CALLER_FIELDS = frozenset({"caller", "plugin_id", "principal", "plugin", "owner"})


def _b64_len(size: int) -> int:
    return 4 * ((size + 2) // 3)


# The largest load frame the daemon can need: its largest manifest and its largest package file
# (limits.py, the sizes daemon/plugins.py reads) as base64, plus the load's envelope around them with the longest plugin id.
# Computed from those constants, never a second number; no load comes in parts in this release.
_LOAD_ENVELOPE = len(json.dumps({"type": "load", "plugin_id": "", "manifest_b64": "", "files": {ARTIFACT: ""}},
                                separators=(",", ":")).encode("utf-8")) + _PLUGIN_ID_MAX
MAX_LOAD_FRAME_BYTES = _b64_len(MAX_MANIFEST_BYTES) + _b64_len(MAX_ARTIFACT_BYTES) + _LOAD_ENVELOPE


class ChannelRefusal(Exception):
    """A typed refusal on the channel: ``code`` is one of ``REFUSAL_CODES``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def frame_limit(sender: str) -> int:
    """The largest frame body a receiver reads from ``sender``: ``MAX_LOAD_FRAME_BYTES`` from the daemon
    (only a load may use it), ``MAX_FRAME_BYTES`` (1 MiB) from a host."""
    if sender not in SIDES:
        raise ValueError("sender must be 'daemon' or 'host'")
    return MAX_LOAD_FRAME_BYTES if sender == "daemon" else MAX_FRAME_BYTES


def encode(message: Mapping[str, Any]) -> bytes:
    """One message as one frame of ``control_transport/framing.py``.

    A ``load`` may fill ``MAX_LOAD_FRAME_BYTES``; one that still does not fit is ``load_too_large``, never
    a frame error. Every other message keeps the frame limit of 1 MiB.
    """
    message = dict(message)
    if message.get("type") != "load":
        return encode_frame(message)
    try:
        return encode_frame(message, max_bytes=MAX_LOAD_FRAME_BYTES)
    except FrameError as exc:
        if str(exc) != "frame_too_large":
            raise
        raise ChannelRefusal("load_too_large", f"a load frame holds at most {MAX_LOAD_FRAME_BYTES} bytes") from None


def decode(body: bytes, *, sender: str | None = None) -> dict[str, Any]:
    """One frame body as a message object; a body that is not one JSON object is ``message_invalid``.

    A body above 1 MiB is ``message_invalid`` unless it came from the daemon (``sender="daemon"``), holds a
    ``load`` and is within ``MAX_LOAD_FRAME_BYTES``.
    """
    if sender is not None and sender not in SIDES:
        raise ValueError("sender must be 'daemon' or 'host'")
    if len(body) > (MAX_LOAD_FRAME_BYTES if sender == "daemon" else MAX_FRAME_BYTES):
        raise ChannelRefusal("message_invalid", "frame_too_large")
    try:
        message = decode_body(body)
    except FrameError as exc:
        raise ChannelRefusal("message_invalid", str(exc)) from None
    if len(body) > MAX_FRAME_BYTES and message.get("type") != "load":
        raise ChannelRefusal("message_invalid", "frame_too_large: only a load may pass 1 MiB")
    return message


def _is_id(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_ID


def _is_bytes(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        return False
    return len(value) % 4 == 0


def _check_fields(kind: str, message: dict) -> None:
    """The forms of each field of a message of a known type (host.v1.json $defs)."""
    def bad(field: str) -> ChannelRefusal:
        return ChannelRefusal("message_invalid", f"{kind}: {field}")

    if kind == "hello":
        if message["channel"] != CHANNEL:
            raise bad("channel")
    elif kind == "load":
        plugin_id = message["plugin_id"]
        if type(plugin_id) is not str or not _PLUGIN_ID.fullmatch(plugin_id) or plugin_id == HOST:
            raise bad("plugin_id")
        if not _is_bytes(message["manifest_b64"]):
            raise bad("manifest_b64")
        files = message["files"]
        if (type(files) is not dict or not files or len(files) > 4096
                or not all(type(p) is str and len(p) <= 512 and _PATH.fullmatch(p) and _is_bytes(b)
                           for p, b in files.items())):
            raise bad("files")
    elif kind == "call":
        if not _is_id(message["id"]):
            raise bad("id")
        name = message["name"]
        if type(name) is not str or len(name) > 64 or not _CALL_NAME.fullmatch(name):
            raise bad("name")
        args = message["args"]
        if type(args) is not dict or len(args) > 16 or not all(type(k) is str and _ARG_NAME.fullmatch(k) for k in args):
            raise bad("args")
    elif kind == "core":
        if not _is_id(message["id"]):
            raise bad("id")
        if not _is_bytes(message["bytes_b64"]):
            raise bad("bytes_b64")
    elif kind == "result":
        if not _is_id(message["id"]):
            raise bad("id")
    elif kind == "refusal":
        if not (message["id"] is None or _is_id(message["id"])):
            raise bad("id")
        if type(message["code"]) is not str or not _CODE.fullmatch(message["code"]):
            raise bad("code")
        if type(message["detail"]) is not str or len(message["detail"]) > 1000:
            raise bad("detail")
    elif kind == "bye":
        if type(message["reason"]) is not str or len(message["reason"]) > 200:
            raise bad("reason")


def check_message(message: object, *, receiver: str) -> dict:
    """``message`` as received by ``receiver`` (``"daemon"`` or ``"host"``), or ``ChannelRefusal``.

    Order: an object with a type; a reserved type (``message_reserved``); a type nobody defined
    (``message_unknown``); at the daemon, a call whose name is reserved (``call_reserved``, before any
    other check of that name); a type this receiver's peer never sends (``message_wrong_side``); then
    exactly its fields, a core message that names a caller (``caller_forged``) first among them,
    and their forms (``message_invalid``). The caller may answer every refusal with ``refusal``
    and keep the channel up.
    """
    if receiver not in SIDES:
        raise ValueError("receiver must be 'daemon' or 'host'")
    if type(message) is not dict or type(message.get("type")) is not str or not _TYPE.fullmatch(message["type"]):
        raise ChannelRefusal("message_invalid", "not one object with a message type")
    kind = message["type"]
    if kind in RESERVED_MESSAGE_TYPES:
        raise ChannelRefusal("message_reserved", f"{kind}: {RESERVED_TEXT}")
    if kind not in MESSAGE_TYPES:
        raise ChannelRefusal("message_unknown", kind)
    if kind == "call" and receiver == "daemon" and message.get("name") in RESERVED_CALL_NAMES:
        raise ChannelRefusal("call_reserved", f"{message['name']}: {RESERVED_TEXT}")
    senders, fields = MESSAGE_TYPES[kind]
    sender = "host" if receiver == "daemon" else "daemon"
    if sender not in senders:
        raise ChannelRefusal("message_wrong_side", f"{kind} is never sent by the {sender}")
    if kind == "core" and set(message) & _CALLER_FIELDS:
        raise ChannelRefusal("caller_forged", "the caller is set from the channel, never from a message")
    if set(message) != fields:
        raise ChannelRefusal("message_invalid", f"{kind}: fields must be {', '.join(sorted(fields))}")
    _check_fields(kind, message)
    return message


def _id_of(message: object) -> int | None:
    value = message.get("id") if type(message) is dict else None
    return value if _is_id(value) else None


def refusal(message: object, exc: ChannelRefusal) -> dict:
    """The ``refusal`` message answering ``message``: its id when it had a readable one, else null."""
    return {"type": "refusal", "id": _id_of(message), "code": exc.code, "detail": exc.detail[:1000]}


def _occurrence_ok(value: object) -> bool:
    if type(value) is not str or not _OCCURRENCE.fullmatch(value):
        return False
    try:
        datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _run_request_problem(args: Mapping) -> str | None:
    """The first argument of a run.request that is not in its form, or None (host.v1.json x-run-request)."""
    if type(args["record_kind"]) is not str or args["record_kind"] not in RUN_REQUEST_RECORD_KINDS:
        return "record_kind"
    if type(args["record_id"]) is not str or not _RECORD_ID.fullmatch(args["record_id"]):
        return "record_id"
    if type(args["revision"]) is not int or args["revision"] < 1:
        return "revision"
    if not _occurrence_ok(args["occurrence"]):
        return "occurrence"
    return start_params_problem(args)


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def memory_key_ok(value: object, *, empty: bool = False) -> bool:
    """A key of saved data: 1 to 256 bytes of UTF-8 (0 for a list's prefix) with no control character. A key
    is never a file name: the store keeps it as a row's text."""
    if type(value) is not str or _CONTROL.search(value):
        return False
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return (0 if empty else 1) <= size <= MEMORY_KEY_MAX_BYTES


def _revision_ok(value: object) -> bool:
    return value is None or (type(value) is int and 1 <= value <= MAX_ID)


def _memory_problem(name: str):
    def problem(args: Mapping) -> str | None:
        if "key" in args and not memory_key_ok(args["key"]):
            return "key"
        if "value_b64" in args and not _is_bytes(args["value_b64"]):
            return "value_b64"
        if "if_revision" in args and not _revision_ok(args["if_revision"]):
            return "if_revision"
        if "prefix" in args and not memory_key_ok(args["prefix"], empty=True):
            return "prefix"
        if "after" in args and not (args["after"] is None or memory_key_ok(args["after"])):
            return "after"
        if "limit" in args and not (args["limit"] is None or (type(args["limit"]) is int and 1 <= args["limit"] <= MAX_ID)):
            return "limit"
        return None
    return problem


# Value checks of a call name whose arguments have forms of their own, run once the names are exact.
_ARG_CHECKS: dict[str, Callable[[Mapping], str | None]] = {"run.request": _run_request_problem,
                                                           **{name: _memory_problem(name) for name in MEMORY_CALL_NAMES}}


def _not_offered(handlers: Any, name: str) -> str:
    """The detail of call_not_offered: at the daemon's end, for a memory call, which grant is missing in words."""
    if type(handlers) is _DaemonOffers:
        return handlers.why_not_offered(name) or name
    return name


def answer_call(message: dict, handlers: Mapping[str, Callable[..., Any]]) -> dict:
    """The receiving side's answer to one checked ``call`` message: a ``result`` or a ``refusal``.

    ``handlers`` maps a call name to the plugin function that answers it. A reserved name and a name
    not on the list are refused before ``handlers`` is looked at, so nothing runs; so is a name on the
    list whose arguments are not exactly its own. A handler that raises is ``call_failed``, naming
    only the exception's type.
    """
    name, args = message["name"], message["args"]
    try:
        if name in RESERVED_CALL_NAMES:
            raise ChannelRefusal("call_reserved", f"{name}: {RESERVED_TEXT}")
        if name not in CALL_NAMES:
            raise ChannelRefusal("call_unknown", name)
        if set(args) != set(CALL_NAMES[name]):
            raise ChannelRefusal("call_args_invalid", f"{name} takes: {', '.join(CALL_NAMES[name]) or 'nothing'}")
        problem = _ARG_CHECKS[name](args) if name in _ARG_CHECKS else None
        if problem is not None:
            raise ChannelRefusal("call_args_invalid", f"{name}: {problem}")
        handler = handlers.get(name)
        if not callable(handler):
            raise ChannelRefusal("call_not_offered", _not_offered(handlers, name))
        try:
            value = handler(*(args[arg] for arg in CALL_NAMES[name]))
        except Exception as exc:  # noqa: BLE001 - the plugin's failure is answered typed, never guessed around
            raise ChannelRefusal("call_failed", type(exc).__name__) from None
    except ChannelRefusal as exc:
        return refusal(message, exc)
    return {"type": "result", "id": message["id"], "value": value}


class DaemonEnd:
    """The daemon's side of the channel of one loaded plugin.

    ``core`` is the core service (``CoreService``); its client is taken once, here, for
    ``plugin_id``: that is the caller of every ``core`` message on this channel, set from the
    channel and never read from a message. ``receive`` answers what needs an answer (a ``core``
    request, a host's ``call``, or a refusal of a message it cannot take) and returns ``None`` for the
    rest (``hello``, ``refusal``, ``bye``). The one call the daemon answers is ``run.request``, and only
    when ``runs`` (the daemon's start path: an object whose ``call(method, params, *, origin=...)`` is
    ``DaemonControl.call`` or ``ControlAPI.call``) was given and the core answers that this channel's
    plugin holds ``schedule`` (the ``plugin.permissions`` op, asked at each call, so a revocation counts);
    otherwise it is ``call_not_offered``. The four ``memory.*`` names are answered by ``memory`` (the store the
    daemon built for this plugin and its install identity, given only to a plugin in the person's row
    ``plugin_memory.plugins``) and only while the core answers that the plugin holds ``memory``; otherwise
    ``call_not_offered``, whose words say which of the two grants is missing. Every other host call is ``call_reserved``, ``call_unknown`` or
    ``call_not_offered``, and nothing runs. This end sends no call, so every ``result`` is for an id it
    never called and is refused ``message_invalid``, as at a host.
    """

    def __init__(self, plugin_id: str, core: Any, *, runs: Any = None, memory: Any = None) -> None:
        if type(plugin_id) is not str or not _PLUGIN_ID.fullmatch(plugin_id) or plugin_id == HOST:
            raise ValueError("a plugin host's channel is opened for one plugin id, never for the host")
        self.plugin_id = plugin_id
        self._core = core.client_for(plugin_id)
        self._runs = runs
        if memory is not None and getattr(memory, "plugin_id", None) != plugin_id:
            raise ValueError("a channel is given the store of its own plugin only")
        self._memory = memory

    def host_ended(self) -> None:
        """The daemon saw this channel's host end; a saved-data write that commits after it says so."""
        if self._memory is not None:
            self._memory.host_ended()

    def load(self, plugin_id: str, manifest: bytes, files: Mapping[str, bytes]) -> bytes:
        """The frame of one ``load`` for this channel's plugin; any other plugin id is refused (one channel
        per loaded plugin), and a package that does not fit one load frame is ``load_too_large``."""
        if plugin_id != self.plugin_id:
            raise ValueError("one channel per loaded plugin: a load names the plugin the channel was opened for")
        message = {"type": "load", "plugin_id": plugin_id, "manifest_b64": base64.b64encode(manifest).decode("ascii"),
                   "files": {path: base64.b64encode(data).decode("ascii") for path, data in files.items()}}
        return encode(check_message(message, receiver="host"))

    def receive(self, message: object) -> dict | None:
        try:
            checked = check_message(message, receiver="daemon")
        except ChannelRefusal as exc:
            return refusal(message, exc)
        if checked["type"] == "call":
            return answer_call(checked, _DaemonOffers(self))
        if checked["type"] == "result":
            exc = ChannelRefusal("message_invalid", f"result: id {checked['id']} answers no call the daemon sent")
            return refusal(message, exc)
        if checked["type"] != "core":
            return None
        answer = self._core(base64.b64decode(checked["bytes_b64"], validate=True))
        return {"type": "core", "id": checked["id"], "bytes_b64": base64.b64encode(answer).decode("ascii")}

    def _granted(self, permission: str) -> bool:
        """Whether the core says this channel's plugin holds ``permission`` now (the ``plugin.permissions`` op)."""
        request = canonical({"protocol": PROTOCOL_VERSION, "op": "plugin.permissions",
                             "args": {"plugin_id": self.plugin_id}})
        try:
            answer = parse(self._core(request))
        except Exception:  # noqa: BLE001 - an answer that cannot be read grants nothing
            return False
        result = answer.get("result") if type(answer) is dict and answer.get("ok") is True else None
        granted = result.get("permissions") if type(result) is dict else None
        return type(granted) is list and permission in granted

    def _run_request(self, record_kind: str, record_id: str, revision: int, occurrence: str, tenant: str,
                     engine: str, prompt: str) -> dict:
        """Start one run from a record through ``run.start``'s path. The caller is this channel's plugin; the
        start key is derived here, so the same plugin, record, revision and occurrence
        start at most one run for one tenant; the same occurrence for another tenant is another run."""
        origin = {"kind": RUN_ORIGIN_KIND, "plugin": self.plugin_id, "record_kind": record_kind,
                  "record_id": record_id, "revision": revision, "occurrence": occurrence}
        key = f"{RUN_ORIGIN_KIND}/{self.plugin_id}/{record_kind}/{record_id}/{revision}/{occurrence}"
        params = {"tenant": tenant, "engine": engine, "prompt": prompt, "client_key": key}
        return self._runs.call("run.start", params, origin=origin)


class _DaemonOffers:
    """The daemon end's call table, looked up by ``answer_call`` only once a call's arguments are in form."""

    def __init__(self, end: DaemonEnd) -> None:
        self._end = end
        self._missing: dict[str, str] = {}

    def get(self, name: str, default: Any = None) -> Any:
        end = self._end
        if name == "run.request" and end._runs is not None and end._granted(RUN_REQUEST_PERMISSION):
            return end._run_request
        if name in MEMORY_CALL_NAMES:
            if not end._granted(MEMORY_PERMISSION):
                self._missing[name] = "permission"
            elif end._memory is None:
                self._missing[name] = "row"
            else:
                return {"memory.put": end._memory.put, "memory.get": end._memory.get,
                        "memory.list": end._memory.list_keys, "memory.delete": end._memory.delete}[name]
        return default

    def why_not_offered(self, name: str) -> str | None:
        missing = self._missing.get(name)
        return None if missing is None else memory_words.not_offered_detail(missing, self._end.plugin_id, name)


class HostEnd:
    """The host's side of one channel.

    ``handlers`` maps a call name to the plugin function that answers it. ``call`` makes a call the host
    sends the daemon (a broker handle by name, once one is built) and keeps its id open; ``receive``
    answers a daemon ``call`` through ``answer_call`` and takes a ``result`` only for an id it has open:
    a result for an id this host never called, or one already answered, is refused. What an open call
    got back (its ``result`` or ``refusal``) is kept in ``answers`` under its id.
    """

    def __init__(self, handlers: Mapping[str, Callable[..., Any]]) -> None:
        self._handlers, self._open, self._next_id = handlers, set(), 0
        self.answers: dict[int, dict] = {}

    def call(self, name: str, args: Mapping[str, Any] | None = None) -> dict:
        message = {"type": "call", "id": self._next_id, "name": name, "args": dict(args or {})}
        self._open.add(self._next_id)
        self._next_id += 1
        return message

    def receive(self, message: object) -> dict | None:
        try:
            checked = check_message(message, receiver="host")
            if checked["type"] == "call":
                return answer_call(checked, self._handlers)
            if checked["type"] == "result" and checked["id"] not in self._open:
                raise ChannelRefusal("message_invalid", f"result: id {checked['id']} answers no call this host sent")
        except ChannelRefusal as exc:
            return refusal(message, exc)
        if checked["type"] in ("result", "refusal") and checked["id"] in self._open:
            self._open.discard(checked["id"])
            self.answers[checked["id"]] = checked
        return None
