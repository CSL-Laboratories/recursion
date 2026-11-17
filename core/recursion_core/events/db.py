"""The event store's transaction helper: one SQLite database, one writer connection, one owner at a time.

THE PROTOCOL (the lanes' preamble, version 3.3, "Shared state"; these are the published names)::

    with db.transaction() as tx:      the code that opens it OWNS it for the whole read, modify, write and
                                      commit; anyone else takes part only by being passed tx. A normal exit
                                      commits; an exception rolls back and is raised again. A second
                                      transaction() in the same thread raises ``nested_transaction``.
        tx.append(event)              one event.v1 without ``seq`` and ``time``; both are assigned here.
                                      Returns the event's seq in its log.
        tx.receipt_get(tenant, method, command_id)
                                      None, or ``Receipt(payload_digest, answer)`` (a tuple); the answer is
                                      the caller's own copy.
        tx.receipt_put(tenant, method, command_id, payload_digest, answer)
                                      keeps the receipt; one that exists is never replaced (``ReceiptExists``,
                                      a ``sqlite3.IntegrityError``).
        tx.execute(sql, params)       one statement, reads and writes of rows of tables a lane declared and
                                      nothing else; returns the rows as a list of tuples. To learn which rows
                                      a write changed, use ``RETURNING``.
    db.register_tables(lane, version, ddl)   once per lane per process, at composition; every table is named
                                      <lane>_<name> (``sessions_work``); a lane's handler runs SQL only on
                                      its own tables (by review: a tx does not know which lane holds it).
    db.subscribe(fn)                  ``fn(events)`` after each commit that appended events, outside every
                                      lock of the helper, in commit order; returns an unsubscribe function.

A mutating control method that takes a command_id has its transaction opened by SPINE's S1 dispatcher,
which looks the receipt up IN it before any staleness check, calls the handler with ``ctx.tx``, stores the
answer with ``receipt_put`` and commits. The helper admits one owner at a time across threads, so the
second of two concurrent calls with one command_id waits for the first to commit and then finds its
receipt. The payload digest is the dispatcher's (SHA-256 hex of the params without the command id); the
helper keeps it and the answer, and refuses a digest that is not 64 lower-case hex digits.

LOCK ORDER (published here; part of the protocol):

    1. The helper's lock (one transaction owner at a time) is NEVER taken while a run registry lock, the
       daemon's lock or a run's lock is held by the same thread.
    2. None of those is taken while the helper's lock is held: inside a transaction there is no registry
       method, no daemon method and no engine call. Subscribers and anything that draws or notifies run
       after the commit, outside the helper's lock.

What enforces it here: ``add_lock_probe(name, probe)`` lets the composition name an outer lock and a
function that says whether the calling thread holds it; ``transaction()`` and ``register_tables()`` refuse
(``LockOrderError``, ``lock_order:<name>``) to take the helper's lock while a probe says yes, and a probe
that raises refuses too. The other half belongs to the code that takes those locks:
``require_no_transaction(what)`` raises if the calling thread holds an event-store transaction, and
``in_transaction()`` says whether it does. This module imports nothing from the daemon, the registry or
the control API, so the helper itself never calls them; subscribers run with no lock of the helper held.

DURABILITY. The real store is SQLite in WAL mode with ``synchronous=FULL`` (the brief's working choice;
the contract: "No evidence picks JSONL over SQLite, and both have durability traps"): a commit that has
returned survives the process being killed, and under FULL the WAL is synced at every commit. Opening
checks that both settings took effect, that the file, its WAL and shared-memory files and its lock file are
regular files of this user that nobody else can read or write (one that is open to others is refused, not
narrowed), that the file is this store's (``application_id``) and passes ``quick_check``, takes an
exclusive ``flock`` on ``<path>.lock`` for the life of the store (one writer process), and raises the epoch
by one. The kill -9, full file system and file-size-limit tests in ``events/tests/test_durability.py`` are
the evidence; a power cut was not emulated.

FAILURES. A statement that fails inside a transaction raises to the block; the transaction is rolled back
when the block ends. A statement's rows are fetched under the same rule as the statement (SQLite can find
corruption while it steps through rows), and an append is one write, so a failed append that a caller
catches leaves no hole in the log. If SQLite rolled the transaction back on its own (it may on a full disk or an I/O
error), every later use of that tx raises ``transaction_aborted``, so no write can slip out in autocommit
mode. An I/O error, corruption or a failed rollback marks the store FAILED: every later transaction
raises ``store_failed`` until the store is opened again (after a failed sync the page cache cannot be
trusted). A COMMIT that raises is ``CommitUnknown`` (``commit_outcome_unknown``): the transaction may or
may not be durable, the store is failed, and the caller must not treat it as rolled back; after a reopen
a retry with the same command_id finds the receipt if it committed.

WHAT THIS DOES NOT GIVE. Subscribers are callbacks of this process: one that has not run when the process
dies does not run; a consumer that must not miss an event reads the log from a stored position (lane
EVENTS' later slices). The run registry is a separate file and is not written in these transactions. A
write to an engine is an outside effect that no commit makes exactly once. This module checks the event
envelope (the fields every event has, and which source and trust each type allows); each payload's shape
is the emitting lane's, checked against SPINE's schemas by that lane.
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import weakref
from typing import Callable, NamedTuple

try:
    import fcntl
except ImportError:          # Windows: the daemon does not run there. The fake does, and needs no lock file.
    fcntl = None

LOG = logging.getLogger(__name__)

__all__ = [
    'APPLICATION_ID', 'CommitUnknown', 'EVENT_SCHEMA', 'EVENT_TYPES', 'EventStore', 'FakeStore', 'LockOrderError',
    'Receipt', 'ReceiptExists', 'SCHEMA_VERSION', 'StoreError', 'in_transaction', 'require_no_transaction',
]

APPLICATION_ID = 0x52634576      # 'RcEv' in the file header: a file that is not this store is refused
SCHEMA_VERSION = 1               # of the helper's own tables (PRAGMA user_version)
EVENT_SCHEMA = 'event.v1 1.0-draft.7 (SPINE #368 at c77aa2e0)'    # the envelope rules below are read from it
MAX_DEPTH = 64                   # of a stored value

_HELPER_DDL = (
    'CREATE TABLE store_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)',
    # A log's next seq is one more than its highest (the UNIQUE index answers that), so an append is ONE write.
    'CREATE TABLE event_log (position INTEGER PRIMARY KEY AUTOINCREMENT, log_id TEXT NOT NULL, '
    'seq INTEGER NOT NULL CHECK (seq >= 1), event TEXT NOT NULL, UNIQUE (log_id, seq))',
    'CREATE TABLE receipts (tenant TEXT NOT NULL, method TEXT NOT NULL, command_id TEXT NOT NULL, '
    'payload_digest TEXT NOT NULL, answer TEXT NOT NULL, PRIMARY KEY (tenant, method, command_id))',
    'CREATE TABLE lane_tables (lane TEXT PRIMARY KEY, version INTEGER NOT NULL, ddl_sha256 TEXT NOT NULL, '
    'tables TEXT NOT NULL)',
    # The log and the receipts are append-only in the file itself, not only by convention.
    "CREATE TRIGGER event_log_append_only_u BEFORE UPDATE ON event_log BEGIN SELECT RAISE(ABORT, 'append_only'); END",
    "CREATE TRIGGER event_log_append_only_d BEFORE DELETE ON event_log BEGIN SELECT RAISE(ABORT, 'append_only'); END",
    "CREATE TRIGGER receipts_append_only_u BEFORE UPDATE ON receipts BEGIN SELECT RAISE(ABORT, 'append_only'); END",
    "CREATE TRIGGER receipts_append_only_d BEFORE DELETE ON receipts BEGIN SELECT RAISE(ABORT, 'append_only'); END",
    "INSERT INTO store_meta VALUES ('epoch', 0)",
)

# ------------------------------------------------------------------------------------------- refusals


class StoreError(RuntimeError):
    """A refusal of the helper. ``str(exc)`` is its code."""


class LockOrderError(StoreError):
    """The helper's lock was asked for by a thread that holds a lock which must come after it, or a lock that
    must come after it was asked for inside a transaction."""


class CommitUnknown(StoreError):
    """COMMIT raised: the transaction may or may not be durable. The store is failed until it is reopened."""


class ReceiptExists(sqlite3.IntegrityError):
    """A receipt with this (tenant, method, command_id) is kept already; a receipt is never replaced."""


class Receipt(NamedTuple):
    payload_digest: str
    answer: object


# ------------------------------------------------------------------------- which thread holds what

_HELD = threading.local()


def in_transaction() -> bool:
    """Whether the calling thread holds a transaction (or a declaration) of any event store."""
    return getattr(_HELD, 'store', None) is not None


def require_no_transaction(what: str) -> None:
    """For code that takes a registry, daemon or run lock, or calls an engine: refuse inside a transaction."""
    if in_transaction():
        raise LockOrderError(f'lock_order:{what}')


# ------------------------------------------------------------------------------------ stored values

_LOG_ID = re.compile(r'(?:ses-[A-Za-z0-9_-]{1,60}|run:[A-Za-z0-9_-]{1,64})')
_RUN_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
_TURN_ID = re.compile(r'turn-[A-Za-z0-9_-]{1,59}')
_TOKEN = re.compile(r'[A-Za-z0-9._:-]{1,128}')
_COMMAND_ID = re.compile(r'[A-Za-z0-9._:-]{8,128}')
_DIGEST = re.compile(r'[0-9a-f]{64}')
_LANE = re.compile(r'[a-z][a-z0-9_]{0,31}')


def _check_value(value, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ValueError('value_too_deep')
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        return                      # a value that is not finite is refused by the serialiser
    if isinstance(value, dict):
        for key, inner in value.items():
            if not isinstance(key, str):
                raise TypeError('an object key is text')
            _check_value(inner, depth + 1)
        return
    if isinstance(value, list):
        for inner in value:
            _check_value(inner, depth + 1)
        return
    raise TypeError(f'not a JSON value: {type(value).__name__}')


def _wire(value) -> str:
    """The stored text of a value: canonical JSON (sorted keys, no spaces), UTF-8 that encodes. Only objects
    with text keys, arrays, text, whole and finite numbers, booleans and null are kept, so what is read back
    equals what was written and belongs to the reader. A tuple, a set or any other object is refused. A text
    with a lone surrogate is refused when it is written: SQLite's binding cannot encode it (UnicodeEncodeError,
    a ValueError), and the transaction it was in is rolled back by its block."""
    _check_value(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _timestamp(seconds: float) -> str:
    whole = int(seconds * 1000)
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(whole // 1000)) + '.%03dZ' % (whole % 1000)


# The envelope rules of event.v1 (EVENT_SCHEMA): the type list, and per type the sources it allows, the trust
# it requires and the ids it requires to be present. Every payload is the emitting lane's to shape.
EVENT_TYPES = frozenset({
    'state.changed', 'engine.output', 'tool.call', 'tool.result', 'approval.requested', 'approval.answered',
    'usage', 'limit', 'result', 'replay.gap', 'turn.started', 'turn.ended', 'steer.queued', 'steer.sent',
    'input.accepted', 'interrupt', 'stop.requested', 'stop.result', 'diff', 'checkpoint', 'fork',
    'workspace.grant', 'workspace.revoke', 'route.decision', 'effect.outcome_unknown',
})
_CORE, _ENGINE, _TOOL = frozenset({'core'}), frozenset({'engine'}), frozenset({'engine', 'plugin'})
_TYPE_RULES = {
    'state.changed': (_CORE, 'trusted', ()),
    'engine.output': (_ENGINE, None, ('run_id',)),
    'tool.call': (_TOOL, None, ()),
    'tool.result': (_TOOL, None, ()),
    'approval.requested': (_CORE, 'trusted', ()),
    'approval.answered': (_CORE, 'trusted', ()),
    'result': (_CORE, None, ()),
    'replay.gap': (_CORE, 'trusted', ()),
    'turn.started': (_CORE, None, ('turn_id',)),
    'turn.ended': (_CORE, None, ('turn_id',)),
    'steer.sent': (_CORE, None, ('turn_id',)),
    'input.accepted': (_ENGINE, None, ('turn_id',)),
    'interrupt': (_CORE, None, ()),
    'stop.requested': (_CORE, None, ()),
    'stop.result': (_CORE, 'trusted', ('run_id',)),
    'diff': (_CORE, None, ()),
    'checkpoint': (_CORE, None, ()),
    'fork': (_CORE, None, ()),
    'workspace.grant': (_CORE, None, ()),
    'workspace.revoke': (_CORE, None, ()),
    'route.decision': (_CORE, 'trusted', ()),
    'effect.outcome_unknown': (_CORE, 'trusted', ()),
}
_REQUIRED = ('session_id', 'run_id', 'turn_id', 'type', 'source', 'trust', 'payload')
_OPTIONAL = ('causation_id', 'correlation_id', 'command_id')


def _envelope(event) -> dict:
    """The event as it will be stored, less ``seq`` and ``time``; ValueError or TypeError when it breaks a rule
    of the envelope. A caller never supplies ``seq`` or ``time``: the helper assigns both."""
    if not isinstance(event, dict):
        raise TypeError('an event is an object')
    for key in ('seq', 'time'):
        if key in event:
            raise ValueError(f'assigned_by_the_store:{key}')
    unknown = set(event) - set(_REQUIRED) - set(_OPTIONAL)
    if unknown:
        raise ValueError(f'unknown_field:{sorted(unknown)[0]}')
    missing = [key for key in _REQUIRED if key not in event]
    if missing:
        raise ValueError(f'missing_field:{missing[0]}')
    log, run, turn = event['session_id'], event['run_id'], event['turn_id']
    if not isinstance(log, str) or not _LOG_ID.fullmatch(log):
        raise ValueError('session_id_not_a_log_id')
    if run is not None and (not isinstance(run, str) or not _RUN_ID.fullmatch(run)):
        raise ValueError('run_id_invalid')
    if turn is not None and (not isinstance(turn, str) or not _TURN_ID.fullmatch(turn)):
        raise ValueError('turn_id_invalid')
    kind, source, trust = event['type'], event['source'], event['trust']
    if kind not in EVENT_TYPES:
        raise ValueError('type_unknown')
    if source not in ('core', 'engine', 'plugin', 'client'):
        raise ValueError('source_unknown')
    if trust not in ('trusted', 'untrusted'):
        raise ValueError('trust_unknown')
    if source in ('engine', 'plugin') and trust != 'untrusted':
        raise ValueError('engine_or_plugin_content_is_untrusted')
    sources, required_trust, ids = _TYPE_RULES.get(kind, (None, None, ()))
    if sources is not None and source not in sources:
        raise ValueError(f'source_not_allowed_for:{kind}')
    if required_trust is not None and trust != required_trust:
        raise ValueError(f'trust_not_allowed_for:{kind}')
    for key in ids:
        if event[key] is None:
            raise ValueError(f'{key}_required_for:{kind}')
    if not isinstance(event['payload'], dict):
        raise TypeError('a payload is an object')
    for key, pattern in (('causation_id', _TOKEN), ('correlation_id', _TOKEN), ('command_id', _COMMAND_ID)):
        if key in event and (not isinstance(event[key], str) or not pattern.fullmatch(event[key])):
            raise ValueError(f'{key}_invalid')
    return json.loads(_wire(event))          # an owned copy, checked as a stored value


def _receipt_key(tenant, method, command_id) -> None:
    if not isinstance(tenant, str) or not tenant:          # common.v1: a tenant is any non-empty string
        raise TypeError('a receipt is keyed by a tenant: non-empty text')
    if not isinstance(method, str) or not _TOKEN.fullmatch(method):
        raise TypeError('a receipt is keyed by a method name')
    if not isinstance(command_id, str) or not _COMMAND_ID.fullmatch(command_id):
        raise TypeError('a receipt is keyed by a command_id: 8 to 128 of A-Z a-z 0-9 . _ : -')


def _fatal(exc: BaseException) -> bool:
    """An error after which the connection's view of the file cannot be trusted: I/O, corruption, a file that
    is not a database or cannot be opened, a broken lock protocol, a file that became read-only."""
    code = getattr(exc, 'sqlite_errorcode', None)
    return isinstance(exc, sqlite3.Error) and code is not None and (code & 0xFF) in (8, 10, 11, 14, 15, 26)


# ------------------------------------------------------------------------------------- the transaction


class Transaction:
    """One open transaction. Only the thread that opened it may use it, and only until it exits."""

    __slots__ = ('_store', '_owner', '_active', '_events')

    def __init__(self, store: 'EventStore') -> None:
        self._store = store
        self._owner = threading.get_ident()
        self._active = True
        self._events: list = []

    def _use(self) -> None:
        if not self._active or threading.get_ident() != self._owner:
            raise StoreError('transaction_not_active_for_caller')
        if self._store._failed is not None:
            raise StoreError('store_failed')

    def _run(self, sql: str, params=()) -> list:
        """Run one statement and fetch ALL its rows inside the same handler: SQLite can find corruption or an I/O
        error while it steps through rows, after the statement began, and that must fail the store too."""
        conn = self._store._conn
        if not conn.in_transaction:          # SQLite ended it on its own: nothing more may be written
            raise StoreError('transaction_aborted')
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            if _fatal(exc):
                self._store._fail(exc)
            raise

    def append(self, event) -> int:
        """One event.v1 without ``seq`` and ``time``; returns its seq, which is gapless in its log. The seq is
        read from the log itself and the event is ONE insert, so an append that fails writes nothing: a caller
        that catches the failure and goes on leaves no hole."""
        self._use()
        record = _envelope(event)
        log = record['session_id']
        (highest,), = self._run('SELECT COALESCE(MAX(seq), 0) FROM event_log WHERE log_id = ?', (log,))
        seq = highest + 1
        record['seq'] = seq
        record['time'] = _timestamp(self._store._clock())
        text = _wire(record)
        self._run('INSERT INTO event_log (log_id, seq, event) VALUES (?, ?, ?)', (log, seq, text))
        self._events.append(text)
        return seq

    def receipt_get(self, tenant, method, command_id):
        """None, or ``Receipt(payload_digest, answer)``; the answer is the caller's own copy."""
        self._use()
        _receipt_key(tenant, method, command_id)
        rows = self._run('SELECT payload_digest, answer FROM receipts WHERE tenant = ? AND method = ? AND command_id = ?',
                         (tenant, method, command_id))
        return None if not rows else Receipt(rows[0][0], json.loads(rows[0][1]))

    def receipt_put(self, tenant, method, command_id, payload_digest, answer) -> None:
        """Keep the receipt in this transaction. One that exists is never replaced: ``ReceiptExists``."""
        self._use()
        _receipt_key(tenant, method, command_id)
        if not isinstance(payload_digest, str) or not _DIGEST.fullmatch(payload_digest):
            raise TypeError('a payload digest is SHA-256 as 64 lower-case hex digits')
        text = _wire(answer)
        try:
            self._run('INSERT INTO receipts (tenant, method, command_id, payload_digest, answer) VALUES (?, ?, ?, ?, ?)',
                      (tenant, method, command_id, payload_digest, text))
        except sqlite3.IntegrityError as exc:
            raise ReceiptExists('receipt_exists') from exc

    def execute(self, sql, params=()) -> list:
        """One statement on tables a lane declared: rows read, inserted, updated or deleted, and nothing else
        (no helper table, no schema change, no pragma, no attach, no transaction control). Returns the rows."""
        self._use()
        if not isinstance(sql, str):
            raise TypeError('sql is text')
        conn = self._store._conn
        conn.set_authorizer(self._store._lane_tables_only)
        try:
            return self._run(sql, params)
        finally:
            conn.set_authorizer(None)


