"""A charge to a routine's budget is for a run that exists (control/routines.py, control/api.py).

Each start is the real ``ControlAPI`` over the real ``RoutineStore`` (a temporary SQLite file, fake clocks)
and the in-memory run registry, made to fail where a test says so: the state a registry that is down, or
a process that ends between the charge and the run's record, leaves behind.
"""

from __future__ import annotations

import sqlite3
import threading
import unittest

from recursion_core.control import routines as r
from recursion_core.control.api import ControlAPI
from recursion_core.control.registry import InMemoryRunRegistry, StaleGeneration
from recursion_core.control.tests.fakes import FakeKillTree, FakeStarter, run_ids
from recursion_core.control.tests.test_routine_bounds import OWNER, PLUGIN, BoundsCase, a_record

ORIGIN = {"kind": "trigger", "record_kind": "routine.v1", "record_id": "morning-digest", "plugin": PLUGIN,
          "occurrence": "2026-11-24T08:00:00Z", "revision": 1}
WAIT_S = 0.5   # how long the failed start's release waits for the retry's launch before it goes on
JOIN_S = 30    # how long each thread of a test is waited for


class FlakyRegistry(InMemoryRunRegistry):
    """The in-memory registry whose first ``fail`` puts raise ``error``."""

    def __init__(self, fail=1, error=OSError("registry down")):
        super().__init__()
        self.fail, self.error = fail, error

    def put(self, rec):
        if self.fail > 0:
            self.fail -= 1
            raise self.error
        return super().put(rec)


class UnreadableRegistry(InMemoryRunRegistry):
    def get(self, run_id):
        raise OSError("registry down")


class ChargeCase(BoundsCase):
    def store(self, cls=r.RoutineStore):
        return cls(self.path, wall=lambda: self.wall[0], monotonic=lambda: self.mono[0], boot=lambda: self.boot)

    def api(self, store, registry=None, starter=None):
        self.starter = starter or FakeStarter()
        return ControlAPI(registry if registry is not None else self.registry, FakeKillTree(), self.starter,
                          clock=lambda: 1000.0, new_run_id=self.run_ids, routines=store)

    def start(self, api, key="k1"):
        return api.call("run.start", {"client_key": key}, origin=ORIGIN)

    def used(self, store):
        return store.get("morning-digest")["used"]

    def rows(self):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT run_id FROM charges ORDER BY run_id").fetchall()


class NoChargeWithoutARunRecordTest(ChargeCase):
    """The first put of the launch record fails; the start is sent again with the same key."""

    def failed_then_retried(self, limit, error=OSError("registry down"), detail="put:OSError"):
        self.registry = FlakyRegistry(error=error)
        store = self.store()
        store.admit(OWNER, a_record(budget={"unit": "runs", "limit": limit}))
        api = self.api(store)
        first = self.start(api)
        self.assertEqual((first["code"], first["detail"]), ("registry_unavailable", detail))
        kept = store.get("morning-digest")
        self.assertEqual((kept["used"], kept["last_refusal"], self.starter.calls), (0, None, []))
        again = self.start(api)
        self.assertEqual(again["code"], "started", again)
        self.assertEqual((self.used(store), len(self.registry.list()), len(self.starter.calls)), (1, 1, 1))
        self.assertIsNone(store.get("morning-digest")["last_refusal"])
        self.assertEqual(first["charged"], False)
        self.assertEqual(first["text"], r.TEXT["start_unrecorded"].format(detail=detail))
        self.assertEqual(self.rows(), [])  # the run is recorded: its charge stands, no row is held

    def test_a_budget_of_one_is_not_spent_by_a_start_whose_run_was_never_recorded(self):
        self.failed_then_retried(1)

    def test_a_budget_of_five_is_charged_once_for_the_one_run(self):
        self.failed_then_retried(5)

    def test_a_put_that_meets_a_newer_generation_gives_the_charge_back_too(self):
        self.failed_then_retried(1, error=StaleGeneration("run-1", 1, 2), detail="put:StaleGeneration")

    def test_a_person_start_whose_put_fails_answers_as_before(self):
        self.registry = FlakyRegistry()
        api = self.api(self.store())
        answer = api.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p", "client_key": "k1"})
        self.assertEqual(answer, {"code": "registry_unavailable", "detail": "put:OSError"})


class StuckStore(r.RoutineStore):
    """The real store that cannot be written when a charge is given back."""

    def give_back(self, origin, run_id):
        return False


class UnrecordingStore(r.RoutineStore):
    """The real store that cannot be written when the run's record is stored."""

    def recorded(self, origin, run_id):
        return False


