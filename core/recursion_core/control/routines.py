"""The routine record a person writes, kept in a store the core owns (``routine.record.v1.json``).

A routine record is the authority a plugin holding ``schedule`` asks a run from: WHAT may be started
(the tenant, the engine and the prompt) and which plugin may ask for it. WHEN it runs stays the
plugin's: no schedule language is kept here. The record kind is ``routine.v1``, the one kind
``run.request`` names (``plugin_security/host_channel.py``, ``RUN_REQUEST_RECORD_KINDS``); there is no
second kind.

Who writes. Only the person writes, changes or removes a record, through these commands of the core
(``RoutineStore.admit``, ``change``, ``remove``), each called with the principal the request came in as
(``common.v1.json`` ``$defs/principal``, derived by the server, never read from the request). The kinds
that stand for the person are ``WRITER_KINDS``: the owner on the owner-only socket and a paired device.
Every other principal kind (a plugin among them) is refused ``writer_not_allowed`` and nothing is stored,
so a plugin holding ``schedule`` can neither create, change nor widen a record. Anyone the composition
lets read may ``list`` the records: each shows the plugin that may ask for it.

The store. One SQLite file the composition names (in the daemon's state folder), opened per call with
``synchronous=FULL`` and written in one ``BEGIN IMMEDIATE`` transaction per command, so threads,
processes and a restart see one set of records. A record carries ``revision`` (1 at its first write,
one more at every change); a change names the revision it read, and an older one is refused
``conflict``. Removing a record removes it at once: nothing reads an older copy.

Every sentence a person reads about a routine is in ``TEXT``, one table, by code.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import re
import sqlite3
import time
from typing import Callable, Iterator, Mapping

from recursion_core.control.api import start_params_problem

__all__ = ["RECORD_KIND", "TEXT", "WRITER_KINDS", "RoutineStore"]

RECORD_KIND = "routine.v1"
# The principal kinds that stand for the person (common.v1.json $defs/principal). Only these write.
WRITER_KINDS = ("owner", "device")
# What the person writes; the core writes the rest (kind, revision, written_by, written_at).
PERSON_FIELDS = ("record_id", "plugin", "tenant", "engine", "prompt")
CHANGEABLE = ("plugin", "tenant", "engine", "prompt")
_RECORD_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")   # host.v1.json x-run-request record_id
_PLUGIN = re.compile(r"[a-z][a-z0-9_.-]{1,63}")     # common.v1.json $defs/owner, less 'host'
_LABEL = re.compile(r"[A-Za-z0-9._:-]{1,128}")      # common.v1.json $defs/token

OK = "ok"
# Every sentence a person reads about a routine: what happened, and the one thing they can do.
TEXT = {
    "routine_invalid": "This routine was not saved: a field is missing, unknown or not in its form. "
                       "Correct the field named in the detail and save it again.",
    "writer_not_allowed": "This routine was not saved: only you write, change or remove a routine, from "
                          "your own Recursion or a paired device. Write it there if you want it.",
    "routine_unknown": "There is no routine with this id. List your routines to see the ids you have.",
    "conflict": "This routine changed after it was read, so nothing was changed. Read it again and make "
                "the change on what it is now.",
}


def _refused(code: str, detail: str = "") -> dict:
    return {"code": code, "detail": detail, "text": TEXT[code]}


def _instant(seconds: float) -> str:
    moment = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def writer_problem(writer: object) -> str | None:
    """None when ``writer`` is a principal that stands for the person, else the refusal code."""
    if not isinstance(writer, Mapping) or set(writer) != {"kind", "id"}:
        return "writer_not_allowed"
    if writer["kind"] not in WRITER_KINDS or type(writer["id"]) is not str or not _LABEL.fullmatch(writer["id"]):
        return "writer_not_allowed"
    return None


def fields_problem(fields: Mapping, names: tuple) -> str | None:
    """The first of ``names`` in ``fields`` that is not in its form, or None."""
    for name in names:
        if name not in fields:
            continue
        value = fields[name]
        if name == "record_id" and (type(value) is not str or not _RECORD_ID.fullmatch(value)):
            return name
        if name == "plugin" and (type(value) is not str or not _PLUGIN.fullmatch(value) or value == "host"):
            return name
        if name in ("tenant", "engine", "prompt") and type(value) is not str:
            return name
    probe = {"tenant": "t", "engine": "e", "prompt": "p", **{k: fields[k] for k in names if k in fields}}
    return start_params_problem(probe)


class RoutineStore:
    """The core's store of routine records, and the person's commands on it."""

    def __init__(self, path: str, *, wall: Callable[[], float] = time.time) -> None:
        self._path = str(path)
        self._wall = wall
        with self._write() as db:
            db.execute("CREATE TABLE IF NOT EXISTS routines (record_id TEXT PRIMARY KEY, body TEXT NOT NULL)")

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        finally:
            db.close()

    @staticmethod
    def _get(db: sqlite3.Connection, record_id: str) -> dict | None:
        row = db.execute("SELECT body FROM routines WHERE record_id = ?", (record_id,)).fetchone()
        return json.loads(row[0]) if row is not None else None

    @staticmethod
    def _put(db: sqlite3.Connection, record: dict) -> None:
        db.execute("INSERT OR REPLACE INTO routines (record_id, body) VALUES (?, ?)",
                   (record["record_id"], json.dumps(record, sort_keys=True, ensure_ascii=False)))

    def _stamp(self, record: dict, writer: Mapping) -> None:
        record["written_by"] = {"kind": writer["kind"], "id": writer["id"]}
        record["written_at"] = _instant(self._wall())

    # -- the person's commands ------------------------------------------------------------------
    def admit(self, writer: object, record: object) -> dict:
        """Write a new routine record, or write one again whole. Its revision starts at 1 and rises by one."""
        problem = writer_problem(writer)
        if problem is not None:
            return _refused(problem)
        if not isinstance(record, Mapping):
            return _refused("routine_invalid", "record")
        unknown = sorted(set(record) - set(PERSON_FIELDS))
        if unknown:
            return _refused("routine_invalid", unknown[0])
        missing = [name for name in PERSON_FIELDS if name not in record]
        if missing:
            return _refused("routine_invalid", missing[0])
        problem = fields_problem(record, PERSON_FIELDS)
        if problem is not None:
            return _refused("routine_invalid", problem)
        with self._write() as db:
            prior = self._get(db, record["record_id"])
            stored = {"kind": RECORD_KIND, **{name: record[name] for name in PERSON_FIELDS},
                      "revision": 1 if prior is None else prior["revision"] + 1}
            self._stamp(stored, writer)
            self._put(db, stored)
        return {"code": OK, "routine": stored}

    def change(self, writer: object, record_id: object, changes: object, *, revision: object) -> dict:
        """Change some of a record's fields. ``revision`` is the one read; an older one is ``conflict``."""
        problem = writer_problem(writer)
        if problem is not None:
            return _refused(problem)
        if not isinstance(changes, Mapping) or not changes:
            return _refused("routine_invalid", "changes")
        unknown = sorted(set(changes) - set(CHANGEABLE))
        if unknown:
            return _refused("routine_invalid", unknown[0])
        problem = fields_problem(changes, CHANGEABLE)
        if problem is not None:
            return _refused("routine_invalid", problem)
        with self._write() as db:
            prior = self._get(db, record_id) if type(record_id) is str else None
            if prior is None:
                return _refused("routine_unknown", str(record_id)[:64])
            if type(revision) is not int or revision != prior["revision"]:
                return _refused("conflict", str(prior["revision"]))
            stored = {**prior, **{name: changes[name] for name in changes}, "revision": prior["revision"] + 1}
            self._stamp(stored, writer)
            self._put(db, stored)
        return {"code": OK, "routine": stored}

    def remove(self, writer: object, record_id: object) -> dict:
        """Remove a record at once: nothing reads it after this answer."""
        problem = writer_problem(writer)
        if problem is not None:
            return _refused(problem)
        with self._write() as db:
            if type(record_id) is not str or self._get(db, record_id) is None:
                return _refused("routine_unknown", str(record_id)[:64])
            db.execute("DELETE FROM routines WHERE record_id = ?", (record_id,))
        return {"code": OK, "removed": record_id}

    def list(self) -> dict:
        """Every record, by id: each names the plugin that may ask for it."""
        with self._write() as db:
            rows = db.execute("SELECT body FROM routines ORDER BY record_id").fetchall()
        return {"code": OK, "routines": [json.loads(row[0]) for row in rows]}

    def get(self, record_id: str) -> dict | None:
        """The record as it is now, or None."""
        with self._write() as db:
            return self._get(db, record_id)
