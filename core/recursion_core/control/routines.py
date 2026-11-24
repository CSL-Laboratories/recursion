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

The lease and the budget. ``admit`` is the person's admission: it gives the record a lease
(``lease_seconds`` from now) and a budget (``budget``: how many runs it may start), and starts both
afresh. ``ControlAPI`` asks ``admit_start`` before it starts a run whose origin is a trigger: a run is
refused, and the refusal kept on the record (``last_refusal``) where the person reads it, once the
lease has ended or the budget is spent; otherwise the start is charged to the budget in the same
transaction, so of many starts at once no more than the budget are taken. ``bounds_of`` gives the
leases a record is bound to and the budgets a start is charged to, each as a list (one entry each
today): the earliest lease end ends the routine, and every budget must have room.

How the lease is measured is ``LEASE_CLOCK``: ``counted`` (seconds of the machine's monotonic clock,
summed in the store across every restart of the core within one boot of the machine, so a restart keeps
the count and a wall clock set back or forward changes nothing; time across a reboot is not counted,
and where the boot cannot be told the count is kept only while one process runs) or
``wall`` (the wall clock, never read as earlier than the latest wall time the store has seen, so a
clock set back does not reopen an ended lease). A record keeps the clock it was admitted under.

A change (``change``) may shorten the lease or lower the budget; one that lengthens the lease or raises
the budget is refused ``widen_needs_admission``: only the person's new admission widens a record.

Every sentence a person reads about a routine, or a run asked from one, is in ``TEXT``, one table, by code.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import re
import sqlite3
import time
import uuid
from typing import Callable, Iterator, Mapping

__all__ = ["BUDGET_UNITS", "LEASE_CLOCKS", "RECORD_KIND", "TEXT", "WRITER_KINDS", "RoutineStore", "bounds_of"]

RECORD_KIND = "routine.v1"
# The principal kinds that stand for the person (common.v1.json $defs/principal). Only these write.
WRITER_KINDS = ("owner", "device")
LEASE_CLOCKS = ("counted", "wall")
BUDGET_UNITS = ("runs",)

# Figures. Each value below becomes a row of the person's settings file, with this value as its shipped
# default, once that file's one reader is in this code; until then the composition (or a test) may give
# another value to RoutineStore, which wins over the value here.
LEASE_CLOCK = "counted"          # how a lease is measured: "counted" or "wall"
LEASE_MISSING = "refused"        # a record written with no lease: "refused", "default" or "no_lease"
DEFAULT_LEASE_S = 30 * 24 * 3600  # the lease a record gets when LEASE_MISSING is "default": thirty days
DEFAULT_BUDGET = {"unit": "runs", "limit": 100}  # the budget a record written with none gets

# What the person writes; the core writes the rest.
PERSON_FIELDS = ("record_id", "plugin", "tenant", "engine", "prompt", "lease_seconds", "budget")
REQUIRED_PERSON_FIELDS = ("record_id", "plugin", "tenant", "engine", "prompt")
CHANGEABLE = ("plugin", "tenant", "engine", "prompt", "lease_seconds", "budget")
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
    "routine_unknown": "There is no routine with this id for this plugin, so nothing was done. List your "
                       "routines to see the ids you have and the plugin each one is for.",
    "conflict": "This routine changed after it was read, so nothing was changed. Read it again and make "
                "the change on what it is now.",
    "widen_needs_admission": "A longer lease or a larger budget needs the routine written again whole, so "
                             "nothing was changed. Write it again with the lease and budget you want.",
    "routine_lease_ended": "This routine's lease has ended, so no run was started. Write the routine again "
                           "to give it a new lease.",
    "routine_budget_spent": "This routine has started every run its budget allows, so no run was started. "
                            "Write the routine again to give it a new budget.",
    "origin_not_admitted": "This kind of run is reserved and not available in this release, so no run was "
                           "started. There is nothing for you to do.",
}
# The refusals of a start that are kept on the record, for the person to read.
START_REFUSALS = ("routine_unknown", "routine_lease_ended", "routine_budget_spent")


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


def _budget_ok(value: object) -> bool:
    return (isinstance(value, Mapping) and set(value) == {"unit", "limit"} and value["unit"] in BUDGET_UNITS
            and type(value["limit"]) is int and value["limit"] >= 0)


