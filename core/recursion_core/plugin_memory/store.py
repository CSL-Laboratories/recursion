"""The store of one plugin's saved data for one install identity, kept by the daemon.

ONE SQLite file per plugin AND install identity: ``<state>/plugin-memory/<plugin id>/<identity>/memory.sqlite3``
under the state folder the daemon is given (the daemon's one resolver; nothing here picks a folder), owner-only,
WAL. Only the daemon opens it: no host, plugin or run is given its path or a descriptor, and no answer or
refusal carries a path.

* IDENTITY. ``identity_for``: the signer key id of a signed plugin; for an unsigned one ``unsigned-person`` or
  ``unsigned-model`` by the install record's origin mark. A store opened for the same plugin id with another
  identity is a new, empty store; the earlier one is listed by ``set_aside`` and is never deleted or handed.
* THE CALLS (``put``, ``get``, ``list_keys``, ``delete``) are what the channel's ``memory.*`` names reach once
  both grants hold. Each answers ``{"ok": true, ...}`` or ``{"ok": false, "code": ..., what, why, next,
  changed}`` (the words of ``words.py``). No argument names a plugin, an install or a namespace.
* FRAMES. Every answer fits one frame of the channel: ``MAX_VALUE_BYTES`` (the largest value a put can bring
  and a get can carry) is derived here from ``MAX_FRAME_BYTES``, never a second number; a list fills its page
  until the next key would pass the frame.
* BOUNDS are settings rows (``plugin_memory.*``, read when Recursion starts); a write over one is refused
  ``memory_full`` naming the row, in ONE transaction, so nothing is half written; a full disk gives the same
  code with the disk as the cause.
* OPENING. A file that fails SQLite's integrity check, or that SQLite calls corrupt or not a database, is
  moved aside (never deleted) and this store answers ``memory_unreadable``; other plugins' stores are other
  files and go on. Any other error at open (the file busy or locked by another connection, read-only, a
  full disk) moves nothing: the file stays where it is, the store answers ``memory_unavailable`` and tries
  to open it again at the next call. The words tell a read-only file from a read-only folder and name the
  act for each. A file another program holds, or that cannot be written, AFTER the store opened ends that one
  call in ``memory_unavailable`` too, in words, after the call's wait ``CALL_WAIT_S``: the call's transaction
  is rolled back, so the write did not happen, and nothing is moved.
* THE RECORD. One row per write, delete and set-aside goes to ``record`` (the daemon's sink for its event
  log): time, plugin, identity, call, the key's SHA-256 and length (its text only when
  ``plugin_memory.record_key_text`` is true), the store's bytes before and after, the revision and the result
  or refusal code. A read is not recorded. A write committed after ``host_ended`` was called says so. The
  row of a write is sent BEFORE its commit: a sink that raises rolls the write back, so no write stands
  without its row. Each open sends one set-aside row for every other install's store it finds beside its
  own, so a return to an earlier signer leaves rows as a first install does.
* The table keeps a namespace column ``ns`` and a ``kind`` column, each held empty by the table's own
  check, so a later slice fills them without a migration. There is no state column.
"""

from __future__ import annotations

import base64
import collections
import datetime
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable

from recursion_core.control_transport.framing import MAX_FRAME_BYTES, encode_frame
from recursion_core.daemon.launcher import private_dir
from recursion_core.plugin_security.host_channel import MAX_ID, memory_key_ok
from recursion_core.settings import NO_LIMIT

from . import words

STORE_DIR = "plugin-memory"
STORE_FILE = "memory.sqlite3"
DAMAGED = ".damaged-"
UNSIGNED = ("unsigned-person", "unsigned-model")
ORIGINS = ("person", "model")
# The rows this store reads (settings.py declares them); one value serves every granted plugin.
ROWS = ("plugin_memory.plugins", "plugin_memory.max_bytes_per_plugin", "plugin_memory.max_item_bytes",
        "plugin_memory.max_items_per_plugin", "plugin_memory.list_page_max", "plugin_memory.calls_per_minute",
        "plugin_memory.sync", "plugin_memory.wal_max_bytes", "plugin_memory.record_each_write",
        "plugin_memory.record_key_text")
