"""The first outside reads of #400 (two Codex reads and a DeepSeek read of ``bcb11bc6``), each finding taken as a
test that fails on that head: the service's lifecycle, delivery, waiting across a restart, the bindings a
restart retires, a row that cannot be read, two callers at once, the methods' parameters, the accessor's
trusted lines, the logging of a failing subscriber, and the drivers' way to the queue. Several are modelled
on the reviewers' own checks (``lifecycle_checks.py``, ``survivor_regressions.py``). No test waits on the
wall clock except to check that something has NOT happened yet.
"""

from __future__ import annotations

import copy
import json
import threading
import types
import unittest

from recursion_core.approvals import effects
from recursion_core.approvals import store as rows
from recursion_core.approvals.effects import EffectRefused
from recursion_core.approvals.model import ApprovalRefused
from recursion_core.approvals.queue import ApprovalQueue
from recursion_core.approvals.tests.test_queue import V1, Clock, FakeStore, _vector_effect, needs_fakes

try:
    from recursion_core.approvals.hooks import install, queue_of
    from recursion_core.control.hooks import Hooks, Principal
except ImportError:  # pragma: no cover
    Hooks = None

OWNER = {"kind": "owner", "id": "local"}


class _Base(unittest.TestCase):
    serial = True

    def setUp(self) -> None:
        self.store = FakeStore(serial=self.serial)
        self.addCleanup(self.store.close)
        self.clock = Clock()
        self.queue = ApprovalQueue(clock=self.clock, retry_seconds=0.0)
        self.queue.attach(self.store)
        self.addCleanup(self._stop_worker, self.queue)
        self.engine = _vector_effect("g01-a-tool-call.json")
        self.plugin = _vector_effect("g03-a-plugin-effect.json")

    @staticmethod
    def _stop_worker(queue) -> None:
        queue._closing.set()
        queue._wake.set()
        if queue._worker is not None and queue._worker.ident is not None:
            queue._worker.join(10)

    def engine_request(self, request_id: str = "toolu_1", queue=None) -> str:
        return (queue or self.queue).request("engine_tool", self.engine, session_id=self.engine["session_id"],
                                             run_id=self.engine["run_id"], turn_id="turn-0001",
                                             engine_request_id=request_id)

    def plugin_request(self, queue=None, **change) -> str:
        return (queue or self.queue).request("plugin_effect", dict(self.plugin, **change), run_id=self.plugin["run_id"])

    def restarted(self) -> ApprovalQueue:
        queue = ApprovalQueue(clock=self.clock, retry_seconds=0.0)
        queue.attach(self.store, declare_tables=False)
        self.addCleanup(self._stop_worker, queue)
        return queue

    def call(self, queue, method: str, params: dict) -> dict:
        callbacks: list = []
        with self.store.transaction() as tx:
            answer = (queue.open_in(tx, OWNER, params) if method == "approval.open"
                      else queue.answer_in(tx, OWNER, params, callbacks.append))
        for fn in callbacks:
            fn()
        return answer

    def approve(self, approval_id: str, queue=None) -> dict:
        queue = queue or self.queue
        shown = self.call(queue, "approval.open", {"command_id": "o-" + approval_id, "approval_id": approval_id,
                                                   "surface": "cli"})["approval"]
        return self.call(queue, "approval.answer", {"command_id": "a-" + approval_id, "approval_id": approval_id,
                                                    "surface": "cli", "expected_revision": shown["revision"],
                                                    "digest": shown["digest"], "answer": "approve"})["approval"]

    def start_worker(self, queue=None) -> ApprovalQueue:
        queue = queue or self.queue
        queue._worker = threading.Thread(target=queue._run, daemon=True)
        queue._worker.start()
        return queue


