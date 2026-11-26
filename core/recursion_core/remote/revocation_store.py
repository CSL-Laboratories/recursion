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
(``None``: not yet). A connection id is kept as what the door gave: a ``str``, an ``int`` of at most
``ID_INT_DIGITS_MAX`` digits, or a tuple of them at most ``ID_DEPTH_MAX`` deep.

ONE RULE holds for every key and every value the file holds: written with the file's own encoding and read back
with its own decoding, it comes back IDENTICAL, equal, of the same types all the way down and with the same code
points (``comes_back_identical``). JSON's decoder reads two lone surrogates in a row back as the one character
they would form, so a device id made of them would come back as another id: such an id is refused.
``storable_id`` asks the rule of a connection id as the file encodes it; the book asks ``storable_id`` at connect,
and ``comes_back_identical`` of a device id at add, connect, admit and revoke, so no id reaches a write unproven.

Nothing the next start cannot read is written. A write checks every row of its change with the checks a read
makes; a row that fails them (a time that is not a wall-clock time, an id that does not come back the same) is
left out of the write and named, never the rest: a device's revocation is never held back by one of its
connection records. The bytes are then read back through the same checks before they replace the file, and the
record read back must be the record meant, identical, or nothing is written. A write that left rows out raises
``RevocationPartlyWritten`` once the rest is on disk.

A file an earlier writer left may hold an id written as two lone surrogates in a row. It is read as the character
they form, and that id stays revoked; the two-surrogate id itself is refused at every door of the book, so
neither form of it pairs again. Two rows such a file holds for what reads back as one connection id are kept as
the one that holds longer (a connection not yet released before one released, else the later release), so no
revocation of an id is lost where they meet.

A missing file is an empty record only while no revocation was ever written in that folder. The first write
whose record holds a revoked device then writes a mark beside the file, ``.revocations.json.recorded``, all or
nothing (``replace_file``), under the same lock; a folder without the mark whose record holds a revocation
(written before the mark existed, or stopped between the two writes) gets it at the next write, which every
start makes. While the mark is there, a missing file is refused like an unreadable one, so
deleting the file never lets a revoked phone pair again. ``forget-every-revocation`` (``main``) empties the
record and removes the mark, the one knowing way to start a new record.

An OLDER copy of the file put back is refused too. Beside the mark the same step writes a count,
``.revocations.json.count``: how many revoked devices the record held at its last write. A device's revocation
is never taken out of the record but by the forget command, so a record that holds fewer devices than the count
says is a copy from before the last revocation: a phone revoked after it would be let in again, so it is refused
like an unreadable file, with words that say how many revocations are missing and the two ways on (put back the
LATEST copy, or start knowingly with fewer by the forget command). The count is written after the record, so a
stop between the two leaves a count that is lower, which is read and caught up at the next write; a folder
written before the count existed gets it at the next write, which every start makes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import sys
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
REVOCATIONS_MARK = f".{REVOCATIONS_FILE}.recorded"   # beside the file: revocations were written in it
MARK_TEXT = (f"Revocations were recorded in {REVOCATIONS_FILE} of this folder. While this file is here, a missing "
             f"{REVOCATIONS_FILE} refuses every phone.\n").encode("utf-8")
REVOCATIONS_COUNT = f".{REVOCATIONS_FILE}.count"   # beside the file: how many revocations it held at its last write


def count_text(count: int) -> bytes:
    """The bytes of the count beside the record: the count first, then what it means."""
    return (f"{count} revocations were recorded in {REVOCATIONS_FILE} of this folder; a copy of it that holds fewer "
            f"is refused.\n").encode("utf-8")