PACE_WINDOW_S = 60.0  # "per minute" is the row's own meaning, not a figure a person picks
# How long an open waits for a store file another connection holds, in seconds. It becomes the settings row
# plugin_memory.open_wait_seconds; until that row exists it is this one named value.
OPEN_WAIT_S = 5.0
# How long each call after the open waits for a store file another connection holds, in seconds, before it is
# answered memory_unavailable. It becomes the settings row plugin_memory.call_wait_seconds; until that row
# exists it is this one named value.
CALL_WAIT_S = 5.0
_KEY_ID = re.compile(r"[a-z0-9]{1,16}:[0-9a-f]{1,64}")
_PLUGIN_ID = re.compile(r"[a-z][a-z0-9_.-]{1,63}")
_SQLITE_FULL, _SQLITE_CORRUPT, _SQLITE_NOTADB = 13, 11, 26
_SQLITE_BUSY, _SQLITE_LOCKED, _SQLITE_READONLY = 5, 6, 8
_TIME_SHAPE = "2026-11-23T21:00:00.000Z"  # the form of written_at: its length is what the frame bound counts


def _frame_len(message: dict) -> int:
    return len(encode_frame(message, max_bytes=1 << 62)) - 4


# The longest key a frame can carry: 256 bytes that each escape to two in JSON.
_LONGEST_KEY = '"' * 256
_PUT_ENVELOPE = _frame_len({"type": "call", "id": MAX_ID, "name": "memory.put",
                            "args": {"key": _LONGEST_KEY, "value_b64": "", "if_revision": MAX_ID}})
_GET_ENVELOPE = _frame_len({"type": "result", "id": MAX_ID,
                            "value": {"ok": True, "value_b64": "", "revision": MAX_ID, "written_at": _TIME_SHAPE}})
# The largest value one put can bring and one get can carry, as base64 inside one frame: derived, never set.
MAX_VALUE_BYTES = (MAX_FRAME_BYTES - max(_PUT_ENVELOPE, _GET_ENVELOPE)) // 4 * 3
_LIST_ENVELOPE = _frame_len({"type": "result", "id": MAX_ID,
                             "value": {"ok": True, "keys": [], "more": True, "after": _LONGEST_KEY}})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    ns TEXT NOT NULL DEFAULT '' CHECK (ns = ''),
    kind TEXT NOT NULL DEFAULT '' CHECK (kind = ''),
    key TEXT NOT NULL,
    value BLOB NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    written_at TEXT NOT NULL,
    PRIMARY KEY (ns, key)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS totals (
    one INTEGER PRIMARY KEY CHECK (one = 1),
    items INTEGER NOT NULL,
    bytes INTEGER NOT NULL
);
INSERT OR IGNORE INTO totals (one, items, bytes) VALUES (1, 0, 0);
"""


def identity_for(signer_key_id: str | None, origin: str | None = None) -> str:
    """The install identity a store is keyed by: the signer key id, or ``unsigned-<origin>``."""
    if signer_key_id is not None:
        if type(signer_key_id) is not str or not _KEY_ID.fullmatch(signer_key_id):
            raise ValueError("a signer key id is a scheme and hex digits")
        return signer_key_id
    if origin not in ORIGINS:
        raise ValueError("an unsigned plugin's identity comes from its install record's origin: person or model")
    return f"unsigned-{origin}"


def _dir_name(identity: str) -> str:
    if identity in UNSIGNED:
        return identity
    if type(identity) is not str or not _KEY_ID.fullmatch(identity):
        raise ValueError("an identity is a signer key id or one of the unsigned marks")
    return identity.replace(":", "-")


def _plugin_dir(state_dir, plugin_id: str) -> Path:
    if type(plugin_id) is not str or not _PLUGIN_ID.fullmatch(plugin_id):
        raise ValueError("a store is kept for one plugin id")
    return Path(state_dir) / STORE_DIR / plugin_id


def set_aside(state_dir, plugin_id: str, identity: str) -> list[dict]:
    """What of ``plugin_id``'s saved data is set aside and not given to the install ``identity``: the stores of
    other install identities and damaged files. Names only, never a path; nothing is deleted or opened."""
    found = []
    base = _plugin_dir(state_dir, plugin_id)
    own = _dir_name(identity)
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return found
    for name in names:
        folder = base / name
        if not stat.S_ISDIR(os.lstat(folder).st_mode):
            continue
        for file in sorted(os.listdir(folder)):
            if name != own and file == STORE_FILE:
                found.append({"install": name, "why": "another install"})
            elif file.startswith(STORE_FILE + DAMAGED) and not file.endswith(("-wal", "-shm")):
                found.append({"install": name, "why": "damaged", "file": file})
    return found


def for_plugin(state_dir, loaded, plugin_id: str, identity: str, *, record: Callable[[dict], Any], **kw):
    """The store the daemon hands a plugin's channel, or None when the plugin is not in plugin_memory.plugins."""
    if plugin_id not in loaded["plugin_memory.plugins"]:
        return None
    return PluginMemory(state_dir, plugin_id, identity, settings=loaded, record=record, **kw)


