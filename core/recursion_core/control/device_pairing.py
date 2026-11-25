"""Device pairing on the control socket: the settled limits, and the daemon's pair and revoke.

The numbers below are named once, and they are the same names and the same numbers the relay names.
This package does not import the relay. There is no passkey here, so a paired device may watch and
Stop only. An assertion does not grant a scope: a passkey-less device still may not run or approve.
The socket accepts an assertion key and ignores it. This module does not turn an assertion into a
scope, and it does not write an audit row.

A pairing lasts at most 30 days, and 7 days if unused. A dangerous action needs a fresh assertion
per action, valid 60 seconds. The use of a device (when it was paired, when a call was last
accepted, whether it was revoked, and its label) is kept beside the credential file. The token
itself stays in the credential store, which keeps only the hash.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from recursion_core.control.credentials import CredentialRefused

log = logging.getLogger(__name__)

# Settled pairing values. One block, and the only place these numbers are named on the control side.
# A pairing lasts at most 30 days, and 7 days if unused. A dangerous action needs a fresh assertion
# per action, valid 60 seconds. Without a passkey a paired device may watch and Stop only.
PAIRING_MAX_AGE_S = 30 * 24 * 60 * 60
PAIRING_UNUSED_AGE_S = 7 * 24 * 60 * 60
DANGEROUS_ASSERTION_TTL_S = 60.0
DEVICE_SCOPES = ("read", "stop")
DANGEROUS_ACTIONS = ("run", "approve")

# Printable ASCII, no leading or trailing space: the same label rule the relay uses.
_LABEL = re.compile(r"[!-~](?:[ -~]{0,62}[!-~])?")
_USE_FIELDS = frozenset({"paired_at", "last_accepted_at", "revoked", "label"})


class PairingRefused(Exception):
    """A pairing step was refused. ``code`` is a typed word; ``detail`` is for the operator."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def pairing_lapsed(paired_at: float, last_accepted_at: float | None, now: float) -> bool:
    """True at the 30-day cap, or at 7 days with no accepted call since pairing or since the last one.

    ``last_accepted_at is None`` means there has been no accepted call since pairing, so the
    unused bound is measured from ``paired_at``. At the bound the pairing has lapsed.
    """

    if now >= paired_at + PAIRING_MAX_AGE_S:
        return True
    idle_from = paired_at if last_accepted_at is None else last_accepted_at
    return now >= idle_from + PAIRING_UNUSED_AGE_S


def device_label(label: object) -> str:
    """A device's name as the owner typed it: 1 to 64 printable ASCII characters, no edge spaces."""

    if not isinstance(label, str) or not _LABEL.fullmatch(label):
        raise PairingRefused("label_invalid", "a label is 1 to 64 printable ASCII characters")
    return label


def pairing_scopes(scopes: object) -> list[str]:
    """The scopes a pairing may mint: a non-empty set drawn from read and stop, in that order.

    Run, approve, manage, an unknown name, a duplicate, an empty list, or a value that is not a
    list or tuple are ``scope_refused``. A string is not a list of scopes.
    """

    if isinstance(scopes, (str, bytes)) or not any(type(scopes) is allowed for allowed in (list, tuple)):
        raise PairingRefused("scope_refused", "scope_refused")
    if not scopes:
        raise PairingRefused("scope_refused", "scope_refused")
    named: list[str] = []
    for scope in scopes:
        if type(scope) is not str or scope not in DEVICE_SCOPES:
            raise PairingRefused("scope_refused", "scope_refused")
        named.append(scope)
    if len(set(named)) != len(named):
        raise PairingRefused("scope_refused", "scope_refused")
    return [scope for scope in DEVICE_SCOPES if scope in named]


