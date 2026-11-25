"""A start asked from a routine record runs what the record says, never what the caller sends
(control/routines.py, control/api.py).

The person admits a plugin to start ONE routine: what is run, as whom, on which engine. Each run here is
asked the way a plugin asks it (a ``run.request`` at the daemon's end of its channel) or the way the daemon
itself would (``ControlAPI.call("run.start", ..., origin=...)``), with the core's routine store given to
the API.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import unittest
from pathlib import Path

from recursion_core.control import routines as r
from recursion_core.control.api import ControlAPI
from recursion_core.control.tests.fakes import FakeKillTree, FakeStarter
from recursion_core.control.tests.test_routine_bounds import (
    LEASE_S, OWNER, PLUGIN, BoundsCase, FailingOnce, Granting, a_record, at,
)
from recursion_core.plugin_security import host_channel as hc

REPO = Path(__file__).resolve().parents[4]
SCHEMA = json.loads((REPO / "schemas/control/v1/routine.record.v1.json").read_text(encoding="utf-8"))
RECORD = a_record()
SLOW_S = 0.4         # the two-thread run's starter takes this long, as the reproduction's did
JOIN_S = 30          # and each thread is waited for this long
VALUES = ("tenant", "engine", "prompt")  # what a start from a record runs


def trigger(occurrence="2026-11-24T00:01:00Z", **changes):
    origin = {"kind": "trigger", "plugin": PLUGIN, "record_kind": "routine.v1", "record_id": "morning-digest",
              "revision": 1, "occurrence": occurrence}
    origin.update(changes)
    return {k: v for k, v in origin.items() if v is not None}


def what_ran(starter):
    return [{k: call[k] for k in VALUES} for call in starter.calls]


class WhatRunsCase(BoundsCase):
    def request(self, d, **values):
        """A run.request at the daemon's end of the plugin's channel, with the record's values but ``values``."""
        args = {"record_kind": "routine.v1", "record_id": "morning-digest", "revision": 1, "occurrence": at(1),
                **{k: RECORD[k] for k in VALUES}, **values}
        answer = hc.DaemonEnd(PLUGIN, Granting(), runs=d.api).receive(
            {"type": "call", "id": 1, "name": "run.request", "args": args})
        self.assertEqual(answer["type"], "result", answer)
        return answer["value"]

    def assertDiffers(self, d, answer, names):
        self.assertEqual((answer["code"], answer["detail"]), ("request_invalid", "routine_differs"), answer)
        for name in names:
            self.assertIn(name, answer["text"])
        for prompt in (RECORD["prompt"], "delete everything"):
            self.assertNotIn(prompt, answer["text"])
        kept = d.store.get("morning-digest")
        self.assertEqual((kept["last_refusal"]["code"], kept["last_refusal"]["differs"]), ("routine_differs", names))
        self.assertEqual((kept["used"], d.starter.calls), (0, []))

    def corrupt(self, d, **values):
        """Change the record in the store's file itself, the way nothing but the person's commands may."""
        with sqlite3.connect(d.store._path) as db:
            body = json.loads(db.execute("SELECT body FROM routines WHERE record_id = 'morning-digest'").fetchone()[0])
            body.update(values)
            db.execute("UPDATE routines SET body = ? WHERE record_id = 'morning-digest'", (json.dumps(body),))
        db.close()