class OneKeyOneChargeTest(ChargeCase):
    def test_a_retry_takes_the_charge_its_failed_start_still_holds(self):
        self.registry = FlakyRegistry()
        store = self.store(StuckStore)
        store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 1}))
        api = self.api(store)
        first = self.start(api)
        self.assertEqual((first["code"], self.used(store)), ("registry_unavailable", 1))
        again = self.start(api)
        self.assertEqual(again["code"], "started", again)
        self.assertEqual((self.used(store), len(self.registry.list())), (1, 1))
        self.assertEqual(again["run"]["run_id"], "run-1")  # the run id of the held charge
        self.assertEqual(first["charged"], True)
        self.assertEqual(first["text"], r.TEXT["start_unrecorded_held"].format(detail="put:OSError"))
        self.assertEqual(self.rows(), [])

    def test_a_retry_after_a_process_ended_between_the_charge_and_the_record_is_charged_once(self):
        store = self.store()
        store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 1}))
        api = self.api(store)
        # The process ended here: charged in the store, no run record.
        store.admit_start(ORIGIN, launching=lambda: "run-ended", key=("default", "k1"))
        self.assertEqual((self.used(store), self.rows()), (1, [("run-ended",)]))
        again = self.start(api)
        self.assertEqual(again["code"], "started", again)
        self.assertEqual((again["run"]["run_id"], self.used(store), self.rows()), ("run-ended", 1, []))

    def test_a_start_with_another_key_is_charged_for_itself(self):
        store = self.store()
        store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 5}))
        api = self.api(store)
        store.admit_start(ORIGIN, launching=lambda: "run-ended", key=("default", "k1"))
        self.assertEqual(self.start(api, key="k2")["code"], "started")
        self.assertEqual((self.used(store), self.rows()), (2, [("run-ended",)]))

    def test_a_held_charge_whose_lease_has_ended_is_given_back_with_the_refusal(self):
        store = self.store()
        store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 5}))
        api = self.api(store)
        store.admit_start(ORIGIN, launching=lambda: "run-ended", key=("default", "k1"))
        self.later(1000)
        answer = self.start(api)
        self.assertEqual((answer["code"], answer["detail"]), ("request_invalid", "routine_lease_ended"))
        self.assertEqual((self.used(store), self.rows()), (0, []))


class AChargeWithNoRunIsGivenBackWhenTheDaemonStartsTest(ChargeCase):
    def test_a_charge_with_no_run_record_is_given_back_and_logged(self):
        store = self.store()
        store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 5}))
        store.admit_start(ORIGIN, launching=lambda: "run-lost", key=("default", "k1"))
        self.assertEqual(self.used(store), 1)
        with self.assertLogs("recursion_core.control.routines", "WARNING") as logged:
            self.api(self.store())  # the daemon starts again over the same store file
        self.assertEqual(logged.output, ["WARNING:recursion_core.control.routines:" + r.TEXT["charge_given_back"].format(
            run_id="run-lost", record_id="morning-digest")])
        self.assertEqual((self.used(store), self.rows()), (0, []))

    def test_a_charge_whose_run_is_recorded_stands(self):
        store = self.store(UnrecordingStore)
        store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 5}))
        self.assertEqual(self.start(self.api(store))["code"], "started")
        self.assertEqual((self.used(store), len(self.rows())), (1, 1))  # the row could not be dropped
        self.api(self.store())
        self.assertEqual((self.used(store), self.rows()), (1, []))

    def test_a_registry_that_cannot_answer_leaves_the_charge_held(self):
        store = self.store()
        store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 5}))
        store.admit_start(ORIGIN, launching=lambda: "run-lost", key=("default", "k1"))
        self.api(self.store(), registry=UnreadableRegistry())
        self.assertEqual((self.used(store), self.rows()), (1, [("run-lost",)]))

    def test_a_new_admission_drops_the_rows_of_the_old_budget(self):
        store = self.store()
        store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 5}))
        store.admit_start(ORIGIN, launching=lambda: "run-lost", key=("default", "k1"))
        store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 5}))
        self.api(self.store())
        self.assertEqual((self.used(store), self.rows()), (0, []))


class HookedStore(r.RoutineStore):
    """The real store whose first ``launched`` waits (at most ``WAIT_S``) for a same-key retry to launch."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.releasing, self.retry_launching, self.released = threading.Event(), threading.Event(), threading.Event()
        self.calls = 0

    def launched(self, origin, run_id):
        self.calls += 1
        if self.calls == 1:
            self.releasing.set()
            self.retry_launching.wait(WAIT_S)
        try:
            return super().launched(origin, run_id)
        finally:
            if self.calls == 1:
                self.released.set()


class FailsThenAsksAChange(FakeStarter):
    """The first launch fails; the second (the retry) asks the person's change while it is launching."""

    def __init__(self, store):
        super().__init__()
        self.store, self.changed = store, None

    def __call__(self, params):
        if not self.calls:
            self.calls.append(params)
            raise RuntimeError("launch failed")
        self.store.retry_launching.set()
        self.store.released.wait(JOIN_S)
        revision = self.store.get("morning-digest")["revision"]
        self.changed = self.store.change(OWNER, "morning-digest", {"prompt": "Only the failures."}, revision=revision)
        return super().__call__(params)