def _urlsafe(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _row_ok(device_id: object, row: object) -> bool:
    if type(device_id) is not str or type(row) is not dict or set(row) != _USE_FIELDS:
        return False
    paired, last, revoked, label = (row["paired_at"], row["last_accepted_at"], row["revoked"], row["label"])
    if type(paired) is not int or paired < 0 or type(revoked) is not bool or type(label) is not str:
        return False
    return last is None or (type(last) is int and last >= 0)


@dataclass
class _Assertion:
    assertion_id: str
    action: str
    issued_at: float
    used: bool = False


@dataclass
class AssertionBook:
    """A fresh assertion for one dangerous action, valid for ``DANGEROUS_ASSERTION_TTL_S`` seconds.

    ``run`` and ``approve`` are the dangerous actions. Each assertion authorizes its own action
    once, and only while ``now`` is still before ``issued_at + DANGEROUS_ASSERTION_TTL_S`` (at 60
    seconds, and later, it is refused). Replaying it is refused. A wrong action does not spend it.
    The assertion does not grant a scope: a passkey-less device still may not run or approve.

    ``clock`` is a monotonic clock (seconds). Tests inject one.
    """

    clock: Callable[[], float] = time.monotonic
    random_bytes: Callable[[int], bytes] = secrets.token_bytes

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, _Assertion] = {}

    def issue(self, action: str) -> str:
        """A new assertion id for one dangerous action. Watch and Stop take none."""

        if not isinstance(action, str) or action not in DANGEROUS_ACTIONS:
            raise PairingRefused("action_not_dangerous", "only a dangerous action takes an assertion")
        with self._lock:
            now = self.clock()
            assertion_id = _urlsafe(self.random_bytes(9))
            if assertion_id in self._by_id:
                raise PairingRefused("assertion_busy", "assertion id collision")
            self._by_id[assertion_id] = _Assertion(assertion_id, action, now)
            return assertion_id

    def accept(self, assertion_id: object, action: str) -> None:
        """Consume ``assertion_id`` for ``action`` once, inside the 60-second window.

        The clock is read under the lock, before the assertion is marked used, so a second
        caller either waits and is then refused or does not run inside this one.
        """

        if not isinstance(action, str):
            raise PairingRefused("action_invalid", "an action is a string")
        with self._lock:
            now = self.clock()
            record = self._by_id.get(assertion_id) if type(assertion_id) is str else None
            if record is None:
                raise PairingRefused("assertion_invalid")
            if now >= record.issued_at + DANGEROUS_ASSERTION_TTL_S:
                raise PairingRefused("assertion_expired")
            if record.used:
                raise PairingRefused("assertion_replayed")
            if record.action != action:
                raise PairingRefused("assertion_wrong_action")
            record.used = True


