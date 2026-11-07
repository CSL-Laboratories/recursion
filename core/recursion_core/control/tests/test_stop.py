"""Stop semantics through the control API (PRODUCT.md 398cd812 S6, 11.3, 11.8)."""

from __future__ import annotations

import json
import threading
import unittest

from recursion_core.control import api as a
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

    def test_start_failure_is_typed(self) -> None:
        def refuse(params: dict) -> dict:
            raise RuntimeError("refused")

        registry = InMemoryRunRegistry()
        api = a.ControlAPI(registry, FakeKillTree(), refuse, clock=lambda: 0.0)
        self.assertEqual(api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["code"], a.START_FAILED)
        self.assertEqual(registry.list(), [])


if __name__ == "__main__":
    unittest.main()
