"""No routine store, no start; a person's change or removal says when an earlier start may still run the old
values; a start from a record carries nothing the record does not bind (control/api.py, control/routines.py).

Each run is asked the way a plugin asks it (a ``run.request`` at the daemon's end of its channel) or the way
the daemon itself would (``ControlAPI.call("run.start", ..., origin=...)``).
"""

from __future__ import annotations

import threading
import time
import unittest

from recursion_core.control import routines as r
from recursion_core.control.api import ControlAPI
from recursion_core.control.tests.fakes import FakeKillTree, FakeStarter
from recursion_core.control.tests.test_routine_bounds import OWNER, PLUGIN, Granting, a_record, at
from recursion_core.control.tests.test_routine_what_runs import RECORD, VALUES, WhatRunsCase, trigger, what_ran
from recursion_core.plugin_security import host_channel as hc

JOIN_S = 30                # each thread is waited for this long
ROUNDS = 50                # the race below runs this many rounds for each way and each delay
DELAYS_S = (0.0, 0.05)     # and its launch path takes this long in each (a launch outlasts a change)


def store_absent(answer):
    return (answer.get("code"), answer.get("detail")) == ("request_invalid", "routine_store_absent")


class NoRoutineStoreTest(WhatRunsCase):
    """Where the API is given no routine store there is no record to run from: a plugin's start runs nothing."""

    def api_without_store(self):
        self.starter = FakeStarter()
        return ControlAPI(self.registry, FakeKillTree(), self.starter, clock=lambda: 1000.0, new_run_id=self.run_ids)

    def assertNoStore(self, answer):
        self.assertTrue(store_absent(answer), answer)
        self.assertEqual(answer["text"], r.TEXT["routine_store_absent"])
        self.assertEqual((self.starter.calls, self.registry.list()), ([], []))

    def test_a_plugins_request_with_no_routine_store_starts_nothing(self):
        api = self.api_without_store()
        args = {"record_kind": "routine.v1", "record_id": "morning-digest", "revision": 1, "occurrence": at(1),
                "tenant": "other", "engine": "other_engine", "prompt": "delete everything"}
        answer = hc.DaemonEnd(PLUGIN, Granting(), runs=api).receive(
            {"type": "call", "id": 1, "name": "run.request", "args": args})
        self.assertEqual(answer["type"], "result", answer)
        self.assertNoStore(answer["value"])

    def test_the_daemons_own_trigger_start_with_no_routine_store_starts_nothing_retried_or_bare(self):
        api = self.api_without_store()
        params = {"tenant": "other", "engine": "other_engine", "prompt": "delete everything", "client_key": "k"}
        for _ in range(2):
            self.assertNoStore(api.call("run.start", params, origin=trigger()))
        self.assertNoStore(api.call("run.start", {}, origin=trigger()))  # what is missing is the store, not a value

    def test_the_words_say_what_is_missing_and_what_the_person_does(self):
        text = r.TEXT["routine_store_absent"]
        for words in ("no routine store", "no run was started", "nothing was charged",
                      "until a routine store is configured", "start the run yourself"):
            self.assertIn(words, text)

    def test_a_persons_own_start_with_no_routine_store_still_starts(self):
        api = self.api_without_store()
        answer = api.call("run.start", {"tenant": "default", "engine": "claude_code", "prompt": "p"})
        self.assertEqual(answer["code"], "started", answer)


class HeldStarter(FakeStarter):
    """A launch path that waits, once it is entered, until the test lets it go (or fails then, ``fail``)."""

    def __init__(self, fail=False):
        super().__init__()
        self.entered, self.go, self.fail = threading.Event(), threading.Event(), fail

    def __call__(self, params):
        self.entered.set()
        self.go.wait(JOIN_S)
        launched = super().__call__(params)
        if self.fail:
            raise OSError("the launch path failed")
        return launched


