"""A paired device's lifecycle as a model (lane REMOTE, first slice): admitted, expired, revoked.

In memory and pure: no token, no file, and no daemon reads it. It fixes the rules the device store and
the phone door of this lane's later slices keep, so that they are tested before either exists:

* A device holds the scopes its pairing granted and never manage. Asking for manage is ``scope_refused``,
  and so is asking for any scope it was not granted.
* A device at or past its expiry, the 30-day cap, or 7 days without an accepted call is refused
  ``device_expired``; a revoked one ``device_revoked``, at once, on its very next request. Revocation
  is final: the same device id is never admitted again.
* ``revoke`` names every live connection the device holds, so the door closes each one. A connection
  that was not closed is still refused on its next request, because every request is checked against
  the device's state at that moment (``check_connection``), never against an earlier answer.
* ``connect`` and ``revoke`` are decided under one lock: a connection is either refused or named by the
  revocation, never admitted and missed.
* A record the book hands out is a frozen snapshot: changing it is refused, and the book's own state
  changes only under its lock (a revocation replaces the record).
* A wrong argument (no grant, a device id that is not a non-empty ``str``, a subclass of ``str`` included,
  an expiry that is not a finite number, a connection id that cannot be a key) is a typed refusal, never a
  ``TypeError``.

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


@dataclass(frozen=True)
class Admission:
    """One decision. It is not a capability: the next request is decided again."""

    ok: bool
    code: str | None = None
    detail: str = ""


ADMITTED = Admission(True)


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


def _key(conn_id: object) -> bool:
    try:
        hash(conn_id)
    except TypeError:
        return False
    return True


@dataclass
class DeviceBook:
    """Paired devices and their live connections, in memory, under one lock. ``clock`` is monotonic."""

    clock: Callable[[], float] = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _devices: dict[str, DeviceRecord] = field(default_factory=dict, init=False, repr=False)
    _connections: dict[Hashable, str] = field(default_factory=dict, init=False, repr=False)

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
            now = self.clock()
            if grant.device_id in self._devices:
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
        with self._lock:
            decision, record, now = self._admit(device_id, scope)
            if decision.ok and record is not None and now is not None:
                if conn_id in self._connections:
                    return Admission(False, "connection_exists")
                self._connections[conn_id] = device_id  # type: ignore[assignment]
                self._note(record, now)
            return decision

    def check_connection(self, conn_id: Hashable, scope: str) -> Admission:
        """A request on a live connection, decided against the device as it is now."""
        if not _key(conn_id):
            return Admission(False, "connection_invalid")
        with self._lock:
            device_id = self._connections.get(conn_id)
            if device_id is None:
                return Admission(False, "connection_unknown")
            return self._accepted(device_id, scope)

    def disconnect(self, conn_id: Hashable) -> None:
        if not _key(conn_id):
            return
        with self._lock:
            self._connections.pop(conn_id, None)

    def revoke(self, device_id: str) -> tuple[Hashable, ...]:
        """Revoke a device for good and return its live connections, which the caller must close."""
        with self._lock:
            record = self._devices.get(device_id) if type(device_id) is str else None
            if record is None:
                raise PairingRefused("device_unknown")
            if record.revoked_at is None:
                self._devices[device_id] = dataclasses.replace(record, revoked_at=self.clock())
            closing = tuple(conn for conn, owner in self._connections.items() if owner == device_id)
            for conn in closing:
                del self._connections[conn]
            return closing

    def list(self) -> list[dict]:
        with self._lock:
            now = self.clock()
            return [record.public(now) for record in self._devices.values()]

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
