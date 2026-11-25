"""A routine record a person wrote ends with its lease and its budget (control/routines.py, control/api.py).

Each run is asked the way a plugin asks it: a ``run.request`` at the daemon's end of a plugin host's channel,
started through ``run.start``'s one admission path with the core's routine store given to the API.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core.control import api as control_api
from recursion_core.control import routines as r
from recursion_core.control.api import ControlAPI
from recursion_core.control.registry import InMemoryRunRegistry
from recursion_core.control.tests.fakes import FakeKillTree, FakeStarter, run_ids
from recursion_core.plugin_security import host_channel as hc
from recursion_core.plugin_security.canonical import canonical, parse

OWNER = {"kind": "owner", "id": "owner"}
PLUGIN = "demo.schedule"
LEASE_S = 100
THREADS = 8          # the budget race starts this many threads, no more
JOIN_S = 30          # and waits this long for each


class Granting:
    """A stand-in core that says every plugin holds ``schedule``."""

    def client_for(self, caller):
        def call(request):
            message = parse(request)
            assert message.get("op") == "plugin.permissions"
            return canonical({"protocol": "csl-core/v0", "ok": True, "result": {"permissions": ["schedule"]}})
        return call


class Daemon:
    """One daemon's life over a routine store file and a run registry that outlive it."""

    def __init__(self, case, **options):
        self.case = case
        self.store = r.RoutineStore(case.path, wall=lambda: case.wall[0], monotonic=lambda: case.mono[0],
                                    boot=lambda: case.boot, **options)
        self.starter = FakeStarter()
        self.api = ControlAPI(case.registry, FakeKillTree(), self.starter, clock=lambda: 1000.0,
                              new_run_id=case.run_ids, routines=self.store)

    def ask(self, occurrence, plugin=PLUGIN, record_id="morning-digest"):
        args = {"record_kind": "routine.v1", "record_id": record_id, "revision": 1, "occurrence": occurrence,
                "tenant": "default", "engine": "claude_code", "prompt": "Summarise what changed overnight."}
        end = hc.DaemonEnd(plugin, Granting(), runs=self.api)
        answer = end.receive({"type": "call", "id": 1, "name": "run.request", "args": args})
        self.case.assertEqual(answer["type"], "result", answer)
        return answer["value"]


def a_record(**changes):
    record = {"record_id": "morning-digest", "plugin": PLUGIN, "tenant": "default", "engine": "claude_code",
              "prompt": "Summarise what changed overnight.", "lease_seconds": LEASE_S,
              "budget": {"unit": "runs", "limit": 10}}
    record.update(changes)
    return record


def at(n):
    return f"2026-11-24T{n // 60:02d}:{n % 60:02d}:00Z"


class BoundsCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, "routines.sqlite3")
        self.wall, self.mono, self.boot = [1795464000.0], [50.0], "boot-1"
        self.registry = InMemoryRunRegistry()
        self.run_ids = run_ids()  # one sequence for every daemon over this registry

    def later(self, seconds, *, wall=True, mono=True):
        self.wall[0] += seconds if wall else 0
        self.mono[0] += seconds if mono else 0

    def assertStarted(self, answer):
        self.assertEqual(answer["code"], "started", answer)

    def assertRefusedStart(self, answer, code):
        self.assertEqual((answer["code"], answer["detail"]), ("request_invalid", code), answer)
        self.assertEqual(answer["text"], r.TEXT[code])

    def daemon(self, **options):
        return Daemon(self, **options)


