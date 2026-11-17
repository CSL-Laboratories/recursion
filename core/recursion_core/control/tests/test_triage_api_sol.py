"""Proposed regression tests for sweep S2 / s2b-control-api; no live engines.

This draft keeps T01 only: a Stop the registry refuses with StopUnattested
answers pending, never stopped.
"""
from __future__ import annotations

import unittest

from recursion_core.control.api import ControlAPI
from recursion_core.control.registry import EMPTY, InMemoryRunRegistry

PARAMS = {"tenant": "t", "engine": "e", "prompt": "hello"}


class Cleanup:
    def __init__(self, answer=EMPTY):
        self.answer, self.requests = answer, []

    def request_stop(self, cgroup):
        self.requests.append(cgroup)

    def confirm(self, cgroup, timeout_s):
        return self.answer


def make_api(*, registry=None, cleanup=None, starter=None):
    registry = registry if registry is not None else InMemoryRunRegistry()
    cleanup = cleanup if cleanup is not None else Cleanup()
    launches = []
    ids = iter(range(1, 201))

    def launch(params):
        launches.append(params["run_id"])
        return {"cgroup": "/cg/" + params["run_id"]}

    api = ControlAPI(registry, cleanup, starter or launch, lambda: 1.0,
                     new_run_id=lambda: "run-" + str(next(ids)))
    return api, registry, launches


class ControlSweepRegressions(unittest.TestCase):
    def test_f9_refused_attestation_never_answers_stopped(self):
        for second in (True, OSError("synthetic process-table error")):
            with self.subTest(second=second):
                samples = []

                def alive(pid):
                    samples.append(pid)
                    if len(samples) == 1:
                        return False
                    if isinstance(second, Exception):
                        raise second
                    return second

                registry = InMemoryRunRegistry(processes=alive)
                api, _, _ = make_api(registry=registry)
                rid = api.call("run.start", PARAMS)["run"]["run_id"]
                registry.join_member(rid, registry.get(rid)["generation"], "child", 123)
                reply = api.call("run.stop", {"run_id": rid})
                self.assertEqual(reply["code"], "pending", reply)
                self.assertNotEqual(registry.get(rid)["state"], "stopped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