@needs_fakes
class ServiceLifecycleTests(_Base):
    def test_stop_keeps_the_worker_until_its_join_succeeds(self) -> None:  # Astra R1, Sol R5
        class Worker:
            calls, alive, ident = 0, True, 1

            def join(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise KeyboardInterrupt("an interruption while joining")
                self.alive = False

        worker = Worker()
        self.queue._worker = worker
        with self.assertRaises(KeyboardInterrupt):
            self.queue.stop()
        self.assertIs(self.queue._worker, worker)
        self.queue.stop()
        self.assertEqual((worker.calls, worker.alive, self.queue._worker), (2, False, None))

    def test_stop_signals_joins_and_withdraws_ready(self) -> None:  # Sol M04, M06
        self.queue.start(types.SimpleNamespace(store=self.store))
        self.assertTrue(self.queue.ready)
        worker = self.queue._worker
        self.queue.stop()
        self.assertFalse(self.queue.ready)
        self.assertFalse(worker.is_alive())

    def test_start_after_attach_recovers(self) -> None:  # DS F5, Sol M03
        approval_id = self.engine_request()
        queue = ApprovalQueue(clock=self.clock)
        queue.attach(self.store, declare_tables=False)
        self.addCleanup(queue.stop)
        queue.start(types.SimpleNamespace(store=self.store))
        with self.store.transaction() as tx:
            self.assertEqual(rows.load(tx, approval_id)["state"], "expired")

    def test_the_worker_itself_denies_at_the_deadline(self) -> None:  # Sol M02
        approval_id = self.plugin_request()
        done = threading.Event()
        self.queue.subscribe_answered(lambda record, binding: done.set())
        self.clock.now += 301
        self.start_worker()
        self.assertTrue(done.wait(10), "the deadline worker never denied a due request")
        self.assertEqual(self.queue.wait(approval_id, timeout=0)["state"], "expired")

    def test_a_request_wakes_a_sleeping_worker_before_it_tells_a_surface(self) -> None:  # Sol M01, Astra R3, DS M11
        seen: list = []
        self.queue.subscribe_requested(lambda record: seen.append(self.queue._wake.is_set()))
        self.queue._wake.clear()
        self.plugin_request()
        self.assertEqual(seen, [True])

    def test_a_failed_worker_withdraws_ready_and_tries_again(self) -> None:  # Astra R5, Sol R4, DS F6
        calls, second, again = [], threading.Event(), threading.Event()
        real_sweep, real_wait = self.queue.sweep, self.queue._wake.wait

        def sweep():
            calls.append(self.queue.ready)
            if len(calls) == 1:
                raise OSError("a store that failed once")
            second.set()
            return real_sweep()

        def wait(timeout=None):
            if second.is_set():
                again.set()
            return real_wait(0 if timeout is None and not second.is_set() else timeout)

        self.queue.ready = True
        self.queue.sweep, self.queue._wake.wait = sweep, wait
        self.start_worker()
        self.assertTrue(again.wait(10), "the worker did not come back after a failure")
        self.assertEqual(calls[:2], [True, False])  # ready withdrawn after the failure
        self.assertTrue(self.queue.ready)  # and given back once a sweep succeeded

    def test_no_sweep_after_closing(self) -> None:  # DS M12
        swept = []
        self.queue.sweep = lambda: swept.append(1) or []
        self.queue._closing.set()
        self.queue._wake.set()
        self.queue._run()
        self.assertEqual(swept, [])

    def test_an_unreadable_due_row_is_set_aside(self) -> None:  # DS F7, Astra R6
        approval_id = self.plugin_request()
        with self.store.transaction() as tx:
            tx.execute("UPDATE approvals_records SET record = ? WHERE approval_id = ?", ("{broken", approval_id))
        self.clock.now += 301
        with self.assertLogs("recursion_core.approvals.queue", level="ERROR"):
            self.assertEqual(self.queue.sweep(), [])
        with self.store.transaction() as tx:
            self.assertIsNone(rows.next_deadline(tx))
            self.assertEqual(rows.due(tx, 10**15), [])


@needs_fakes
class DeliveryTests(_Base):
    def test_a_request_no_subscriber_took_is_offered_again_after_restart(self) -> None:  # Astra R2, Sol R2
        approval_id = self.plugin_request()
        queue = self.restarted()
        drawn: list = []
        queue.subscribe_requested(lambda record: drawn.append(record["approval_id"]))
        queue.recover()
        self.assertEqual(drawn, [approval_id])
        queue.recover()
        self.assertEqual(drawn, [approval_id])  # taken now: not offered a second time

    def test_a_request_whose_subscriber_failed_is_offered_again(self) -> None:  # Astra R2, DS F3
        self.queue.subscribe_requested(lambda record: 1 / 0)
        with self.assertLogs("recursion_core.approvals.queue", level="ERROR"):
            approval_id = self.plugin_request()
        queue = self.restarted()
        drawn: list = []
        queue.subscribe_requested(lambda record: drawn.append(record["approval_id"]))
        queue.recover()
        self.assertEqual(drawn, [approval_id])

    def test_a_subscriber_failure_is_logged_by_type_only(self) -> None:  # DS M15, Sol R7
        def leaky(record, binding):
            raise RuntimeError("leaked " + binding["engine_request_id"])

        self.queue.subscribe_answered(leaky)
        approval_id = self.engine_request("toolu_private_request")
        with self.assertLogs("recursion_core.approvals.queue", level="ERROR") as logged:
            self.approve(approval_id)
        text = "\n".join(logged.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("toolu_private_request", text)

    def test_a_marker_that_fails_after_the_commit_does_not_fail_the_request(self) -> None:  # Astra R3, DS F4
        self.queue.subscribe_requested(lambda record: setattr(self.store, "fail_commit", True))
        try:
            with self.assertLogs("recursion_core.approvals.queue", level="ERROR"):
                approval_id = self.plugin_request()
        finally:
            self.store.fail_commit = False
        with self.store.transaction() as tx:
            self.assertIsNotNone(rows.load(tx, approval_id))

    def test_subscribers_are_given_copies(self) -> None:  # Astra M07, M08
        seen: list = []
        self.queue.subscribe_requested(lambda record: record["effect"]["input"].clear())
        self.queue.subscribe_requested(lambda record: seen.append(copy.deepcopy(record["effect"]["input"])))
        self.queue.subscribe_answered(lambda record, binding: binding.clear())
        bindings: list = []
        self.queue.subscribe_answered(lambda record, binding: bindings.append(dict(binding)))
        approval_id = self.engine_request()
        self.approve(approval_id)
        self.assertEqual(seen, [self.engine["input"]])
        self.assertEqual(bindings[0]["engine_request_id"], "toolu_1")
        with self.store.transaction() as tx:
            self.assertEqual(rows.binding_of(tx, approval_id)["engine_request_id"], "toolu_1")

    def test_subscribing_while_another_unsubscribes_keeps_the_new_one(self) -> None:  # Sol M20
        snapshot, release = threading.Event(), threading.Event()
        drawn: list = []

        class PausedList(list):
            def __iter__(self):
                held = list(super().__iter__())
                snapshot.set()
                if not release.wait(10):
                    raise RuntimeError("never released")
                return iter(held)

        self.queue._requested = PausedList()
        unsubscribe = self.queue.subscribe_requested(lambda record: None)
        remover = threading.Thread(target=unsubscribe)
        remover.start()
        self.assertTrue(snapshot.wait(10))
        adder = threading.Thread(target=lambda: self.queue.subscribe_requested(
            lambda record: drawn.append(record["approval_id"])))
        adder.start()
        release.set()
        remover.join(10)
        adder.join(10)
        approval_id = self.plugin_request()
        self.assertIn(approval_id, drawn)


@needs_fakes
class WaitAndRestartTests(_Base):
    def test_waiting_for_a_request_made_before_a_restart_blocks_until_it_expires(self) -> None:  # Astra R4, Sol R3
        approval_id = self.plugin_request()
        queue = self.restarted()
        queue.recover()
        answer, returned = [], threading.Event()
        waiter = threading.Thread(target=lambda: (answer.append(queue.wait(approval_id)), returned.set()))
        waiter.start()
        self.assertFalse(returned.wait(0.3), "wait returned while the request was pending")
        self.clock.now += 301
        queue.sweep()
        waiter.join(10)
        self.assertEqual(answer[0]["state"], "expired")

    def test_an_approved_engine_request_cannot_be_carried_out_after_a_restart(self) -> None:  # Sol R1
        approval_id = self.engine_request()
        approved = self.approve(approval_id)
        with self.store.transaction() as tx:
            old_binding = rows.binding_of(tx, approval_id)
        queue = self.restarted()
        self.assertEqual(queue.recover()["retired"], [approval_id])
        with self.assertRaises(ApprovalRefused) as refused:
            queue.consume(approval_id, approved["revision"], approved["digest"], old_binding)
        self.assertEqual(refused.exception.code, "binding_mismatch")

    def test_recovery_expires_a_due_plugin_request(self) -> None:  # Sol M05
        approval_id = self.plugin_request()
        self.clock.now += 301
        queue = self.restarted()
        self.assertEqual(queue.recover()["expired"], [approval_id])

    def test_a_row_that_is_not_json_is_record_invalid_and_left_out_of_the_list(self) -> None:  # Astra R6
        approval_id = self.plugin_request()
        other = self.plugin_request(payload={"n": 2})
        with self.store.transaction() as tx:
            tx.execute("UPDATE approvals_records SET record = ? WHERE approval_id = ?", ("{incomplete", approval_id))
            with self.assertRaises(ApprovalRefused) as refused:
                rows.load(tx, approval_id)
        self.assertEqual(refused.exception.code, "record_invalid")
        with self.assertLogs("recursion_core.approvals.store", level="ERROR"):
            listed = [s["approval_id"] for s in self.queue.list_page({})["approvals"]]
        self.assertEqual(listed, [other])

    def test_the_answer_event_is_the_cores_and_trusted(self) -> None:  # Sol M07
        self.plugin_request()
        self.clock.now += 301
        self.queue.sweep()
        (answered,) = [e for e in self.store.events if e["type"] == "approval.answered"]
        self.assertEqual((answered["source"], answered["trust"]), ("core", "trusted"))

    def test_consumption_checks_the_record_agrees_with_itself(self) -> None:  # Sol M17
        approval_id = self.engine_request()
        approved = self.approve(approval_id)
        with self.store.transaction() as tx:
            binding = rows.binding_of(tx, approval_id)
            held = rows.load(tx, approval_id)
            held["effect"]["input"]["command"] = "a changed command"
            rows.save(tx, held)
        with self.assertRaises(ApprovalRefused) as refused:
            self.queue.consume(approval_id, approved["revision"], approved["digest"], binding)
        self.assertEqual(refused.exception.code, "record_invalid")


class _Race:
    """Two callers released together; each result kept, exceptions too."""

    def __init__(self, *calls) -> None:
        self.barrier = threading.Barrier(len(calls))
        self.results = [None] * len(calls)
        self.threads = [threading.Thread(target=self._run, args=(i, fn)) for i, fn in enumerate(calls)]

    def _run(self, index, fn) -> None:
        self.barrier.wait(10)
        try:
            self.results[index] = fn()
        except BaseException as exc:  # noqa: BLE001 - kept for the assertion
            self.results[index] = exc

    def run(self) -> list:
        for thread in self.threads:
            thread.start()
        for thread in self.threads:
            thread.join(30)
        return self.results


@unittest.skipUnless(Hooks is not None, "SPINE's seam files (#369) are not in this tree")
class _ConcurrentBase(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.hooks = Hooks()
        install(self.hooks, self.queue)
        self.hooks.freeze()
        self.hooks.methods.store = self.store

    def dispatch(self, method: str, params: dict, principal) -> dict:
        return self.hooks.methods.dispatch(method, params, principal, lambda m, p: None)

    def _answers_at_once(self, rounds: int) -> list:
        outcomes = []
        for number in range(rounds):
            approval_id = self.plugin_request(payload={"round": number})
            owner, phone = Principal("owner", "local"), Principal("device", "phone-1")
            shown = self.dispatch("approval.open", {"command_id": "op-o-%05d" % number, "approval_id": approval_id,
                                                    "surface": "cli"}, owner)["approval"]
            self.dispatch("approval.open", {"command_id": "op-p-%05d" % number, "approval_id": approval_id,
                                            "surface": "phone"}, phone)
            params = {"approval_id": approval_id, "expected_revision": shown["revision"], "digest": shown["digest"]}
            first = dict(params, command_id="an-o-%05d" % number, surface="cli", answer="approve")
            second = dict(params, command_id="an-p-%05d" % number, surface="phone", answer="deny")
            results = _Race(lambda: self.dispatch("approval.answer", first, owner),
                            lambda: self.dispatch("approval.answer", second, phone)).run()
            retried = [self.dispatch("approval.answer", p, who) if r["code"] == "unavailable" else r
                       for r, p, who in zip(results, (first, second), (owner, phone))]
            outcomes.append((approval_id, results, retried))
        return outcomes


@needs_fakes
class SerialStoreRaceTests(_ConcurrentBase):
    serial = True

    def test_two_answers_at_once_one_wins_the_other_is_already_resolved(self) -> None:  # DS F8
        for approval_id, results, _ in self._answers_at_once(20):
            codes = sorted((r["code"], r.get("detail")) for r in results)
            self.assertEqual(codes, [("conflict", "already_resolved"), ("ok", None)], approval_id)

    def test_two_consumers_at_once_one_carries_it_out_the_other_is_refused_typed(self) -> None:  # DS F8
        for number in range(10):
            approval_id = self.engine_request("toolu_%d" % number)
            approved = self.approve(approval_id)
            with self.store.transaction() as tx:
                binding = rows.binding_of(tx, approval_id)
            take = lambda: self.queue.consume(approval_id, approved["revision"], approved["digest"], binding)
            results = _Race(take, take).run()
            refused = [r for r in results if isinstance(r, BaseException)]
            self.assertEqual(len(refused), 1, results)
            self.assertIsInstance(refused[0], ApprovalRefused)
            self.assertEqual(refused[0].code, "already_consumed")


@needs_fakes
class ConcurrentStoreRaceTests(SerialStoreRaceTests):
    """SPINE's second kind of fake: two transactions at once, the second writer refused."""
    serial = False

    def test_two_answers_at_once_one_wins_the_other_is_already_resolved(self) -> None:  # DS F8
        for approval_id, results, retried in self._answers_at_once(20):
            codes = sorted(r["code"] for r in results)
            self.assertIn(codes, (["conflict", "ok"], ["ok", "unavailable"]), approval_id)
            final = sorted((r["code"], r.get("detail")) for r in retried)
            self.assertEqual(final, [("conflict", "already_resolved"), ("ok", None)], approval_id)
            answered = [e for e in self.store.events if e["type"] == "approval.answered"
                        and e["payload"]["approval_id"] == approval_id]
            self.assertEqual(len(answered), 1)


@needs_fakes
class MethodShapeTests(_ConcurrentBase):
    def test_list_without_a_store_is_unavailable_store_absent(self) -> None:  # Astra R7
        hooks = Hooks()
        install(hooks, ApprovalQueue(clock=self.clock))
        hooks.freeze()
        hooks.methods.store = self.store
        self.assertEqual(hooks.methods.dispatch("approval.list", {}, Principal("owner", "local"), lambda m, p: None),
                         {"code": "unavailable", "detail": "store_absent"})

    def test_list_parameters_are_checked(self) -> None:  # Sol R6, DS F2
        owner = Principal("owner", "local")
        cases = (("limit", {"limit": 0}), ("limit", {"limit": -1}), ("limit", {"limit": 1001}), ("limit", {"limit": True}),
                 ("limit", {"limit": "5"}), ("state", {"state": "nope"}), ("session_id", {"session_id": "ses-1\n"}),
                 ("after", {"after": "x"}), ("foo", {"foo": 1}))
        for detail, params in cases:
            with self.subTest(params=params):
                self.assertEqual(self.dispatch("approval.list", params, owner), {"code": "request_invalid", "detail": detail})
        self.assertEqual(self.dispatch("approval.list", {"limit": 1000, "state": "pending"}, owner)["code"], "ok")

    def test_open_and_answer_take_no_unknown_member_and_check_ids(self) -> None:  # Sol R6, DS F11
        owner = Principal("owner", "local")
        approval_id = self.plugin_request()
        base = {"command_id": "cmd-shape-0001", "approval_id": approval_id, "surface": "cli",
                "expected_revision": 1, "digest": "0" * 64, "answer": "approve"}
        cases = (("force", dict(base, force=True)), ("approval_id", dict(base, approval_id="not-an-id")),
                 ("digest", dict(base, digest="ABC")), ("digest", dict(base, digest="0" * 63 + "G")))
        for detail, params in cases:
            with self.subTest(detail=detail):
                self.assertEqual(self.dispatch("approval.answer", params, owner), {"code": "request_invalid", "detail": detail})
        self.assertEqual(self.dispatch("approval.open", {"command_id": "cmd-shape-0002", "approval_id": approval_id,
                                                         "surface": "cli", "extra": 1}, owner),
                         {"code": "request_invalid", "detail": "extra"})

    def test_the_session_filter_lists_only_that_log(self) -> None:  # Astra M12
        self.engine_request()
        mine = self.plugin_request()  # no session: its log is run:<run_id>
        listed = self.dispatch("approval.list", {"session_id": "run:" + self.plugin["run_id"]},
                               Principal("owner", "local"))["approvals"]
        self.assertEqual([s["approval_id"] for s in listed], [mine])

    def test_drivers_reach_the_registered_queue_and_ask(self) -> None:  # Sol R8, DS F9
        self.assertIs(queue_of(self.hooks), self.queue)
        approval_id = self.queue.ask(self.engine, {"engine_request_id": "toolu_ask"}, turn_id="turn-0002")
        with self.store.transaction() as tx:
            self.assertEqual(rows.binding_of(tx, approval_id)["engine_request_id"], "toolu_ask")
        for binding in ({}, {"engine_request_id": "x", "run_id": "other"}, "toolu"):
            with self.subTest(binding=binding):
                with self.assertRaises(ApprovalRefused) as refused:
                    self.queue.ask(self.engine, binding)
                self.assertEqual(refused.exception.code, "binding_mismatch")


@needs_fakes
class AccessorReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = _vector_effect("g01-a-tool-call.json")
        self.plugin = _vector_effect("g03-a-plugin-effect.json")

    def test_a_plugin_effect_outside_its_shape_or_too_large_is_refused(self) -> None:  # DS F11
        for code, effect in (("effect", dict(self.plugin, amount_minor="1")),
                             ("effect", dict(self.plugin, currency="usd")),
                             ("too_large", dict(self.plugin, payload={"k%03d" % i: "v" * 3000 for i in range(90)}))):
            with self.subTest(code=code):
                with self.assertRaises(EffectRefused) as refused:
                    effects.bind("plugin_effect", effect)
                self.assertEqual(refused.exception.code, code)

    def test_private_use_text_is_escaped_before_it_is_drawn(self) -> None:  # Sol M10
        bound = effects.bind("engine_tool", dict(self.engine, input={"note": "private-\ue000-end"}))
        drawn = "\n".join(line["text"] for line in bound.rendered)
        self.assertNotIn("\ue000", drawn)
        self.assertIn("\\ue000", drawn)


if __name__ == "__main__":
    unittest.main()