class TheLeaseTest(BoundsCase):
    def test_an_occurrence_after_the_lease_end_is_refused_and_kept_where_the_person_reads(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        d.store.list()
        self.later(LEASE_S - 1)
        self.assertStarted(d.ask(at(1)))
        self.later(2)
        self.assertRefusedStart(d.ask(at(2)), "routine_lease_ended")
        self.assertEqual(len(d.starter.calls), 1)
        kept = d.store.get("morning-digest")["last_refusal"]
        self.assertEqual((kept["code"], kept["plugin"], kept["occurrence"]), ("routine_lease_ended", PLUGIN, at(2)))
        self.assertEqual(d.store.list()["routines"][0]["last_refusal"], kept)

    def test_an_occurrence_after_the_lease_end_after_a_restart_is_refused(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        self.assertStarted(d.ask(at(1)))
        self.later(LEASE_S + 1)  # nothing touches the store before the daemon stops
        again = self.daemon()
        self.assertRefusedStart(again.ask(at(2)), "routine_lease_ended")
        self.assertEqual(again.starter.calls, [])

    def test_counted_time_across_a_reboot_is_not_counted(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        self.later(LEASE_S - 10)
        self.assertStarted(d.ask(at(1)))
        self.boot, self.mono[0] = "boot-2", 5.0  # the machine started again: its monotonic clock too
        self.later(LEASE_S)
        rebooted = self.daemon()
        self.assertStarted(rebooted.ask(at(2)))  # the reboot's own seconds count from its first reading
        self.later(11)
        self.assertRefusedStart(rebooted.ask(at(3)), "routine_lease_ended")

    def test_the_wall_clock_set_back_across_the_lease_end_changes_nothing(self):
        for clock in r.LEASE_CLOCKS:
            with self.subTest(clock=clock):
                self.setUp()
                d = self.daemon(lease_clock=clock)
                d.store.admit(OWNER, a_record())
                self.later(LEASE_S + 1)
                self.assertRefusedStart(d.ask(at(1)), "routine_lease_ended")
                self.later(-10 * LEASE_S, mono=False)
                self.assertRefusedStart(d.ask(at(2)), "routine_lease_ended")
                self.assertRefusedStart(self.daemon(lease_clock=clock).ask(at(3)), "routine_lease_ended")
                self.assertEqual(d.starter.calls, [])

    def test_counted_time_is_not_moved_by_the_wall_clock(self):
        d = self.daemon(lease_clock="counted")
        d.store.admit(OWNER, a_record())
        self.later(100 * LEASE_S, mono=False)
        self.assertStarted(d.ask(at(1)))
        self.assertEqual(d.store.get("morning-digest")["lease"]["clock"], "counted")

    def test_a_missing_lease_is_refused_as_its_first_value_and_each_other_value_is_kept(self):
        d = self.daemon()
        record = a_record()
        del record["lease_seconds"]
        answer = d.store.admit(OWNER, record)
        self.assertEqual((answer["code"], answer["detail"]), ("routine_invalid", "lease_seconds"))
        self.assertEqual(r.LEASE_MISSING, "refused")
        self.assertEqual(d.store.list()["routines"], [])
        no_lease = self.daemon(lease_missing="no_lease")
        self.assertIsNone(no_lease.store.admit(OWNER, record)["routine"]["lease"])
        self.later(1000 * LEASE_S)
        self.assertStarted(no_lease.ask(at(1)))
        default = self.daemon(lease_missing="default", default_lease_s=7)
        stored = default.store.admit(OWNER, record)["routine"]
        self.assertEqual(stored["lease_seconds"], 7)
        self.later(8)
        self.assertRefusedStart(default.ask(at(2)), "routine_lease_ended")

    def test_the_earliest_of_the_leases_a_record_is_bound_to_ends_it(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        own = r.bounds_of

        def with_an_earlier_lease(record):
            leases, budgets = own(record)
            return leases + [{"clock": "counted", "ends": 0.0}], budgets

        with mock.patch.object(r, "bounds_of", with_an_earlier_lease):
            self.assertRefusedStart(d.ask(at(1)), "routine_lease_ended")

        def with_a_spent_budget(record):
            leases, budgets = own(record)
            return leases, budgets + [{"unit": "runs", "limit": 1, "used": 1}]

        with mock.patch.object(r, "bounds_of", with_a_spent_budget):
            self.assertRefusedStart(d.ask(at(2)), "routine_budget_spent")
        self.assertStarted(d.ask(at(3)))
        leases, budgets = r.bounds_of(d.store.get("morning-digest"))
        self.assertEqual(len(leases), 1)
        self.assertEqual(budgets, [{"unit": "runs", "limit": 10, "used": 1}])


class TheBudgetTest(BoundsCase):
    def test_an_occurrence_after_the_budget_is_spent_is_refused(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 2}))
        first = d.ask(at(1))
        self.assertStarted(first)
        self.assertStarted(d.ask(at(2)))
        self.assertRefusedStart(d.ask(at(3)), "routine_budget_spent")
        retried = d.ask(at(1))  # the same occurrence again is the same run, charged nothing
        self.assertEqual(retried["run"]["run_id"], first["run"]["run_id"])
        self.assertEqual(d.store.get("morning-digest")["used"], 2)
        self.assertEqual(d.store.get("morning-digest")["last_refusal"]["code"], "routine_budget_spent")

    def test_an_occurrence_after_the_budget_is_spent_after_a_restart_is_refused(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 1}))
        self.assertStarted(d.ask(at(1)))
        again = self.daemon()
        self.assertRefusedStart(again.ask(at(2)), "routine_budget_spent")
        self.assertEqual(again.starter.calls, [])

    def test_of_many_starts_at_once_no_more_than_the_budget_are_taken(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 3}))
        codes, start = [], threading.Barrier(THREADS)

        def ask(n):
            start.wait(JOIN_S)
            answer = d.ask(at(n))
            codes.append(answer.get("detail") if answer["code"] != "started" else "started")

        threads = [threading.Thread(target=ask, args=(n,)) for n in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(JOIN_S)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(codes), ["routine_budget_spent"] * (THREADS - 3) + ["started"] * 3)
        self.assertEqual(len(d.starter.calls), 3)


