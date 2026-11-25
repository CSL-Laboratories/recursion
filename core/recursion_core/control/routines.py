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
transaction, so of many starts at once no more than the budget are taken.

A charge is for a run that exists. The charge of a start is a row of the store (``charges``) that names
the run id the start is launched under, made in the transaction that charges it (``admit_start`` given
``launching``), with the tenant and client key the start was sent with. The row is the charge: ``used``
moves only with it, one up when it is made and one down when it is given back. The API stores the run's
launch record and then drops the row (``recorded``): the charge stands for a run that is in the run list.
When the launch record cannot be stored, the API gives the charge back in one transaction with its row
(``give_back``) before it answers, so nothing was charged and ``last_refusal`` is not touched. A start sent
again with the same client key while a row of its key is held (the give-back could not be written, or the
process ended between the charge and the record) takes that row and its run id and is not charged again,
the budget not read a second time. When the API is built over the store (the daemon starts), every row
whose run id the run list does not hold is given back (``settle``), with one warning line that names the
routine and the run id; a row whose run is in the list is dropped and its charge stands. A new admission
of the record starts its budget afresh and drops its rows; a removal drops them with the record. So
``used`` counts the runs in the run list and the starts between their charge and their record, never a
start whose run was not recorded. ``bounds_of`` gives the
leases a record is bound to and the budgets a start is charged to, each as a list (one entry each
today): the earliest lease end ends the routine, and every budget must have room.