COMMAND_FORGET = "forget-every-revocation"
# The most digits of a connection id that is a whole number: a figure of the file's format, not a setting. It is
# CPython's sys.int_info.str_digits_check_threshold, the lowest limit that sys.set_int_max_str_digits,
# PYTHONINTMAXSTRDIGITS or -X int_max_str_digits can put on turning a whole number into text and back, so an id
# written under one setting of that limit is read back under any other.
ID_INT_DIGITS_MAX = 640
# How deep the tuples of a connection id may nest: a figure of the file's format, not a setting, the one figure
# the write and the read of an id share.
ID_DEPTH_MAX = 32
_INT_BOUND = 10 ** ID_INT_DIGITS_MAX


class RevocationStoreError(Exception):
    """The record of revocations cannot be read or written. ``str()`` says which file, why, and what to do."""


class RevocationPartlyWritten(RevocationStoreError):
    """A change is on disk but for some of its rows, which could not be written so that the next start reads them
    back: ``devices`` (revocations not on disk) and ``connections`` (connection records not on disk). ``str()``
    says which, why, and what to do."""

    def __init__(self, words: str, devices: frozenset, connections: frozenset) -> None:
        super().__init__(words)
        self.devices = devices
        self.connections = connections


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


def _dumps(doc: object) -> bytes:
    """The store's one encoding: of the file, and of the round trip that proves a connection id."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _identical(back: object, value: object, floor: bool) -> bool:
    """Whether ``back`` is ``value`` again: equal, of the same types all the way down, the same code points; with
    ``floor``, every whole number of at most ``ID_INT_DIGITS_MAX`` digits."""
    if type(back) is not type(value):
        return False
    if type(value) is list:
        return len(back) == len(value) and all(_identical(b, v, floor) for b, v in zip(back, value))  # type: ignore[arg-type]
    if type(value) is dict:
        return len(back) == len(value) and all(  # type: ignore[arg-type]
            type(k) is str and k in back and _identical(back[k], v, floor) for k, v in value.items())  # type: ignore[index, operator]
    if type(value) is float:
        return back.hex() == value.hex()  # type: ignore[attr-defined]
    if type(value) is int and floor and not -_INT_BOUND < value < _INT_BOUND:
        return False
    return back == value


def comes_back_identical(value: object) -> bool:
    """The one rule of every key and every value the record writes: written with the record's own encoding and read
    back with its own decoding, ``value`` comes back IDENTICAL: equal, of the same types all the way down (a tuple
    comes back a list, a subclass of ``str`` a ``str``, a whole-number key a ``str`` key: none is identical), with
    the same code points (two lone surrogates in a row come back as the one character they form: not identical),
    and every whole number of at most ``ID_INT_DIGITS_MAX`` digits, so that it reads back under any setting of the
    interpreter's limit. Anything the encoding or the decoding refuses is not identical either."""
    try:
        return _identical(json.loads(_dumps(value)), value, True)
    except (ValueError, TypeError, RecursionError, OverflowError, MemoryError):
        return False


def shown_id(device_id: object) -> str:
    """An id as words show it: as it is when every character of it can be printed, else as escapes."""
    return device_id if type(device_id) is str and device_id.isprintable() else ascii(device_id)


def _same(back: object, conn_id: object) -> bool:
    if type(back) is not type(conn_id):
        return False
    if type(conn_id) is tuple:
        return len(back) == len(conn_id) and all(_same(b, c) for b, c in zip(back, conn_id))  # type: ignore[arg-type]
    return back == conn_id


def storable_id(conn_id: object) -> bool:
    """Whether a connection id can be kept in the file: its encoding in the file comes back identical
    (``comes_back_identical``) and decodes to the same id, of the same types. So a ``str`` that comes back the
    same, an ``int`` (not a ``bool``) of at most ``ID_INT_DIGITS_MAX`` digits, or a tuple of them at most
    ``ID_DEPTH_MAX`` deep."""
    try:
        encoded = _encode_id(conn_id)  # type: ignore[arg-type]
        return comes_back_identical(encoded) and _same(_decode_id(encoded), conn_id)
    except (ValueError, TypeError, RecursionError, OverflowError, MemoryError):
        return False


