"""The record of revoked devices and of their connections, kept on the daemon's machine across a restart.

``DeviceBook`` (``device_state``) takes an optional store of revocations, anything with the three methods of
``RevocationStore``: ``load``, ``put`` and ``forget``. The book stays pure: it is tested with a stand-in, and
``FileRevocationStore`` is the one store of this package that writes a file.

``FileRevocationStore`` keeps one file, ``revocations.json`` in the relay's folder under the daemon's state
folder (``relay_store.relay_folder``: owner-only, mode 0700). It writes the way the tree's credential store
writes, through the same code (``control.credentials``): every change is a read-modify-write under the
exclusive ``flock`` of ``owner_file_lock``, and the new file is written beside the old one, synced, renamed over
it and its folder synced (``replace_file``), so the file holds every change or none of it. A call returns only
when its change is on disk; a write that cannot be made is a ``RevocationStoreError`` that says what to do,
never a bare ``OSError``.

The file holds, for each revoked device, its id and the wall-clock time of its revocation, and for each
connection a revocation named: its id, its device and the wall-clock time the door's disconnect of it came
(``None``: not yet). A connection id is kept as what the door gave: a ``str``, an ``int`` or a tuple of them.
"""

from __future__ import annotations

import json
import math
import os
import stat
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from ..control.credentials import CredentialStoreError, owner_file_lock, replace_file, sweep_temps
from .relay_store import RelayStateRefused, relay_folder

REVOCATIONS_FILE = "revocations.json"   # <state folder>/relay/revocations.json
REVOCATIONS_VERSION = 1                 # a figure of the file's format, not a setting
# How long a writer waits for the file's lock before its write is refused, in seconds. The one place the
# first value is named: it becomes the row remote.device.revocations_lock_timeout_s of the person's settings
# file when that file is in this tree, an argument of FileRevocationStore still winning.
REVOCATIONS_LOCK_TIMEOUT_S: float = 10.0


class RevocationStoreError(Exception):
    """The record of revocations cannot be read or written. ``str()`` says which file, why, and what to do."""


@dataclass(frozen=True)
class StoredConnection:
    """A connection a revocation named: whose it was, and when the door's disconnect came (wall clock)."""

    device_id: str
    released_wall: float | None = None


@dataclass(frozen=True)
class Revocations:
    """What a store holds, or a change to it: revoked devices (id -> wall time of the revocation) and the
    connections the revocations named (id -> ``StoredConnection``). ``boot`` is the id the book drew at its
    last start (``None`` in a change that does not name one)."""

    devices: Mapping[str, float] = field(default_factory=dict)
    connections: Mapping[Hashable, StoredConnection] = field(default_factory=dict)
    boot: str | None = None


class RevocationStore(Protocol):
    def load(self) -> Revocations:
        """Everything the store holds. Raises ``RevocationStoreError`` when it cannot be read."""

    def put(self, change: Revocations) -> None:
        """Add or replace the devices and connections of ``change`` (and its boot, when it names one); returns
        only when the change is durable. Raises ``RevocationStoreError`` when it is not."""

    def forget(self, conn_ids: Iterable[Hashable]) -> None:
        """Drop the records of these connections; returns only when that is durable."""


def storable_id(conn_id: object) -> bool:
    """Whether a connection id can be kept in the file: a ``str``, an ``int`` (not a ``bool``), or a tuple of them."""
    if type(conn_id) in (str, int):
        return True
    return type(conn_id) is tuple and all(storable_id(part) for part in conn_id)


def _encode_id(conn_id: Hashable) -> object:
    if type(conn_id) is str:
        return ["s", conn_id]
    if type(conn_id) is int:
        return ["i", conn_id]
    return ["t", [_encode_id(part) for part in conn_id]]  # type: ignore[union-attr]


def _decode_id(value: object, depth: int = 0) -> Hashable:
    if depth > 32 or type(value) is not list or len(value) != 2:
        raise ValueError("a connection id of an unknown shape")
    tag, inner = value
    if tag == "s" and type(inner) is str:
        return inner
    if tag == "i" and type(inner) is int:
        return inner
    if tag == "t" and type(inner) is list:
        return tuple(_decode_id(part, depth + 1) for part in inner)
    raise ValueError("a connection id of an unknown shape")


