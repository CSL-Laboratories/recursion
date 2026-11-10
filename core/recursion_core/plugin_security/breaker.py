"""Engine breaker keyed by connection (CSL-608, grill Q8) and the faulted-run register.

The breaker trips on the connection a route is billed through (an API key or a
subscription login), never on an engine or model name. Only a typed limit event
trips it; the core's own timeouts and deadlines never do (S3, S4). A tripped
connection resets only through a successful leased probe: ``begin_probe`` hands out
one lease per tripped connection, and ``reset_after_probe`` clears the trip only for
that lease. Each trip stores a generation. The lease is bound to the generation it
saw, and a reset deletes the row only when that generation is still current, so a
probe that finished before a later trip cannot clear it. That holds for two breakers
on one store: the generation lives in SQLite, not in one process's lease table. A
lease is one use and expires after ``probe_lease_seconds``.

A run that ended in a fault is recorded here too, so an effect proposed by that run,
or by any run on a tripped connection, is refused at the approval gate (4.2, B15).
The host binds each run to its connection, the plugin that owns it, and the provider
and model version it runs on. An effect may cite only a run its own plugin owns, so a
plugin whose run faulted cannot borrow another healthy run's id.

Runs outlive a daemon restart (grill Q38), so bindings, faults and trips are kept in a
SQLite store (WAL, ``synchronous=FULL``) and survive a restart; losing them would remove
a refusal. Probe leases stay in memory: a restart only means probing again. Like the
authority ledger, the daemon never creates the store; ``create_breaker_store`` is the
installer's step, and a missing store refuses. The same-uid limit in ``authority.py``
applies: in-process plugin code can rewrite this file (B17).
This is the domain-neutral half; any consumer's own breakers stay with the consumer.
"""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import threading
import time
from typing import Callable

from .errors import Refused
from .paths import check_store_path

__all__ = ["ConnectionBreaker", "TYPED_LIMITS", "create_breaker_store"]

TYPED_LIMITS = frozenset({"plan_limit", "rate_limit", "usage_window_exhausted"})
_NOT_ENGINE_FAULTS = frozenset({"core_timeout", "core_deadline", "caller_cancelled"})
_ID = re.compile(r"[A-Za-z0-9_.:@/-]{1,128}")
_STORE_MAGIC = "csl-breaker-store/v0"


def _check_id(value: object, code: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise Refused(code)
    return value


def create_breaker_store(path: str) -> None:
    """Installer step: create the breaker store. Refuses if it exists."""
    path = check_store_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        db = sqlite3.connect(path, isolation_level=None)
    except sqlite3.OperationalError as exc:
        if "unable to open" in str(exc).lower():
            try:
                os.unlink(path)
            except OSError:
                pass
            raise Refused("store_path_too_long") from None
        raise
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        db.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, connection TEXT NOT NULL,"
                   " plugin_id TEXT NOT NULL, provider TEXT NOT NULL, model_version TEXT NOT NULL)")
        db.execute("CREATE TABLE faulted (run_id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE tripped (connection TEXT PRIMARY KEY, event_type TEXT NOT NULL,"
                   " generation INTEGER NOT NULL)")
        db.execute("INSERT INTO meta VALUES ('magic', ?)", (_STORE_MAGIC,))
    finally:
        db.close()