class TheHoldOfAFailedLaunchTest(ChargeCase):
    def test_a_late_release_of_a_failed_start_never_hides_its_retry_from_a_change(self):
        store = self.store(HookedStore)
        store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 5}))
        starter = FailsThenAsksAChange(store)
        api = self.api(store, starter=starter)
        answers = {}
        first = threading.Thread(target=lambda: answers.update(first=self.start(api)))
        retry = threading.Thread(target=lambda: answers.update(retry=self.start(api)))
        first.start()
        self.assertTrue(store.releasing.wait(JOIN_S))
        retry.start()
        first.join(JOIN_S)
        retry.join(JOIN_S)
        self.assertEqual((answers["first"]["code"], answers["retry"]["code"]), ("start_failed", "started"))
        run_id = answers["retry"]["run"]["run_id"]
        self.assertEqual(starter.changed["code"], "ok")
        self.assertEqual(starter.changed.get("starting"), [run_id], starter.changed)
        self.assertEqual(self.used(store), 1)


class WritesThenRaises(InMemoryRunRegistry):
    """The in-memory registry whose first put raises, after storing the record when ``writes``; the next
    ``unreadable`` reads after that put raise too."""

    def __init__(self, writes=True, unreadable=0):
        super().__init__()
        self.fail, self.writes, self.unreadable, self.blind = 1, writes, unreadable, 0

    def put(self, rec):
        if self.fail > 0:
            self.fail -= 1
            if self.writes:
                super().put(rec)
            self.blind = self.unreadable
            raise OSError("written, answer lost" if self.writes else "registry down")
        return super().put(rec)

    def get(self, run_id):
        if self.blind > 0:
            self.blind -= 1
            raise OSError("registry down")
        return super().get(run_id)


def reconcile(registry):
    """What the daemon's reconcile does to a starting record with no unit (DaemonControl._reconcile_one)."""
    for rec in registry.list():
        if rec.get("state") == "starting" and not rec.get("cgroup"):
            rec["state"] = "start_failed"
            registry.put(rec)


class APutThatRaisesTest(ChargeCase):
    """The put of the launch record raises; only a record proven absent gives its charge back."""

    def budget_of_one(self, registry):
        self.registry = registry
        store = self.store()
        store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 1}))
        return store, self.api(store)

    def and_then(self, store, api, first):
        """The same-key retry, the daemon's reconcile, the retry again, then a start with another key."""
        answers = [first, self.start(api)]
        reconcile(self.registry)
        answers.append(self.start(api))
        other = self.start(api, key="k2")
        self.assertEqual((other["code"], other["detail"]), ("request_invalid", "routine_budget_spent"), answers)
        self.assertEqual((self.used(store), len(self.registry.list()), len(self.starter.calls)), (1, 1, 1), answers)
        self.assertEqual(self.rows(), [])
        return answers

    def test_a_record_that_is_there_is_the_start_made_and_keeps_its_charge(self):
        store, api = self.budget_of_one(WritesThenRaises())
        first = self.start(api)
        self.assertEqual((first["code"], self.used(store)), ("started", 1), first)
        answers = self.and_then(store, api, first)
        self.assertEqual([(a["code"], a["duplicate"]) for a in answers[1:]], [("started", True)] * 2)

    def test_a_record_that_is_not_there_gives_the_charge_back(self):
        store, api = self.budget_of_one(WritesThenRaises(writes=False))
        first = self.start(api)
        self.assertEqual((first["code"], first["charged"], self.used(store)), ("registry_unavailable", False, 0))
        self.assertEqual(first["text"], r.TEXT["start_unrecorded"].format(detail="put:OSError"))
        self.assertEqual(self.and_then(store, api, first)[1]["code"], "started")

    def unknown(self, writes):
        store, api = self.budget_of_one(WritesThenRaises(writes=writes, unreadable=1))
        first = self.start(api)
        self.assertEqual((first["code"], first["charged"], self.used(store)), ("registry_unavailable", True, 1))
        self.assertEqual(first["text"], r.TEXT["start_record_unknown"].format(detail="put:OSError, get:OSError"))
        self.assertEqual(self.and_then(store, api, first)[1]["code"], "started")  # launched once, charged once

    def test_a_record_written_that_cannot_be_read_back_keeps_the_charge_and_says_so(self):
        self.unknown(writes=True)

    def test_a_record_not_written_that_cannot_be_read_back_keeps_the_charge_and_says_so(self):
        self.unknown(writes=False)

    def test_a_record_that_cannot_be_read_back_is_given_back_at_the_next_start_when_it_is_not_there(self):
        store, api = self.budget_of_one(WritesThenRaises(writes=False, unreadable=1))
        self.assertEqual(self.start(api)["charged"], True)
        self.api(self.store())  # the daemon starts again: the run list does not hold the run
        self.assertEqual((self.used(store), self.rows()), (0, []))

    def test_two_runs_on_a_budget_of_one(self):
        """A put that wrote and raised; the retry; the reconcile; the retry; another key (budget one)."""
        store, api = self.budget_of_one(WritesThenRaises())
        self.start(api)
        self.start(api)
        reconcile(self.registry)
        self.start(api)
        self.start(api, key="k2")
        self.assertEqual(len(self.starter.calls), 1)
        self.assertEqual(self.used(store), len(self.registry.list()))


if __name__ == "__main__":
    unittest.main()