def _encode_id(conn_id: Hashable, depth: int = 0) -> object:
    if depth > ID_DEPTH_MAX:
        raise ValueError(f"a connection id nested more than {ID_DEPTH_MAX} deep")
    if type(conn_id) is str:
        return ["s", conn_id]
    if type(conn_id) is int:
        if not -_INT_BOUND < conn_id < _INT_BOUND:
            raise ValueError(f"a connection id of more than {ID_INT_DIGITS_MAX} digits")
        return ["i", conn_id]
    if type(conn_id) is tuple:
        return ["t", [_encode_id(part, depth + 1) for part in conn_id]]
    raise ValueError("a connection id that is not a str, an int or a tuple of them")


def _decode_id(value: object, depth: int = 0) -> Hashable:
    if depth > ID_DEPTH_MAX or type(value) is not list or len(value) != 2:
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
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:   # a whole number too large for a float: not a time
        return False


class _Unreadable(ValueError):
    """Why bytes are not a record this store reads: the words of the refusal."""


def _device_fault(device_id: object, at: object) -> str | None:
    if type(device_id) is not str or not device_id or not _wall(at, False):
        return "it holds a revoked device of an unknown shape"
    return None


def _device_row_fault(device_id: object, at: object) -> str | None:
    """What is wrong with a device's row for a write, by the read's own checks and the one rule: ``"id"``,
    ``"time"``, or ``None``."""
    if type(device_id) is not str or not device_id or not comes_back_identical({device_id: 0.0}):
        return "id"
    if _device_fault(device_id, at) is not None or not comes_back_identical({device_id: at}):
        return "time"
    return None


def _writable_device(device_id: object, at: object) -> bool:
    """The check a write makes on a device's row before it is written: the read's own, and the row comes back
    identical."""
    return _device_row_fault(device_id, at) is None


def _connection_fault(entry: object, devices: Mapping) -> str | None:
    if type(entry) is not dict or set(entry) != {"id", "device", "released"} \
            or type(entry["device"]) is not str or entry["device"] not in devices \
            or not _wall(entry["released"], True):
        return "it holds a revoked connection of an unknown shape"
    try:
        _decode_id(entry["id"])
    except (ValueError, RecursionError):
        return "it holds a connection id of an unknown shape"
    return None


def _holds_longer(entry: dict, held: dict) -> bool:
    """Whether a connection row holds its id longer than another row of the same id: not yet released before
    released, else the later release."""
    if held["released"] is None:
        return False
    return entry["released"] is None or entry["released"] > held["released"]


def _connection_rows(entries: list) -> dict:
    """The connection rows of a checked record by their id. Two rows for what reads back as one id (an earlier
    writer wrote them apart) are kept as the one that holds longer, so no revocation of that id is lost."""
    rows: dict = {}
    for entry in entries:
        conn_id = _decode_id(entry["id"])
        if conn_id not in rows or _holds_longer(entry, rows[conn_id]):
            rows[conn_id] = entry
    return rows