def _wall(value: object, optional: bool) -> bool:
    if value is None:
        return optional
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class FileRevocationStore:
    """``revocations.json`` in ``<state_dir>/relay``. See the module text."""

    def __init__(self, state_dir: str, *, lock_timeout_s: float = REVOCATIONS_LOCK_TIMEOUT_S) -> None:
        if isinstance(lock_timeout_s, bool) or not isinstance(lock_timeout_s, (int, float)) \
                or not 0 < lock_timeout_s < float("inf"):
            raise RevocationStoreError("setting_invalid: remote.device.revocations_lock_timeout_s is a number of "
                                       "seconds above 0")
        try:
            folder = relay_folder(state_dir)
        except (RelayStateRefused, OSError) as exc:
            raise RevocationStoreError(f"the folder of the record of revoked phones under {state_dir} cannot be "
                                       f"used ({exc}): it must be a folder of the daemon's user, mode 0700") from None
        self.path = os.path.join(folder, REVOCATIONS_FILE)
        self._lock_path = os.path.join(folder, f".{REVOCATIONS_FILE}.lock")
        self._timeout = float(lock_timeout_s)
        self._uid = os.geteuid()

    # -- reading ------------------------------------------------------------------------------------
    def _refused(self, why: str) -> RevocationStoreError:
        return RevocationStoreError(f"{self.path}: {why}; the file must be readable by the daemon's user, or be "
                                    f"restored from a copy")

    def _read(self) -> dict:
        """The file's document, checked. A missing file is an empty record (the first start)."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        except FileNotFoundError:
            return {"version": REVOCATIONS_VERSION, "boot": None, "devices": {}, "connections": []}
        except OSError as exc:
            raise self._refused(f"it cannot be opened ({type(exc).__name__})") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != self._uid or stat.S_IMODE(st.st_mode) & 0o077:
                raise self._refused("it is not an owner-only file of the daemon's user")
            with os.fdopen(fd, "rb") as handle:
                fd = None
                raw = handle.read()
        except OSError as exc:
            raise self._refused(f"it cannot be read ({type(exc).__name__})") from None
        finally:
            if fd is not None:
                os.close(fd)
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise self._refused("it is not JSON") from None
        if type(doc) is not dict or set(doc) != {"version", "boot", "devices", "connections"} \
                or type(doc["version"]) is not int or doc["version"] != REVOCATIONS_VERSION \
                or not (doc["boot"] is None or type(doc["boot"]) is str) \
                or type(doc["devices"]) is not dict or type(doc["connections"]) is not list:
            raise self._refused("it has an unknown shape")
        for device_id, at in doc["devices"].items():
            if not device_id or not _wall(at, False):
                raise self._refused("it holds a revoked device of an unknown shape")
        for entry in doc["connections"]:
            if type(entry) is not dict or set(entry) != {"id", "device", "released"} \
                    or type(entry["device"]) is not str or entry["device"] not in doc["devices"] \
                    or not _wall(entry["released"], True):
                raise self._refused("it holds a revoked connection of an unknown shape")
            try:
                _decode_id(entry["id"])
            except (ValueError, RecursionError):
                raise self._refused("it holds a connection id of an unknown shape") from None
        return doc

    def load(self) -> Revocations:
        try:
            with owner_file_lock(self._lock_path, self._uid, self._timeout):
                sweep_temps(self.path)
                doc = self._read()
        except CredentialStoreError as exc:
            raise self._refused(str(exc)) from None
        connections = {_decode_id(e["id"]): StoredConnection(e["device"], e["released"]) for e in doc["connections"]}
        return Revocations(dict(doc["devices"]), connections, doc["boot"])

    # -- writing ------------------------------------------------------------------------------------
    def _change(self, change: Revocations | None, forget: Iterable[Hashable]) -> None:
        gone = set(forget)
        try:
            with owner_file_lock(self._lock_path, self._uid, self._timeout):
                doc = self._read()
                kept = {_decode_id(e["id"]): e for e in doc["connections"]}
                for conn_id in gone:
                    kept.pop(conn_id, None)
                if change is not None:
                    doc["devices"].update({d: float(at) for d, at in change.devices.items()})
                    for conn_id, record in change.connections.items():
                        kept[conn_id] = {"id": _encode_id(conn_id), "device": record.device_id,
                                         "released": record.released_wall}
                    if change.boot is not None:
                        doc["boot"] = change.boot
                doc["connections"] = list(kept.values())
                data = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
                replace_file(self.path, data)
        except RevocationStoreError:
            raise
        except CredentialStoreError as exc:
            raise RevocationStoreError(f"{self.path}: the record of revoked phones could not be written ({exc}); "
                                       f"the folder must be writable by the daemon's user") from None

    def put(self, change: Revocations) -> None:
        for conn_id, record in change.connections.items():
            if record.device_id not in change.devices:
                raise RevocationStoreError("a revoked connection is written with its device's revocation")
            if not storable_id(conn_id):
                raise RevocationStoreError(f"the connection id {conn_id!r} cannot be kept: a str, an int or a "
                                           f"tuple of them")
        self._change(change, ())

    def forget(self, conn_ids: Iterable[Hashable]) -> None:
        self._change(None, conn_ids)