def fields_problem(fields: Mapping, names: tuple) -> str | None:
    """The first of ``names`` in ``fields`` that is not in its form, or None."""
    from recursion_core.control.api import start_params_problem  # api.py imports this module

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
        if name == "lease_seconds" and (type(value) is not int or value < 1):
            return name
        if name == "budget" and not _budget_ok(value):
            return name
    probe = {"tenant": "t", "engine": "e", "prompt": "p",
             **{k: fields[k] for k in ("tenant", "engine", "prompt") if k in names and k in fields}}
    return start_params_problem(probe)


def boot_id() -> str | None:
    """This boot of the machine (Linux), or None where it cannot be read."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def bounds_of(record: Mapping) -> tuple[list[dict], list[dict]]:
    """The leases this record is bound to and the budgets a start from it is charged to, each a list.

    Today each list holds the record's own entry: its lease (``{"clock", "ends"}``; none when the record
    has no lease) and its budget (``{"unit", "limit", "used"}``). The ending rule reads the earliest lease
    end and every budget, so an entry added here later is read without a change to the check.
    """
    leases = [] if record.get("lease") is None else [{"clock": record["lease"]["clock"], "ends": record["lease"]["ends"]}]
    budgets = [{"unit": record["budget"]["unit"], "limit": record["budget"]["limit"], "used": record["used"]}]
    return leases, budgets


class RoutineStore:
    """The core's store of routine records, and the person's commands on it."""

    def __init__(self, path: str, *, wall: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, boot: Callable[[], str | None] = boot_id,
                 lease_clock: str = LEASE_CLOCK,
                 lease_missing: str = LEASE_MISSING, default_lease_s: int = DEFAULT_LEASE_S,
                 default_budget: Mapping = DEFAULT_BUDGET) -> None:
        if lease_clock not in LEASE_CLOCKS or lease_missing not in ("refused", "default", "no_lease"):
            raise ValueError("lease_clock or lease_missing is not one of its values")
        if type(default_lease_s) is not int or default_lease_s < 1 or not _budget_ok(default_budget):
            raise ValueError("default_lease_s or default_budget is not in its form")
        self._path = str(path)
        self._wall, self._monotonic = wall, monotonic
        self._clock, self._missing = lease_clock, lease_missing
        self._default_lease_s, self._default_budget = default_lease_s, dict(default_budget)
        self._boot = boot() or f"process-{uuid.uuid4().hex}"  # a boot that cannot be told is this process
        with self._write() as db:
            db.execute("CREATE TABLE IF NOT EXISTS routines (record_id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS clocks (name TEXT PRIMARY KEY, value NOT NULL)")

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

    # -- the two clocks, kept in the store ------------------------------------------------------
    def _clocks(self, db: sqlite3.Connection) -> dict:
        """The counted seconds and the latest wall time, both advanced to now and written back."""
        kept = dict(db.execute("SELECT name, value FROM clocks").fetchall())
        counted, latest = kept.get("counted", 0.0), kept.get("wall", 0.0)
        reading = self._monotonic()
        if kept.get("boot") == self._boot and reading > kept.get("seen", reading):
            counted += reading - kept["seen"]
        latest = max(latest, self._wall())
        db.executemany("INSERT OR REPLACE INTO clocks (name, value) VALUES (?, ?)",
                       [("counted", counted), ("wall", latest), ("seen", reading), ("boot", self._boot)])
        return {"counted": counted, "wall": latest}

    def _stamp(self, record: dict, writer: Mapping) -> None:
        record["written_by"] = {"kind": writer["kind"], "id": writer["id"]}
        record["written_at"] = _instant(self._wall())

    def _lease(self, now: dict, seconds: int | None, clock: str, start: float | None = None) -> dict | None:
        if seconds is None:
            return None
        start = now[clock] if start is None else start
        return {"clock": clock, "start": start, "ends": start + seconds}

    # -- the person's commands ------------------------------------------------------------------
    def admit(self, writer: object, record: object) -> dict:
        """The person's admission: write a record whole, with a new lease and a fresh budget from now."""
        problem = writer_problem(writer)
        if problem is not None:
            return _refused(problem)
        if not isinstance(record, Mapping):
            return _refused("routine_invalid", "record")
        unknown = sorted(set(record) - set(PERSON_FIELDS))
        if unknown:
            return _refused("routine_invalid", unknown[0])
        missing = [name for name in REQUIRED_PERSON_FIELDS if name not in record]
        if "lease_seconds" not in record and self._missing == "refused":
            missing.append("lease_seconds")
        if missing:
            return _refused("routine_invalid", missing[0])
        problem = fields_problem(record, PERSON_FIELDS)
        if problem is not None:
            return _refused("routine_invalid", problem)
        seconds = record.get("lease_seconds", self._default_lease_s if self._missing == "default" else None)
        with self._write() as db:
            now = self._clocks(db)
            prior = self._get(db, record["record_id"])
            stored = {"kind": RECORD_KIND, **{name: record[name] for name in REQUIRED_PERSON_FIELDS},
                      "lease_seconds": seconds, "budget": dict(record.get("budget", self._default_budget)),
                      "revision": 1 if prior is None else prior["revision"] + 1,
                      "lease": self._lease(now, seconds, self._clock), "used": 0, "last_refusal": None}
            self._stamp(stored, writer)
            stored["admitted_at"] = stored["written_at"]
            self._put(db, stored)
        return {"code": OK, "routine": stored}

    def change(self, writer: object, record_id: object, changes: object, *, revision: object) -> dict:
        """Change some of a record's fields. ``revision`` is the one read; an older one is ``conflict``. A
        change may shorten the lease or lower the budget, never lengthen or raise them."""
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
            now = self._clocks(db)
            prior = self._get(db, record_id) if type(record_id) is str else None
            if prior is None:
                return _refused("routine_unknown", str(record_id)[:64])
            if type(revision) is not int or revision != prior["revision"]:
                return _refused("conflict", str(prior["revision"]))
            widened = self._widens(prior, changes)
            if widened is not None:
                return _refused("widen_needs_admission", widened)
            stored = {**prior, **{name: changes[name] for name in changes}, "revision": prior["revision"] + 1}
            if "lease_seconds" in changes:  # a shorter lease from the same start, or a first one from now
                kept = prior["lease"]
                stored["lease"] = (self._lease(now, changes["lease_seconds"], self._clock) if kept is None else
                                   {**kept, "ends": kept["start"] + changes["lease_seconds"]})
            self._stamp(stored, writer)
            self._put(db, stored)
        return {"code": OK, "routine": stored}

    @staticmethod
    def _widens(prior: Mapping, changes: Mapping) -> str | None:
        """The field of ``changes`` that would give the record a longer lease or a larger budget, or None."""
        if "lease_seconds" in changes and prior["lease_seconds"] is not None                 and changes["lease_seconds"] > prior["lease_seconds"]:
            return "lease_seconds"
        if "budget" in changes and (changes["budget"]["unit"] != prior["budget"]["unit"]
                                    or changes["budget"]["limit"] > prior["budget"]["limit"]):
            return "budget"
        return None

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
        """Every record, by id: each names the plugin that may ask for it, its lease, budget and last refusal."""
        with self._write() as db:
            self._clocks(db)
            rows = db.execute("SELECT body FROM routines ORDER BY record_id").fetchall()
        return {"code": OK, "routines": [json.loads(row[0]) for row in rows]}

    def get(self, record_id: str) -> dict | None:
        """The record as it is now, or None."""
        with self._write() as db:
            return self._get(db, record_id)

    # -- the core's check, before a run is started from a record ----------------------------------
    def admit_start(self, origin: Mapping) -> dict | None:
        """None when a start from ``origin``'s record may go on (and it is charged), else the refusal.

        Read in one transaction with the charge: the record as it is now (never an older copy), its
        plugin, its leases (the earliest end ends it) and its budgets (each must have room). A refusal is
        kept on the record as ``last_refusal``.
        """
        record_id = origin.get("record_id")
        with self._write() as db:
            now = self._clocks(db)
            record = self._get(db, record_id) if type(record_id) is str else None
            if record is None or origin.get("record_kind") != RECORD_KIND:
                return _refused("routine_unknown", str(record_id)[:64])
            code = None
            leases, budgets = bounds_of(record)
            if origin.get("plugin") != record["plugin"]:
                code = "routine_unknown"
            elif any(now[lease["clock"]] >= lease["ends"] for lease in leases):
                code = "routine_lease_ended"
            elif any(budget["used"] >= budget["limit"] for budget in budgets):
                code = "routine_budget_spent"
            if code is not None:
                record["last_refusal"] = {"code": code, "at": _instant(self._wall()),
                                          "plugin": str(origin.get("plugin")), "occurrence": str(origin.get("occurrence"))}
                self._put(db, record)
                return _refused(code, record_id)
            record["used"] += 1
            self._put(db, record)
        return None