def _code(exc: BaseException) -> int | None:
    value = getattr(exc, "sqlite_errorcode", None)
    return value & 0xFF if type(value) is int else None


def _read_only(path: Path) -> dict:
    """The words' fill for a store file that could not be written: the file, its folder, or both, as they are now."""
    names = [f"{path}{suffix}" for suffix in ("", "-wal", "-shm") if os.path.lexists(f"{path}{suffix}")]
    file = any(not os.access(name, os.W_OK) for name in names)
    folder = not os.access(path.parent, os.W_OK | os.X_OK)
    if file and folder:
        return {"cause": "the file and the folder that holds it are read-only",
                "act": "make the folder that holds the plugin's saved data in Recursion's state folder writable "
                       "again, and the files in it"}
    if folder:
        return {"cause": "the folder that holds it is read-only",
                "act": "make the folder that holds the plugin's saved data in Recursion's state folder writable "
                       "again; the file itself is writable"}
    if file:
        return {"cause": "the file is read-only",
                "act": "make the plugin's saved-data file in Recursion's state folder writable again, with the "
                       "files beside it that begin with its name; the folder itself is writable"}
    return {"cause": "it could not be written",
            "act": "check that the folder that holds the plugin's saved data in Recursion's state folder, and the "
                   "files in it, can be written"}


def _not_open(exc: BaseException, path: Path) -> dict:
    """The words' fill for a store file that could not be opened or used now and was left in place."""
    code = _code(exc)
    if code in (_SQLITE_BUSY, _SQLITE_LOCKED):
        return {"cause": "another program was using it", "act": "end the other program that holds it"}
    if code == _SQLITE_READONLY:
        return _read_only(path)
    if code == _SQLITE_FULL:
        return {"cause": "your disk is full", "act": "free some space on your disk"}
    return {"cause": "SQLite could not open it", "act": "restart Recursion"}


