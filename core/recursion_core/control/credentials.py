"""Control API credentials: principal kinds, the scopes each may hold, and tokens the daemon mints.

Lane CONTROL, frontier plan v3.3, step 2. Nothing in the daemon reads this module yet: the authorizer
in ``scopes.py`` takes a ``CredentialStore`` when it is registered into seam S5 (a later pull request).

SCOPES are the four of the method table (``schemas/control/v1/methods.v1.json``, ``$defs/scope``):
``read`` looks, ``run`` starts, steers and stops work, ``approve`` shows and answers approvals, and
``manage`` changes the install.

PRINCIPAL KINDS and the most each may ever hold (``CEILING``). A grant above the ceiling is refused at
mint, and the authorizer intersects with the ceiling again on every call, so a record that somehow
names more is still held to it.

    owner     read run approve manage   the user on the owner-only socket; never a bearer token
    internal  read run approve manage   the daemon's own code, by name; never a bearer token
    device    read run approve          the page and paired devices: pairing never grants manage (O34)
    plugin    read run                  a loaded plugin: never approve (PRODUCT.md 1169-1171), never manage
    session   read run                  an engine's own session: never approve or manage (session.token.v1)
    agent     read run                  the MCP face: never approve, never manage

``owner`` and ``internal`` come from the way a request arrives, never from a token, so they cannot be
minted. ``agent`` is a credential kind with no principal kind in ``common.v1`` yet; how its calls are
attributed is open (``questions.md``).

TOKENS. ``CredentialStore.mint`` makes 32 random bytes, URL-safe base64, behind a prefix a secret scanner
can recognise: ``rst1_`` for a session (as ``session.token.v1`` writes it) and ``rct1_`` for every other
kind. The token is returned ONCE and never stored, logged or put in an event: the store keeps its
SHA-256. Each grant has a ``token_id`` (``stk-...`` for a session, ``ctk-...`` otherwise), a label that is
safe to log and cannot be turned into the token.

THE STORE is one JSON file in an owner-only directory: the directory is ``0700`` and this user's, the
file a regular file of this user's, ``0600``, opened without following a symlink. It is written whole
to a new file and renamed into place, so a reader sees the old grants or the new ones. It is not the
event database: tokens are secrets, and the event log is shown to surfaces.

TWO STORES ON ONE FILE (two instances, or a second process) never drop each other's grants. Every write
takes an exclusive lock on a sibling file, ``.<name>.lock`` (owner-only, like the store), and reads the
file again under it before writing. A store reads the file again whenever it has changed since it last
read or wrote it, so a grant or a revocation written through one store is seen by every other at its
next check. Opening a store, under the same lock, removes the temporary files a killed writer left. A
revocation made in two stores keeps, in each, the time that store first recorded, so while both write
the file's time is the last writer's, and each store sends its own notice once (round 5, DeepSeek F7).

THE COMMIT POINT is the rename. A write that fails before it changes nothing (``CredentialStoreError``);
one that fails after it, syncing the directory, has made its change (``CredentialStoreUncertain``): memory
keeps what the file now says, so a mint is issued, not half issued, and only its durability waits for the
next successful write.

REVOCATION takes effect at once: from the first ``revoke`` on, ``verify`` refuses that token in every
thread, and every other store on the file refuses it at its next check. Its time is never earlier than the
grant's issue, even when the wall clock has stepped back. ``revoke`` answers success only once the revocation is written: if the write fails it raises
(the token stays refused in memory) and the next ``revoke`` writes again; a revocation already written
answers at once. Its notice goes out once, after the write that made it durable.

THE PAGE'S OWN DOOR is the device principal ``page`` (``control/hooks.py`` ``DEVICE``). No grant may name
it, at mint or in the file: the page holds its scopes by being that door, and a token must never alias it.

A TOKEN PRINCIPAL holds only its own grant (``grant_scopes``); ``held`` (the union of an identity's live
grants) is for principals the host attributes by identity, plugins.

EVENTS. ``emit(event_type, payload)`` is called after a mint or a revoke has been written, outside the
store's lock: ``credential.issued`` with ``token_id``, ``kind``, ``id``, ``scopes``, ``issued_at`` and
``expires_at``; ``credential.revoked`` with ``token_id``, ``kind``, ``id`` and ``revoked_at``. Neither
carries the token or its digest. The event names wait on lane SPINE's event schema. An ``emit`` that
raises an ``Exception`` is logged and the change stands. One that raises a ``BaseException`` (a
``KeyboardInterrupt``) propagates after the change is written, so a mint interrupted there leaves a written,
live grant whose token was never returned; it counts toward its plugin's identity grants until it is
revoked (round 6, DeepSeek 7).

FAILED CHECKS are counted per peer (``FailedAuthLimit``): after ``max_failures`` failed checks within
``window_s`` seconds the peer is refused, valid token or not, until the window has passed. Every failed
check is logged with the peer and the reason, never the token. A store whose file can no longer be read
refuses every token (``store_unavailable``), which is not counted against the peer. All refusals are
``scope_refused`` with
a detail (``CredentialRefused``), the method table's existing code.

POSIX only: on a platform without uids or ``O_NOFOLLOW`` the store refuses to open.
"""