class _Opening:
    """What ``transaction()`` returns: the transaction begins on ``__enter__`` and ends on ``__exit__``."""

    __slots__ = ('_store', '_tx')

    def __init__(self, store: 'EventStore') -> None:
        self._store, self._tx = store, None

    def __enter__(self) -> Transaction:
        self._tx = self._store._begin()
        return self._tx

    def __exit__(self, kind, exc, tb) -> bool:
        tx, self._tx = self._tx, None
        if tx is not None:
            self._store._end(tx, exc)
        return False


# ------------------------------------------------------------------------------------------ the store


class EventStore:
    """The helper over a real file: WAL, ``synchronous=FULL``, one writer process (``flock``), one writer
    connection, one transaction owner at a time."""

    _SYNCHRONOUS = 'FULL'
    _SYNCHRONOUS_LEVEL = 2

    def __init__(self, path, *, clock: Callable[[], float] | None = None) -> None:
        if fcntl is None:
            raise StoreError('unsupported_platform')
        self._setup(os.fspath(path), clock)
        self._lock_fd = self._take_file_lock(self._path + '.lock')
        try:
            self._make_file(self._path)
            self._open()
        except BaseException:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise

    # -- opening

    def _setup(self, path: str, clock) -> None:
        self._path = path
        self._clock = clock if clock is not None else time.time
        self._lock = threading.Lock()            # the helper's lock: one transaction owner at a time
        self._guard = threading.Lock()           # closed, failed and the open count; never held while waiting
        self._publish_guard = threading.Lock()   # the publication queue; never held while a subscriber runs
        self._pending: collections.deque = collections.deque()
        self._publishing = False
        self._subscribers: list = []
        self._probes: list = []
        self._declared: dict = {}                # lane -> its tables, declared in this process
        self._lane_tables: set = set()
        self._open_count = 0
        self._commits = self._rollbacks = 0
        self._closed = False
        self._failed: str | None = None
        self._conn = None
        self._lock_fd = None
        self._epoch = 0

    @staticmethod
    def _private(info, code: str) -> None:
        """A file of the store is a regular file of this user that nobody else can read or write."""
        if not stat.S_ISREG(info.st_mode):
            raise StoreError(f'{code}_not_a_regular_file')
        if hasattr(os, 'geteuid') and info.st_uid != os.geteuid():
            raise StoreError(f'{code}_owned_by_another_user')
        if info.st_mode & 0o077:
            raise StoreError(f'{code}_not_private')

    @staticmethod
    def _take_file_lock(path: str) -> int:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            EventStore._private(os.fstat(fd), 'store_lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise StoreError('store_in_use') from None
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _make_file(path: str) -> None:
        """The database file: made here, 0600, with its directory synced; or one that exists, that is a regular
        file of this user, not a link, and open to nobody else, and so are the WAL and shared-memory files
        beside it if they exist (SQLite gives those the database file's mode). A file open to others is
        refused, never quietly narrowed: what it held may already have been read. The sidecars are checked
        first, whether the database file exists or is about to be made: SQLite would open either."""
        for suffix in ('-wal', '-shm'):
            try:
                info = os.lstat(path + suffix)
            except FileNotFoundError:
                continue
            EventStore._private(info, 'store_sidecar')
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            EventStore._private(os.lstat(path), 'store_path')
            return
        os.close(fd)
        folder = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)

    def _open(self) -> None:
        conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False, timeout=0)
        try:
            # First, read only: a file that is not this store is refused before anything in it is changed (its
            # journal mode included).
            try:
                application = conn.execute('PRAGMA application_id').fetchone()[0]
            except sqlite3.DatabaseError as exc:
                if getattr(exc, 'sqlite_errorcode', 0) & 0xFF == 26:      # SQLITE_NOTADB
                    raise StoreError('not_an_event_store') from exc
                raise
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
            fresh = application == 0 and version == 0 and not names
            if not fresh:
                if application != APPLICATION_ID:
                    raise StoreError('not_an_event_store')
                if version > SCHEMA_VERSION:
                    raise StoreError('store_schema_too_new')
                if version < SCHEMA_VERSION:
                    raise StoreError('store_schema_too_old')
            if conn.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() != 'wal':
                raise StoreError('wal_not_available')
            conn.execute(f'PRAGMA synchronous={self._SYNCHRONOUS}')
            if conn.execute('PRAGMA synchronous').fetchone()[0] != self._SYNCHRONOUS_LEVEL:
                raise StoreError('synchronous_not_set')
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA trusted_schema=OFF')
            if conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                raise StoreError('foreign_keys_not_set')
            if fresh:
                conn.execute('BEGIN IMMEDIATE')
                try:
                    for statement in _HELPER_DDL:
                        conn.execute(statement)
                    conn.execute(f'PRAGMA application_id={APPLICATION_ID}')
                    conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                    conn.execute('COMMIT')
                except BaseException:
                    if conn.in_transaction:
                        conn.execute('ROLLBACK')
                    raise
            if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise StoreError('store_corrupt')
            conn.execute('BEGIN IMMEDIATE')
            try:
                conn.execute("UPDATE store_meta SET value = value + 1 WHERE key = 'epoch'")
                self._epoch = conn.execute("SELECT value FROM store_meta WHERE key = 'epoch'").fetchone()[0]
                conn.execute('COMMIT')
            except BaseException:
                if conn.in_transaction:
                    conn.execute('ROLLBACK')
                raise
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    # -- what it is

    @property
    def epoch(self) -> int:
        """Rises by one each time the store is opened; a cursor carries it."""
        return self._epoch

    @property
    def path(self) -> str:
        return self._path

    # -- lock order

    def add_lock_probe(self, name: str, probe: Callable[[], bool]) -> None:
        """Name a lock that must never be held when the helper's lock is taken, and a function that says whether
        the calling thread holds it. Made at composition, before the store is used."""
        if not isinstance(name, str) or not _TOKEN.fullmatch(name):
            raise TypeError('a lock probe has a name: a token')
        if not callable(probe):
            raise TypeError('a lock probe is a function of no arguments')
        with self._guard:
            self._probes.append((name, probe))

    def _check_lock_order(self) -> None:
        with self._guard:
            probes = tuple(self._probes)
        for name, probe in probes:
            try:
                held = probe()
            except Exception as exc:
                raise LockOrderError(f'lock_order:{name}') from exc
            if held:
                raise LockOrderError(f'lock_order:{name}')

    # -- the transaction

    def transaction(self) -> _Opening:
        """``with store.transaction() as tx``; see the module's text."""
        return _Opening(self)

    def _begin(self) -> Transaction:
        if in_transaction():
            raise StoreError('nested_transaction')
        self._check_lock_order()
        self._lock.acquire()
        try:
            with self._guard:
                if self._closed:
                    raise StoreError('store_closed')
                if self._failed is not None:
                    raise StoreError('store_failed')
                self._open_count += 1
            try:
                self._statement('BEGIN IMMEDIATE')
                tx = Transaction(self)
                _HELD.store = self
            except BaseException:
                with self._guard:
                    self._open_count -= 1
                raise
            return tx
        except BaseException:
            self._lock.release()
            raise

    def _end(self, tx: Transaction, error) -> None:
        committed = False
        try:
            if error is None:
                self._commit(tx)
                committed = True
            else:
                self._rollback()
        finally:
            tx._active = False
            _HELD.store = None
            with self._guard:
                self._open_count -= 1
            self._lock.release()
        if committed:
            self._publish()

    def _before_commit(self) -> None:
        """Where the fake can be told to fail a commit before anything is published."""

    def _commit(self, tx: Transaction) -> None:
        try:
            if self._failed is not None:
                raise StoreError('store_failed')
            if not self._conn.in_transaction:   # SQLite ended it on its own: there is nothing to commit
                raise StoreError('transaction_aborted')
            self._before_commit()
        except BaseException:
            self._rollback()
            raise
        try:
            self._conn.execute('COMMIT')
        except BaseException as exc:
            self._fail(exc, 'commit_outcome_unknown')
            with contextlib.suppress(Exception):
                if self._conn.in_transaction:
                    self._conn.execute('ROLLBACK')
            raise CommitUnknown('commit_outcome_unknown') from exc
        self._commits += 1
        if tx._events:
            with self._publish_guard:
                self._pending.append(tuple(tx._events))

    def _rollback(self, counted: bool = True) -> None:
        if counted:
            self._rollbacks += 1
        try:
            if self._conn.in_transaction:
                self._conn.execute('ROLLBACK')
        except sqlite3.Error as exc:
            self._fail(exc, 'rollback_failed')

    def _statement(self, sql: str) -> None:
        self._query(sql)

    def _query(self, sql: str, params=()) -> list:
        """The helper's own statement, outside a lane's transaction: every row fetched under the fatal rule."""
        try:
            return self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            if _fatal(exc):
                self._fail(exc)
            raise

    def _fail(self, exc: BaseException, why: str | None = None) -> None:
        with self._guard:
            if self._failed is None:
                self._failed = why or getattr(exc, 'sqlite_errorname', None) or type(exc).__name__
        LOG.error('event store failed (%s): %s', self._failed, exc)

    def _lane_tables_only(self, action, first, second, database, source) -> int:
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        # The database name is not checked: SQLite names none for some reads it adds itself (an ORDER BY on an
        # integer primary key), and no lane can make a temporary table or attach a file (both are refused).
        if action in (sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) \
                and first in self._lane_tables:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    # -- after the commit

    def subscribe(self, fn: Callable[[tuple], object]) -> Callable[[], None]:
        """Call ``fn(events)`` with the events of each commit that appended any, after the commit, outside every
        lock of the helper, in commit order, one commit at a time. Each call gets its own copies. A subscriber
        that raises is logged and does not affect the commit, the caller or another subscriber. Returns a
        function that unsubscribes."""
        if not callable(fn):
            raise TypeError('a subscriber is a function of one argument')
        entry = (object(), fn)       # a registration of its own: the same function may subscribe twice
        with self._publish_guard:
            self._subscribers.append(entry)

        def unsubscribe() -> None:
            """Remove THIS registration (by identity, not by equality); a second call does nothing."""
            with self._publish_guard:
                self._subscribers = [other for other in self._subscribers if other is not entry]
        return unsubscribe

    def _publish(self) -> None:
        """Deliver what is queued. One thread delivers at a time, in commit order; a commit made while another
        thread is delivering is delivered by that thread, so the caller of a commit may return before its
        subscribers have run. A subscriber may open a transaction of its own: no lock of the helper is held."""
        with self._publish_guard:
            if self._publishing:
                return
            self._publishing = True
        try:
            while True:
                with self._publish_guard:
                    if not self._pending:
                        self._publishing = False
                        return
                    batch = self._pending.popleft()
                    subscribers = [entry[1] for entry in self._subscribers]
                for fn in subscribers:
                    try:
                        fn(tuple(json.loads(text) for text in batch))
                    except Exception:    # noqa: BLE001 - a subscriber's failure is its own
                        LOG.exception('an event-store subscriber failed')
        except BaseException:
            with self._publish_guard:
                self._publishing = False
            raise

    # -- tables of the lanes

    def register_tables(self, lane: str, version: int, ddl: str) -> None:
        """Declare a lane's tables, once per process, at composition: ``CREATE TABLE`` and ``CREATE INDEX`` on its
        own new tables and nothing else, all of it or none of it. A lane whose tables a previous run of the
        store declared declares again with the same version and text, which changes nothing; a changed
        declaration is refused (``declaration_changed``): no migration exists yet."""
        if not isinstance(lane, str) or not _LANE.fullmatch(lane):
            raise TypeError('a lane is named by lower-case letters, digits and _')
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise TypeError('a declaration has a version: a whole number from 1')
        if not isinstance(ddl, str) or not ddl.strip():
            raise TypeError('a declaration is SQL text')
        if in_transaction():
            raise StoreError('nested_transaction')
        self._check_lock_order()
        digest = hashlib.sha256(ddl.encode('utf-8')).hexdigest()
        with self._lock:
            with self._guard:
                if self._closed:
                    raise StoreError('store_closed')
                if self._failed is not None:
                    raise StoreError('store_failed')
                if lane in self._declared:
                    raise StoreError('declared_once')
            _HELD.store = self
            try:
                tables = self._declare(lane, version, ddl, digest)
            finally:
                _HELD.store = None
            self._declared[lane] = tables
            self._lane_tables |= tables

    def _declare(self, lane: str, version: int, ddl: str, digest: str) -> frozenset:
        conn = self._conn
        rows = self._query('SELECT version, ddl_sha256, tables FROM lane_tables WHERE lane = ?', (lane,))
        if rows:
            if (rows[0][0], rows[0][1]) != (version, digest):
                raise StoreError('declaration_changed')
            return frozenset(json.loads(rows[0][2]))
        taken = {name for (name,) in self._query("SELECT name FROM sqlite_master")}
        rules = _DeclarationRules(taken, lane + '_')
        self._statement('BEGIN IMMEDIATE')
        try:
            for statement in _statements(ddl):
                if not _has_sql(statement):
                    continue                    # a comment or blank text after the last statement
                (before,), = conn.execute('SELECT count(*) FROM sqlite_master').fetchall()
                conn.set_authorizer(rules)
                try:
                    conn.execute(statement)
                finally:
                    conn.set_authorizer(None)
                (after,), = conn.execute('SELECT count(*) FROM sqlite_master').fetchall()
                if after == before:             # CREATE ... IF NOT EXISTS on a name that exists, or a bare SELECT
                    raise StoreError('declaration_statement_made_nothing')
            added = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()} - taken
            if not added:
                raise StoreError('declaration_made_no_table')
            # CREATE TABLE ... AS SELECT makes rows without an INSERT the authorizer could refuse: a declaration
            # declares, so every table it made must be empty.
            for table in sorted(added):
                quoted = '"' + table.replace('"', '""') + '"'
                if conn.execute(f'SELECT 1 FROM {quoted} LIMIT 1').fetchall():
                    raise StoreError('declaration_wrote_rows')
            conn.execute('INSERT INTO lane_tables (lane, version, ddl_sha256, tables) VALUES (?, ?, ?, ?)',
                         (lane, version, digest, json.dumps(sorted(added))))
        except BaseException as exc:
            if _fatal(exc):
                self._fail(exc)
            self._rollback(counted=False)       # a declaration is not a transaction of the counts
            if rules.misnamed and isinstance(exc, sqlite3.DatabaseError):
                raise StoreError(f'table_not_named_for_lane:{rules.misnamed[0]}') from exc
            raise
        try:
            conn.execute('COMMIT')
        except BaseException as exc:
            self._fail(exc, 'commit_outcome_unknown')
            with contextlib.suppress(Exception):
                if conn.in_transaction:
                    conn.execute('ROLLBACK')
            raise CommitUnknown('commit_outcome_unknown') from exc
        return frozenset(added)

    # -- closing

    def close(self) -> None:
        """Close the store. Refused (``transactions_active``) while a transaction is open; a transaction that
        waited for its turn while the store closed is refused (``store_closed``) when its turn comes. A second
        close does nothing."""
        with self._guard:
            if self._open_count:
                raise StoreError('transactions_active')
            if self._closed:
                return
            self._closed = True
        with self._lock:              # a thread between taking the lock and reading "closed" finishes first
            if self._conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.close()
            self._release_file_lock()

    def _release_file_lock(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)   # closing the descriptor releases the flock
            self._lock_fd = None