class DevicePairings:
    """Who the owner paired, for how long, and which device ids must not be minted again.

    ``path`` is the use file (``device-use.json``) next to the credential file. ``store`` is the
    credential store that mints and revokes the token. ``wall`` is the same clock the store uses,
    read as whole seconds. A use file that cannot be trusted is left as it is: pair and bind
    refuse ``store_unavailable`` and do not replace it.
    """

    def __init__(self, path: str, store, *, wall: Callable[[], float] = time.time) -> None:
        if not isinstance(path, str) or not os.path.isabs(path):
            raise PairingRefused("store_unavailable", "store_unavailable")
        self.path, self.store, self._wall = path, store, wall
        self._lock = threading.RLock()
        self._devices: dict[str, dict] = {}
        self._broken = False
        self._load()

    def pair(self, label: object, scopes: object, expires_at: int | None = None) -> dict:
        """Mint a new device. The id is chosen here. Returns ``{device_id, token}``."""

        name = device_label(label)
        granted = pairing_scopes(scopes)
        with self._lock:
            self._refuse_if_broken()
            return self._mint(self._fresh_id(), granted, name, expires_at)

    def mint_device(self, device_id: str, scopes, *, label: str, expires_at: int | None) -> dict:
        """Mint ``device_id`` again. A revoked id is refused ``device_revoked`` and is not reused."""

        name = device_label(label)
        granted = pairing_scopes(scopes)
        with self._lock:
            self._refuse_if_broken()
            row = self._devices.get(device_id)
            if row is not None and row.get("revoked"):
                raise PairingRefused("device_revoked", "device_revoked")
            if type(device_id) is not str:
                raise PairingRefused("device_unknown", "device_unknown")
            return self._mint(device_id, granted, name, expires_at)

    def revoke(self, device_id: object) -> None:
        """Revoke every live device grant of ``device_id``, and never mint that id again."""

        if type(device_id) is not str:
            raise PairingRefused("device_unknown", "device_unknown")
        with self._lock:
            self._refuse_if_broken()
            grants = self._live_grants(device_id)
            row = self._devices.get(device_id)
            if row is None and not grants:
                raise PairingRefused("device_unknown", "device_unknown")
            for credential in grants:
                self.store.revoke(credential.token_id)
            if row is None:
                self._devices[device_id] = {
                    "paired_at": self._now(), "last_accepted_at": None, "revoked": True, "label": "",
                }
            else:
                row["revoked"] = True
            self._save()

    def credential_for(self, token: object, claim: object, peer: object):
        """The live device credential ``token`` names for ``claim``, or a refusal.

        A credential that is not a device is ``credential_kind_refused``. A claim that is not that
        device's id is ``device_claim_mismatch``. At the 30-day bound the credential store also
        calls the token expired; when the pairing bounds are met the refusal is ``pairing_lapsed``.
        """

        with self._lock:
            self._refuse_if_broken()
            try:
                credential = self.store.verify(token, peer=peer)
            except CredentialRefused as exc:
                if exc.detail != "token_expired":
                    raise
                return self._expired(token, claim, exc)
            if credential.kind != "device":
                raise PairingRefused("credential_kind_refused", "credential_kind_refused")
            if type(claim) is not str or claim != credential.id:
                raise PairingRefused("device_claim_mismatch", "device_claim_mismatch")
            self._check_row(credential.id, credential.issued_at)
            return credential

    def note_accepted(self, device_id: object) -> None:
        """An accepted call from this device. A refusal is not an accepted call. Pairing is not one."""

        if type(device_id) is not str:
            return
        with self._lock:
            if self._broken:
                return
            row = self._devices.get(device_id)
            if row is None or row.get("revoked"):
                return
            row["last_accepted_at"] = self._now()
            try:
                self._save()
            except (OSError, PairingRefused):
                log.exception("the device's last accepted call was not written")

    def _mint(self, device_id: str, granted: list[str], label: str, expires_at: int | None) -> dict:
        now = self._now()
        cap = now + PAIRING_MAX_AGE_S
        if expires_at is None:
            expires_at = cap
        elif type(expires_at) is not int or expires_at <= now or expires_at > cap:
            raise PairingRefused("expiry_invalid", "expiry_invalid")
        try:
            token, credential = self.store.mint("device", device_id, granted, expires_at=expires_at)
        except CredentialRefused as exc:
            raise PairingRefused(exc.detail, exc.detail) from None
        self._devices[device_id] = {
            "paired_at": credential.issued_at, "last_accepted_at": None, "revoked": False, "label": label,
        }
        try:
            self._save()
        except (OSError, PairingRefused):
            log.exception("device %s was minted but its use file was not written", device_id)
        return {"device_id": device_id, "token": token}

    def _expired(self, token: object, claim: object, exc: CredentialRefused):
        record = self._peek(token)
        if record is None or record.get("kind") != "device":
            raise exc
        if type(claim) is not str or claim != record.get("id"):
            raise PairingRefused("device_claim_mismatch", "device_claim_mismatch")
        self._check_row(record["id"], record["issued_at"])
        raise exc

    def _check_row(self, device_id: str, issued_at: int) -> None:
        row = self._devices.get(device_id)
        if row is not None and row.get("revoked"):
            raise PairingRefused("token_revoked", "token_revoked")
        paired_at = issued_at if row is None else row["paired_at"]
        last = None if row is None else row.get("last_accepted_at")
        if pairing_lapsed(paired_at, last, self._now()):
            raise PairingRefused("pairing_lapsed", "pairing_lapsed")

    def _live_grants(self, device_id: str):
        return [credential for credential in self.store.grants()
                if credential.kind == "device" and credential.id == device_id and credential.revoked_at is None]

    def _peek(self, token: object):
        if type(token) is not str:
            return None
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self.store._lock:
            token_id = self.store._by_digest.get(digest)
            record = self.store._grants.get(token_id) if token_id is not None else None
            if record is None or record.get("token_sha256") != digest:
                return None
            return {"kind": record["kind"], "id": record["id"], "issued_at": record["issued_at"]}

    def _fresh_id(self) -> str:
        for _ in range(8):
            device_id = "dev-" + secrets.token_hex(8)
            if device_id not in self._devices:
                return device_id
        raise PairingRefused("store_unavailable", "store_unavailable")

    def _now(self) -> int:
        try:
            now = int(self._wall())
        except Exception as exc:  # noqa: BLE001 - a clock that cannot be read cannot pair
            raise PairingRefused("store_unavailable", "store_unavailable") from exc
        return max(0, now)

    def _refuse_if_broken(self) -> None:
        if self._broken:
            raise PairingRefused("store_unavailable", "store_unavailable")

    def _load(self) -> None:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
        except FileNotFoundError:
            self._devices, self._broken = {}, False
            return
        except OSError:
            self._broken = True
            return
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
                self._broken = True
                return
            raw = os.read(fd, 1024 * 1024 + 1)
        except OSError:
            self._broken = True
            return
        finally:
            os.close(fd)
        if len(raw) > 1024 * 1024:
            self._broken = True
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            self._broken = True
            return
        devices = data.get("devices") if type(data) is dict and data.get("version") == 1 else None
        if type(devices) is not dict:
            self._broken = True
            return
        loaded: dict[str, dict] = {}
        for device_id, row in devices.items():
            if not _row_ok(device_id, row):
                self._broken = True
                return
            loaded[device_id] = row
        self._devices, self._broken = loaded, False

    def _save(self) -> None:
        if self._broken:
            raise PairingRefused("store_unavailable", "store_unavailable")
        payload = json.dumps({"version": 1, "devices": self._devices}, sort_keys=True, separators=(",", ":"))
        tmp = f"{self.path}.{os.getpid()}.tmp"
        flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(tmp, flags, 0o600)
        except OSError as exc:
            raise PairingRefused("store_unavailable", "store_unavailable") from exc
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        except OSError as exc:
            os.close(fd)
            raise PairingRefused("store_unavailable", "store_unavailable") from exc
        os.close(fd)
        try:
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            raise PairingRefused("store_unavailable", "store_unavailable") from exc
        try:
            dir_fd = os.open(os.path.dirname(self.path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            log.exception("the device use file was replaced but its directory was not synced")
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            log.exception("the device use file was replaced but its directory was not synced")
        finally:
            os.close(dir_fd)
