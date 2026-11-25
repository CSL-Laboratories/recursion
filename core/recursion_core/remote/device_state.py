"""A paired device's lifecycle as a model (lane REMOTE, first slice): admitted, expired, revoked.

Pure: no token and no file of its own, and no daemon reads it yet. What must outlive the daemon, the record
of revocations, goes to an optional store (``revocation_store``: ``load``, ``put``, ``forget``) that the book is
given; without one the book keeps everything in memory, as before. It fixes the rules the device store and
the phone door of this lane's later slices keep, so that they are tested before either exists:

* A device holds the scopes its pairing granted and never manage. Asking for manage is ``scope_refused``,
  and so is asking for any scope it was not granted.
* A device at or past its expiry, the 30-day cap, or 7 days without an accepted call is refused
  ``device_expired``; a revoked one ``device_revoked``, at once, on its very next request. Revocation
  is final: the same device id is never admitted again, and with a store never paired again either, after a
  restart too (``device_exists``).
* ``revoke`` names every live connection the device holds, so the door closes each one, and keeps each
  as the revoked device's. Until the door's ``disconnect`` of it, a request on such a connection is
  refused ``device_revoked`` (``check_connection``), and no device is admitted on its id: ``connect``
  answers ``connection_exists``, so a connection the door still holds is never counted as another
  device's. Every other request is checked against the device's state at that moment, never against an
  earlier answer.
* After that ``disconnect`` the book keeps the record for ``revoked_connection_keep_s`` seconds on its
  clock, and the same holds while it is kept. ``None`` (the first value, ``REVOKED_CONNECTION_KEEP_S``)
  never forgets: that id is never admitted again in the book's life, nor, with a store, after a restart.
  ``0`` drops it at the disconnect, and the id is then admitted as any new one.
* With a store, ``revoke`` returns only when the device's revocation and every connection it named are
  written and synced, and ``disconnect`` writes the release of such a connection the same way. A book built
  on the same store after a restart refuses as the book before it did. Each record carries the wall-clock
  time of its release beside the book's monotonic clock: at a restart a record is kept while the wall clock
  is before its release plus the keep time; a record whose release had not come when the daemon stopped
  counts as released at the restart (the door's connections ended with the daemon). A wall clock that moved
  back keeps a record longer; one that moved forward may forget it early.
* A store that cannot be read at the start fails closed: ``connect`` and ``add`` refuse everything with
  ``revocations_unreadable`` (read again at each try) until it can be read, because a lost record must never
  admit a revoked id. A revocation whose write fails still holds in memory, and ``revoke`` raises
  ``RevocationNotWritten`` (its ``connections`` are the ones to close), which says the revocation holds until
  the daemon stops but was not written; the next revoke of that device writes it again.
* ``connect`` and ``revoke`` are decided under one lock: a connection is either refused or named by the
  revocation, never admitted and missed. Writes are made outside that lock, one at a time in the order of the
  changes, so ``admit`` and ``check_connection`` never wait on the disk.
* A record the book hands out is a frozen snapshot: changing it is refused, and the book's own state
  changes only under its lock (a revocation replaces the record).
* A wrong argument (no grant, a device id that is not a non-empty ``str``, a subclass of ``str`` included,
  an expiry that is not a finite number, a connection id that cannot be a key, or with a store one it cannot
  keep) is a typed refusal, never a ``TypeError``.

How long a device stays paired is capped by the settled values in ``pairing.py`` (record
20261115-csl238-harness-dr-09-phone-pairing-01): at most 30 days from pairing, and 7 days from
pairing or from the last accepted call, whichever bound comes first. The caller may name an
earlier expiry. A later one is brought back to the 30-day cap. An accepted call is an admission
that is allowed (``admit``, ``connect`` or ``check_connection``). A refused call does not move
the unused bound.
"""

from __future__ import annotations

import dataclasses
import math
import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field

from .pairing import (MANAGE, PAIRING_MAX_AGE_S, PairingGrant, PairingRefused, device_label, device_scopes,
                      pairing_lapsed)
from .revocation_store import RevocationStore, RevocationStoreError, Revocations, StoredConnection, storable_id


# How long the book keeps the record of a revoked device's connection after the door's disconnect of it,
# in seconds on the book's clock: None never forgets (the id is never admitted again in the book's life),
# 0 drops it at the disconnect. Before the disconnect it is always kept. The one place the first value is
# named: it becomes the row remote.device.revoked_connection_keep_s of the person's settings file when that
# file is in this tree, an argument of DeviceBook still winning.
REVOKED_CONNECTION_KEEP_S: float | None = None