from __future__ import annotations

import base64
import collections
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Callable, Collection, Hashable, Iterable
from dataclasses import dataclass

try:
    import fcntl
except ImportError:   # not POSIX: the store refuses to open (the end of the module text)
    fcntl = None

log = logging.getLogger(__name__)

SCOPES = ("read", "run", "approve", "manage")
ALL_SCOPES = frozenset(SCOPES)
KINDS = ("owner", "internal", "device", "plugin", "session", "agent")
CEILING: dict[str, frozenset[str]] = {
    "owner": ALL_SCOPES,
    "internal": ALL_SCOPES,
    "device": frozenset({"read", "run", "approve"}),
    "plugin": frozenset({"read", "run"}),
    "session": frozenset({"read", "run"}),
    "agent": frozenset({"read", "run"}),
}
MINTABLE = frozenset({"device", "plugin", "session", "agent"})
# The page's own door is the device principal with this id (control/hooks.py DEVICE). It holds its scopes by
# being that door, not by a grant, so no grant may carry its identity: a token verified to (device, page)
# would otherwise hold the page's scopes, and revoking that token would not take them away.
PAGE_DEVICE_ID = "page"
RESERVED = frozenset({("device", PAGE_DEVICE_ID)})
SCOPE_REFUSED = "scope_refused"
STORE_VERSION = 1

_LABEL = re.compile(r"[A-Za-z0-9._:-]{1,128}")   # common.v1 $defs/token: a principal's id
_TOKEN = re.compile(r"(rst1|rct1)_[A-Za-z0-9_-]{43}")
_TOKEN_ID = re.compile(r"(stk|ctk)-[A-Za-z0-9_-]{1,60}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RECORD_FIELDS = frozenset({"token_id", "token_sha256", "kind", "id", "scopes", "issued_at", "expires_at",
                            "revoked_at"})