class WhileAStartIsLaunchedTest(WhatRunsCase):
    """A start admitted from the record a moment before is still being launched with the record's old values:
    the person's change or removal answers ``ok`` and names that run."""

    def held(self, d, fail=False):
        d.starter = HeldStarter(fail)
        d.api = ControlAPI(self.registry, FakeKillTree(), d.starter, clock=lambda: 1000.0, new_run_id=self.run_ids,
                           routines=d.store)
        d.store.admit(OWNER, a_record())
        answers = []
        thread = threading.Thread(target=lambda: answers.append(
            d.api.call("run.start", {"client_key": "k"}, origin=trigger())))
        thread.start()
        self.assertTrue(d.starter.entered.wait(JOIN_S))
        return thread, answers

    def finish(self, d, thread, answers):
        d.starter.go.set()
        thread.join(JOIN_S)
        self.assertFalse(thread.is_alive())
        return answers[0]

    def revision(self, d):
        return d.store.get("morning-digest")["revision"]

    def test_a_change_while_a_start_is_launched_names_the_run_that_may_run_the_old_values(self):
        d = self.daemon()
        thread, answers = self.held(d)
        changed = d.store.change(OWNER, "morning-digest", {"prompt": "Only the failures."}, revision=self.revision(d))
        started = self.finish(d, thread, answers)
        self.assertEqual(started["code"], "started", started)
        run_id = started["run"]["run_id"]
        self.assertEqual((changed["code"], changed["starting"]), ("ok", [run_id]), changed)
        self.assertEqual(changed["text"], r.TEXT["routine_changed_while_starting"].format(runs=run_id))
        self.assertEqual(what_ran(d.starter), [{k: RECORD[k] for k in VALUES}])  # the old values, as it said
        again = d.store.change(OWNER, "morning-digest", {"prompt": "Everything."}, revision=self.revision(d))
        self.assertEqual(sorted(again), ["code", "routine"])  # nothing is being launched: nothing more is said

    def test_a_removal_while_a_start_is_launched_names_the_run(self):
        d = self.daemon()
        thread, answers = self.held(d)
        removed = d.store.remove(OWNER, "morning-digest")
        started = self.finish(d, thread, answers)
        run_id = started["run"]["run_id"]
        self.assertEqual((removed["code"], removed["starting"]), ("ok", [run_id]), removed)
        self.assertEqual(removed["text"], r.TEXT["routine_removed_while_starting"].format(runs=run_id))
        d.store.admit(OWNER, a_record())
        self.assertEqual(d.store.remove(OWNER, "morning-digest"), {"code": "ok", "removed": "morning-digest"})

    def test_a_launch_that_failed_is_no_longer_named(self):
        d = self.daemon()
        thread, answers = self.held(d, fail=True)
        self.assertEqual(self.finish(d, thread, answers)["code"], "start_failed")
        changed = d.store.change(OWNER, "morning-digest", {"prompt": "Only the failures."}, revision=self.revision(d))
        self.assertEqual(sorted(changed), ["code", "routine"])

    def test_the_words_say_the_run_may_still_start_and_what_the_person_does(self):
        for code in ("routine_changed_while_starting", "routine_removed_while_starting"):
            text = r.TEXT[code].format(runs="run-9")
            for words in ("run-9", "may run with what the routine said before", "Stop that run"):
                self.assertIn(words, text)