How the lease is measured is ``LEASE_CLOCK``: ``counted`` (seconds of the machine's monotonic clock,
summed in the store across every restart of the core within one boot of the machine, so a restart keeps
the count and a wall clock set back or forward changes nothing; time across a reboot is not counted) or
``wall`` (the wall clock counted forward in the store: each command adds, in its own transaction, the
seconds the wall clock moved forward since the store's last reading, and nothing when it moved back). So
a wall clock set back costs a ``wall`` lease nothing beyond its instant: an ended lease stays ended, and a
running one, or one admitted after the step, ends in its time (only the seconds between the store's last
reading before the step and its first reading after it go uncounted). A clock set forward ends a lease
early, the safe side. One reading far in the future ends the leases that run at that moment and no lease
admitted after it. A record keeps the clock it was admitted under.

Where the boot cannot be told (``boot_id`` reads none: macOS and Windows today), counted seconds cannot be
summed across processes or restarts, so the store counts none there and the lease falls back to the wall
clock: a record is admitted with a ``wall`` lease, and a ``counted`` lease the store meets there is moved
to the wall clock in the same transaction, to end at the earlier of the counted time it had left (counted
on from the wall clock's forward count when the counted seconds were last written) and its admission's wall
time plus its lease (placed on the forward count by how far the count runs ahead of the wall clock). So a
lease ends there no later than the wall clock's forward count says, across a restart, with a second store
open on the file and after a clock set back or a far-future reading, and the record's ``lease`` names the
clock that ends it whenever it is read: ``get`` moves it and writes it as ``list`` does.

What a start from a record runs. The person admits a plugin to start ONE routine: what is run, as whom,
on which engine. So a start from a record runs what the record says, never what the caller sends:
``admit_start`` answers with the record's ``tenant``, ``engine`` and ``prompt`` (``RUN_VALUES``), and the
API starts those. A caller may leave the three out; each one it does send must be the record's, else the
start is refused ``routine_differs``, whose sentence names which of the three differs (never the prompt's
text). The record keeps its prompt by a digest as well as in text (``prompt_sha256``, the SHA-256 of its
UTF-8 bytes, written by ``admit`` and by every ``change`` of the prompt); a prompt in the store that no
longer matches its digest, a tenant, engine or prompt that is no longer in its form, or a record that
changed between the start's first read of it and the read that charges or relaunches it, is refused
``routine_changed``. Neither refusal is charged to the budget, and both are kept on the record.

Which refusals are kept on the record (``START_REFUSALS``): every refusal of a start that names this
record: another plugin's ask (``routine_unknown``), ``routine_changed``, ``routine_differs``,
``routine_lease_ended`` and ``routine_budget_spent``. Two refusals are not kept: a record id the store does
not hold (there is no record to keep it on) and an origin whose ``record_kind`` is not ``routine.v1`` or is
missing (it names a record of another kind, not this one, so it is not shown as an ask of this routine).
A kept refusal carries the plugin and the occurrence it was asked with only when each is a value in its
form (a plugin id; an occurrence as ``run.request`` takes it, a UTC instant to the second); otherwise the
field is absent, never an empty or a made-up value.

While a start is launched. ``admit_start`` given ``launching`` (a callable that makes the run id) holds
that run id in the store, in the same transaction that charges the start (or checks its relaunch), until
the API says that start has answered (``launched``). A person's ``change`` or ``remove`` of the record in
that moment still answers ``ok``, and its answer carries ``starting``: the run ids that may still start
with the values the record had before, and a sentence that says so and what the person can do (stop the
run). The API lets a start's hold go before it lets go of the start's lock, so a same-key start that
launches the run again under the same run id is held again only after the earlier hold is gone, and no
late release of the earlier start can take the later start's hold away. An answer without ``starting``
means that no start admitted before it was still being launched. A
run already launched is in the run list with the values it started with; the person stops it there.

What runs today. No daemon composes this store yet: no command of a person reaches it from outside the
process, and a running daemon starts its runs as before.

A change (``change``) may shorten the lease or lower the budget; one that lengthens the lease or raises
the budget is refused ``widen_needs_admission``: only the person's new admission widens a record.

Every sentence a person reads about a routine, or a run asked from one, is in ``TEXT``, one table, by code.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import logging
import re
import sqlite3
import time
from typing import Callable, Iterator, Mapping

__all__ = ["BUDGET_UNITS", "LEASE_CLOCKS", "RECORD_KIND", "RUN_VALUES", "START_REFUSALS", "TEXT", "WRITER_KINDS",
           "RoutineStore", "bounds_of", "prompt_digest"]

RECORD_KIND = "routine.v1"
# The principal kinds that stand for the person (common.v1.json $defs/principal). Only these write.
WRITER_KINDS = ("owner", "device")
LEASE_CLOCKS = ("counted", "wall")
BUDGET_UNITS = ("runs",)
# What a start from a record runs; each is read from the record, never from the caller (module text).
RUN_VALUES = ("tenant", "engine", "prompt")

# Figures. Each value below becomes a row of the person's settings file, with this value as its shipped
# default, once that file's one reader is in this code; until then the composition (or a test) may give
# another value to RoutineStore, which wins over the value here.
LEASE_CLOCK = "counted"          # how a lease is measured: "counted" or "wall"
LEASE_MISSING = "refused"        # a record written with no lease: "refused", "default" or "no_lease"
DEFAULT_LEASE_S = 30 * 24 * 3600  # the lease a record gets when LEASE_MISSING is "default": thirty days
DEFAULT_BUDGET = {"unit": "runs", "limit": 100}  # the budget a record written with none gets
BUSY_WAIT_S = 30.0               # how long a command waits for another command's hold on the store, in seconds

# What the person writes; the core writes the rest.
PERSON_FIELDS = ("record_id", "plugin", "tenant", "engine", "prompt", "lease_seconds", "budget")
REQUIRED_PERSON_FIELDS = ("record_id", "plugin", "tenant", "engine", "prompt")
CHANGEABLE = ("plugin", "tenant", "engine", "prompt", "lease_seconds", "budget")
_RECORD_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")   # host.v1.json x-run-request record_id
_PLUGIN = re.compile(r"[a-z][a-z0-9_.-]{1,63}")     # common.v1.json $defs/owner, less 'host'
_LABEL = re.compile(r"[A-Za-z0-9._:-]{1,128}")      # common.v1.json $defs/token

_log = logging.getLogger(__name__)

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
    "routine_differs": "The plugin asked this routine for a run with another {what} than the routine you "
                       "admitted, so no run was started and nothing was charged. The request is kept on the "
                       "routine; if you want that run, write the routine again with it.",
    "routine_store_absent": "A plugin asked to start a run from a routine, but this daemon has no routine "
                            "store, so no run was started and nothing was charged. Plugins cannot start runs "
                            "until a routine store is configured; until then, start the run yourself from "
                            "your own Recursion.",
    "routine_unbound_values": "The plugin sent values with its request that the routine you admitted does not "
                              "name, so no run was started and nothing was charged. A run from a routine "
                              "carries only its tenant, engine and prompt; if you want another run, start it "
                              "yourself or write the routine again.",
    "routine_changed_while_starting": "The routine was changed, but a run started from it a moment before was "
                                      "still being launched and may run with what the routine said before "
                                      "({runs}). Stop that run if you do not want it.",
    "routine_removed_while_starting": "The routine was removed, but a run started from it a moment before was "
                                      "still being launched and may run with what the routine said before "
                                      "({runs}). Stop that run if you do not want it.",
    "start_unrecorded": "The run could not be recorded, so it was not started: the daemon could not write its "
                        "run list ({detail}). Nothing was charged to the routine; send the start again.",
    "start_unrecorded_held": "The run could not be recorded, so it was not started: the daemon could not write "
                             "its run list ({detail}), and could not give the routine its charge back yet. The "
                             "charge is held for this start: send the start again with the same key and it is "
                             "charged once; if no run is recorded for it, it is given back when the daemon "
                             "starts again.",
    "charge_given_back": "A charge for run {run_id}, which was never recorded, was given back to the routine "
                         "{record_id}. No run was started for it, so there is nothing for you to do.",
    "routine_changed": "This routine changed while a run was being started from it, or was changed in its "
                       "store by something other than your own commands, so no run was started and nothing "
                       "was charged. Read the routine, and write it again if it does not say what you want.",
}
# The refusals of a start that are kept on the record, for the person to read (module text: which are not).
START_REFUSALS = ("routine_unknown", "routine_changed", "routine_differs", "routine_lease_ended",
                  "routine_budget_spent")


def _refused(code: str, detail: str = "", differs: tuple = ()) -> dict:
    if code == "routine_differs":  # the sentence names which values differ: "tenant", "engine and prompt"
        what = differs[0] if len(differs) == 1 else ", ".join(differs[:-1]) + " and " + differs[-1]
        return {"code": code, "detail": detail, "differs": list(differs), "text": TEXT[code].format(what=what)}
    return {"code": code, "detail": detail, "text": TEXT[code]}


def prompt_digest(prompt: str) -> str:
    """The SHA-256 of the prompt's UTF-8 bytes, as hex: the digest a record keeps beside its prompt."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


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
                 default_budget: Mapping = DEFAULT_BUDGET, busy_wait_s: float = BUSY_WAIT_S) -> None:
        if lease_clock not in LEASE_CLOCKS or lease_missing not in ("refused", "default", "no_lease"):
            raise ValueError("lease_clock or lease_missing is not one of its values")
        if type(default_lease_s) is not int or default_lease_s < 1 or not _budget_ok(default_budget):
            raise ValueError("default_lease_s or default_budget is not in its form")
        if type(busy_wait_s) not in (int, float) or not busy_wait_s >= 0:
            raise ValueError("busy_wait_s is not a number of seconds")
        self._path = str(path)
        self._wall, self._monotonic = wall, monotonic
        self._clock, self._missing = lease_clock, lease_missing
        self._default_lease_s, self._default_budget = default_lease_s, dict(default_budget)
        self._busy_wait_s = float(busy_wait_s)
        self._boot = boot() or None  # None: the boot cannot be told, and no counted second is summed (module text)
        with self._write() as db:
            db.execute("CREATE TABLE IF NOT EXISTS routines (record_id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS clocks (name TEXT PRIMARY KEY, value NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS launching (record_id TEXT NOT NULL, run_id TEXT NOT NULL, "
                       "PRIMARY KEY (record_id, run_id))")
            # A charge whose run record is not stored yet: the row is the charge (module text).
            db.execute("CREATE TABLE IF NOT EXISTS charges (record_id TEXT NOT NULL, run_id TEXT NOT NULL, "
                       "tenant TEXT, client_key TEXT, PRIMARY KEY (record_id, run_id))")

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path, timeout=self._busy_wait_s, isolation_level=None)
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
        """The counted seconds and the wall clock's forward count, both advanced to now and written back.

        ``wall`` grows by the seconds the wall clock moved forward since the store's last reading
        (``wall_seen``) and by nothing when it moved back (module text). ``counted_at`` is the forward count
        when the counted seconds were last written; ``ahead`` is how far the count runs ahead of the reading.
        """
        kept = dict(db.execute("SELECT name, value FROM clocks").fetchall())
        reading = self._wall()
        wall = kept.get("wall_count", reading) + max(0.0, reading - kept.get("wall_seen", reading))
        rows = [("wall_count", wall), ("wall_seen", reading)]
        counted, counted_at = kept.get("counted", 0.0), kept.get("counted_at", wall)
        if self._boot is not None:  # where no boot is told the count stays as it is kept (module text)
            seen = self._monotonic()
            if kept.get("boot") == self._boot and seen > kept.get("seen", seen):
                counted += seen - kept["seen"]
            counted_at = wall
            rows += [("counted", counted), ("counted_at", counted_at), ("seen", seen), ("boot", self._boot)]
        db.executemany("INSERT OR REPLACE INTO clocks (name, value) VALUES (?, ?)", rows)
        return {"counted": counted, "counted_at": counted_at, "wall": wall, "ahead": wall - reading}

    def _lease_clock(self) -> str:
        """The clock a new lease is measured on: the configured one, or the wall clock where no boot is told."""
        return "wall" if self._boot is None and self._clock == "counted" else self._clock

    def _measured(self, record: dict, now: dict) -> bool:
        """Where no boot is told, move a counted lease to the wall clock (module text). True when moved."""
        lease = record.get("lease")
        if self._boot is not None or lease is None or lease["clock"] != "counted":
            return False
        admitted = datetime.datetime.strptime(record["admitted_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
        by_admission = (admitted.replace(tzinfo=datetime.timezone.utc).timestamp() + record["lease_seconds"]
                        + now["ahead"])  # the admission's wall time, placed on the forward count
        ends = min(now["counted_at"] + (lease["ends"] - now["counted"]), by_admission)
        record["lease"] = {"clock": "wall", "start": max(0.0, ends - record["lease_seconds"]), "ends": ends}
        return True

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
                      "prompt_sha256": prompt_digest(record["prompt"]), "lease_seconds": seconds, "budget": dict(record.get("budget", self._default_budget)),
                      "revision": 1 if prior is None else prior["revision"] + 1,
                      "lease": self._lease(now, seconds, self._lease_clock()), "used": 0, "last_refusal": None}
            self._stamp(stored, writer)
            stored["admitted_at"] = stored["written_at"]
            self._put(db, stored)
            db.execute("DELETE FROM charges WHERE record_id = ?", (record["record_id"],))  # a fresh budget
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
            self._measured(prior, now)
            if type(revision) is not int or revision != prior["revision"]:
                return _refused("conflict", str(prior["revision"]))
            widened = self._widens(prior, changes)
            if widened is not None:
                return _refused("widen_needs_admission", widened)
            stored = {**prior, **{name: changes[name] for name in changes}, "revision": prior["revision"] + 1}
            if "prompt" in changes:  # the person's own change of the prompt is digested as an admission's is
                stored["prompt_sha256"] = prompt_digest(changes["prompt"])
            if "lease_seconds" in changes:  # a shorter lease from the same start, or a first one from now
                kept = prior["lease"]
                stored["lease"] = (self._lease(now, changes["lease_seconds"], self._lease_clock()) if kept is None else
                                   {**kept, "ends": kept["start"] + changes["lease_seconds"]})
            self._stamp(stored, writer)
            self._put(db, stored)
            starting = self._starting(db, record_id)
        return _told({"code": OK, "routine": stored}, starting, "routine_changed_while_starting")

    @staticmethod
    def _widens(prior: Mapping, changes: Mapping) -> str | None:
        """The field of ``changes`` that would give the record a longer lease or a larger budget, or None."""
        if ("lease_seconds" in changes and prior["lease_seconds"] is not None
                and changes["lease_seconds"] > prior["lease_seconds"]):
            return "lease_seconds"
        if "budget" in changes and (changes["budget"]["unit"] != prior["budget"]["unit"]
                                    or changes["budget"]["limit"] > prior["budget"]["limit"]):
            return "budget"
        return None

    def remove(self, writer: object, record_id: object) -> dict:
        """Remove a record at once: no start is admitted from it after this answer. A start admitted from it
        just before may still be launching with its values; the answer then names it (module text)."""
        problem = writer_problem(writer)
        if problem is not None:
            return _refused(problem)
        with self._write() as db:
            if type(record_id) is not str or self._get(db, record_id) is None:
                return _refused("routine_unknown", str(record_id)[:64])
            db.execute("DELETE FROM routines WHERE record_id = ?", (record_id,))
            db.execute("DELETE FROM charges WHERE record_id = ?", (record_id,))
            starting = self._starting(db, record_id)
        return _told({"code": OK, "removed": record_id}, starting, "routine_removed_while_starting")

    @staticmethod
    def _starting(db: sqlite3.Connection, record_id: str) -> list:
        """The run ids admitted from ``record_id`` whose start has not answered yet (module text)."""
        return [row[0] for row in db.execute("SELECT run_id FROM launching WHERE record_id = ? ORDER BY run_id",
                                             (record_id,))]

    def launched(self, origin: Mapping, run_id: str) -> bool:
        """The start of ``run_id``, admitted from ``origin``'s record, has answered: the store stops holding it.
        False when the store cannot be written; the run id is then still named as one that may start."""
        try:
            with self._write() as db:
                db.execute("DELETE FROM launching WHERE record_id = ? AND run_id = ?",
                           (origin.get("record_id"), run_id))
        except sqlite3.Error:
            return False
        return True

    def recorded(self, origin: Mapping, run_id: str) -> bool:
        """The launch record of ``run_id`` is stored: its charge stands, and its row is dropped (module text).
        False when the store cannot be written; the row then stays until ``settle`` finds the run listed."""
        try:
            with self._write() as db:
                db.execute("DELETE FROM charges WHERE record_id = ? AND run_id = ?", (origin.get("record_id"), run_id))
        except sqlite3.Error:
            return False
        return True

    def give_back(self, origin: Mapping, run_id: str) -> bool:
        """No launch record of ``run_id`` was stored: its charge, its row and its hold go in one transaction.
        True when nothing of it is charged any more; False when the store cannot be written (the row is held)."""
        try:
            with self._write() as db:
                self._give_back(db, origin.get("record_id"), run_id)
        except sqlite3.Error:
            return False
        return True

    def _give_back(self, db: sqlite3.Connection, record_id: str, run_id: str) -> bool:
        """One step on one row: the row goes, and ``used`` goes down by one only if the row was there."""
        db.execute("DELETE FROM launching WHERE record_id = ? AND run_id = ?", (record_id, run_id))
        if db.execute("DELETE FROM charges WHERE record_id = ? AND run_id = ?", (record_id, run_id)).rowcount != 1:
            return False
        record = self._get(db, record_id)
        if record is not None and record["used"] > 0:
            record["used"] -= 1
            self._put(db, record)
        return True

    def settle(self, recorded: Callable[[str], bool]) -> list:
        """Give back every charge whose run id ``recorded`` says the run list does not hold; drop the row of
        one it holds. Called when the API is built over this store (module text). A row ``recorded`` cannot
        answer for (it raises) is left as it is. Answers the ``(record_id, run_id)`` given back."""
        with self._write() as db:
            rows = db.execute("SELECT record_id, run_id FROM charges ORDER BY record_id, run_id").fetchall()
        given = []
        for record_id, run_id in rows:
            try:
                listed = bool(recorded(run_id))
            except Exception:
                continue
            with self._write() as db:
                if listed:
                    db.execute("DELETE FROM charges WHERE record_id = ? AND run_id = ?", (record_id, run_id))
                elif self._give_back(db, record_id, run_id):
                    given.append((record_id, run_id))
        for record_id, run_id in given:
            _log.warning(TEXT["charge_given_back"].format(run_id=run_id, record_id=record_id))
        return given

    def list(self) -> dict:
        """Every record, by id: each names the plugin that may ask for it, its lease, budget and last refusal."""
        with self._write() as db:
            now = self._clocks(db)
            records = [json.loads(row[0]) for row in db.execute("SELECT body FROM routines ORDER BY record_id")]
            for record in records:
                if self._measured(record, now):
                    self._put(db, record)
        return {"code": OK, "routines": records}

    def get(self, record_id: str) -> dict | None:
        """The record as it is now, or None. A counted lease met where no boot is told is moved and written,
        as ``list`` does, so the record names the clock that ends it."""
        with self._write() as db:
            now = self._clocks(db)
            record = self._get(db, record_id)
            if record is not None and self._measured(record, now):
                self._put(db, record)
            return record

    # -- the core's check, before a run is started from a record ----------------------------------
    def admit_start(self, origin: Mapping, asked: Mapping | None = None, *, charge: bool = True,
                    reread: bool = False, launching: Callable[[], str] | None = None,
                    key: tuple | None = None) -> dict:
        """Whether a start from ``origin``'s record may go on, and what it runs: ``{"code": "ok", "start":
        {...}}`` with the record's ``tenant``, ``engine`` and ``prompt``, else the refusal.

        Read in one transaction with the charge: the record as it is now (never an older copy), its
        plugin, its prompt against its digest, each value of ``asked`` (what the caller sent of
        ``RUN_VALUES``: any of them, or none) against the record's, then its leases (the earliest end ends
        it) and its budgets (each must have room). With ``charge`` (a fresh start) the start is charged;
        without it (the API's check before it takes the start lock, or the relaunch of a run already
        charged) nothing is charged and the lease and budget are not read. With ``reread``, ``asked`` holds
        the values this start read from the record a moment before, so a value that differs now means the
        record changed in between: ``routine_changed``, not the caller's ``routine_differs``. Which refusals
        are kept on the record as ``last_refusal``, and why two are not, is in the module text. With
        ``launching``, the start's run id is made by it in the same transaction and held until ``launched``
        (the answer carries it as ``run_id``), so a change or removal in between names it. With ``charge`` and
        ``launching`` the charge is a row naming that run id and ``key`` (the start's tenant and client key);
        a row of the same ``key`` already held is taken instead, with its run id, and nothing is charged again
        (module text).
        """
        asked = {} if asked is None else asked
        record_id = origin.get("record_id")
        with self._write() as db:
            now = self._clocks(db)
            record = self._get(db, record_id) if type(record_id) is str else None
            if record is None or origin.get("record_kind") != RECORD_KIND:
                return _refused("routine_unknown", str(record_id)[:64])  # not kept: module text
            self._measured(record, now)  # written below on every path: refused or charged
            taken = None  # the run id of a charge of this start's key that is still held (module text)
            if charge and launching is not None and key is not None and type(key[1]) is str:
                row = db.execute("SELECT run_id FROM charges WHERE record_id = ? AND tenant = ? AND client_key = ? "
                                 "ORDER BY run_id", (record_id, key[0], key[1])).fetchone()
                taken = row[0] if row is not None else None
            code, differs = None, ()
            leases, budgets = bounds_of(record)
            if origin.get("plugin") != record["plugin"]:
                code = "routine_unknown"
            elif _changed(record):
                code = "routine_changed"
            elif _differs(record, asked):
                code, differs = ("routine_changed", ()) if reread else ("routine_differs", _differs(record, asked))
            elif charge and any(now[lease["clock"]] >= lease["ends"] for lease in leases):
                code = "routine_lease_ended"
            elif charge and taken is None and any(budget["used"] >= budget["limit"] for budget in budgets):
                code = "routine_budget_spent"
            if code is not None:
                if taken is not None:  # no run will be recorded under the held charge: it goes back with its row
                    db.execute("DELETE FROM charges WHERE record_id = ? AND run_id = ?", (record_id, taken))
                    record["used"] = max(record["used"] - 1, 0)
                record["last_refusal"] = _kept_refusal(code, _instant(self._wall()), origin, differs)
                self._put(db, record)
                return _refused(code, record_id, differs)
            if charge and taken is None:
                record["used"] += 1
            self._put(db, record)
            run_id = taken if taken is not None else launching() if launching is not None else None
            if charge and taken is None and run_id is not None:
                db.execute("INSERT INTO charges (record_id, run_id, tenant, client_key) VALUES (?, ?, ?, ?)",
                           (record_id, run_id, *(key if key is not None else (None, None))))
            if run_id is not None:
                db.execute("INSERT OR REPLACE INTO launching (record_id, run_id) VALUES (?, ?)", (record_id, run_id))
        answer = {"code": OK, "start": {name: record[name] for name in RUN_VALUES}}
        if run_id is not None:
            answer["run_id"] = run_id
        return answer


def _told(answer: dict, starting: list, code: str) -> dict:
    """A change's or removal's ``ok``, with the runs that may still start with the old values when any."""
    if starting:
        answer.update(starting=starting, text=TEXT[code].format(runs=", ".join(starting)))
    return answer


def _changed(record: Mapping) -> bool:
    """True when the record's tenant, engine or prompt is no longer in its form, or its prompt no longer
    matches the digest kept beside it: the record was changed in the store, not by the person's commands."""
    from recursion_core.control.api import start_params_problem  # api.py imports this module

    if start_params_problem({name: record.get(name) for name in RUN_VALUES}) is not None:
        return True
    return record.get("prompt_sha256") != prompt_digest(record["prompt"])


def _differs(record: Mapping, asked: Mapping) -> tuple:
    """The names of ``RUN_VALUES`` the caller sent with a value that is not the record's, in their order.
    The prompt is compared by its digest, so neither it nor the record's is ever repeated."""
    differs = []
    for name in RUN_VALUES:
        if name not in asked:
            continue
        if name == "prompt":
            value = asked[name]
            same = type(value) is str and _encodes(value) and prompt_digest(value) == record["prompt_sha256"]
        else:
            same = asked[name] == record[name]
        if not same:
            differs.append(name)
    return tuple(differs)


def _encodes(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _kept_refusal(code: str, at: str, origin: Mapping, differs: tuple) -> dict:
    """The ``last_refusal`` kept for the person: the plugin and the occurrence only when each is in its form."""
    from recursion_core.plugin_security.host_channel import _occurrence_ok  # the form run.request takes

    kept = {"code": code, "at": at}
    plugin = origin.get("plugin")
    if type(plugin) is str and _PLUGIN.fullmatch(plugin) and plugin != "host":
        kept["plugin"] = plugin
    if _occurrence_ok(origin.get("occurrence")):
        kept["occurrence"] = origin["occurrence"]
    if differs:
        kept["differs"] = list(differs)
    return kept
