"""Stop semantics through the control API (PRODUCT.md 055cc7a5, CSLR-D-011: S6, 11.3, 11.8)."""

from __future__ import annotations

import hashlib
import json
import threading
import unittest

from recursion_core.control import api as a
from recursion_core.control import mcp_tools
from recursion_core.control.registry import EMPTY, POPULATED, TIMEOUT, UNKNOWN, InMemoryRunRegistry
from recursion_core.control.tests.fakes import FakeKillTree, make_api, started


class StopTest(unittest.TestCase):
    def test_confirmed_empty_cgroup_is_stopped(self) -> None:
        api, registry, kill, _ = make_api(FakeKillTree([EMPTY]))
        run = started(api)
        res = api.call("run.stop", {"run_id": run})
        self.assertEqual((res["code"], res["state"]), (a.STOPPED, "stopped"))
        self.assertEqual(kill.requests, ["/sys/fs/cgroup/fake/run-1"])
        self.assertEqual(registry.get(run)["state"], "stopped")

    def test_unconfirmed_is_pending_never_stopped(self) -> None:
        cases = [(POPULATED, "cgroup_populated"), (TIMEOUT, "confirm_timeout"), (UNKNOWN, "confirm_unknown"),
                 ("something-new", "confirm_unknown"), (OSError("pidfd"), "cleanup_error:OSError")]
        for answer, reason in cases:
            api, registry, _, _ = make_api(FakeKillTree([answer]))
            run = started(api)
            res = api.call("run.stop", {"run_id": run})
            self.assertEqual((res["code"], res["state"], res["reason"]), (a.PENDING, "pending", reason))
            self.assertNotIn(registry.get(run)["state"], ("stopped", "terminated"))
            self.assertNotIn("stopped", json.dumps(res))
            self.assertNotIn("terminated", json.dumps(res))

    def test_request_failure_is_pending(self) -> None:
        api, _, kill, _ = make_api(FakeKillTree([EMPTY], request_error=PermissionError()))
        res = api.call("run.stop", {"run_id": started(api)})
        self.assertEqual((res["code"], res["reason"]), (a.PENDING, "cleanup_error:PermissionError"))
        self.assertEqual(kill.confirms, [])

    def test_failed_cleanup_request_is_retried_on_the_next_stop(self) -> None:
        # Devin r4503746546: a request that never went out must not leave the run stranded as pending.
        kill = FakeKillTree([EMPTY], request_error=PermissionError())
        api, registry, _, _ = make_api(kill)
        run = started(api)
        self.assertEqual(api.call("run.stop", {"run_id": run})["reason"], "cleanup_error:PermissionError")
        self.assertIs(registry.get(run)["stop"]["requested"], False)
        kill.request_error = None  # the transient error clears
        res = api.call("run.stop", {"run_id": run})
        self.assertEqual(res["code"], a.STOPPED)
        self.assertEqual(len(kill.requests), 2)  # the failed attempt, then the retry
        self.assertEqual(len(kill.confirms), 1)
        self.assertIs(registry.get(run)["stop"]["requested"], True)

    def test_issued_cleanup_request_is_not_resent_while_pending(self) -> None:
        kill = FakeKillTree([POPULATED, EMPTY])
        api, registry, _, _ = make_api(kill)
        run = started(api)
        self.assertEqual(api.call("run.stop", {"run_id": run})["code"], a.PENDING)
        self.assertIs(registry.get(run)["stop"]["requested"], True)
        self.assertEqual(api.call("run.stop", {"run_id": run})["code"], a.STOPPED)
        self.assertEqual(len(kill.requests), 1)

    def test_repeat_stop_on_stopped_run_sends_nothing(self) -> None:
        api, _, kill, _ = make_api(FakeKillTree([EMPTY]))
        run = started(api)
        api.call("run.stop", {"run_id": run})
        for _ in range(3):
            res = api.call("run.stop", {"run_id": run})
            self.assertEqual((res["code"], res["repeat"]), (a.STOPPED, True))
        self.assertEqual(len(kill.requests), 1)
        self.assertEqual(len(kill.confirms), 1)

    def test_pending_stop_rechecks_without_second_kill(self) -> None:
        api, registry, kill, _ = make_api(FakeKillTree([POPULATED, TIMEOUT, EMPTY]))
        run = started(api)
        self.assertEqual(api.call("run.stop", {"run_id": run})["reason"], "cgroup_populated")
        self.assertEqual(api.call("run.stop", {"run_id": run})["reason"], "confirm_timeout")
        res = api.call("run.stop", {"run_id": run})
        self.assertEqual(res["code"], a.STOPPED)
        self.assertEqual(len(kill.requests), 1)
        self.assertEqual(len(kill.confirms), 3)
        self.assertEqual(registry.get(run)["stop"]["checks"], 3)

    def test_unknown_run_is_typed_error(self) -> None:
        api, _, kill, _ = make_api()
        self.assertEqual(api.call("run.stop", {"run_id": "nope"})["code"], a.RUN_UNKNOWN)
        self.assertEqual(api.call("run.stop", {})["code"], a.REQUEST_INVALID)
        self.assertEqual(api.call("run.kill", {"run_id": "x"})["code"], a.METHOD_UNKNOWN)
        self.assertEqual(kill.requests, [])

    def test_concurrent_stops_send_one_kill(self) -> None:
        api, _, kill, _ = make_api(FakeKillTree([EMPTY]))
        run = started(api)
        barrier, results = threading.Barrier(12), []

        def go() -> None:
            barrier.wait()
            results.append(api.call("run.stop", {"run_id": run})["code"])

        threads = [threading.Thread(target=go) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [a.STOPPED] * 12)
        self.assertEqual(len(kill.requests), 1)

    def test_hung_confirm_on_one_run_does_not_queue_another_runs_stop(self) -> None:
        # Red-team r1 finding 2 (11.3, D2-55 / B-19): Stop is never queued behind another run's Stop.
        entered, release = threading.Event(), threading.Event()

        class BlockingKillTree(FakeKillTree):
            def confirm(self, cgroup: str, timeout_s: float) -> str:
                if cgroup.endswith("/run-1"):
                    entered.set()
                    release.wait(10)
                return super().confirm(cgroup, timeout_s)

        kill = BlockingKillTree([EMPTY])
        api, _, _, _ = make_api(kill)
        run_a, run_b = started(api), started(api)
        slow: list[str] = []
        t = threading.Thread(target=lambda: slow.append(api.call("run.stop", {"run_id": run_a})["code"]))
        t.start()
        try:
            self.assertTrue(entered.wait(5))
            done: list[str] = []
            u = threading.Thread(target=lambda: done.append(api.call("run.stop", {"run_id": run_b})["code"]))
            u.start()
            u.join(2)
            self.assertEqual(done, [a.STOPPED])  # run B stopped while run A's confirm is still blocked
            self.assertEqual(slow, [])
        finally:
            release.set()
            t.join()
        self.assertEqual(slow, [a.STOPPED])
        self.assertEqual(sorted(kill.requests), ["/sys/fs/cgroup/fake/run-1", "/sys/fs/cgroup/fake/run-2"])

    def test_stop_during_launch_is_kept_and_acted_on(self) -> None:
        # Red-team r3 finding 2: a Stop that lands while the starter runs answers pending, and the start
        # carries it out once the launch is recorded.
        in_starter, release = threading.Event(), threading.Event()

        def slow(params: dict) -> dict:
            in_starter.set()
            release.wait(10)
            return {"cgroup": f"/cg/{params['run_id']}"}

        api, registry, kill, _ = make_api(FakeKillTree([EMPTY]), starter=slow)
        out: list[dict] = []
        t = threading.Thread(target=lambda: out.append(
            api.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p"})))
        t.start()
        try:
            self.assertTrue(in_starter.wait(5))
            stop = api.call("run.stop", {"run_id": "run-1"})
            self.assertEqual((stop["code"], stop["reason"]), (a.PENDING, "cgroup_unknown"))
            self.assertIsNotNone(registry.get("run-1")["stop"])  # the stop intent is recorded
            self.assertEqual(kill.requests, [])
        finally:
            release.set()
            t.join()
        self.assertEqual((out[0]["code"], out[0]["stop"]["code"]), (a.STARTED, a.STOPPED))
        self.assertEqual(kill.requests, ["/cg/run-1"])
        rec = registry.get("run-1")
        self.assertEqual((rec["state"], rec["cgroup"]), ("stopped", "/cg/run-1"))
        self.assertEqual(api.call("run.stop", {"run_id": "run-1"})["repeat"], True)
        self.assertEqual(len(kill.requests), 1)

    def test_stop_on_refused_start_blocks_same_key_relaunch(self) -> None:
        def refuse(params: dict) -> dict:
            raise RuntimeError("refused")

        api, registry, kill, _ = make_api(starter=refuse)
        p = {"tenant": "t1", "engine": "codex", "prompt": "p", "client_key": "k"}
        api.call("run.start", p)
        self.assertEqual(api.call("run.stop", {"run_id": "run-1"})["reason"], "cgroup_unknown")
        again = api.call("run.start", p)
        self.assertEqual((again["code"], again["detail"]), (a.START_FAILED, "stop_requested"))
        self.assertEqual(kill.requests, [])

    def _plant(self, stop, *, state: str = "running", cgroup: str | None = "/cg/r"):
        registry = InMemoryRunRegistry([{
            "run_id": "r", "tenant": "t1", "engine": "codex", "state": state,
            "cgroup": cgroup, "started_at": 1.0, "stop": stop, "start_key": None,
        }])
        return registry

    def test_missing_requested_means_the_cleanup_was_not_sent(self) -> None:
        # .get("requested", True) treated a missing field as already sent, so retries never signalled.
        kill = FakeKillTree([POPULATED, EMPTY])
        registry = self._plant({"requested_at": 1.0, "checks": 0, "reason": None})
        api, _, _, _ = make_api(kill, registry)
        first = api.call("run.stop", {"run_id": "r"})
        self.assertEqual((first["code"], first["reason"]), (a.PENDING, "cgroup_populated"))
        self.assertEqual(kill.requests, ["/cg/r"])
        self.assertIs(registry.get("r")["stop"]["requested"], True)
        second = api.call("run.stop", {"run_id": "r"})
        self.assertEqual(second["code"], a.STOPPED)
        self.assertEqual(kill.requests, ["/cg/r"])

    def test_non_bool_requested_still_signals(self) -> None:
        # Truthiness treated a stored "false" / "False" / "0" / 1.0 / "true" as already sent.
        # An empty cgroup then answered stopped and request_stop was never called.
        for requested in ("false", "False", "0", 1.0, "true"):
            with self.subTest(requested=requested):
                kill = FakeKillTree([EMPTY])
                registry = self._plant({
                    "requested": requested, "requested_at": 1.0, "checks": 0, "reason": None,
                })
                api, _, _, _ = make_api(kill, registry)
                res = api.call("run.stop", {"run_id": "r"})
                self.assertEqual(kill.requests, ["/cg/r"])
                self.assertEqual((res["code"], res["state"]), (a.STOPPED, "stopped"))
                self.assertIs(registry.get("r")["stop"]["requested"], True)

    def test_non_bool_requested_is_stored_as_false_when_the_signal_fails(self) -> None:
        # A failed send must not leave the registry string in place, or the next stop looks sent.
        for requested in ("false", "False", "0", 1.0, "true"):
            with self.subTest(requested=requested):
                kill = FakeKillTree([EMPTY], request_error=PermissionError("denied"))
                registry = self._plant({
                    "requested": requested, "requested_at": 1.0, "checks": 0, "reason": None,
                })
                api, _, _, _ = make_api(kill, registry)
                res = api.call("run.stop", {"run_id": "r"})
                self.assertEqual(kill.requests, ["/cg/r"])
                self.assertEqual((res["code"], res["reason"]), (a.PENDING, "cleanup_error:PermissionError"))
                self.assertIs(registry.get("r")["stop"]["requested"], False)
                kill.request_error = None
                again = api.call("run.stop", {"run_id": "r"})
                self.assertEqual(again["code"], a.STOPPED)
                self.assertEqual(kill.requests, ["/cg/r", "/cg/r"])
                self.assertIs(registry.get("r")["stop"]["requested"], True)

    def test_non_dict_stop_is_a_fresh_intent_and_stays_inside_call(self) -> None:
        kill = FakeKillTree([EMPTY])
        registry = self._plant(1)
        api, _, _, _ = make_api(kill, registry)
        res = api.call("run.stop", {"run_id": "r"})
        self.assertEqual(res["code"], a.STOPPED)
        self.assertEqual(kill.requests, ["/cg/r"])
        self.assertIs(registry.get("r")["stop"]["requested"], True)
        self.assertIsInstance(registry.get("r")["stop"], dict)

    def test_stop_missing_checks_still_returns_pending_after_the_signal(self) -> None:
        kill = FakeKillTree([POPULATED])
        registry = self._plant({"requested": False, "reason": None})
        api, _, _, _ = make_api(kill, registry)
        res = api.call("run.stop", {"run_id": "r"})
        self.assertEqual((res["code"], res["state"], res["reason"]), (a.PENDING, "pending", "cgroup_populated"))
        self.assertEqual(kill.requests, ["/cg/r"])
        self.assertEqual(registry.get("r")["stop"]["checks"], 1)
        self.assertEqual(registry.get("r")["state"], "stop_pending")

    def test_non_dict_stop_before_launch_does_not_escape(self) -> None:
        kill = FakeKillTree([EMPTY])
        registry = self._plant(1, state="starting", cgroup=None)
        api, _, _, _ = make_api(kill, registry)
        res = api.call("run.stop", {"run_id": "r"})
        self.assertEqual((res["code"], res["reason"]), (a.PENDING, "cgroup_unknown"))
        self.assertEqual(kill.requests, [])
        self.assertIsInstance(registry.get("r")["stop"], dict)
        self.assertEqual(registry.get("r")["stop"]["checks"], 1)


class ReadsAndStartTest(unittest.TestCase):
    def test_reads_have_no_side_effects(self) -> None:
        api, registry, kill, starter = make_api(FakeKillTree([POPULATED]))
        run = started(api)
        api.call("run.stop", {"run_id": run})
        before = json.dumps(registry.list(), sort_keys=True)
        for _ in range(5):
            status = api.call("run.status", {"run_id": run})
            listing = api.call("run.list")
            status["run"]["state"] = "stopped"  # mutating an answer must not reach the registry
            listing["runs"].clear()
        self.assertEqual(json.dumps(registry.list(), sort_keys=True), before)
        self.assertEqual((len(kill.requests), len(kill.confirms), len(starter.calls)), (1, 1, 1))
        self.assertEqual(api.call("run.status", {"run_id": "nope"})["code"], a.RUN_UNKNOWN)

    def test_start_registers_and_dedupes_client_key(self) -> None:
        api, registry, _, starter = make_api()
        p = {"tenant": "t1", "engine": "codex", "prompt": "p", "client_key": "k1"}
        first, second = api.call("run.start", p), api.call("run.start", p)
        self.assertEqual((first["code"], first["duplicate"]), (a.STARTED, False))
        self.assertEqual((second["code"], second["duplicate"], second["run"]["run_id"]), (a.STARTED, True, "run-1"))
        self.assertEqual(len(starter.calls), 1)
        self.assertEqual(registry.get("run-1")["state"], "running")
        self.assertEqual(api.call("run.start", {"tenant": "t1"})["code"], a.REQUEST_INVALID)

    def test_client_key_dedupe_survives_a_restart(self) -> None:
        # Red-team r1 finding 1 (11.3, B-17): a start re-issued after a daemon restart runs once.
        api, registry, kill, starter = make_api()
        p = {"tenant": "t1", "engine": "codex", "prompt": "p", "client_key": "k1"}
        first = api.call("run.start", p)
        self.assertEqual(registry.get("run-1")["start_key"]["client_key"], "k1")
        self.assertEqual(registry.get("run-1")["start_key"]["prompt_sha256"], hashlib.sha256(b"p").hexdigest())
        restarted = a.ControlAPI(registry, kill, starter, clock=lambda: 2000.0)  # fresh API, same registry
        again = restarted.call("run.start", p)
        self.assertEqual((again["code"], again["duplicate"]), (a.STARTED, True))
        self.assertEqual(again["run"]["run_id"], first["run"]["run_id"])
        self.assertEqual(restarted.call("run.start", {**p, "prompt": "other"}),
                         {"code": a.REQUEST_INVALID, "detail": "client_key_reused"})
        self.assertEqual(len(starter.calls), 1)
        self.assertEqual(len(registry.list()), 1)

    def test_client_key_is_scoped_to_the_tenant(self) -> None:
        # Devin r4503746563: another tenant reusing a key gets its own run.
        api, registry, _, starter = make_api()
        a_run = api.call("run.start", {"tenant": "A", "engine": "codex", "prompt": "p", "client_key": "daily"})
        b_run = api.call("run.start", {"tenant": "B", "engine": "codex", "prompt": "p", "client_key": "daily"})
        self.assertEqual((a_run["duplicate"], b_run["duplicate"]), (False, False))
        self.assertNotEqual(a_run["run"]["run_id"], b_run["run"]["run_id"])
        self.assertEqual(b_run["run"]["tenant"], "B")
        self.assertEqual(len(starter.calls), 2)

    def test_reused_client_key_with_different_request_is_refused(self) -> None:
        api, registry, _, starter = make_api()
        api.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p", "client_key": "k"})
        for changed in ({"engine": "claude_code"}, {"prompt": "other"}):
            with self.subTest(changed=changed):
                res = api.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p",
                                             "client_key": "k", **changed})
                self.assertEqual(res, {"code": a.REQUEST_INVALID, "detail": "client_key_reused"})
        self.assertEqual(len(starter.calls), 1)
        self.assertEqual(len(registry.list()), 1)

    def test_start_failure_is_typed(self) -> None:
        def refuse(params: dict) -> dict:
            raise RuntimeError("refused")

        registry = InMemoryRunRegistry()
        api = a.ControlAPI(registry, FakeKillTree(), refuse, clock=lambda: 0.0)
        res = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})
        self.assertEqual((res["code"], res["detail"], res["recorded"]), (a.START_FAILED, "RuntimeError", True))
        self.assertEqual([r["state"] for r in registry.list()], ["start_failed"])  # the attempt stays visible

    def test_same_key_retry_after_a_refused_start_relaunches_under_the_same_run_id(self) -> None:
        calls: list[dict] = []

        def flaky(params: dict) -> dict:
            calls.append(params)
            if len(calls) == 1:
                raise RuntimeError("refused")
            return {"cgroup": f"/cg/{params['run_id']}"}

        api, registry, _, _ = make_api(starter=flaky)
        p = {"tenant": "t1", "engine": "codex", "prompt": "p", "client_key": "k"}
        self.assertEqual(api.call("run.start", p)["code"], a.START_FAILED)
        again = api.call("run.start", p)
        self.assertEqual((again["code"], again["duplicate"], again["run"]["run_id"]), (a.STARTED, False, "run-1"))
        self.assertEqual([c["run_id"] for c in calls], ["run-1", "run-1"])
        self.assertEqual([(r["run_id"], r["state"]) for r in registry.list()], [("run-1", "running")])