def _document(raw: bytes) -> dict:
    """The record in ``raw``, checked; else ``_Unreadable``. The one set of checks a read and a write make."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _Unreadable("it is not JSON") from None
    if type(doc) is not dict or set(doc) != {"version", "boot", "devices", "connections"} \
            or type(doc["version"]) is not int or doc["version"] != REVOCATIONS_VERSION \
            or not (doc["boot"] is None or type(doc["boot"]) is str) \
            or type(doc["devices"]) is not dict or type(doc["connections"]) is not list:
        raise _Unreadable("it has an unknown shape")
    for device_id, at in doc["devices"].items():
        fault = _device_fault(device_id, at)
        if fault is not None:
            raise _Unreadable(fault)
    for entry in doc["connections"]:
        fault = _connection_fault(entry, doc["devices"])
        if fault is not None:
            raise _Unreadable(fault)
    return doc


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
        self._mark_path = os.path.join(folder, REVOCATIONS_MARK)
        self._count_path = os.path.join(folder, REVOCATIONS_COUNT)
        self._state_dir = state_dir
        self._timeout = float(lock_timeout_s)
        self._uid = os.geteuid()

    # -- reading ------------------------------------------------------------------------------------
    def _refused(self, why: str) -> RevocationStoreError:
        return RevocationStoreError(f"{self.path}: {why}; the file must be readable by the daemon's user, or be "
                                    f"restored from its LATEST copy (an older copy would let every phone revoked "
                                    f"after it pair again; deleting it lets no phone in: a missing record is "
                                    f"refused too)")

    def _marked(self) -> bool:
        """Whether the mark is there. Anything in its place that cannot be looked at counts as there."""
        try:
            os.lstat(self._mark_path)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    def _missing(self) -> RevocationStoreError:
        command = f"python -m recursion_core.remote.revocation_store --state-dir {self._state_dir} {COMMAND_FORGET}"
        return RevocationStoreError(
            f"{self.path} is missing, but revocations were recorded in it ({self._mark_path} says so). Put the "
            f"file back from a copy, the LATEST one: an older copy would let every phone revoked after it pair "
            f"again; or, to start a new record knowingly, stop the daemon, run `{command}` (it "
            f"empties the record, so every phone revoked before may pair again), then remove every paired phone "
            f"and pair each one anew")

    def _older(self, held: int, recorded: int) -> RevocationStoreError:
        command = f"python -m recursion_core.remote.revocation_store --state-dir {self._state_dir} {COMMAND_FORGET}"
        missing = recorded - held
        return RevocationStoreError(
            f"{self.path} is OLDER than the last revocation written in it: {missing} of the {recorded} revocations "
            f"written in it {'is' if missing == 1 else 'are'} missing ({self._count_path} counts {recorded}, the "
            f"file holds {held}). A phone revoked after that copy would be let in again, so no phone is let in. "
            f"Next: put back the LATEST copy of the file; or, to start knowingly with fewer, stop the daemon, run "
            f"`{command}` (it empties the record, so every phone revoked before may pair again), then remove every "
            f"paired phone and pair each one anew")

    def _recorded(self) -> int:
        """How many revocations the count beside the record says were written: 0 when it is not there. Anything in
        its place that cannot be read as a count is refused, like the record."""
        where = f"the count beside it, {self._count_path},"
        try:
            fd = os.open(self._count_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise self._refused(f"{where} cannot be opened ({type(exc).__name__})") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != self._uid or stat.S_IMODE(st.st_mode) & 0o077:
                raise self._refused(f"{where} is not an owner-only file of the daemon's user")
            with os.fdopen(fd, "rb") as handle:
                fd = None
                raw = handle.read(len(count_text(0)) + 64)
        except OSError as exc:
            raise self._refused(f"{where} cannot be read ({type(exc).__name__})") from None
        finally:
            if fd is not None:
                os.close(fd)
        head = raw.split(b" ", 1)[0]
        if not (head.isdigit() and head.isascii() and raw == count_text(int(head))):
            raise self._refused(f"{where} does not hold a count of revocations")
        return int(head)

    def _counted(self, doc: dict) -> dict:
        """``doc``, unless it holds fewer revoked devices than the count says were written: then refused."""
        recorded = self._recorded()
        if len(doc["devices"]) < recorded:
            raise self._older(len(doc["devices"]), recorded)
        return doc

    def _read(self) -> dict:
        """The file's document, checked. A missing file is an empty record only when no revocation was ever
        written here (no mark); with the mark it is refused. A record that holds fewer revocations than the count
        beside it is refused."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        except FileNotFoundError:
            if self._marked():
                raise self._missing() from None
            return self._counted({"version": REVOCATIONS_VERSION, "boot": None, "devices": {}, "connections": []})
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
            doc = _document(raw)
        except _Unreadable as exc:
            raise self._refused(str(exc)) from None
        return self._counted(doc)

    def load(self) -> Revocations:
        try:
            with owner_file_lock(self._lock_path, self._uid, self._timeout):
                sweep_temps(self.path)
                doc = self._read()
        except CredentialStoreError as exc:
            raise self._refused(str(exc)) from None
        connections = {conn_id: StoredConnection(e["device"], e["released"])
                       for conn_id, e in _connection_rows(doc["connections"]).items()}
        return Revocations(dict(doc["devices"]), connections, doc["boot"])

    # -- writing ------------------------------------------------------------------------------------
    def _change(self, change: Revocations | None, forget: Iterable[Hashable]) -> None:
        gone = set(forget)
        try:
            with owner_file_lock(self._lock_path, self._uid, self._timeout):
                doc = self._read()
                kept = _connection_rows(doc["connections"])
                for conn_id in gone:
                    kept.pop(conn_id, None)
                left_devices: dict = {}
                left_connections: set = set()
                if change is not None:
                    # Every row is checked as the next start's read will check it; a row that fails is left
                    # out and named, and the rest is written: a revocation never waits on its connections.
                    for device_id, at in change.devices.items():
                        at = float(at) if type(at) is int and _wall(at, False) else at
                        if _writable_device(device_id, at):
                            doc["devices"][device_id] = at
                        else:
                            left_devices[device_id] = _device_row_fault(device_id, at) or "time"
                    for conn_id, record in change.connections.items():
                        entry = {"id": _encode_id(conn_id) if storable_id(conn_id) else None,
                                 "device": record.device_id, "released": record.released_wall}
                        if entry["id"] is None or _connection_fault(entry, doc["devices"]) is not None \
                                or not comes_back_identical(entry):
                            left_connections.add(conn_id)
                        else:
                            kept[conn_id] = entry
                    if change.boot is not None:
                        doc["boot"] = change.boot
                doc["connections"] = list(kept.values())
                try:
                    data = _dumps(doc)
                    # Read back as the next start will read it, before it replaces the file; what it holds must
                    # be the record meant, identical, so no revocation is written under another id.
                    same = _identical(_document(data), doc, False)
                except _Unreadable as exc:
                    raise RevocationStoreError(f"{self.path}: the record of revoked phones was not written: what "
                                               f"would be written could not be read back at the next start ({exc}); "
                                               f"the file is as it was") from None
                except (ValueError, TypeError, RecursionError, OverflowError, MemoryError) as exc:
                    raise RevocationStoreError(f"{self.path}: the record of revoked phones was not written: it could "
                                               f"not be encoded ({type(exc).__name__}); the file is as it was") from None
                if not same:
                    raise RevocationStoreError(f"{self.path}: the record of revoked phones was not written: what would "
                                               f"be written does not read back the same, so a revocation could come back "
                                               f"under another id; the file is as it was")
                replace_file(self.path, data)
                if doc["devices"] and not self._marked():
                    # After the record, so that a write of it is never undone by the mark's. A stop between
                    # the two leaves a record without its mark until the next start's write adds it.
                    replace_file(self._mark_path, MARK_TEXT)
                if doc["devices"] and self._recorded() != len(doc["devices"]):
                    # After the record too: a stop between the two leaves a lower count, caught up next time.
                    replace_file(self._count_path, count_text(len(doc["devices"])))
                if left_devices or left_connections:
                    raise RevocationPartlyWritten(self._partly(left_devices, left_connections),
                                                  frozenset(left_devices), frozenset(left_connections))
        except RevocationStoreError:
            raise
        except CredentialStoreError as exc:
            raise RevocationStoreError(f"{self.path}: the record of revoked phones could not be written ({exc}); "
                                       f"the folder must be writable by the daemon's user") from None

    def _partly(self, devices: dict, connections: set) -> str:
        parts = []
        by_id = sorted(shown_id(d) for d, why in devices.items() if why == "id")
        by_time = sorted(shown_id(d) for d, why in devices.items() if why == "time")
        if by_id:
            parts.append(f"{len(by_id)} revocations were not written ({', '.join(by_id)}): the id of each does not "
                         f"come back the same from the record's encoding (two lone surrogates in a row are read back "
                         f"as the one character they form, which is another id), and no door of the book takes such "
                         f"an id; give each phone an id the pairing door makes")
        if by_time:
            parts.append(f"{len(by_time)} revocations were not written ({', '.join(by_time)}): the time of "
                         f"each is not a wall-clock time (a finite number of seconds, 0 or more), so the machine's "
                         f"clock must be set right")
        if connections:
            parts.append(f"{len(connections)} connection records were not written: each has an id this file cannot "
                         f"write and read back the same, a release time that is not a wall-clock time, or a phone "
                         f"whose revocation was not written")
        return f"{self.path}: " + "; ".join(parts) + "; the rest of the change is on disk"

    def put(self, change: Revocations) -> None:
        """A row that cannot be written is left out and named (``RevocationPartlyWritten``, raised once the rest
        is on disk); see the module text."""
        for record in change.connections.values():
            if record.device_id not in change.devices:
                raise RevocationStoreError("a revoked connection is written with its device's revocation")
        if change.boot is not None and (type(change.boot) is not str or not comes_back_identical(change.boot)):
            raise RevocationStoreError(f"{self.path}: the record of revoked phones was not written: its boot id "
                                       f"{shown_id(change.boot)} does not come back the same from the record's "
                                       f"encoding; the file is as it was")
        self._change(change, ())

    def forget(self, conn_ids: Iterable[Hashable]) -> None:
        self._change(None, conn_ids)

    def forget_every_revocation(self) -> None:
        """Empty the record and remove the mark and the count, whatever the file holds or whether it is there:
        every phone revoked before may then pair again. The one knowing way to start a new record."""
        empty = {"version": REVOCATIONS_VERSION, "boot": None, "devices": {}, "connections": []}
        try:
            with owner_file_lock(self._lock_path, self._uid, self._timeout):
                replace_file(self.path, _dumps(empty))
                for beside in (self._mark_path, self._count_path):
                    try:
                        os.unlink(beside)
                    except FileNotFoundError:
                        pass
        except CredentialStoreError as exc:
            raise RevocationStoreError(f"{self.path}: the record of revoked phones could not be emptied ({exc}); "
                                       f"the folder must be writable by the daemon's user") from None
        except OSError as exc:
            raise RevocationStoreError(f"{self._mark_path} or {self._count_path}: the record is empty but the mark or "
                                       f"the count beside it could not be removed ({type(exc).__name__}); remove "
                                       f"those files by hand") from None


def main(argv: list[str] | None = None) -> int:
    """``python -m recursion_core.remote.revocation_store --state-dir <dir> forget-every-revocation``."""
    parser = argparse.ArgumentParser(
        prog="python -m recursion_core.remote.revocation_store",
        description="The record of revoked phones. forget-every-revocation empties it, so every phone revoked "
                    "before may pair again: run it with the daemon stopped, then remove every paired phone and pair "
                    "each one anew.")
    parser.add_argument("--state-dir", required=True, help="the daemon's state folder")
    parser.add_argument("command", choices=[COMMAND_FORGET])
    args = parser.parse_args(argv)
    try:
        store = FileRevocationStore(args.state_dir)
        store.forget_every_revocation()
    except RevocationStoreError as exc:
        print(f"nothing was forgotten: {exc}", file=sys.stderr)
        return 1
    print(f"the record of revoked phones in {store.path} is now empty: every phone revoked before may pair again; "
          f"remove every paired phone and pair each one anew")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