def race(case, way, rounds, delay_s):
    """``rounds`` starts, each against the person's change of prompt and engine (``way`` "change") or removal
    (``way`` "remove") of its own record at the same moment. Returns the counts; ``unnamed``, ``mixed`` and
    ``named_not_old`` must be 0: a launch of the old values still going on when the person was told ``ok``
    that the answer did not name, a launch of values that are no record's whole three, and a run named that
    did not launch the old values."""
    counts = {"old_ran": 0, "old_ran_named": 0, "new_ran": 0, "refused": 0, "unnamed": 0, "mixed": 0,
              "named_not_old": 0}
    store = case.daemon().store
    entries, ended = [], {}

    class Timed(FakeStarter):
        def __call__(self, params):
            entries.append((params["run_id"], {k: params[k] for k in VALUES}))
            time.sleep(delay_s)
            launched = super().__call__(params)
            ended[params["run_id"]] = time.monotonic()  # the launch call returns after this
            return launched

    api = ControlAPI(case.registry, FakeKillTree(), Timed(), clock=lambda: 1000.0, new_run_id=case.run_ids,
                     routines=store)
    old = {k: RECORD[k] for k in VALUES}
    new = {**old, "prompt": "Only the failures.", "engine": "codex"}
    for n in range(rounds):
        record_id = f"race-{way}-{delay_s}-{n}"  # a record and a start key of its own each round
        store.admit(OWNER, a_record(record_id=record_id))
        before, gate, out = len(entries), threading.Barrier(2), {}

        def start():
            gate.wait(JOIN_S)
            out["start"] = api.call("run.start", {"client_key": record_id}, origin=trigger(record_id=record_id))

        def person():
            gate.wait(JOIN_S)
            if way == "change":
                revision = store.get(record_id)["revision"]
                answer = store.change(OWNER, record_id, {"prompt": new["prompt"], "engine": new["engine"]},
                                      revision=revision)
            else:
                answer = store.remove(OWNER, record_id)
            out["person"], out["at"] = answer, time.monotonic()

        threads = [threading.Thread(target=start), threading.Thread(target=person)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(JOIN_S)
        case.assertFalse(any(thread.is_alive() for thread in threads))
        case.assertEqual(out["person"]["code"], "ok", out["person"])
        named = out["person"].get("starting", [])
        ran = entries[before:]
        case.assertEqual(len(ran), 1 if out["start"]["code"] == "started" else 0, out["start"])
        for run_id, values in ran:
            if values == old:
                counts["old_ran"] += 1
                counts["old_ran_named"] += run_id in named
                counts["unnamed"] += ended[run_id] > out["at"] and run_id not in named
            elif values == new:
                counts["new_ran"] += 1
            else:
                counts["mixed"] += 1
        counts["refused"] += out["start"]["code"] != "started"
        counts["named_not_old"] += sum(1 for run_id in named if (run_id, old) not in ran)
    return counts


class AStartAgainstThePersonTest(WhatRunsCase):
    """The person is never told ``ok`` with nothing more while a start of the old values is still launched."""

    def test_a_race_of_starts_against_changes_and_removals_never_hides_an_old_run(self):
        for way in ("change", "remove"):
            for delay_s in DELAYS_S:
                counts = race(self, way, ROUNDS, delay_s)
                self.assertEqual((counts["unnamed"], counts["mixed"], counts["named_not_old"]), (0, 0, 0),
                                 (way, delay_s, counts))
                if way == "remove":
                    self.assertEqual(counts["new_ran"], 0, counts)


class WhatTheRecordDoesNotBindTest(WhatRunsCase):
    """What is not bound to the record does not travel: a key the record does not name is refused."""

    def test_a_key_the_record_does_not_name_is_refused_charged_nothing_and_never_handed_on(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        for extra in ({"cwd": "/"}, {"model": "another"}, {"env": {"A": "1"}}, {"options": {}}, {"run_id": "mine"}):
            answer = d.api.call("run.start", {**{k: RECORD[k] for k in VALUES}, "client_key": "k", **extra},
                                origin=trigger())
            self.assertEqual((answer["code"], answer["detail"]), ("request_invalid", "routine_unbound_values"), answer)
            self.assertEqual(answer["text"], r.TEXT["routine_unbound_values"])
        self.assertEqual((d.starter.calls, d.store.get("morning-digest")["used"]), ([], 0))
        self.assertEqual(d.api.call("run.start", {"client_key": "k"}, origin=trigger())["code"], "started")
        self.assertEqual(set(d.starter.calls[0]), set(VALUES) | {"client_key", "run_id"})

    def test_a_persons_own_start_is_answered_as_before(self):
        d = self.daemon()
        answer = d.api.call("run.start", {"tenant": "default", "engine": "claude_code", "prompt": "p", "cwd": "/"})
        self.assertEqual(answer["code"], "started", answer)


if __name__ == "__main__":
    unittest.main()