class TheReproducersRunTest(WhatRunsCase):
    def test_a_plugins_request_with_another_tenant_engine_and_prompt_starts_nothing(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        answer = self.request(d, tenant="other", engine="other_engine", prompt="delete everything")
        self.assertDiffers(d, answer, ["tenant", "engine", "prompt"])
        self.assertIn("tenant, engine and prompt", answer["text"])
        self.assertEqual(self.registry.list(), [])

    def test_the_daemons_own_start_with_another_tenant_engine_and_prompt_starts_nothing(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        answer = d.api.call("run.start", {"tenant": "other", "engine": "other_engine", "prompt": "do something else",
                                          "client_key": "k1"}, origin=trigger(occurrence="1"))
        self.assertDiffers(d, answer, ["tenant", "engine", "prompt"])
        self.assertNotIn("occurrence", d.store.get("morning-digest")["last_refusal"])  # "1" is not an occurrence

    def test_each_of_the_three_alone_is_refused_named_and_charged_nothing(self):
        for name, value in (("tenant", "other"), ("engine", "other_engine"), ("prompt", "delete everything")):
            with self.subTest(name=name):
                self.setUp()
                d = self.daemon()
                d.store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 1}))
                answer = self.request(d, **{name: value})
                self.assertDiffers(d, answer, [name])
                self.assertEqual(answer["text"], r.TEXT["routine_differs"].format(what=name))
                self.assertEqual(self.request(d)["code"], "started")  # the budget of one is still whole
                self.assertEqual(what_ran(d.starter), [{k: RECORD[k] for k in VALUES}])

    def test_what_starts_is_the_records_tenant_engine_and_prompt_and_the_caller_may_leave_them_out(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        answer = d.api.call("run.start", {"client_key": "k1"}, origin=trigger())
        self.assertEqual(answer["code"], "started", answer)
        self.assertEqual(what_ran(d.starter), [{k: RECORD[k] for k in VALUES}])
        self.assertEqual((answer["run"]["tenant"], answer["run"]["engine"]), (RECORD["tenant"], RECORD["engine"]))
        self.assertEqual(d.store.get("morning-digest")["used"], 1)

    def test_a_value_sent_out_of_its_form_is_refused_as_run_start_refuses_it(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        for name, value in (("tenant", ""), ("engine", 7), ("prompt", "\ud800")):
            with self.subTest(name=name):
                answer = d.api.call("run.start", {name: value, "client_key": f"k-{name}"}, origin=trigger())
                self.assertEqual((answer["code"], answer["detail"]), ("request_invalid", name))
        self.assertEqual((d.starter.calls, d.store.get("morning-digest")["used"]), ([], 0))


class TheChangedRecordTest(WhatRunsCase):
    def test_the_record_keeps_its_prompt_by_a_digest_and_the_persons_change_digests_it_again(self):
        d = self.daemon()
        stored = d.store.admit(OWNER, a_record())["routine"]
        self.assertEqual(stored["prompt_sha256"], r.prompt_digest(RECORD["prompt"]))
        changed = d.store.change(OWNER, "morning-digest", {"prompt": "Only the failures."}, revision=1)["routine"]
        self.assertEqual(changed["prompt_sha256"], r.prompt_digest("Only the failures."))
        self.assertDiffers(d, self.request(d), ["prompt"])  # the plugin still sends the old prompt
        self.assertEqual(self.request(d, revision=2, prompt="Only the failures.")["code"], "started")

    def test_a_prompt_changed_in_the_store_is_refused_kept_and_charged_nothing(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        self.corrupt(d, prompt="delete everything")
        for values in ({}, {"prompt": "delete everything"}):
            with self.subTest(values=values):
                answer = self.request(d, **values)
                self.assertEqual((answer["code"], answer["detail"]), ("request_invalid", "routine_changed"))
                self.assertEqual(answer["text"], r.TEXT["routine_changed"])
        kept = d.store.list()["routines"][0]
        self.assertEqual((kept["last_refusal"]["code"], kept["used"], d.starter.calls), ("routine_changed", 0, []))
        self.assertNotIn("differs", kept["last_refusal"])

    def test_a_tenant_or_an_engine_out_of_its_form_in_the_store_is_refused(self):
        for name in ("tenant", "engine"):
            with self.subTest(name=name):
                self.setUp()
                d = self.daemon()
                d.store.admit(OWNER, a_record())
                self.corrupt(d, **{name: ""})
                answer = d.api.call("run.start", {"client_key": "k1"}, origin=trigger())
                self.assertEqual(answer["detail"], "routine_changed", answer)
                self.assertEqual(d.starter.calls, [])


class ChangingStore:
    """The real store, which the person changes right after the start's first read of it (``when``)."""

    def __init__(self, store, when):
        self.store, self.when, self.reads = store, when, 0

    def admit_start(self, origin, asked=None, *, charge=True, reread=False, launching=None):
        answer = self.store.admit_start(origin, asked, charge=charge, reread=reread, launching=launching)
        if not charge and not reread:
            self.reads += 1
            if self.reads == self.when:
                revision = self.store.get("morning-digest")["revision"]
                self.store.change(OWNER, "morning-digest", {"prompt": "Only the failures."}, revision=revision)
        return answer

    def launched(self, origin, run_id):
        return self.store.launched(origin, run_id)


class EveryWayToTheLaunchTest(WhatRunsCase):
    """Each way to the one launch call reads the record again inside the start lock."""

    def test_a_fresh_start_whose_record_changed_after_its_first_read_starts_nothing(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        d.api = ControlAPI(self.registry, FakeKillTree(), d.starter, clock=lambda: 1000.0, new_run_id=self.run_ids,
                           routines=ChangingStore(d.store, when=1))
        answer = self.request(d)
        self.assertEqual((answer["code"], answer["detail"]), ("request_invalid", "routine_changed"), answer)
        self.assertEqual((d.starter.calls, d.store.get("morning-digest")["used"]), ([], 0))

    def test_a_relaunch_whose_record_changed_since_the_failed_launch_starts_nothing(self):
        d = self.daemon()
        d.starter = FailingOnce()
        d.store.admit(OWNER, a_record())
        d.api = ControlAPI(self.registry, FakeKillTree(), d.starter, clock=lambda: 1000.0, new_run_id=self.run_ids,
                           routines=ChangingStore(d.store, when=2))
        self.assertEqual(self.request(d)["code"], "start_failed")
        answer = self.request(d)  # the same occurrence: the run is relaunched only if the record still says so
        self.assertEqual((answer["code"], answer["detail"]), ("request_invalid", "routine_changed"), answer)
        self.assertEqual(len(d.starter.calls), 1)
        self.assertEqual(d.store.get("morning-digest")["used"], 1)

    def test_a_relaunch_of_an_unchanged_record_runs_its_values_and_is_charged_once(self):
        d = self.daemon()
        d.starter = FailingOnce()
        d.store.admit(OWNER, a_record())
        d.api = ControlAPI(self.registry, FakeKillTree(), d.starter, clock=lambda: 1000.0, new_run_id=self.run_ids,
                           routines=d.store)
        self.assertEqual(self.request(d)["code"], "start_failed")
        self.assertEqual(self.request(d)["code"], "started")  # the same occurrence, relaunched
        self.assertEqual(what_ran(d.starter), [{k: RECORD[k] for k in VALUES}] * 2)
        self.assertEqual(d.store.get("morning-digest")["used"], 1)

    def test_a_surfaces_start_that_carries_an_origin_in_its_params_is_not_a_start_from_a_record(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        answer = d.api.call("run.start", {"tenant": "default", "engine": "claude_code", "prompt": "p",
                                          "origin": trigger()})
        self.assertEqual(answer["code"], "started")
        self.assertNotIn("origin", answer["run"])
        self.assertEqual((d.store.get("morning-digest")["used"], d.store.get("morning-digest")["last_refusal"]),
                         (0, None))


class TheKeptRefusalTest(WhatRunsCase):
    def test_a_refusal_is_kept_with_a_plugin_and_an_occurrence_only_when_each_is_in_its_form(self):
        for label, origin, absent in (("an empty occurrence", trigger(occurrence=""), "occurrence"),
                                      ("no occurrence", trigger(occurrence=None), "occurrence"),
                                      ("an occurrence that is not an instant", trigger(occurrence="1"), "occurrence")):
            with self.subTest(origin=label):
                self.setUp()
                d = self.daemon()
                d.store.admit(OWNER, a_record())
                self.later(LEASE_S + 1)  # past the lease of the record
                answer = d.api.call("run.start", {"client_key": "k1"}, origin=origin)
                self.assertEqual(answer["detail"], "routine_lease_ended")
                kept = d.store.get("morning-digest")["last_refusal"]
                self.assertEqual((kept["code"], kept["plugin"]), ("routine_lease_ended", PLUGIN))
                self.assertNotIn(absent, kept)
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        self.assertEqual(d.api.call("run.start", {"client_key": "k2"}, origin=trigger(plugin=None))["detail"],
                         "routine_unknown")
        kept = d.store.get("morning-digest")["last_refusal"]
        self.assertEqual((kept["code"], kept["occurrence"]), ("routine_unknown", "2026-11-24T00:01:00Z"))
        self.assertNotIn("plugin", kept)
        self.assertNotIn("None", json.dumps(kept))

    def test_the_kept_refusal_is_shown_on_the_list_with_exactly_the_published_fields(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        self.request(d, engine="other_engine", prompt="delete everything")
        kept = d.store.list()["routines"][0]["last_refusal"]
        self.assertEqual({k: kept[k] for k in ("code", "plugin", "occurrence", "differs")},
                         {"code": "routine_differs", "plugin": PLUGIN, "occurrence": at(1),
                          "differs": ["engine", "prompt"]})
        refusal = SCHEMA["$defs"]["refusal"]
        self.assertLessEqual(set(kept), set(refusal["properties"]))
        self.assertEqual(set(refusal["required"]), {"code", "at"})
        self.assertEqual(refusal["properties"]["code"]["enum"], list(r.START_REFUSALS))
        self.assertEqual(refusal["properties"]["differs"]["items"]["enum"], list(r.RUN_VALUES))

    def test_a_wrong_or_missing_record_kind_is_not_kept_and_the_module_text_says_which_are(self):
        d = self.daemon()
        d.store.admit(OWNER, a_record())
        for label, origin in (("routine.v2", trigger(record_kind="routine.v2")),
                              ("no kind", trigger(record_kind=None))):
            with self.subTest(origin=label):
                answer = d.api.call("run.start", {"client_key": f"k-{label}"}, origin=origin)
                self.assertEqual(answer["detail"], "routine_unknown")
        self.assertEqual((d.store.get("morning-digest")["last_refusal"], d.store.get("morning-digest")["used"]),
                         (None, 0))
        kept_paragraph = next(p for p in r.__doc__.split("\n\n") if p.startswith("Which refusals are kept"))
        for code in r.START_REFUSALS:
            self.assertIn(f"``{code}``", kept_paragraph)
        self.assertIn("record_kind", kept_paragraph)
        self.assertNotIn("Every refusal", r.RoutineStore.admit_start.__doc__)


class SlowStarter(FakeStarter):
    def __call__(self, params):
        time.sleep(SLOW_S)
        return super().__call__(params)


class TwoStartsWithOneKeyTest(WhatRunsCase):
    def test_two_starts_at_once_with_the_same_key_start_one_run_charged_once(self):
        """Holds today's answer: the start lock of one (tenant, key) lets the second find the first's run."""
        d = self.daemon()
        d.starter = SlowStarter()
        d.api = ControlAPI(self.registry, FakeKillTree(), d.starter, clock=lambda: 1000.0, new_run_id=self.run_ids,
                           routines=d.store)
        d.store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 1}))
        answers, start = {}, threading.Barrier(2)

        def go(n):
            start.wait(JOIN_S)
            answers[n] = d.api.call("run.start", {k: RECORD[k] for k in VALUES} | {"client_key": "same"},
                                    origin=trigger(occurrence="1"))

        threads = [threading.Thread(target=go, args=(n,)) for n in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(JOIN_S)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([answers[n]["code"] for n in range(2)], ["started", "started"])
        self.assertEqual(answers[0]["run"]["run_id"], answers[1]["run"]["run_id"])
        self.assertEqual(len(d.starter.calls), 1)
        self.assertEqual((d.store.get("morning-digest")["used"], d.store.get("morning-digest")["last_refusal"]),
                         (1, None))


if __name__ == "__main__":
    unittest.main()