class OnlyTheNewAdmissionWidensTest(BoundsCase):
    def test_a_plugins_write_that_tries_to_lengthen_the_lease_is_refused(self):
        d = self.daemon()
        stored = d.store.admit(OWNER, a_record())["routine"]
        plugin = {"kind": "plugin", "id": PLUGIN}
        answer = d.store.change(plugin, "morning-digest", {"lease_seconds": 1000 * LEASE_S}, revision=1)
        self.assertEqual(answer["code"], "writer_not_allowed")
        answer = d.store.admit(plugin, a_record(lease_seconds=1000 * LEASE_S))
        self.assertEqual(answer["code"], "writer_not_allowed")
        self.assertEqual(d.store.get("morning-digest"), stored)
        self.later(LEASE_S + 1)
        self.assertRefusedStart(d.ask(at(1)), "routine_lease_ended")

    def test_a_widening_write_without_a_new_admission_is_refused(self):
        d = self.daemon()
        stored = d.store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 1}))["routine"]
        for changes in ({"lease_seconds": 2 * LEASE_S}, {"budget": {"unit": "runs", "limit": 5}}):
            with self.subTest(changes=changes):
                answer = d.store.change(OWNER, "morning-digest", changes, revision=1)
                self.assertEqual(answer["code"], "widen_needs_admission")
                self.assertEqual(answer["text"], r.TEXT["widen_needs_admission"])
        self.assertEqual(d.store.get("morning-digest"), stored)
        self.assertStarted(d.ask(at(1)))
        self.later(LEASE_S + 1)
        self.assertRefusedStart(d.ask(at(2)), "routine_lease_ended")
        self.later(10)
        admitted = d.store.admit(OWNER, a_record(lease_seconds=2 * LEASE_S, budget={"unit": "runs", "limit": 5}))
        self.assertEqual(admitted["code"], "ok")
        self.assertEqual((admitted["routine"]["used"], admitted["routine"]["revision"]), (0, 2))
        self.assertEqual(admitted["routine"]["admitted_at"], r._instant(self.wall[0]))
        self.assertStarted(d.ask(at(3)))

    def test_a_shorter_lease_or_a_smaller_budget_is_a_change_that_takes_effect(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        answer = d.store.change(OWNER, "morning-digest", {"lease_seconds": 10, "budget": {"unit": "runs", "limit": 1}},
                                revision=1)
        self.assertEqual(answer["code"], "ok")
        self.assertStarted(d.ask(at(1)))
        self.assertRefusedStart(d.ask(at(2)), "routine_budget_spent")
        self.later(11)
        self.assertRefusedStart(d.ask(at(3)), "routine_lease_ended")


class WhatIsAskedTest(BoundsCase):
    def test_a_record_of_another_plugin_or_none_at_all_starts_nothing(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        self.assertRefusedStart(d.ask(at(1), plugin="other.schedule"), "routine_unknown")
        self.assertEqual(d.store.get("morning-digest")["last_refusal"]["plugin"], "other.schedule")
        self.assertRefusedStart(d.ask(at(2), record_id="evening"), "routine_unknown")
        d.store.remove(OWNER, "morning-digest")
        self.assertRefusedStart(d.ask(at(3)), "routine_unknown")
        self.assertEqual(d.starter.calls, [])

    def test_only_the_admitted_origin_kinds_reach_the_start_path(self):
        self.assertEqual(control_api.ADMITTED_RUN_ORIGINS, ("trigger",))
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        origin = {"kind": "colour", "plugin": PLUGIN, "record_kind": "routine.v1", "record_id": "morning-digest",
                  "revision": 1, "occurrence": at(1)}
        answer = d.api.call("run.start", {"tenant": "default", "engine": "claude_code", "prompt": "p",
                                          "client_key": "k-1"}, origin=origin)
        self.assertRefusedStart(answer, "origin_not_admitted")
        self.assertEqual((d.starter.calls, self.registry.list()), ([], []))
        self.assertEqual(d.store.get("morning-digest")["used"], 0)
        self.assertStarted(d.api.call("run.start", {"tenant": "default", "engine": "claude_code", "prompt": "p"}))

    def test_a_trigger_start_with_no_routine_store_is_refused_and_starts_nothing(self):
        api = ControlAPI(self.registry, FakeKillTree(), FakeStarter(), clock=lambda: 1000.0, new_run_id=run_ids())
        end = hc.DaemonEnd(PLUGIN, Granting(), runs=api)
        args = {"record_kind": "routine.v1", "record_id": "nowhere", "revision": 1, "occurrence": at(1),
                "tenant": "default", "engine": "claude_code", "prompt": "p"}
        answer = end.receive({"type": "call", "id": 1, "name": "run.request", "args": args})
        self.assertEqual((answer["value"]["code"], answer["value"]["detail"]),
                         ("request_invalid", "routine_store_absent"))


class WhereTheBootCannotBeToldTest(BoundsCase):
    """No boot id can be read (macOS and Windows today): the lease still ends, by the wall clock."""

    STEP_S = 20  # the stand-in clocks move this many seconds between two asks
    ASKS = 30    # and this many asks are made, well past the lease

    def setUp(self):
        super().setUp()
        self.boot = None

    def codes_of(self, ask):
        codes = []
        for n in range(1, self.ASKS + 1):
            self.later(self.STEP_S)
            answer = ask(n)
            codes.append("started" if answer["code"] == "started" else answer["detail"])
        return codes

    def assertEndsWithItsLease(self, codes):
        before = (LEASE_S - 1) // self.STEP_S
        self.assertEqual(codes, ["started"] * before + ["routine_lease_ended"] * (self.ASKS - before))

    def test_a_counted_lease_ends_with_a_second_store_open_on_the_same_file(self):
        person = r.RoutineStore(self.path, wall=lambda: self.wall[0], monotonic=lambda: self.mono[0],
                                boot=lambda: self.boot)
        d = self.daemon()
        self.assertEqual(r.LEASE_CLOCK, "counted")
        self.assertEqual(person.admit(OWNER, a_record())["routine"]["lease"]["clock"], "wall")

        def ask(n):
            person.list()
            return d.ask(at(n))

        self.assertEndsWithItsLease(self.codes_of(ask))
        self.assertEqual(person.get("morning-digest")["lease"]["clock"], "wall")
        self.assertEqual(person.get("morning-digest")["last_refusal"]["code"], "routine_lease_ended")

    def test_a_counted_lease_ends_across_restarts_of_the_daemon(self):
        self.daemon().store.admit(OWNER, a_record())
        self.assertEndsWithItsLease(self.codes_of(lambda n: self.daemon().ask(at(n))))

    def test_a_counted_lease_met_there_ends_no_later_than_the_wall_clock_says(self):
        self.boot = "boot-1"
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        self.later(LEASE_S // 2)
        self.assertStarted(d.ask(at(1)))
        self.later(LEASE_S // 2 + 10, mono=False)  # the machine slept: the wall clock ran, the counted one did not
        self.boot = None
        there = self.daemon()
        self.assertRefusedStart(there.ask(at(2)), "routine_lease_ended")
        self.assertEqual(there.store.get("morning-digest")["lease"]["clock"], "wall")
        self.assertEqual(there.starter.calls, [])


class TheCountAcrossARestartTest(BoundsCase):
    def test_a_lease_spent_partly_before_a_restart_and_the_rest_after_ends(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        self.later(LEASE_S - 40)
        self.assertStarted(d.ask(at(1)))
        again = self.daemon()
        self.assertStarted(again.ask(at(2)))
        self.later(41)
        self.assertRefusedStart(again.ask(at(3)), "routine_lease_ended")
        self.assertRefusedStart(self.daemon().ask(at(4)), "routine_lease_ended")


class AWallClockSetWrongTest(BoundsCase):
    """The wall clock is set back, set forward or read once far in the future (the clocks are stand-ins)."""

    STEP_S = 20   # the stand-in clocks move this many seconds between two asks
    ASKS = 12     # and this many asks are made, well past the lease
    YEAR_S = 365 * 24 * 3600
    # Where a lease is on the wall clock: no boot told (a counted lease falls back to it), or a store told to use it.
    PLACES = (("no boot told", None, {}), ("boot told, wall lease", "boot-1", {"lease_clock": "wall"}))
    FAR = (("a hundred years ahead", None), ("the reading 1e13", 10 ** 13))

    def codes_of(self, options, first, record_id="morning-digest"):
        """Ask every STEP_S seconds, each time from a daemon started again over the same store file."""
        codes = []
        for n in range(first, first + self.ASKS):
            self.later(self.STEP_S)
            answer = self.daemon(**options).ask(at(n), record_id=record_id)
            codes.append("started" if answer["code"] == "started" else answer["detail"])
        return codes

    def assertEndsIn(self, codes, left_s):
        before = (left_s - 1) // self.STEP_S
        self.assertEqual(codes, ["started"] * before + ["routine_lease_ended"] * (self.ASKS - before))

    def read_once_far_ahead(self, store, absolute):
        real = self.wall[0]
        self.wall[0] = real + 100 * self.YEAR_S if absolute is None else float(absolute)
        store.list()  # the one bad reading the store takes
        self.wall[0] = real

    def test_a_clock_set_back_a_year_costs_a_running_lease_nothing_beyond_its_instant(self):
        for label, boot, options in self.PLACES:
            with self.subTest(place=label):
                self.setUp()
                self.boot = boot
                d = self.daemon(**options)
                d.store.admit(OWNER, a_record())
                self.later(LEASE_S // 2)
                self.assertStarted(d.ask(at(1)))  # the store reads the clock here
                self.later(-self.YEAR_S, mono=False)  # the step back: no time passes
                # Only the seconds between the store's last reading before the step and its first after it go
                # uncounted: one step of the stand-in clocks here.
                self.assertEndsIn(self.codes_of(options, 2), LEASE_S - LEASE_S // 2 + self.STEP_S)

    def test_a_lease_admitted_after_a_clock_set_back_ends_in_its_time(self):
        for label, boot, options in self.PLACES:
            with self.subTest(place=label):
                self.setUp()
                self.boot = boot
                d = self.daemon(**options)
                d.store.list()
                self.later(-3600, mono=False)
                d.store.admit(OWNER, a_record())
                self.assertEndsIn(self.codes_of(options, 1), LEASE_S)

    def test_a_clock_set_forward_ends_a_lease_early(self):
        for label, boot, options in self.PLACES:
            with self.subTest(place=label):
                self.setUp()
                self.boot = boot
                d = self.daemon(**options)
                d.store.admit(OWNER, a_record())
                self.later(LEASE_S // 2)
                self.assertStarted(d.ask(at(1)))
                self.later(self.YEAR_S, mono=False)
                self.assertRefusedStart(d.ask(at(2)), "routine_lease_ended")

    def test_one_far_future_reading_ends_what_runs_then_and_no_lease_admitted_after(self):
        for label, boot, options in self.PLACES:
            for far, absolute in self.FAR:
                with self.subTest(place=label, far=far):
                    self.setUp()
                    self.boot = boot
                    d = self.daemon(**options)
                    d.store.admit(OWNER, a_record(record_id="before"))
                    self.later(10)
                    self.read_once_far_ahead(d.store, absolute)
                    self.assertRefusedStart(d.ask(at(1), record_id="before"), "routine_lease_ended")
                    d.store.admit(OWNER, a_record(record_id="after"))
                    self.assertEndsIn(self.codes_of(options, 2, "after"), LEASE_S)

    def test_a_counted_lease_admitted_after_a_far_future_reading_and_met_where_no_boot_is_told_ends_in_its_time(self):
        for far, absolute in self.FAR:
            with self.subTest(far=far):
                self.setUp()
                d = self.daemon()
                self.read_once_far_ahead(d.store, absolute)
                self.later(10)
                self.assertEqual(d.store.admit(OWNER, a_record())["routine"]["lease"]["clock"], "counted")
                self.boot = None
                self.assertEndsIn(self.codes_of({}, 1), LEASE_S)

    def test_a_routine_removed_and_admitted_again_a_year_after_a_far_future_reading_ends_in_its_time(self):
        self.boot = None
        d = self.daemon()
        self.read_once_far_ahead(d.store, None)
        d.store.admit(OWNER, a_record())
        self.later(self.YEAR_S)
        again = self.daemon()
        self.assertEqual(again.store.remove(OWNER, "morning-digest")["code"], "ok")
        again.store.admit(OWNER, a_record())
        self.assertEndsIn(self.codes_of({}, 1), LEASE_S)

    def test_a_counted_lease_moved_to_the_wall_clock_ends_within_its_lease_of_the_wall_count(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        self.later(LEASE_S - 40)  # counted seconds no command writes
        self.boot = None
        self.assertEndsIn(self.codes_of({}, 1), 40)
        self.setUp()
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        self.later(LEASE_S + 50, wall=False)  # counted seconds no command writes, while the wall clock stands
        self.boot = None
        self.daemon().ask(at(1))
        self.later(LEASE_S, mono=False)
        self.assertRefusedStart(self.daemon().ask(at(2)), "routine_lease_ended")

    def test_a_counted_lease_moved_after_a_clock_set_back_counts_the_seconds_no_command_wrote(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        self.later(-3600, mono=False)
        self.later(10)
        self.assertStarted(d.ask(at(1)))  # ten counted seconds written, the wall clock an hour behind
        self.later(60)  # counted seconds no command writes
        self.boot = None
        self.assertEndsIn(self.codes_of({}, 2), LEASE_S - 70)

    def test_get_names_the_clock_that_ends_the_record_as_list_does(self):
        self.daemon().store.admit(OWNER, a_record())
        self.boot = None
        there = self.daemon().store
        self.assertEqual(there.get("morning-digest")["lease"]["clock"], "wall")
        self.boot = "boot-1"
        self.assertEqual(self.daemon().store.get("morning-digest")["lease"]["clock"], "wall")  # written, not only shown
        self.assertEqual(there.get("morning-digest"), there.list()["routines"][0])
        self.assertIsNone(there.get("evening"))


class FailingOnce(FakeStarter):
    """A launch path that fails its first launch and starts every later one."""

    def __call__(self, params):
        launched = super().__call__(params)
        if len(self.calls) == 1:
            raise OSError("the launch path failed")
        return launched


class AFailedLaunchTest(BoundsCase):
    def test_a_failed_launch_is_charged_once_and_its_retry_nothing(self):
        d = self.daemon()
        d.starter = FailingOnce()
        d.api = ControlAPI(self.registry, FakeKillTree(), d.starter, clock=lambda: 1000.0, new_run_id=self.run_ids,
                           routines=d.store)
        d.store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 5}))
        failed = d.ask(at(1))
        self.assertNotEqual(failed["code"], "started", failed)
        self.assertEqual(d.store.get("morning-digest")["used"], 1)
        retried = d.ask(at(1))
        self.assertStarted(retried)
        self.assertEqual(d.store.get("morning-digest")["used"], 1)
        self.assertEqual(len(d.starter.calls), 2)
        self.assertStarted(d.ask(at(2)))
        self.assertEqual(d.store.get("morning-digest")["used"], 2)


class TheAllowListTest(BoundsCase):
    def test_an_origin_with_no_kind_a_near_miss_or_a_kind_that_is_not_a_string_is_refused(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        base = {"plugin": PLUGIN, "record_kind": "routine.v1", "record_id": "morning-digest", "revision": 1,
                "occurrence": at(1)}
        origins = [("no kind", dict(base))] + [(repr(kind), {**base, "kind": kind})
                                               for kind in ("Trigger", "TRIGGER", " trigger", 7, ["trigger"], None)]
        for n, (label, origin) in enumerate(origins):
            with self.subTest(origin=label):
                answer = d.api.call("run.start", {"tenant": "default", "engine": "claude_code", "prompt": "p",
                                                  "client_key": f"k-{n}"}, origin=origin)
                self.assertRefusedStart(answer, "origin_not_admitted")
        self.assertEqual((d.starter.calls, self.registry.list()), ([], []))
        self.assertEqual(d.store.get("morning-digest")["used"], 0)
        self.assertIsNone(d.store.get("morning-digest")["last_refusal"])


if __name__ == "__main__":
    unittest.main()