class _DeclarationRules:
    """The SQLite authorizer of one declaration: what each action of its statements may do. A temporary table and
    an attached file are other action codes, refused by the default; a name that exists (the helper's tables
    among them) is refused, so CREATE TABLE IF NOT EXISTS cannot pass quietly; a name beginning sqlite_ SQLite
    refuses itself. Every table is named <lane>_<name> (the Liaison's ruling of Nov 14, 4:03 PM). Its decisions
    are tested one by one as well as through SQLite, because SQLite does not ask about every object a statement
    touches (an index of a constant reads no column, review 2 of #373)."""

    _SCHEMA = ('sqlite_master', 'sqlite_schema')

    def __init__(self, taken, prefix: str) -> None:
        self.taken, self.prefix = frozenset(taken), prefix
        self.created: set = set()
        self.indexes: set = set()
        self.misnamed: list = []

    def __call__(self, action, first, second, database, source) -> int:
        if action == sqlite3.SQLITE_CREATE_TABLE:
            if first in self.taken:
                return sqlite3.SQLITE_DENY
            if not first.startswith(self.prefix) or first == self.prefix:
                self.misnamed.append(first)
                return sqlite3.SQLITE_DENY
            self.created.add(first)
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_CREATE_INDEX:
            # Only on a table this declaration made. A name that exists is refused by the statement rule:
            # CREATE INDEX IF NOT EXISTS on it makes no call here at all, and makes nothing.
            if second not in self.created:
                return sqlite3.SQLITE_DENY
            self.indexes.add(first)
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_REINDEX:              # what creating an index asks for, of that same index
            return sqlite3.SQLITE_OK if first in self.indexes else sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE) and first in self._SCHEMA:
            return sqlite3.SQLITE_OK                       # what a CREATE writes, and nothing a statement can name
        if action == sqlite3.SQLITE_READ and (first in self.created or first in self._SCHEMA):
            return sqlite3.SQLITE_OK
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY


def _has_sql(statement: str) -> bool:
    """Whether a piece of declaration text holds more than blanks, semicolons and comments. It reads from the start
    in order, as SQLite does: a ``--`` comment runs to the end of its line, a ``/*`` comment to its ``*/`` or to the
    end of the text, and the first character of anything else is SQL. So a comment marker inside another comment
    never hides a statement (review 3 of #373). The text that is executed is never altered."""
    position, end = 0, len(statement)
    while position < end:
        if statement[position] in ' \t\n\f\r;':
            position += 1
        elif statement.startswith('--', position):
            newline = statement.find('\n', position + 2)
            position = end if newline < 0 else newline + 1
        elif statement.startswith('/*', position):
            closing = statement.find('*/', position + 2)
            position = end if closing < 0 else closing + 2
        else:
            return True
    return False


def _statements(ddl: str):
    statement = ''
    for line in ddl.splitlines(keepends=True):
        for char in line:
            statement += char
            if char == ';' and sqlite3.complete_statement(statement):
                yield statement
                statement = ''
    if statement.strip():
        yield statement


# --------------------------------------------------------------------------------------------- the fake


class FakeStore(EventStore):
    """The helper's code over a temporary file with durability off, for other lanes' tests.

    Same interface and the same refusals as ``EventStore`` (it IS that code); what differs: the file is in a
    temporary folder that goes with the object, ``synchronous`` is OFF, there is no lock file, so nothing
    tested against it proves recovery from a crash. ``fail_commit = True`` makes the next commits fail
    before anything is published (an ``OSError``, rolled back; the store stays usable). ``receipts``,
    ``events``, ``rows``, ``commits``, ``rollbacks``, ``open_transactions`` and ``closed`` are for a test's
    assertions: copies of what is committed, read through a connection of their own outside every
    transaction; nothing can be written through them.
    """

    _SYNCHRONOUS = 'OFF'
    _SYNCHRONOUS_LEVEL = 0

    def __init__(self, log: list | None = None, *, clock: Callable[[], float] | None = None) -> None:
        folder = tempfile.mkdtemp(prefix='events-fake-store-')
        self._finalizer = weakref.finalize(self, shutil.rmtree, folder, True)
        self._setup(os.path.join(folder, 'events.sqlite3'), clock)
        self._log = log
        self.fail_commit = False
        self._open()

    def _before_commit(self) -> None:
        if self.fail_commit:
            raise OSError('the commit failed')

    def close(self) -> None:
        was_closed = self._closed
        super().close()
        if self._log is not None and not was_closed:
            self._log.append(('close', 'store', None))

    # -- for assertions only

    def _reader(self):
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.execute('PRAGMA query_only=ON')
        return contextlib.closing(conn)

    def _read(self, sql: str, params=()) -> list:
        with self._reader() as conn:
            return conn.execute(sql, params).fetchall()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def commits(self) -> int:
        return self._commits

    @property
    def rollbacks(self) -> int:
        return self._rollbacks

    @property
    def open_transactions(self) -> int:
        return self._open_count

    @property
    def receipts(self) -> dict:
        """``{(tenant, method, command_id): Receipt(payload_digest, answer)}``, as committed."""
        return {(tenant, method, command_id): Receipt(digest, json.loads(answer))
                for tenant, method, command_id, digest, answer in
                self._read('SELECT tenant, method, command_id, payload_digest, answer FROM receipts')}

    @property
    def events(self) -> list:
        """Every committed event, in the order of the commits."""
        return [json.loads(text) for (text,) in self._read('SELECT event FROM event_log ORDER BY position')]

    @property
    def rows(self) -> dict:
        """Every row of every table declared in this process: ``{(table, its first column): {the other columns}}``."""
        out = {}
        with self._reader() as conn:
            for table in sorted(self._lane_tables):
                cursor = conn.execute(f'SELECT * FROM "{table}"')
                names = [column[0] for column in cursor.description]
                for row in cursor.fetchall():
                    out[(table, row[0])] = dict(zip(names[1:], row[1:]))
        return out