class ConnectionBreaker:
    def __init__(self, store_path: str, *, clock: Callable[[], float] = time.monotonic,
                 probe_lease_seconds: float = 300.0) -> None:
        store_path = check_store_path(store_path)
        if not os.path.isfile(store_path):
            raise Refused("breaker_store_missing")
        self._db = sqlite3.connect(store_path, isolation_level=None, check_same_thread=False,
                                   timeout=30)
        try:
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=30000")
            meta = dict(self._db.execute("SELECT k, v FROM meta").fetchall())
        except sqlite3.DatabaseError:
            self._db.close()
            raise Refused("breaker_store_invalid") from None
        if meta.get("magic") != _STORE_MAGIC:
            self._db.close()
            raise Refused("breaker_store_invalid")
        self._ensure_generation()
        self._lock = threading.Lock()
        self._clock = clock
        self._lease_seconds = probe_lease_seconds
        # lease id, expiry, trip generation the lease is allowed to clear
        self._leases: dict[str, tuple[str, float, int]] = {}

    def bind_run(self, run_id: str, connection: str, plugin_id: str, provider: str,
                 model_version: str) -> None:
        """Record a run's connection, owner, provider and model; a run cannot move any of them."""
        row = (_check_id(run_id, "invalid_run_id"), _check_id(connection, "invalid_connection"),
               _check_id(plugin_id, "invalid_plugin_id"), _check_id(provider, "invalid_provider"),
               _check_id(model_version, "invalid_model_version"))
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                bound = self._db.execute("SELECT run_id, connection, plugin_id, provider, model_version"
                                         " FROM runs WHERE run_id=?", (row[0],)).fetchone()
                if bound is None:
                    self._db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?)", row)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        if bound is not None and tuple(bound) != row:
            raise Refused("run_connection_mismatch" if bound[1] != row[1] else "run_binding_mismatch")

    def report(self, connection: str, event_type: str) -> bool:
        """Report a typed event. Returns True when it tripped the connection."""
        connection = _check_id(connection, "invalid_connection")
        if event_type in _NOT_ENGINE_FAULTS:
            return False
        if event_type not in TYPED_LIMITS:
            raise Refused("untyped_breaker_event")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT generation FROM tripped WHERE connection=?", (connection,)).fetchone()
                generation = 1 if row is None or row[0] is None else int(row[0]) + 1
                self._db.execute(
                    "INSERT INTO tripped (connection, event_type, generation) VALUES (?, ?, ?) "
                    "ON CONFLICT(connection) DO UPDATE SET event_type = excluded.event_type, "
                    "generation = excluded.generation",
                    (connection, event_type, generation))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._leases.pop(connection, None)
        return True

    def fault_run(self, run_id: str) -> None:
        run_id = _check_id(run_id, "invalid_run_id")
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO faulted VALUES (?)", (run_id,))

    def begin_probe(self, connection: str) -> str:
        """Lease the one probe allowed on a tripped connection. Returns the lease id."""
        connection = _check_id(connection, "invalid_connection")
        with self._lock:
            row = self._db.execute(
                "SELECT generation FROM tripped WHERE connection=?", (connection,)).fetchone()
            if row is None or row[0] is None:
                raise Refused("connection_not_tripped")
            now = self._clock()
            held = self._leases.get(connection)
            if held is not None and held[1] > now:
                raise Refused("probe_lease_held")
            lease = secrets.token_hex(16)
            self._leases[connection] = (lease, now + self._lease_seconds, int(row[0]))
            return lease

    def reset_after_probe(self, connection: str, lease: object, probe_ok: bool) -> bool:
        """Spend the probe lease. The trip clears only for a live lease and ``probe_ok is True``."""
        connection = _check_id(connection, "invalid_connection")
        with self._lock:
            held = self._leases.get(connection)
            if held is None or type(lease) is not str or not secrets.compare_digest(held[0], lease):
                return False
            del self._leases[connection]
            if held[1] <= self._clock() or probe_ok is not True:
                return False
            # A trip that landed after this lease was issued has a newer generation.
            cursor = self._db.execute(
                "DELETE FROM tripped WHERE connection=? AND generation=?", (connection, held[2]))
            return cursor.rowcount == 1

    def _ensure_generation(self) -> None:
        """Stores created before the trip generation column gain it, starting at 1."""
        columns = [row[1] for row in self._db.execute("PRAGMA table_info(tripped)")]
        if "generation" in columns:
            return
        try:
            self._db.execute(
                "ALTER TABLE tripped ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
        except sqlite3.DatabaseError:
            self._db.close()
            raise Refused("breaker_store_invalid") from None

    def _is_tripped(self, connection: str) -> bool:
        return self._db.execute("SELECT 1 FROM tripped WHERE connection=?", (connection,)).fetchone() is not None

    def tripped(self, connection: str) -> bool:
        with self._lock:
            return self._is_tripped(connection)

    def check(self, run_id: str, connection: str, plugin_id: str) -> dict:
        """Refuse an effect from a faulted, mismatched or unowned run, or a tripped connection.

        Returns the run's bound provider and model version for the authority profile.
        """
        with self._lock:
            bound = self._db.execute("SELECT connection, plugin_id, provider, model_version FROM runs"
                                     " WHERE run_id=?", (run_id,)).fetchone()
            if bound is None:
                raise Refused("run_unknown")
            if bound[1] != plugin_id:
                raise Refused("run_not_owned")
            if bound[0] != connection:
                raise Refused("run_connection_mismatch")
            if self._db.execute("SELECT 1 FROM faulted WHERE run_id=?", (run_id,)).fetchone():
                raise Refused("run_faulted")
            if self._is_tripped(connection):
                raise Refused("connection_tripped")
            return {"provider": bound[2], "model_version": bound[3]}

    def close(self) -> None:
        self._db.close()
