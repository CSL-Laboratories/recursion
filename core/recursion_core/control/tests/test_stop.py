"""Stop semantics through the control API (PRODUCT.md 398cd812 S6, 11.3, 11.8)."""

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
        registry = FailingRegistry()
        api, _, kill, starter = make_api(FakeKillTree([EMPTY]), registry=registry)
        registry.fail_puts = "running"  # the launch intent lands; the write after launch fails
        res = api.call("run.start", self.P)
        self.assertEqual((res["code"], res["run_id"], res["detail"]),
                         (a.START_UNCONFIRMED, "run-1", "registry_unavailable"))
        self.assertEqual(len(starter.calls), 1)
        listed = api.call("run.list")["runs"]
        self.assertEqual([(r["run_id"], r["state"]) for r in listed], [("run-1", "starting")])
        retry = api.call("run.start", self.P)  # same key: answered from the intent, nothing relaunched
        self.assertEqual((retry["code"], retry["duplicate"], retry["run"]["run_id"]), (a.STARTED, True, "run-1"))
        self.assertEqual(len(starter.calls), 1)
        stop = api.call("run.stop", {"run_id": "run-1"})  # found, and truthfully not stopped
        self.assertEqual((stop["code"], stop["reason"]), (a.PENDING, "cgroup_unknown"))
        self.assertEqual(kill.requests, [])

    def test_starter_handle_without_cgroup_is_typed_and_the_run_stays_visible(self) -> None:
        api, registry, kill, _ = make_api(starter=lambda params: {"run_id": params["run_id"]})
        res = api.call("run.start", self.P)
        self.assertEqual((res["code"], res["detail"]), (a.START_UNCONFIRMED, "starter_handle_invalid"))
        self.assertEqual(registry.get("run-1")["state"], "starting")
        self.assertEqual(api.call("run.stop", {"run_id": "run-1"})["code"], a.PENDING)
        self.assertEqual(kill.requests, [])

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


if __name__ == "__main__":
    unittest.main()
