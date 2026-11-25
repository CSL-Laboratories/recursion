"""The one channel between the daemon and a plugin host (``csl-plugin-host/1``).

The schema is ``schemas/plugin/v1/host.v1.json``: it names every message (``hello``, ``load``,
``call``, ``core``, ``result``, ``refusal``, ``bye``), holds the ONE append-only list of call names,
the reserved names and the refusal codes. This module is the code side of that schema: the names
below are the schema's, and a test holds them equal to it. Frames are those of
``control_transport/framing.py``, reused as they are: ``encode``/``decode`` here only wrap its
``encode_frame``/``decode_body``.

What this module settles (the host program uses this channel; the wall and broker handles are later work):

* ``check_message(message, receiver=...)``: a message is one object of a known type with exactly
  its required and permitted optional fields, sent by a side that may send it. An unknown type is refused
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
import json
import re
from typing import Any, Callable, Mapping

from recursion_core.control_transport.errors import FrameError
from recursion_core.control_transport.framing import MAX_FRAME_BYTES, decode_body, encode_frame

from .limits import ARTIFACT, MAX_ARTIFACT_BYTES, MAX_MANIFEST_BYTES
from .protocol import HOST

__all__ = [
    "CALL_NAMES", "CHANNEL", "INVENTORY", "MAX_FRAME_BYTES", "MAX_LOAD_FRAME_BYTES", "MESSAGE_TYPES",
    "RESERVED_CALL_NAMES", "RESERVED_MESSAGE_TYPES", "RESERVED_RUN_ORIGINS", "RESERVED_TEXT", "REFUSAL_CODES",
    "ChannelRefusal", "DaemonEnd", "HostEnd", "answer_call", "check_message", "decode", "encode", "frame_limit",
    "refusal",
]

CHANNEL = "csl-plugin-host/1"
RESERVED_TEXT = "reserved; not available in this release"

# type -> (the sides that may send it, its required fields). host.v1.json x-message-types.
MESSAGE_TYPES: dict[str, tuple[frozenset, frozenset]] = {
    "hello": (frozenset({"daemon", "host"}), frozenset({"type", "channel"})),
    "load": (frozenset({"daemon"}), frozenset({"type", "plugin_id", "manifest_b64", "files"})),
    "call": (frozenset({"daemon", "host"}), frozenset({"type", "id", "name", "args"})),
    "core": (frozenset({"daemon", "host"}), frozenset({"type", "id", "bytes_b64"})),
    "result": (frozenset({"daemon", "host"}), frozenset({"type", "id", "value"})),
    "refusal": (frozenset({"daemon", "host"}), frozenset({"type", "id", "code", "detail"})),
    "bye": (frozenset({"daemon", "host"}), frozenset({"type", "reason"})),
}
# (message type, sender) -> fields that sender may add. host.v1.json x-message-types.optional_from.
OPTIONAL_MESSAGE_FIELDS = {("hello", "daemon"): frozenset({"gil_check"}), ("hello", "host"): frozenset({"wall"})}

# The ONE append-only list of call names, each with the arguments it takes, in order. host.v1.json
# x-call-names. A later surface kind is a new name here, never a new message type.
CALL_NAMES: dict[str, tuple[str, ...]] = {
    "registry": (),
    "operator": ("command",),
    "surfaces": (),
    "surface": ("kind", "name"),
}
# Reserved names (host.v1.json x-reserved): answered as reserved, never built here. When built, each
# reserved call name is sent by a plugin's host to the daemon.
RESERVED_CALL_NAMES = ("run.request", "computer.open", "computer.act", "computer.observe", "computer.close",
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
                 "host_gil_enabled", "host_start_refused", "plugin_wall_unproven", "load_too_large")

SIDES = ("daemon", "host")
MAX_ID = 2 ** 53 - 1
_TYPE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_CALL_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_ARG_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PLUGIN_ID = re.compile(r"[a-z][a-z0-9_.-]{1,63}")
_PLUGIN_ID_MAX = 64
_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*")
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
        if "gil_check" in message and message["gil_check"] not in ("on", "off"):
            raise bad("gil_check")
        if "wall" in message and type(message["wall"]) is not dict:
            raise bad("wall")
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
    its required and permitted optional fields, a core message that names a caller (``caller_forged``) first among them,
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
    optional = OPTIONAL_MESSAGE_FIELDS.get((kind, sender), frozenset())
    if not fields <= message.keys() <= (fields | optional):
        detail = f"{kind}: fields must be {', '.join(sorted(fields))}"
        if optional:
            detail += f" (optional: {', '.join(sorted(optional))})"
        raise ChannelRefusal("message_invalid", detail)
    _check_fields(kind, message)
    return message


def _id_of(message: object) -> int | None:
    value = message.get("id") if type(message) is dict else None
    return value if _is_id(value) else None


def refusal(message: object, exc: ChannelRefusal) -> dict:
    """The ``refusal`` message answering ``message``: its id when it had a readable one, else null."""
    return {"type": "refusal", "id": _id_of(message), "code": exc.code, "detail": exc.detail[:1000]}


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
        handler = handlers.get(name)
        if not callable(handler):
            raise ChannelRefusal("call_not_offered", name)
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
    rest (``hello``, ``refusal``, ``bye``). The daemon offers no call of its own in this release: a
    host's call is ``call_reserved``, ``call_unknown`` or ``call_not_offered``, and nothing runs. This
    end sends no call either, so every ``result`` is for an id it never called and is refused
    ``message_invalid``, as at a host.
    """

    def __init__(self, plugin_id: str, core: Any) -> None:
        if type(plugin_id) is not str or not _PLUGIN_ID.fullmatch(plugin_id) or plugin_id == HOST:
            raise ValueError("a plugin host's channel is opened for one plugin id, never for the host")
        self.plugin_id = plugin_id
        self._core = core.client_for(plugin_id)

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
            return answer_call(checked, {})
        if checked["type"] == "result":
            exc = ChannelRefusal("message_invalid", f"result: id {checked['id']} answers no call the daemon sent")
            return refusal(message, exc)
        if checked["type"] != "core":
            return None
        answer = self._core(base64.b64decode(checked["bytes_b64"], validate=True))
        return {"type": "core", "id": checked["id"], "bytes_b64": base64.b64encode(answer).decode("ascii")}


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