class PluginMemory:
    """One plugin's saved data for one install identity. Thread-safe: one call at a time."""

    def __init__(self, state_dir, plugin_id: str, identity: str, *, settings, record: Callable[[dict], Any],
                 clock: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic) -> None:
        if not callable(record):
            raise TypeError("a store records every write: give it the daemon's record sink")
        self.plugin_id, self.identity = plugin_id, identity
        self._state_dir = state_dir
        self._folder = _plugin_dir(state_dir, plugin_id) / _dir_name(identity)
        self._path = self._folder / STORE_FILE
        self._sink, self._clock, self._monotonic = record, clock, monotonic
        rows = {name: settings[name] for name in ROWS}
        self.max_bytes = rows["plugin_memory.max_bytes_per_plugin"]
        self.row_item_bytes = rows["plugin_memory.max_item_bytes"]
        self.max_items = rows["plugin_memory.max_items_per_plugin"]
        self.page_max = rows["plugin_memory.list_page_max"]
        self.calls_per_minute = rows["plugin_memory.calls_per_minute"]
        self.sync = "NORMAL" if rows["plugin_memory.sync"] == "normal" else "FULL"
        self.wal_max = rows["plugin_memory.wal_max_bytes"]
        self.record_each = rows["plugin_memory.record_each_write"] is not False
        self.record_key_text = rows["plugin_memory.record_key_text"] is True
        self._lock = threading.Lock()
        self._calls: collections.deque = collections.deque()
        self._host_ended = False
        self._summary: collections.Counter = collections.Counter()
        self._db: sqlite3.Connection | None = None
        self.unreadable = False
        self.unavailable: dict | None = None  # why the file could not be opened now, when it could not
        self._open()

    # -- what a person is shown
    @property
    def max_item_bytes(self) -> int:
        """The item limit in force: the row, held to what fits one frame (``MAX_VALUE_BYTES``)."""
        row = self.row_item_bytes
        return MAX_VALUE_BYTES if row is NO_LIMIT else min(row, MAX_VALUE_BYTES)

    def shown(self) -> list[str]:
        held = " (held to what fits one message between Recursion and a plugin)" \
            if self.row_item_bytes is NO_LIMIT or self.row_item_bytes > MAX_VALUE_BYTES else ""
        return [f"plugin_memory.max_item_bytes in force: {words.size_text(self.max_item_bytes)}{held}"]

    # -- opening
    def _open(self) -> None:
        private_dir(self._folder)
        others = set_aside(self._state_dir, self.plugin_id, self.identity)
        new = not os.path.lexists(self._path)
        if new:
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
        info = os.lstat(self._path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise PermissionError("a store file must be a regular file of the daemon's own user")
        if info.st_mode & 0o077:
            os.chmod(self._path, 0o600)
        db = None
        try:
            db = sqlite3.connect(str(self._path), timeout=OPEN_WAIT_S, isolation_level=None,
                                 check_same_thread=False)
            if not new:
                (verdict,), = db.execute("PRAGMA integrity_check(1)").fetchall()
                if verdict != "ok":
                    db.close()
                    self._set_aside_damaged()
                    return
            mode, = db.execute("PRAGMA journal_mode=WAL").fetchone()
            if str(mode).lower() != "wal":
                raise sqlite3.OperationalError("not in WAL mode")
            db.execute(f"PRAGMA synchronous={self.sync}")
            db.executescript(_SCHEMA)
            db.execute(f"PRAGMA busy_timeout = {int(CALL_WAIT_S * 1000)}")
        except sqlite3.DatabaseError as exc:
            if db is not None:
                db.close()
            if _code(exc) in (_SQLITE_CORRUPT, _SQLITE_NOTADB):
                self._set_aside_damaged()
            else:
                self.unavailable = _not_open(exc, self._path)
            return
        except BaseException:
            if db is not None:
                db.close()
            raise
        self._db, self.unavailable = db, None
        for item in others:
            if item["why"] == "another install":
                self._record("set_aside", None, "set_aside", install=item["install"], forced=True)

    def _set_aside_damaged(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except sqlite3.Error:
                pass
            self._db = None
        stamp = datetime.datetime.fromtimestamp(self._clock(), datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        aside = self._path.with_name(STORE_FILE + DAMAGED + stamp)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.replace(f"{self._path}{suffix}", f"{aside}{suffix}")
            except FileNotFoundError:
                pass
        self.unreadable = True
        self._record("set_aside", None, "memory_unreadable", forced=True)

    # -- the record
    def host_ended(self) -> None:
        """The daemon saw this plugin's host end: a write that commits after this says so in its row."""
        with self._lock:
            self._host_ended = True
            if not self.record_each and self._summary:
                counts, self._summary = dict(self._summary), collections.Counter()
                self._record("summary", None, "summary", counts=counts, forced=True)

    def _record(self, call: str, key: str | None, result: str, *, revision: int | None = None,
                before: int | None = None, after: int | None = None, forced: bool = False, **more) -> None:
        if not self.record_each and not forced:
            self._summary[f"{call} {result}"] += 1
            return
        raw = None if key is None else key.encode("utf-8")
        row = {"time": datetime.datetime.fromtimestamp(self._clock(), datetime.timezone.utc).isoformat(),
               "plugin": self.plugin_id, "identity": self.identity, "call": call,
               "key_sha256": None if raw is None else hashlib.sha256(raw).hexdigest(),
               "key_length": None if raw is None else len(raw),
               "bytes_before": before, "bytes_after": after, "revision": revision, "result": result,
               "committed_after_host_ended": self._host_ended and result == "ok"}
        if self.record_key_text and key is not None:
            row["key"] = key
        row.update(more)
        self._sink(row)

    # -- answers
    def _refused(self, entry: str, **fill: str) -> dict:
        return {"ok": False, "code": words.CODE_OF[entry], **words.render(entry, self.plugin_id, **fill)}

    def _paced(self) -> dict | None:
        limit = self.calls_per_minute
        if limit is NO_LIMIT:
            return None
        now = self._monotonic()
        while self._calls and now - self._calls[0] >= PACE_WINDOW_S:
            self._calls.popleft()
        if len(self._calls) >= limit:
            return self._refused("memory_paced", limit=str(limit))
        self._calls.append(now)
        return None

    def _call(self, call: str, key: str | None, work: Callable[[], dict]) -> dict:
        writes = call in ("memory.put", "memory.delete")
        with self._lock:
            if self.unavailable is not None and not self.unreadable:
                self._open()
                if self.unavailable is not None:
                    return self._refused("memory_unavailable", **self.unavailable)
            if self.unreadable or self._db is None:
                return self._refused("memory_unreadable")
            answer = self._paced()
            if answer is None:
                try:
                    answer = work()
                except sqlite3.DatabaseError as exc:
                    if _code(exc) == _SQLITE_FULL:
                        answer = self._refused("memory_full_disk")
                    elif _code(exc) in (_SQLITE_CORRUPT, _SQLITE_NOTADB):
                        self._set_aside_damaged()
                        return self._refused("memory_unreadable")
                    elif _code(exc) in (_SQLITE_BUSY, _SQLITE_LOCKED, _SQLITE_READONLY):
                        # held or not writable after the open: the transaction is rolled back, nothing moved
                        answer = self._refused("memory_unavailable_call", **_not_open(exc, self._path))
                    else:
                        raise
            if writes and not answer["ok"]:
                self._record(call, key, answer["code"])
            return answer

    # -- the four calls (arguments already in the channel's form)
    def put(self, key: str, value_b64: str, if_revision: int | None) -> dict:
        value = base64.b64decode(value_b64.encode("ascii"), validate=True)

        def work() -> dict:
            limit = self.max_item_bytes
            if len(value) > limit:
                entry = "memory_item_too_large" if limit == MAX_VALUE_BYTES else "memory_item_too_large_row"
                return self._refused(entry, size=words.size_text(len(value)), limit=words.size_text(limit))
            self._checkpoint()
            return self._write(lambda tx: self._put_in(tx, key, value, if_revision))
        return self._call("memory.put", key, work)

    def get(self, key: str) -> dict:
        def work() -> dict:
            rows = self._db.execute("SELECT value, revision, written_at FROM items WHERE ns = '' AND key = ?",
                                    (key,)).fetchall()
            if not rows:
                return self._refused("memory_key_unknown")
            value, revision, written_at = rows[0]
            return {"ok": True, "value_b64": base64.b64encode(value).decode("ascii"), "revision": revision,
                    "written_at": written_at}
        return self._call("memory.get", key, work)

    def list_keys(self, prefix: str, after: str | None, limit: int | None) -> dict:
        def work() -> dict:
            caps = [cap for cap in (limit, None if self.page_max is NO_LIMIT else self.page_max) if cap is not None]
            cap = min(caps) if caps else None
            size, keys, more = _LIST_ENVELOPE, [], False
            rows = self._db.execute("SELECT key FROM items WHERE ns = '' AND key > ? AND substr(key, 1, ?) = ? "
                                    "ORDER BY key", (after or "", len(prefix), prefix))
            for (key,) in rows:
                grows = len(json.dumps(key, ensure_ascii=False).encode("utf-8")) + (1 if keys else 0)
                if (cap is not None and len(keys) >= cap) or size + grows > MAX_FRAME_BYTES:
                    more = True
                    break
                keys.append(key)
                size += grows
            rows.close()
            return {"ok": True, "keys": keys, "more": more, "after": keys[-1] if more and keys else None}
        return self._call("memory.list", None, work)

    def delete(self, key: str, if_revision: int | None) -> dict:
        def work() -> dict:
            self._checkpoint()
            return self._write(lambda tx: self._delete_in(tx, key, if_revision))
        return self._call("memory.delete", key, work)

    # -- writing: ONE transaction per write
    def _checkpoint(self) -> None:
        if self.wal_max is NO_LIMIT:
            return
        try:
            size = os.path.getsize(f"{self._path}-wal")
        except FileNotFoundError:
            return
        if size > self.wal_max:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _write(self, step: Callable[[sqlite3.Connection], tuple]) -> dict:
        """ONE transaction. A row of the record is sent before COMMIT: a sink that raises rolls the write back
        (the plugin is answered call_failed and nothing is written). A summary only counts after COMMIT."""
        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            answer, done = step(db)
            if done is None:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                return answer
            if self.record_each:
                self._record(*done[0], **done[1])
            if db.in_transaction:
                db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        if not self.record_each:
            self._record(*done[0], **done[1])
        return answer

    def _between(self) -> None:
        """Between the item and the totals of one write (a test makes it fail to show nothing is half written)."""

    def _totals(self, db) -> tuple[int, int]:
        (items, size), = db.execute("SELECT items, bytes FROM totals WHERE one = 1").fetchall()
        return items, size

    def _put_in(self, db, key: str, value: bytes, if_revision: int | None):
        found = db.execute("SELECT revision, length(CAST(key AS BLOB)) + length(value) FROM items "
                           "WHERE ns = '' AND key = ?", (key,)).fetchall()
        if (if_revision is None and found) or (if_revision is not None and (not found or found[0][0] != if_revision)):
            return self._refused("memory_revision_moved"), None
        items, before = self._totals(db)
        after = before - (found[0][1] if found else 0) + len(key.encode("utf-8")) + len(value)
        count = items + (0 if found else 1)
        if self.max_items is not NO_LIMIT and count > self.max_items:
            return self._refused("memory_full", size=words.count_text(self.max_items),
                                 row="plugin_memory.max_items_per_plugin"), None
        if self.max_bytes is not NO_LIMIT and after > self.max_bytes:
            return self._refused("memory_full", size=words.size_text(self.max_bytes),
                                 row="plugin_memory.max_bytes_per_plugin"), None
        revision = found[0][0] + 1 if found else 1
        if revision > MAX_ID:
            return self._refused("memory_full_revision", size=str(MAX_ID)), None
        written = datetime.datetime.fromtimestamp(self._clock(), datetime.timezone.utc)
        written_at = written.strftime("%Y-%m-%dT%H:%M:%S.") + f"{written.microsecond // 1000:03d}Z"
        db.execute("INSERT INTO items (key, value, revision, written_at) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT (ns, key) DO UPDATE SET value = excluded.value, revision = excluded.revision, "
                   "written_at = excluded.written_at", (key, value, revision, written_at))
        self._between()
        db.execute("UPDATE totals SET items = ?, bytes = ? WHERE one = 1", (count, after))
        done = (("memory.put", key, "ok"), {"revision": revision, "before": before, "after": after})
        return {"ok": True, "revision": revision}, done

    def _delete_in(self, db, key: str, if_revision: int | None):
        found = db.execute("SELECT revision, length(CAST(key AS BLOB)) + length(value) FROM items "
                           "WHERE ns = '' AND key = ?", (key,)).fetchall()
        if not found:
            return self._refused("memory_key_unknown"), None
        if if_revision is not None and found[0][0] != if_revision:
            return self._refused("memory_revision_moved"), None
        items, before = self._totals(db)
        db.execute("DELETE FROM items WHERE ns = '' AND key = ?", (key,))
        self._between()
        db.execute("UPDATE totals SET items = ?, bytes = ? WHERE one = 1", (items - 1, before - found[0][1]))
        done = (("memory.delete", key, "ok"), {"revision": found[0][0], "before": before, "after": before - found[0][1]})
        return {"ok": True, "revision": found[0][0]}, done

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None


__all__ = ["CALL_WAIT_S", "MAX_VALUE_BYTES", "OPEN_WAIT_S", "PluginMemory", "ROWS", "STORE_DIR", "STORE_FILE", "for_plugin",
           "identity_for", "memory_key_ok", "set_aside"]