class CredentialRefused(Exception):
    """A grant or a token the rules refuse. ``code`` is ``scope_refused``; ``detail`` says why."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"{SCOPE_REFUSED}: {detail}")
        self.code, self.detail = SCOPE_REFUSED, detail


class CredentialStoreError(Exception):
    """The store cannot be read, written or trusted. A write that fails before its rename changes nothing: no
    grant is minted, and a revocation stays in memory, unanswered. ``CredentialStoreUncertain`` is the one
    raised after the rename."""


class CredentialStoreUncertain(CredentialStoreError):
    """The new store file is in place, but its directory could not be synced: the change is made (what is in
    memory is what the file says) and a crash before the next successful write could still lose it."""


def ceiling(kind: object) -> frozenset[str]:
    """The most a principal of ``kind`` may hold; nothing for a kind that is not known."""

    kind = _text(kind)   # its text: no hash of a str subclass runs (round 7 of #377, DeepSeek 1)
    return CEILING.get(kind, frozenset()) if kind is not None else frozenset()


def check_grant(kind: object, scopes: object) -> frozenset[str]:
    """``scopes`` as a frozenset, if a credential of ``kind`` may be minted with them; else ``CredentialRefused``."""

    kind = _text(kind)   # its text: no hash or comparison of a str subclass runs (round 7, DeepSeek 1)
    if kind is None or kind not in KINDS:
        raise CredentialRefused("kind_unknown")
    if kind not in MINTABLE:
        raise CredentialRefused("kind_not_mintable")
    # Exactly one of the four built-in containers, so none of a subclass's own __iter__, __len__ or __bool__ is
    # asked: a subclass is refused (round 8 of #377, DeepSeek 1: one that raised left mint untyped).
    # The container's type is compared by identity: ``in`` on a tuple of types would ask the type's own metaclass
    # for equality (round 8 of #377, Codex GPT-6 Astra 2).
    if not any(type(scopes) is allowed for allowed in (set, frozenset, list, tuple)) or not scopes:
        raise CredentialRefused("scopes_invalid")
    if not all(isinstance(s, str) for s in scopes):   # before hashing: a list may hold an unhashable value
        raise CredentialRefused("scopes_invalid")
    wanted = frozenset(_text(s) for s in scopes)   # each read as its text (round 7, DeepSeek 1)
    if len(wanted) != len(scopes) or not wanted <= ALL_SCOPES:
        raise CredentialRefused("scopes_invalid")
    if "manage" in wanted and "manage" not in CEILING[kind]:
        raise CredentialRefused("manage_never_granted")
    if "approve" in wanted and "approve" not in CEILING[kind]:
        raise CredentialRefused("approve_never_granted")
    return wanted


def _digest(token: str) -> str:
    return hashlib.sha256(str.encode(token, "ascii")).hexdigest()   # str's own encode, never a subclass's


def _text(value: object) -> str | None:
    """``value`` as a plain ``str``, so no method of a ``str`` subclass runs on it; None for anything else. The
    round-6 read of #377 by DeepSeek, 2: a token id whose own hash raised left ``revoke`` and ``grant_scopes``
    with that error, and an ``encode`` that raised left ``verify``."""

    return str.__str__(value) if issubclass(type(value), str) else None


def _identity_of(st: os.stat_result) -> tuple:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


@dataclass(frozen=True)
class Credential:
    """One grant as the store keeps it, without its digest: safe to log and to return."""

    token_id: str
    kind: str
    id: str
    scopes: frozenset[str]
    issued_at: int
    expires_at: int | None
    revoked_at: int | None

    def public(self) -> dict:
        return {"token_id": self.token_id, "kind": self.kind, "id": self.id, "scopes": sorted(self.scopes),
                "issued_at": self.issued_at, "expires_at": self.expires_at, "revoked_at": self.revoked_at}


def _peer_key(peer: object) -> Hashable:
    """The key a peer's failed checks are counted under. A ``str`` subclass counts as its text, so its own
    comparison never runs. A peer that cannot key the count (unhashable, or whose hash raises, or whose
    comparison raises in the count's lookups) is refused ``peer_invalid`` (round 6, DeepSeek 1)."""

    text = _text(peer)
    if text is not None:
        return text
    try:
        hash(peer)
    except Exception:  # noqa: BLE001 - any failure to hash is a peer that cannot be counted
        raise CredentialRefused("peer_invalid") from None
    return peer


class FailedAuthLimit:
    """Failed token checks per peer: ``max_failures`` within ``window_s`` seconds, then refused until it passes.

    ``clock`` is monotonic seconds. At most ``max_peers`` peers are remembered; the one that failed least
    recently is forgotten first, so a flood of peers costs bounded memory.
    """

    def __init__(self, *, max_failures: int = 10, window_s: float = 60.0, max_peers: int = 1024,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if max_failures < 1 or not window_s > 0 or max_peers < 1:
            raise ValueError("max_failures, window_s and max_peers must be positive")
        self._max, self._window = int(max_failures), float(window_s)
        self._max_peers, self._clock = int(max_peers), clock
        self._lock = threading.Lock()
        self._failures: collections.OrderedDict[Hashable, collections.deque] = collections.OrderedDict()

    def _recent(self, peer: Hashable, now: float) -> collections.deque | None:
        times = self._failures.get(peer)
        if times is not None:
            while times and now - times[0] >= self._window:
                times.popleft()
            if not times:
                del self._failures[peer]
                return None
        return times

    def blocked(self, peer: Hashable) -> bool:
        key = _peer_key(peer)
        with self._lock:
            now = self._clock()
            try:
                times = self._recent(key, now)
            except Exception:  # noqa: BLE001 - a peer whose comparison raises cannot be counted
                raise CredentialRefused("peer_invalid") from None
            return times is not None and len(times) >= self._max

    def failed(self, peer: Hashable) -> bool:
        """Count one failed check; True when the peer is now refused."""
        key = _peer_key(peer)
        with self._lock:
            now = self._clock()
            try:
                times = self._recent(key, now)
                if times is None:
                    times = self._failures[key] = collections.deque()
                self._failures.move_to_end(key)
            except Exception:  # noqa: BLE001 - a peer whose comparison raises cannot be counted
                raise CredentialRefused("peer_invalid") from None
            times.append(now)
            while len(self._failures) > self._max_peers:
                self._failures.popitem(last=False)
            return len(times) >= self._max


@contextlib.contextmanager
def owner_file_lock(lock_path: str, uid: int, timeout_s: float):
    """The writers' lock of an owner-only file: an exclusive ``flock`` on ``lock_path`` (made 0600 when missing),
    for a whole read-modify-write. A writer that cannot have it within ``timeout_s`` is refused
    (``CredentialStoreError``) and changes nothing. The credential store and the remote relay's record of
    revocations (``remote.revocation_store``) both write through it."""

    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise CredentialStoreError(f"the store's lock cannot be opened ({type(exc).__name__})") from None
    try:
        try:
            st = os.fstat(fd)
        except OSError as exc:
            raise CredentialStoreError(f"the store's lock cannot be read ({type(exc).__name__})") from None
        if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or stat.S_IMODE(st.st_mode) & 0o077:
            raise CredentialStoreError("the store's lock is not an owner-only file of this user")
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CredentialStoreError("the store is held by another writer") from None
                time.sleep(0.005)
            except OSError as exc:
                raise CredentialStoreError(f"the store's lock cannot be taken ({type(exc).__name__})") from None
        yield
    finally:
        # The lock is let go explicitly, then its descriptor closed. Neither failing changes the answer: what
        # the caller did under the lock is done (a mint that is written returns its token), and a close that
        # fails cannot leave the lock held for the rest of the process (round 7 of #377, DeepSeek 2: the
        # OSError escaped mint and revoke untyped, and every later writer here was refused).
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        try:
            os.close(fd)
        except OSError as exc:
            log.warning("the store's lock could not be released cleanly (%s)", type(exc).__name__)


def sweep_temps(path: str) -> None:
    """Remove the temporary files a killed writer of ``path`` left (the read of #377 by DeepSeek V4.1 Flash,
    F13). Called under the writers' lock, when no write is under way, so every one is an orphan."""

    directory = os.path.dirname(path)
    orphan = re.compile(rf"\.{re.escape(os.path.basename(path))}\.[0-9a-f]{{16}}\.tmp")
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if orphan.fullmatch(name):
            try:
                os.unlink(os.path.join(directory, name))
            except OSError:
                pass


def replace_file(path: str, data: bytes) -> None:
    """Write ``data`` to a new owner-only file beside ``path``, sync it, rename it over ``path``, sync the
    directory: all or nothing.

    The rename is the commit point. A failure before it raises ``CredentialStoreError`` and changes nothing
    on disk. A failure after it (syncing the directory) raises ``CredentialStoreUncertain``: the file now
    holds the new data, and only its durability is uncertain until the next write succeeds.
    """

    directory = os.path.dirname(path)
    temp = os.path.join(directory, f".{os.path.basename(path)}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(temp, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise CredentialStoreError(f"the store cannot be written ({type(exc).__name__})") from None
    try:
        dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise CredentialStoreUncertain(
            f"the store was replaced but its directory could not be synced ({type(exc).__name__})") from None


class CredentialStore:
    """Daemon-minted tokens for the control API, kept hashed in one owner-only file. See the module text."""

    # Revocations made in memory whose write has not yet succeeded fully: a later revoke of the same grant
    # writes again instead of answering success for a revocation the file does not hold. Rebound, never
    # mutated, so the class value stays empty.
    _unpersisted: frozenset = frozenset()

    def __init__(self, path: str, *, emit: Callable[[str, dict], None] | None = None,
                 wall: Callable[[], float] = time.time, limit: FailedAuthLimit | None = None,
                 lock_timeout_s: float = 10.0) -> None:
        if os.name == "nt" or not hasattr(os, "getuid") or not hasattr(os, "O_NOFOLLOW") or fcntl is None:
            raise CredentialStoreError("platform_unsupported: the credential store needs uids and O_NOFOLLOW")
        if not isinstance(path, str):
            raise CredentialStoreError("the store path must be a str")
        if not os.path.isabs(path):
            raise CredentialStoreError("the store path must be absolute")
        if "\x00" in path:   # the OS would raise ValueError (the round-2 read of #385 by Sol, a later item)
            raise CredentialStoreError("the store path holds a NUL character")
        if type(lock_timeout_s) not in (int, float) or not 0 < lock_timeout_s < float("inf"):
            raise CredentialStoreError("lock_timeout_s must be a positive number of seconds")
        self._path, self._emit, self._wall = path, emit, wall
        self._lock_path = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.lock")
        self._lock_timeout_s = float(lock_timeout_s)
        self._limit = limit if limit is not None else FailedAuthLimit()
        self._uid = os.getuid()
        self._lock = threading.Lock()
        self._secure_dir(os.path.dirname(path))
        self._grants: dict[str, dict] = {}    # token_id -> record
        self._by_digest: dict[str, str] = {}  # token_sha256 -> token_id
        self._seen: tuple | None = None       # the file's identity when this store last read or wrote it
        with self._file_lock():
            self._sweep_temps()
            self._reload()

    # -- the file -----------------------------------------------------------------------------------
    def _secure_dir(self, directory: str) -> None:
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise CredentialStoreError(f"the store directory cannot be made ({type(exc).__name__})") from None
        try:
            st = os.lstat(directory)
        except OSError as exc:
            raise CredentialStoreError(f"the store directory cannot be read ({type(exc).__name__})") from None
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != self._uid or stat.S_IMODE(st.st_mode) & 0o077:
            raise CredentialStoreError("the store directory is not an owner-only directory of this user")

    def _load(self) -> tuple[list[dict], tuple | None]:
        """The file's grants, checked, and its identity (``_identity``), read from one descriptor."""

        try:
            # O_NONBLOCK: a FIFO at the path would otherwise hold this open until a writer came; with it, the
            # open returns and the type check below refuses it. It changes nothing for a regular file.
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        except FileNotFoundError:
            return [], None
        except OSError as exc:
            raise CredentialStoreError(f"the store cannot be opened ({type(exc).__name__})") from None
        try:
            # Checked on the descriptor, before any read: a directory or a FIFO at the path is refused here.
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != self._uid or stat.S_IMODE(st.st_mode) & 0o077:
                raise CredentialStoreError("the store is not an owner-only file of this user")
            identity = _identity_of(st)
            with os.fdopen(fd, "rb") as handle:
                fd = None
                raw = handle.read()
        except OSError as exc:
            raise CredentialStoreError(f"the store cannot be read ({type(exc).__name__})") from None
        finally:
            if fd is not None:
                os.close(fd)
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise CredentialStoreError("the store is not JSON") from None
        if type(doc) is not dict or set(doc) != {"version", "credentials"} or type(doc["version"]) is not int \
                or doc["version"] != STORE_VERSION or type(doc["credentials"]) is not list:
            raise CredentialStoreError("the store has an unknown shape")
        records, seen_ids, seen_digests = [], set(), set()
        for record in doc["credentials"]:
            self._check_record(record)
            if record["token_id"] in seen_ids or record["token_sha256"] in seen_digests:
                raise CredentialStoreError("the store names one grant twice")
            seen_ids.add(record["token_id"])
            seen_digests.add(record["token_sha256"])
            records.append(record)
        return records, identity

    def _identity(self) -> tuple | None:
        """What the file at the path is now; None when there is none. A write renames a new file into place, so
        any write changes it, and the file only grows from one change to the next."""

        try:
            return _identity_of(os.stat(self._path, follow_symlinks=False))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CredentialStoreError(f"the store cannot be read ({type(exc).__name__})") from None

    def _reload(self) -> dict:
        """Memory becomes what the file says, plus this store's revocations not yet written, each with the time
        first recorded here. Returns the pending ones the file already holds as revoked, with the file's time.
        Lock held."""

        records, identity = self._load()
        grants = {record["token_id"]: record for record in records}
        durable = {}
        for token_id in self._unpersisted:   # refused here from the first revoke on, whatever the file says
            mine, theirs = self._grants.get(token_id), grants.get(token_id)
            if mine is None or theirs is None or mine["revoked_at"] is None:
                continue
            if theirs["revoked_at"] is not None:
                durable[token_id] = theirs["revoked_at"]
            # The time first recorded here stays, whatever another store wrote since: not its later time (the
            # round-3 read of #377 by Astra, 3), and not the earlier of the two, which a clock that stepped back
            # gets wrong (the round-4 reads by Astra, 2, and Sol, 1).
            theirs["revoked_at"] = mine["revoked_at"]
        self._grants = grants
        self._by_digest = {record["token_sha256"]: token_id for token_id, record in grants.items()}
        self._seen = identity
        return durable

    def _refresh(self) -> None:
        """Read the file again if another store has written it since this one last read or wrote it. Lock held."""

        if self._identity() != self._seen:
            self._reload()

    def _wrote(self) -> None:
        """After this store's write, under the writers' lock: the file at the path is the one it wrote."""

        try:
            self._seen = self._identity()
        except CredentialStoreError:
            self._seen = None   # read again at the next check

    def _file_lock(self):
        """The writers' lock (``owner_file_lock``) on ``.<name>.lock``, for a whole read-modify-write (the
        read of #377 by DeepSeek V4.1 Flash, F4: two stores on one file dropped each other's grants). A writer
        that cannot have it within ``lock_timeout_s`` is refused, and changes nothing."""

        return owner_file_lock(self._lock_path, self._uid, self._lock_timeout_s)

    def _sweep_temps(self) -> None:
        """Remove the temporary files a killed writer left (``sweep_temps``). Called under the writers' lock."""

        sweep_temps(self._path)

    @staticmethod
    def _check_record(record: object) -> None:
        def stamp(value: object, optional: bool) -> bool:
            return (optional and value is None) or (type(value) is int and value >= 0)

        ok = (type(record) is dict and set(record) == _RECORD_FIELDS
              and isinstance(record["token_id"], str) and _TOKEN_ID.fullmatch(record["token_id"]) is not None
              and isinstance(record["token_sha256"], str) and _SHA256.fullmatch(record["token_sha256"]) is not None
              and isinstance(record["kind"], str) and record["kind"] in MINTABLE
              and isinstance(record["id"], str) and _LABEL.fullmatch(record["id"]) is not None
              and type(record["scopes"]) is list and record["scopes"]
              and all(isinstance(s, str) for s in record["scopes"])
              and stamp(record["issued_at"], False) and stamp(record["expires_at"], True)
              and stamp(record["revoked_at"], True))
        if not ok:
            raise CredentialStoreError("the store holds a grant of an unknown shape")
        if record["token_id"].startswith("stk-") != (record["kind"] == "session"):
            raise CredentialStoreError("the store holds a grant whose id does not match its kind")
        issued = record["issued_at"]
        if (record["expires_at"] is not None and record["expires_at"] <= issued) \
                or (record["revoked_at"] is not None and record["revoked_at"] < issued):
            raise CredentialStoreError("the store holds a grant whose times are out of order")
        if (record["kind"], record["id"]) in RESERVED:
            raise CredentialStoreError("the store holds a grant for the page's own door, which no grant may name")
        try:
            check_grant(record["kind"], record["scopes"])
        except CredentialRefused as exc:
            raise CredentialStoreError(f"the store holds a grant the rules refuse ({exc.detail})") from None

    def _write(self, grants: Iterable[dict]) -> None:
        """Write every grant to a new file in the same directory, sync it, rename it over the store, sync the
        directory.

        The rename is the commit point. A failure before it raises ``CredentialStoreError`` and changes
        nothing on disk. A failure after it (syncing the directory) raises ``CredentialStoreUncertain``: the
        file now holds the new grants, so the caller keeps them in memory too, and only their durability
        is uncertain until the next write succeeds.
        """

        data = json.dumps({"version": STORE_VERSION, "credentials": list(grants)}, sort_keys=True,
                          separators=(",", ":")).encode("ascii")
        replace_file(self._path, data)

    # -- grants -------------------------------------------------------------------------------------
    @staticmethod
    def _credential(record: dict) -> Credential:
        return Credential(token_id=record["token_id"], kind=record["kind"], id=record["id"],
                          scopes=frozenset(record["scopes"]), issued_at=record["issued_at"],
                          expires_at=record["expires_at"], revoked_at=record["revoked_at"])

    def _now(self) -> int:
        """The wall clock in whole seconds. A clock that does not answer a finite number makes the store unusable:
        ``CredentialStoreError``, never an untyped error out of mint, verify or a read (#385's round-6 F5, owed on
        #377 and answered with its round-8 reads)."""

        try:
            return int(self._wall())
        except Exception as exc:  # noqa: BLE001 - a clock that cannot be read is a store that cannot be used
            raise CredentialStoreError(f"the wall clock did not answer a finite number ({type(exc).__name__})") from None

    def mint(self, kind: str, principal_id: str, scopes: Collection[str], *,
             expires_at: int | None = None) -> tuple[str, Credential]:
        """Mint a token for ``(kind, principal_id)`` holding ``scopes``. Returns ``(token, credential)``.

        The token is returned here once and kept nowhere. Refused (``CredentialRefused``): an unknown or
        unmintable kind, a scope not in ``SCOPES``, ``manage`` for a device, plugin, session or agent,
        ``approve`` for a plugin, session or agent, a principal id that is not a label, or an expiry that
        is not a whole second in the future.
        """

        kind, principal_id = _text(kind), _text(principal_id)   # their text (round 7 of #377, DeepSeek 1)
        granted = check_grant(kind, scopes)   # no isinstance first: it would read the caller's __class__ (round 8, Sol 3)
        if not isinstance(principal_id, str) or _LABEL.fullmatch(principal_id) is None:
            raise CredentialRefused("principal_id_invalid")
        if (kind, principal_id) in RESERVED:
            raise CredentialRefused("principal_id_reserved")
        # Never below zero: a wall clock that answered below zero wrote an issue time the loader refuses, and the
        # whole file then failed to load for every later process (#385's round-6 F3; round 8 of #377, Astra 3, Sol 2).
        now = max(0, self._now())
        if expires_at is not None and (type(expires_at) is not int or expires_at <= now):
            raise CredentialRefused("expiry_invalid")
        session = kind == "session"
        secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")   # 43 characters
        token = ("rst1_" if session else "rct1_") + secret
        record = {"token_id": ("stk-" if session else "ctk-") + secrets.token_urlsafe(16),
                  "token_sha256": _digest(token), "kind": kind, "id": principal_id, "scopes": sorted(granted),
                  "issued_at": now, "expires_at": expires_at, "revoked_at": None}
        uncertain = False
        with self._lock, self._file_lock():
            self._reload()   # what another store wrote is kept
            if record["token_id"] in self._grants or record["token_sha256"] in self._by_digest:
                raise CredentialStoreError("a fresh grant collided with a stored one")  # 2^-128: never in practice
            try:
                self._write([*self._grants.values(), record])
            except CredentialStoreUncertain:
                uncertain = True   # the file holds the grant now, so memory must too; only durability is unsure
            self._wrote()
            self._grants[record["token_id"]] = record
            self._by_digest[record["token_sha256"]] = record["token_id"]
            settled = () if uncertain else self._settle()
        if uncertain:
            log.error("credential %s is issued, but the store's directory could not be synced: a crash before the "
                      "next successful write may lose it", record["token_id"])
        credential = self._credential(record)
        self._notify("credential.issued", {"token_id": credential.token_id, "kind": kind, "id": principal_id,
                                           "scopes": sorted(granted), "issued_at": now, "expires_at": expires_at})
        self._notify_revoked(settled)
        return token, credential

    def revoke(self, token_id: str) -> Credential:
        """Revoke a grant at once. Unknown: ``CredentialRefused``.

        The grant is refused from the first call on, whatever the file write does, and keeps the first
        ``revoked_at``. Success is answered only once the revocation is written: a call whose write failed
        raises ``CredentialStoreError`` (``CredentialStoreUncertain`` when the file was replaced but not
        synced), and the next call writes again. A revocation already written answers at once, without a
        write. The ``credential.revoked`` notice goes out once, after the write that made it durable.
        """

        token_id = _text(token_id)   # its text; anything else is unknown (round 6, DeepSeek 2)
        with self._lock:
            pending_before = isinstance(token_id, str) and token_id in self._unpersisted
            known = self._grants.get(token_id) if isinstance(token_id, str) else None
            if known is not None and known["revoked_at"] is None:
                # Refused at once, before the writers' lock or the file can fail (the round-3 read of #377 by
                # Astra, 2: a lock that could not be opened left the grant live in memory).
                self._revoke_here(known)
            with self._file_lock():
                durable = self._reload()   # what another store wrote is kept, its revocations included
                record = self._grants.get(token_id) if isinstance(token_id, str) else None
                if record is None:
                    raise CredentialRefused("token_unknown")
                if token_id in durable and not pending_before:
                    # Revoked here only just now, from a stale memory: the file already holds another store's
                    # revocation, written and notified there, so there is nothing to write or notify here.
                    record["revoked_at"] = durable[token_id]
                    self._unpersisted = self._unpersisted - {token_id}
                if record["revoked_at"] is not None and token_id not in self._unpersisted:
                    return self._credential(record)
                if record["revoked_at"] is None:
                    self._revoke_here(record)
                self._unpersisted = self._unpersisted | {token_id}
                try:
                    self._write(self._grants.values())
                except CredentialStoreUncertain:
                    self._wrote()
                    raise
                self._wrote()
                settled = self._settle()
                credential = self._credential(record)
        self._notify_revoked(settled)
        return credential

    def _revoke_here(self, record: dict) -> None:
        """Refused from here on, whatever the file write does. Never dated before the grant's issue: after a wall
        clock that stepped back, the store this writes must still load (the round-2 reads of #377, Astra 1 and
        Sol 1). A retry keeps this first time. A wall clock that cannot be read does not undo the revocation: it
        is dated at the grant's issue and kept pending like any other (round 9 of #377: DeepSeek F2, Codex GPT-6
        Astra 2 and GPT-6.1 Sol 2). Lock held."""

        try:
            stamp = self._now()
        except CredentialStoreError:
            stamp = record["issued_at"]
        record["revoked_at"] = max(record["issued_at"], stamp)
        self._unpersisted = self._unpersisted | {record["token_id"]}

    def _settle(self) -> tuple[Credential, ...]:
        """After a write that succeeded fully: every revocation in memory is now durable. Returns the ones that
        were waiting for it, for their notices. Called with the lock held."""

        settled = tuple(self._credential(self._grants[t]) for t in sorted(self._unpersisted) if t in self._grants)
        self._unpersisted = frozenset()
        return settled

    def _notify_revoked(self, settled: Iterable[Credential]) -> None:
        for credential in settled:
            self._notify("credential.revoked", {"token_id": credential.token_id, "kind": credential.kind,
                                                "id": credential.id, "revoked_at": credential.revoked_at})

    def verify(self, token: object, *, peer: Hashable) -> Credential:
        """The live grant ``token`` names, or ``CredentialRefused``. Failed checks count against ``peer``."""

        _peer_key(peer)   # failed checks are counted per peer: it must key (round 5, DeepSeek F2: any raise)
        if self._limit.blocked(peer):
            log.warning("credential check refused for peer %r: too many failed checks", peer)
            raise CredentialRefused("auth_rate_limited")
        reason = None
        if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
            reason = "token_malformed"
        else:
            with self._lock:
                try:
                    self._refresh()
                except CredentialStoreError as exc:   # not the peer's failure: not counted against it
                    log.error("credential check refused for peer %r: %s", peer, exc)
                    raise CredentialRefused("store_unavailable") from None
                token_id = self._by_digest.get(_digest(token))
                record = self._grants.get(token_id) if token_id is not None else None
                if record is None:
                    reason = "token_unknown"
                elif record["revoked_at"] is not None:
                    reason = "token_revoked"
                else:
                    try:
                        expired = record["expires_at"] is not None and self._now() >= record["expires_at"]
                    except CredentialStoreError as exc:   # the clock, not the peer: not counted against it
                        log.error("credential check refused for peer %r: %s", peer, exc)
                        raise CredentialRefused("store_unavailable") from None
                    if not expired:
                        return self._credential(record)
                    reason = "token_expired"
        now_limited = self._limit.failed(peer)
        log.warning("credential check failed for peer %r: %s%s", peer, reason,
                    "; the peer is now refused for a while" if now_limited else "")
        raise CredentialRefused(reason)

    def grant_scopes(self, token_id: object, kind: object, principal_id: object) -> frozenset[str]:
        """The scopes of the one grant ``token_id``, NOW: nothing unless it exists, is live (not revoked, not
        expired) and was minted for exactly ``(kind, principal_id)``. What a principal made from a verified
        token holds: another grant of the same identity never widens it."""

        token_id, kind, principal_id = _text(token_id), _text(kind), _text(principal_id)   # round 6, DeepSeek 2
        now = self._now()
        with self._lock:
            self._refresh()
            record = self._grants.get(token_id) if isinstance(token_id, str) else None
            if (record is None or record["kind"] != kind or record["id"] != principal_id
                    or record["revoked_at"] is not None
                    or (record["expires_at"] is not None and now >= record["expires_at"])):
                return frozenset()
            return frozenset(record["scopes"])

    def held(self, kind: object, principal_id: object) -> frozenset[str]:
        """The scopes ``(kind, principal_id)`` holds NOW: the union of its live grants. For a principal the
        host attributes by identity (a plugin); a principal made from a token holds ``grant_scopes`` of its
        own grant instead. Every grant is within its kind's ceiling (checked at mint and when the file is
        read); the authorizer cuts to it again."""

        kind, principal_id = _text(kind), _text(principal_id)   # compared as their text (round 6, DeepSeek 2)
        now = self._now()
        out: set[str] = set()
        with self._lock:
            self._refresh()
            for record in self._grants.values():
                if (record["kind"] == kind and record["id"] == principal_id and record["revoked_at"] is None
                        and (record["expires_at"] is None or now < record["expires_at"])):
                    out.update(record["scopes"])
        return frozenset(out)

    def grants(self) -> list[Credential]:
        """Every grant, revoked ones included, without digests."""

        with self._lock:
            self._refresh()
            return [self._credential(record) for record in self._grants.values()]

    def _notify(self, event_type: str, payload: dict) -> None:
        if self._emit is None:
            return
        try:
            self._emit(event_type, payload)
        except Exception:  # noqa: BLE001 - the grant is written; a failed notice does not undo it
            log.exception("the %s event could not be recorded", event_type)