class FailingRegistry(InMemoryRunRegistry):
    """Raises ``OSError`` from ``put`` once ``fail_puts`` is set, optionally only for one state."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_puts: str | bool = False
        self.fail_reads = False

    def put(self, record: dict) -> None:
        if self.fail_puts is True or self.fail_puts == record["state"]:
            raise OSError("disk")
        super().put(record)

    def get(self, run_id: str):
        if self.fail_reads:
            raise OSError("disk")
        return super().get(run_id)

    def list(self):
        if self.fail_reads:
            raise OSError("disk")
        return super().list()


class RegistryFailureTest(unittest.TestCase):
    # Red-team r2 finding 1 (S6, 11.3, D3): a registry or handle failure never strands a live run and
    # never escapes the API as a raw exception.
    P = {"tenant": "t1", "engine": "codex", "prompt": "p", "client_key": "k1"}

    def test_failed_write_after_launch_leaves_a_listable_stoppable_deduped_run(self) -> None:
        # Red-team r3 finding 1: the starter's cgroup is kept, so a later Stop reaches request_stop.
        registry = FailingRegistry()
        api, _, kill, starter = make_api(FakeKillTree([EMPTY]), registry=registry)
        registry.fail_puts = "running"  # the launch intent lands; the write after launch fails
        res = api.call("run.start", self.P)
        self.assertEqual((res["code"], res["run_id"], res["detail"]),
                         (a.START_UNCONFIRMED, "run-1", "registry_unavailable"))
        self.assertEqual(len(starter.calls), 1)
        listed = api.call("run.list")["runs"]
        self.assertEqual([(r["run_id"], r["state"]) for r in listed], [("run-1", "starting")])
        retry = api.call("run.start", self.P)  # same key, registry still failing: repeats unconfirmed
        self.assertEqual((retry["code"], retry["duplicate"], retry["run"]["run_id"]),
                         (a.START_UNCONFIRMED, True, "run-1"))
        self.assertEqual(len(starter.calls), 1)
        registry.fail_puts = False  # the registry recovers
        stop = api.call("run.stop", {"run_id": "run-1"})
        self.assertEqual(stop["code"], a.STOPPED)
        self.assertEqual(kill.requests, ["/sys/fs/cgroup/fake/run-1"])
        rec = registry.get("run-1")
        self.assertEqual((rec["state"], rec["cgroup"]), ("stopped", "/sys/fs/cgroup/fake/run-1"))

    def test_stop_aims_at_unrecorded_launch_even_while_the_registry_is_down(self) -> None:
        registry = FailingRegistry()
        api, _, kill, _ = make_api(FakeKillTree([EMPTY]), registry=registry)
        registry.fail_puts = "running"
        api.call("run.start", self.P)
        for _ in range(2):  # the stopped write-back fails too: answer stopped, unrecorded, and try the write again
            registry.fail_puts = True
            stop = api.call("run.stop", {"run_id": "run-1"})
            self.assertEqual((stop["code"], stop["recorded"]), (a.STOPPED, False))
        # The confirmed Stop is kept with the launch (t1430u X1c), so the repeat sends no second kill.
        self.assertEqual(len(kill.requests), 1)
        registry.fail_puts = False
        self.assertEqual(api.call("run.stop", {"run_id": "run-1"}).get("recorded", True), True)
        self.assertEqual(registry.get("run-1")["state"], "stopped")

    def test_same_key_retry_records_the_launch_once_the_registry_recovers(self) -> None:
        registry = FailingRegistry()
        api, _, kill, starter = make_api(registry=registry)
        registry.fail_puts = "running"
        api.call("run.start", self.P)
        registry.fail_puts = False
        retry = api.call("run.start", self.P)
        self.assertEqual((retry["code"], retry["duplicate"]), (a.STARTED, True))
        self.assertEqual(retry["run"]["cgroup"], "/sys/fs/cgroup/fake/run-1")
        self.assertEqual(registry.get("run-1")["state"], "running")
        self.assertEqual((len(starter.calls), kill.requests), (1, []))

    def test_starter_handle_without_cgroup_is_typed_and_the_run_stays_visible(self) -> None:
        api, registry, kill, starter = make_api(starter=lambda params: {"run_id": params["run_id"]})
        res = api.call("run.start", self.P)
        self.assertEqual((res["code"], res["detail"]), (a.START_UNCONFIRMED, "starter_handle_invalid"))
        self.assertEqual(registry.get("run-1")["state"], "starting")
        # Red-team r3 finding 3: the retry repeats the unconfirmed answer, never "started".
        retry = api.call("run.start", self.P)
        self.assertEqual((retry["code"], retry["duplicate"], retry["detail"]),
                         (a.START_UNCONFIRMED, True, "launch_unconfirmed"))
        self.assertTrue(mcp_tools.call_tool(api, "recursion_run_start", self.P)["isError"])
        self.assertEqual(api.call("run.stop", {"run_id": "run-1"})["code"], a.PENDING)
        self.assertEqual(kill.requests, [])

    def test_retry_after_unrecorded_start_failure_relaunches(self) -> None:
        # Red-team r3 finding 3: the starter raised and the start_failed write failed, so the record still
        # says starting. The retry must not answer started; it relaunches under the same run id.
        calls: list[dict] = []

        def flaky(params: dict) -> dict:
            calls.append(params)
            if len(calls) == 1:
                raise RuntimeError("refused")
            return {"cgroup": f"/cg/{params['run_id']}"}

        registry = FailingRegistry()
        api, _, _, _ = make_api(registry=registry, starter=flaky)
        registry.fail_puts = "start_failed"
        first = api.call("run.start", self.P)
        self.assertEqual((first["code"], first["recorded"]), (a.START_FAILED, False))
        self.assertEqual(registry.get("run-1")["state"], "starting")
        again = api.call("run.start", self.P)
        self.assertEqual((again["code"], again["duplicate"], again["run"]["run_id"]), (a.STARTED, False, "run-1"))
        self.assertEqual([c["run_id"] for c in calls], ["run-1", "run-1"])

    def test_retry_after_restart_with_unconfirmed_launch_is_not_started(self) -> None:
        registry = FailingRegistry()
        api, _, kill, starter = make_api(registry=registry)
        registry.fail_puts = "running"
        api.call("run.start", self.P)
        registry.fail_puts = False
        restarted = a.ControlAPI(registry, kill, starter, clock=lambda: 2000.0)  # in-memory cgroup is gone
        retry = restarted.call("run.start", self.P)
        self.assertEqual((retry["code"], retry["duplicate"]), (a.START_UNCONFIRMED, True))
        self.assertEqual(len(starter.calls), 1)

    def test_failed_intent_write_launches_nothing(self) -> None:
        registry = FailingRegistry()
        api, _, _, starter = make_api(registry=registry)
        registry.fail_puts = True
        res = api.call("run.start", self.P)
        self.assertEqual(res, {"code": a.REGISTRY_UNAVAILABLE, "detail": "put:OSError"})
        self.assertEqual(starter.calls, [])

    def test_failed_write_after_confirmed_stop_still_answers_stopped(self) -> None:
        registry = FailingRegistry()
        api, _, kill, _ = make_api(FakeKillTree([EMPTY]), registry=registry)
        run = started(api)
        registry.fail_puts = True
        res = mcp_tools.call_tool(api, "recursion_run_stop", {"run_id": run})["structuredContent"]
        self.assertEqual((res["code"], res["recorded"]), (a.STOPPED, False))
        registry.fail_puts = False
        again = api.call("run.stop", {"run_id": run})  # record still says running: re-sends, re-confirms
        self.assertEqual((again["code"], again.get("recorded", True)), (a.STOPPED, True))
        self.assertEqual(len(kill.requests), 2)
        self.assertEqual(registry.get(run)["state"], "stopped")

    def test_failed_write_on_pending_stop_still_answers_pending(self) -> None:
        registry = FailingRegistry()
        api, _, _, _ = make_api(FakeKillTree([POPULATED]), registry=registry)
        run = started(api)
        registry.fail_puts = True
        res = api.call("run.stop", {"run_id": run})
        self.assertEqual((res["code"], res["reason"], res["recorded"]), (a.PENDING, "cgroup_populated", False))

    def test_registry_read_failures_are_typed_on_every_method(self) -> None:
        registry = FailingRegistry()
        api, _, kill, starter = make_api(registry=registry)
        registry.fail_reads = True
        for method, params in (("run.list", {}), ("run.status", {"run_id": "x"}), ("run.stop", {"run_id": "x"})):
            with self.subTest(method=method):
                self.assertEqual(api.call(method, params)["code"], a.REGISTRY_UNAVAILABLE)
        self.assertEqual((kill.requests, starter.calls), ([], []))


    def test_stop_reaches_an_unrecorded_launch_when_the_registry_cannot_be_read(self) -> None:
        # t1430u X1c (round-3 follow-up): the write after launch failed and now reads fail too. The API
        # still holds that launch's record, so Stop reaches cleanup and says it is unrecorded (S6).
        registry = FailingRegistry()
        api, _, kill, _ = make_api(FakeKillTree([EMPTY]), registry=registry)
        registry.fail_puts = "running"
        self.assertEqual(api.call("run.start", self.P)["code"], a.START_UNCONFIRMED)
        registry.fail_puts, registry.fail_reads = True, True
        stop = api.call("run.stop", {"run_id": "run-1"})
        self.assertEqual((stop["code"], stop["recorded"]), (a.STOPPED, False))
        self.assertEqual(kill.requests, ["/sys/fs/cgroup/fake/run-1"])
        # A run this process never launched still answers registry_unavailable and sends nothing.
        self.assertEqual(api.call("run.stop", {"run_id": "run-2"})["code"], a.REGISTRY_UNAVAILABLE)
        self.assertEqual(len(kill.requests), 1)
        registry.fail_puts, registry.fail_reads = False, False
        api.call("run.stop", {"run_id": "run-1"})
        self.assertEqual(registry.get("run-1")["state"], "stopped")

    def test_same_key_retry_carries_out_an_unrecorded_stop(self) -> None:
        # Bot review 4511961481 at 77aa248c: the post-launch write failed, then Stop's cleanup request
        # raised and its stop_pending write failed. A same-key retry recorded "running" and dropped the Stop.
        registry = FailingRegistry()
        kill = FakeKillTree([EMPTY], request_error=PermissionError("denied"))
        api, _, _, starter = make_api(kill, registry=registry)
        registry.fail_puts = "running"
        api.call("run.start", self.P)
        registry.fail_puts = True
        stop = api.call("run.stop", {"run_id": "run-1"})
        self.assertEqual((stop["code"], stop["reason"], stop["recorded"]),
                         (a.PENDING, "cleanup_error:PermissionError", False))
        registry.fail_puts, kill.request_error = False, None
        retry = api.call("run.start", self.P)
        self.assertEqual((retry["code"], retry["duplicate"], retry["stop"]["code"]), (a.STARTED, True, a.STOPPED))
        self.assertEqual(kill.requests, ["/sys/fs/cgroup/fake/run-1"] * 2)  # the raised request is re-sent
        self.assertEqual(registry.get("run-1")["state"], "stopped")
        self.assertEqual(len(starter.calls), 1)

    def test_same_key_retry_records_a_confirmed_but_unrecorded_stop(self) -> None:
        registry = FailingRegistry()
        api, _, kill, _ = make_api(FakeKillTree([EMPTY]), registry=registry)
        registry.fail_puts = "running"
        api.call("run.start", self.P)
        registry.fail_puts = True
        self.assertEqual(api.call("run.stop", {"run_id": "run-1"})["recorded"], False)
        registry.fail_puts = False
        retry = api.call("run.start", self.P)
        self.assertEqual((retry["code"], retry["stop"]["code"]), (a.STARTED, a.STOPPED))
        self.assertEqual(registry.get("run-1")["state"], "stopped")  # never recorded "running" over the Stop
        self.assertEqual(len(kill.requests), 1)

    def test_failed_start_never_overwrites_a_stop_intent_it_could_not_read(self) -> None:
        # Bot review 4511961630 at 77aa248c: Stop recorded its intent during the launch, the starter raised
        # and the re-read failed; the start_failed write erased the intent, so a fresh API relaunched.
        registry = FailingRegistry()
        in_starter, release = threading.Event(), threading.Event()
        calls: list[str] = []

        def failing(params: dict) -> dict:
            calls.append(params["run_id"])
            if len(calls) == 1:
                in_starter.set()
                release.wait(10)
            raise RuntimeError("refused")

        api, _, kill, _ = make_api(registry=registry, starter=failing)
        out: list[dict] = []
        t = threading.Thread(target=lambda: out.append(api.call("run.start", self.P)))
        t.start()
        try:
            self.assertTrue(in_starter.wait(5))
            self.assertEqual(api.call("run.stop", {"run_id": "run-1"})["code"], a.PENDING)
            registry.fail_reads = True
        finally:
            release.set()
            t.join()
        self.assertEqual((out[0]["code"], out[0]["recorded"]), (a.START_FAILED, False))
        registry.fail_reads = False
        self.assertIsNotNone(registry.get("run-1")["stop"])  # the intent survived
        fresh = a.ControlAPI(registry, kill, failing, clock=lambda: 1000.0)  # a restart
        again = fresh.call("run.start", self.P)
        self.assertNotEqual(again["code"], a.STARTED)
        self.assertEqual(calls, ["run-1"])  # never relaunched after the Stop


class RegistryIndexTest(unittest.TestCase):
    def test_find_start_index_follows_puts(self) -> None:
        # Bot review 4511191935: find_start scanned every record on each keyed start; now it is indexed.
        reg = InMemoryRunRegistry([{"run_id": "r0", "tenant": "t", "start_key": {"client_key": "k0"}}])
        for i in range(1, 2001):
            reg.put({"run_id": f"r{i}", "tenant": "t", "start_key": {"client_key": f"k{i}"}})
        self.assertEqual(reg.find_start("t", "k0")["run_id"], "r0")
        self.assertEqual(reg.find_start("t", "k2000")["run_id"], "r2000")
        self.assertIsNone(reg.find_start("other", "k1"))
        reg.put({"run_id": "r1", "tenant": "t", "start_key": None})
        self.assertIsNone(reg.find_start("t", "k1"))
        reg.put({"run_id": "dup", "tenant": "t", "start_key": {"client_key": "k2"}})
        self.assertEqual(reg.find_start("t", "k2")["run_id"], "r2")  # the first run started with a key wins

class StartConcurrencyTest(unittest.TestCase):
    # t1430u X1c (round-3 follow-up): starts were serialised by one API-wide lock held across the starter,
    # so one hung launch blocked every other start, for every tenant.

    def _blocking_starter(self, block_tenant: str):
        in_starter, release, calls = threading.Event(), threading.Event(), []

        def starter(params: dict) -> dict:
            calls.append(params["run_id"])
            if params["tenant"] == block_tenant:
                in_starter.set()
                release.wait(10)
            return {"cgroup": f"/cg/{params['run_id']}"}

        return starter, in_starter, release, calls

    def test_a_hung_starter_does_not_block_another_start(self) -> None:
        starter, in_starter, release, _ = self._blocking_starter("slow")
        api, _, _, _ = make_api(starter=starter)
        t = threading.Thread(target=api.call, args=(
            "run.start", {"tenant": "slow", "engine": "codex", "prompt": "p", "client_key": "k"}))
        t.start()
        try:
            self.assertTrue(in_starter.wait(5))
            done: list[dict] = []
            other = threading.Thread(target=lambda: done.append(api.call(
                "run.start", {"tenant": "fast", "engine": "codex", "prompt": "p", "client_key": "k"})))
            other.start()
            other.join(5)
            self.assertFalse(other.is_alive(), "a start for another tenant waited on a hung starter")
            self.assertEqual(done[0]["code"], a.STARTED)
            keyless = threading.Thread(target=api.call, args=(
                "run.start", {"tenant": "fast", "engine": "codex", "prompt": "p2"}))
            keyless.start()
            keyless.join(5)
            self.assertFalse(keyless.is_alive(), "a start without a client_key waited on a hung starter")
        finally:
            release.set()
            t.join()

    def test_concurrent_same_key_starts_launch_once(self) -> None:
        starter, in_starter, release, calls = self._blocking_starter("t1")
        api, _, _, _ = make_api(starter=starter)
        p = {"tenant": "t1", "engine": "codex", "prompt": "p", "client_key": "k1"}
        out: list[dict] = []
        threads = [threading.Thread(target=lambda: out.append(api.call("run.start", p))) for _ in range(4)]
        for t in threads:
            t.start()
        self.assertTrue(in_starter.wait(5))
        release.set()
        for t in threads:
            t.join(10)
        self.assertEqual(calls, ["run-1"])
        self.assertEqual(sorted(r["duplicate"] for r in out), [False, True, True, True])
        self.assertEqual({r["run"]["run_id"] for r in out}, {"run-1"})


if __name__ == "__main__":
    unittest.main()