@dataclass(frozen=True)
class Admission:
    """One decision. It is not a capability: the next request is decided again."""

    ok: bool
    code: str | None = None
    detail: str = ""


ADMITTED = Admission(True)
# The detail of connection_exists for an id a revoked device's connection still holds.
REVOKED_CONNECTION_HELD = "the id is held by a revoked device's connection: give the new connection a new id"


def unreadable_detail(reason: str) -> str:
    """The words of ``revocations_unreadable``: what, why, and the step."""
    return ("the record of revoked phones could not be read, so no phone is let in until it can be: that way no "
            f"revoked phone is let in by mistake. {reason}")


class RevocationNotWritten(PairingRefused):
    """The device is revoked in this daemon, but the record could not be written. ``connections`` are the
    live connections the caller must still close."""

    def __init__(self, connections: tuple, reason: str) -> None:
        super().__init__("revocation_unwritten",
                         "the phone is revoked until the daemon stops, but the record could not be written, so "
                         f"after a restart it would not be: {reason}. Revoke it again once the folder is writable")
        self.connections = connections


@dataclass(frozen=True)
class DeviceRecord:
    device_id: str
    label: str
    scopes: tuple[str, ...]
    paired_at: float
    expires_at: float
    revoked_at: float | None = None
    last_accepted_at: float | None = None

    def public(self, now: float) -> dict:
        """What a list shows: no secret is held here, and none is shown."""
        if self.revoked_at is not None:
            state = "revoked"
        elif now >= self.expires_at or pairing_lapsed(self.paired_at, self.last_accepted_at, now):
            state = "expired"
        else:
            state = "active"
        return {"device_id": self.device_id, "label": self.label, "scopes": list(self.scopes),
                "paired_at": self.paired_at, "expires_at": self.expires_at, "state": state}


@dataclass(frozen=True)
class _RevokedConnection:
    """A connection ``revoke`` named: whose it was, and when the door's disconnect came (``None``: not yet),
    on the book's clock and on the wall clock."""

    device_id: str
    released_at: float | None = None
    released_wall: float | None = None


def _key(conn_id: object) -> bool:
    try:
        hash(conn_id)
    except TypeError:
        return False
    return True


