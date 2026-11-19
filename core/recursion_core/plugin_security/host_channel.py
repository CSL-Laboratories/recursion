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
* ``answer_call(message, handlers)``: the host's side of a ``call``. A reserved name is refused
  ``call_reserved`` and an unknown one ``call_unknown`` before any handler is looked up, so nothing
  runs; a name on the list whose arguments are not exactly its own is refused ``call_args_invalid``.
* ``DaemonEnd``: the daemon's side of one channel. The caller of every ``core`` message is the
  plugin the channel was opened for, bound once from ``CoreService.client_for``; nothing in a
  message can name another, and a ``core`` message that tries is refused ``caller_forged`` before
  the core sees it. The bytes go to the core's one dispatcher unchanged; every ``csl-core/v0`` op
  stays the core's.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any, Callable, Mapping

from recursion_core.control_transport.errors import FrameError
from recursion_core.control_transport.framing import decode_body, encode_frame

from .protocol import HOST

__all__ = [
    "CALL_NAMES", "CHANNEL", "INVENTORY", "MESSAGE_TYPES", "RESERVED_CALL_NAMES", "RESERVED_MESSAGE_TYPES",
    "RESERVED_RUN_ORIGINS", "RESERVED_TEXT", "REFUSAL_CODES", "ChannelRefusal", "DaemonEnd", "answer_call",
    "check_message", "decode", "encode", "refusal",
]

CHANNEL = "csl-plugin-host/1"
RESERVED_TEXT = "reserved; not available in this release"

# type -> (the sides that may send it, its exact fields). host.v1.json x-message-types.
MESSAGE_TYPES: dict[str, tuple[frozenset, frozenset]] = {
    "hello": (frozenset({"daemon", "host"}), frozenset({"type", "channel"})),
    "load": (frozenset({"daemon"}), frozenset({"type", "plugin_id", "manifest_b64", "files"})),
    "call": (frozenset({"daemon"}), frozenset({"type", "id", "name", "args"})),
    "core": (frozenset({"daemon", "host"}), frozenset({"type", "id", "bytes_b64"})),
    "result": (frozenset({"host"}), frozenset({"type", "id", "value"})),
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
}
# Reserved names (host.v1.json x-reserved): answered as reserved, never built here.
RESERVED_CALL_NAMES = ("run.request", "computer.open", "computer.act", "computer.observe", "computer.close")
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
                 "call_unknown", "call_reserved", "call_args_invalid", "call_not_offered", "call_failed")

SIDES = ("daemon", "host")
MAX_ID = 2 ** 53 - 1
_TYPE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_CALL_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_ARG_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PLUGIN_ID = re.compile(r"[a-z][a-z0-9_.-]{1,63}")
_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*")
# Fields a core message might use to name a caller. None is ever read: naming one is a forgery.
_CALLER_FIELDS = frozenset({"caller", "plugin_id", "principal", "plugin", "owner"})


class ChannelRefusal(Exception):
    """A typed refusal on the channel: ``code`` is one of ``REFUSAL_CODES``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def encode(message: Mapping[str, Any]) -> bytes:
    """One message as one frame of ``control_transport/framing.py``."""
    return encode_frame(dict(message))


def decode(body: bytes) -> dict[str, Any]:
    """One frame body as a message object; a body that is not one JSON object is ``message_invalid``."""
    try:
        return decode_body(body)
    except FrameError as exc:
        raise ChannelRefusal("message_invalid", str(exc)) from None


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
    (``message_unknown``); a type this receiver's peer never sends (``message_wrong_side``); then
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


def answer_call(message: dict, handlers: Mapping[str, Callable[..., Any]]) -> dict:
    """The host's answer to one checked ``call`` message: a ``result`` or a ``refusal``.

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
    """The daemon's side of one plugin host's channel.

    ``core`` is the core service (``CoreService``); its client is taken once, here, for
    ``plugin_id``: that is the caller of every ``core`` message on this channel, set from the
    channel and never read from a message. ``receive`` answers what needs an answer (a ``core``
    request, or a refusal of a message it cannot take) and returns ``None`` for the rest
    (``hello``, ``result``, ``refusal``, ``bye``), which the code that sent the call reads itself.
    """

    def __init__(self, plugin_id: str, core: Any) -> None:
        if type(plugin_id) is not str or not _PLUGIN_ID.fullmatch(plugin_id) or plugin_id == HOST:
            raise ValueError("a plugin host's channel is opened for one plugin id, never for the host")
        self.plugin_id = plugin_id
        self._core = core.client_for(plugin_id)

    def receive(self, message: object) -> dict | None:
        try:
            checked = check_message(message, receiver="daemon")
        except ChannelRefusal as exc:
            return refusal(message, exc)
        if checked["type"] != "core":
            return None
        answer = self._core(base64.b64decode(checked["bytes_b64"], validate=True))
        return {"type": "core", "id": checked["id"], "bytes_b64": base64.b64encode(answer).decode("ascii")}