@dataclass
class DeviceBook:
    """Paired devices and their live connections, in memory, under one lock. ``clock`` is monotonic; ``wall``
    is the wall clock, read only for what is written to ``store``.

    ``revoked_connection_keep_s``: how long the record of a revoked device's connection is kept after the
    door's disconnect of it (``None``: never forgotten; ``0``: dropped at the disconnect).
    ``store``: where revocations are kept across a restart (``None``: in memory only).
    """

    clock: Callable[[], float] = time.monotonic
    revoked_connection_keep_s: float | None = REVOKED_CONNECTION_KEEP_S
    store: RevocationStore | None = None
    wall: Callable[[], float] = time.time
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _write_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _devices: dict[str, DeviceRecord] = field(default_factory=dict, init=False, repr=False)
    _connections: dict[Hashable, str] = field(default_factory=dict, init=False, repr=False)
    _revoked: dict[Hashable, _RevokedConnection] = field(default_factory=dict, init=False, repr=False)
    _revoked_devices: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _unwritten: set = field(default_factory=set, init=False, repr=False)
    _unreadable: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        keep = self.revoked_connection_keep_s
        if keep is not None and (isinstance(keep, bool) or not isinstance(keep, (int, float))
                                 or math.isnan(keep) or keep < 0):
            raise PairingRefused("setting_invalid", "revoked_connection_keep_s is None (never forget) or a "
                                 "number of seconds, 0 or more")
        if self.store is not None:
            with self._write_lock:
                with self._lock:
                    change = self._load()
                self._write(change)

    # -- the store --------------------------------------------------------------------------------
    def _load(self) -> Revocations | None:
        """Called with both locks held: memory takes what the store holds. Returns the releases to write."""
        try:
            held = self.store.load()  # type: ignore[union-attr]
        except RevocationStoreError as exc:
            self._unreadable = unreadable_detail(str(exc))
            return None
        self._unreadable = None
        now, wall = self.clock(), self.wall()
        self._revoked_devices.update(held.devices)
        released = {}
        for conn_id, stored in held.connections.items():
            if stored.released_wall is None:
                # Not released when the daemon stopped: the door's connections ended with it.
                record = _RevokedConnection(stored.device_id, now, wall)
                released[conn_id] = StoredConnection(stored.device_id, wall)
            else:
                # A wall clock that moved back is read as no time passed (kept longer, the safe side).
                gone = max(0.0, wall - stored.released_wall)
                record = _RevokedConnection(stored.device_id, now - gone, stored.released_wall)
            self._revoked[conn_id] = record
        if not released:
            return None
        devices = {r.device_id: self._revoked_devices[r.device_id] for r in released.values()}
        return Revocations(devices, released)

    def _write(self, change: Revocations | None, forget: tuple = ()) -> str | None:
        """Called with the write lock held and the book's lock free. Returns why it failed, else None."""
        if self.store is None:
            return None
        try:
            if change is not None and (change.devices or change.connections):
                self.store.put(change)
            if forget:
                self.store.forget(forget)
        except RevocationStoreError as exc:
            return str(exc)
        return None

    def _readable(self) -> str | None:
        """Called with the book's lock held: ``None`` when the store was read, else the refusal's words. An
        unreadable store is read again here; what it then holds is taken; the releases it finds stay unwritten until a
        revoke of their device writes them (on disk they are kept longer, the safe side)."""
        if self._unreadable is not None:
            self._load()
        return self._unreadable

    def _change_of(self, device_ids) -> Revocations:
        """Called with the book's lock held: these devices' revocations and every connection record they keep."""
        devices = {d: self._revoked_devices[d] for d in device_ids}
        connections = {c: StoredConnection(r.device_id, r.released_wall)
                       for c, r in self._revoked.items() if r.device_id in devices}
        return Revocations(devices, connections)

    # -- devices ----------------------------------------------------------------------------------
    def add(self, grant: PairingGrant, *, expires_at: float) -> DeviceRecord:
        """Record a redeemed offer as a device and return its frozen record. A known id (revoked too) is refused."""
        if not isinstance(grant, PairingGrant):
            raise PairingRefused("grant_invalid", "a device is added from a PairingGrant")
        if type(grant.device_id) is not str or not grant.device_id:
            raise PairingRefused("grant_invalid", "a device id is a non-empty string")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at):
            raise PairingRefused("expiry_invalid", "a device's expiry is a finite number on the book's clock")
        scopes = device_scopes(grant.scopes)
        label = device_label(grant.label)
        with self._lock:
            if self.store is not None:
                unreadable = self._readable()
                if unreadable is not None:
                    raise PairingRefused("revocations_unreadable", unreadable)
            now = self.clock()
            if grant.device_id in self._devices or grant.device_id in self._revoked_devices:
                raise PairingRefused("device_exists", "a device id is never reused")
            if not expires_at > now:
                raise PairingRefused("expiry_invalid", "a device's expiry must be in the future")
            # A caller may ask for longer. The pairing still lasts at most the settled cap.
            capped = min(float(expires_at), now + PAIRING_MAX_AGE_S)
            record = DeviceRecord(grant.device_id, label, scopes, now, capped)
            self._devices[grant.device_id] = record
            return record

    def admit(self, device_id: object, scope: str) -> Admission:
        with self._lock:
            return self._accepted(device_id, scope)

    def connect(self, device_id: object, conn_id: Hashable, *, scope: str = "read") -> Admission:
        """Admit a new live connection for ``scope`` and remember it, so that a revocation can name it."""
        if not _key(conn_id):
            return Admission(False, "connection_invalid")
        if self.store is not None and not storable_id(conn_id):
            return Admission(False, "connection_invalid", "a connection id the record of revocations can keep is "
                             "a str, an int or a tuple of them")
        with self._lock:
            if self.store is not None:
                unreadable = self._readable()
                if unreadable is not None:
                    return Admission(False, "revocations_unreadable", unreadable)
            decision, record, now = self._admit(device_id, scope)
            if decision.ok and record is not None and now is not None:
                if conn_id in self._connections:
                    return Admission(False, "connection_exists")
                if self._held(conn_id):
                    return Admission(False, "connection_exists", REVOKED_CONNECTION_HELD)
                self._connections[conn_id] = device_id  # type: ignore[assignment]
                self._note(record, now)
            return decision

    def check_connection(self, conn_id: Hashable, scope: str) -> Admission:
        """A request on a live connection, decided against the device as it is now."""
        if not _key(conn_id):
            return Admission(False, "connection_invalid")
        with self._lock:
            if self._held(conn_id):
                return Admission(False, "device_revoked")
            device_id = self._connections.get(conn_id)
            if device_id is None:
                return Admission(False, "connection_unknown")
            return self._accepted(device_id, scope)

    def disconnect(self, conn_id: Hashable) -> None:
        """The door closed ``conn_id``. A connection a revocation named is then kept as the setting says, and with
        a store its release is written (a write that fails leaves the record unreleased on disk: kept longer)."""
        if not _key(conn_id):
            return
        with self._write_lock:
            change, forget = None, ()
            with self._lock:
                self._connections.pop(conn_id, None)
                revoked = self._revoked.get(conn_id)
                if revoked is not None and revoked.released_at is None:
                    if self.revoked_connection_keep_s == 0:
                        del self._revoked[conn_id]
                        forget = (conn_id,)
                    else:
                        record = dataclasses.replace(revoked, released_at=self.clock(), released_wall=self.wall())
                        self._revoked[conn_id] = record
                        change = Revocations({record.device_id: self._revoked_devices[record.device_id]},
                                             {conn_id: StoredConnection(record.device_id, record.released_wall)})
            self._write(change, forget)

    def revoke(self, device_id: str) -> tuple[Hashable, ...]:
        """Revoke a device for good and return its live connections, which the caller must close. With a store it
        returns when the revocation is written; else it raises ``RevocationNotWritten``."""
        with self._write_lock:
            with self._lock:
                record = self._devices.get(device_id) if type(device_id) is str else None
                if record is None:
                    raise PairingRefused("device_unknown")
                if record.revoked_at is None:
                    self._devices[device_id] = dataclasses.replace(record, revoked_at=self.clock())
                    self._revoked_devices[device_id] = self.wall()
                closing = tuple(conn for conn, owner in self._connections.items() if owner == device_id)
                for conn in closing:
                    del self._connections[conn]
                    self._revoked[conn] = _RevokedConnection(device_id)
                change = self._change_of(self._unwritten | {device_id}) if self.store is not None else None
            failed = self._write(change)
            with self._lock:
                if failed is None:
                    self._unwritten.difference_update(change.devices if change is not None else ())
                else:
                    self._unwritten.add(device_id)
            if failed is not None:
                raise RevocationNotWritten(closing, failed)
            return closing

    def list(self) -> list[dict]:
        with self._lock:
            now = self.clock()
            return [record.public(now) for record in self._devices.values()]

    def _held(self, conn_id: Hashable) -> bool:
        """Called with the lock held: whether ``conn_id`` is a revoked device's connection the book still keeps.
        The clock is read only for a record whose disconnect came and whose keeping ends."""
        revoked = self._revoked.get(conn_id)
        if revoked is None:
            return False
        keep = self.revoked_connection_keep_s
        if revoked.released_at is None or keep is None or self.clock() < revoked.released_at + keep:
            return True
        del self._revoked[conn_id]
        return False

    def _accepted(self, device_id: object, scope: str) -> Admission:
        """Admit, and when the call is allowed remember it as the last accepted call."""
        decision, record, now = self._admit(device_id, scope)
        if decision.ok and record is not None and now is not None:
            self._note(record, now)
        return decision

    def _note(self, record: DeviceRecord, now: float) -> None:
        if record.last_accepted_at != now:
            self._devices[record.device_id] = dataclasses.replace(record, last_accepted_at=now)

    def _admit(self, device_id: object, scope: str) -> tuple[Admission, DeviceRecord | None, float | None]:
        """Called with the lock held. The clock is read after the revocation check, inside the window a
        racing revocation would use, so that a test can stand in that window (tests/test_device_state.py)."""
        record = self._devices.get(device_id) if type(device_id) is str else None
        if record is None:
            if type(device_id) is str and device_id in self._revoked_devices:
                return Admission(False, "device_revoked"), None, None
            return Admission(False, "device_unknown"), None, None
        if record.revoked_at is not None:
            return Admission(False, "device_revoked"), None, None
        now = self.clock()
        if now >= record.expires_at or pairing_lapsed(record.paired_at, record.last_accepted_at, now):
            return Admission(False, "device_expired"), None, None
        if scope == MANAGE:
            return Admission(False, "scope_refused", "pairing never grants manage"), None, None
        if scope not in record.scopes:
            return Admission(False, "scope_refused", f"the device was not granted {scope!r}"), None, None
        return ADMITTED, record, now
