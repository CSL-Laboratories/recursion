"""Proposed regression tests for sweep S2 / s2b-control-api; no live engines.

This draft keeps T01 only: a Stop the registry refuses with StopUnattested
answers pending, never stopped. The answer on that draft pins the refusal's
reason, the load path, and the six mutants M02, M04, M05, M06, M07 and M08.
"""
from __future__ import annotations

import inspect
import unittest

from recursion_core.control import api as api_mod
from recursion_core.control.api import ControlAPI
from recursion_core.control.registry import EMPTY, InMemoryRunRegistry, StopUnattested

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

    def test_f1_m02_m05_a_proof_table_error_names_that_error(self):
        """M05: the reason is the table's Exception, not ``member_alive``.

        M02: answering that refusal as ``stale`` does not write the pending row.
        The table says the member is gone on every call it answers, and raises
        only on the proof inside ``put``.
        """
        samples = []

        def alive(pid):
            samples.append(pid)
            if len(samples) == 2:
                raise OSError("synthetic process-table error")
            return False

        registry = InMemoryRunRegistry(processes=alive)
        api, _, _ = make_api(registry=registry)
        rid = api.call("run.start", PARAMS)["run"]["run_id"]
        registry.join_member(rid, registry.get(rid)["generation"], "child", 4242)
        reply = api.call("run.stop", {"run_id": rid})
        self.assertEqual(reply.get("reason"), "cleanup_error:OSError", reply)
        self.assertEqual(reply.get("code"), "pending", reply)
        self.assertEqual(reply.get("state"), "pending")
        self.assertNotIn("repeat", reply)
        self.assertNotIn("attestation", reply)
        stored = registry.get(rid)
        self.assertEqual(stored["state"], "stop_pending")
        self.assertIsNone(stored.get("stop_attestation"))
        self.assertEqual(stored["members"], [{"member_id": "child", "pid": 4242}])
        self.assertEqual(stored["stop"]["reason"], "cleanup_error:OSError")

    def test_f1_a_refusal_with_no_members_is_not_a_live_member(self):
        """A registry that refuses the stopped row, with nobody to be alive, does not answer ``member_alive``."""
        registry = _RefuseStopped()
        api, _, _ = make_api(registry=registry)
        reply = api.call("run.stop", {"run_id": "run-1"})
        self.assertEqual(reply.get("code"), "pending", reply)
        self.assertEqual(reply.get("reason"), "cgroup_changed", reply)
        self.assertNotEqual(reply.get("reason"), "member_alive")
        self.assertEqual(registry.rec["state"], "stop_pending")
        self.assertEqual(registry.rec["members"], [])

    def test_f2_load_refuses_a_stopped_row_when_the_process_table_errors(self):
        """A raising table at load is ``StopUnattested``, not the table's own exception."""
        def boom(pid):
            raise OSError("synthetic process-table error")

        with self.assertRaises(StopUnattested) as caught:
            InMemoryRunRegistry(records=[_stopped_row()], processes=boom)
        self.assertEqual(caught.exception.reason, "cleanup_error:OSError")

    def test_f2_load_still_accepts_a_stopped_row_whose_member_is_gone(self):
        reg = InMemoryRunRegistry(records=[_stopped_row()], processes=lambda pid: False)
        stored = reg.get("run-1")
        self.assertEqual(stored["state"], "stopped")

    def test_m04_a_stale_then_refused_proof_keeps_the_stale_mark(self):
        """M04: the stale mark is kept when the proof is refused after a generation moved.

        The pending write then fails as a disk error, so only that mark puts the run
        in ``_stale_stops``. A refusal answered as ``stale`` (M02) never gets there
        with this reason.
        """
        samples = []

        def alive(pid):
            samples.append(pid)
            if len(samples) == 4:
                raise OSError("synthetic process-table error")
            return False

        registry = _PendingWriteFails(processes=alive)
        api, _, _ = make_api(registry=registry)
        rid = api.call("run.start", PARAMS)["run"]["run_id"]
        registry.join_member(rid, registry.get(rid)["generation"], "child", 4242)

        def gate(snapshot):
            registry._runs[snapshot["run_id"]]["generation"] += 1

        registry.commit_gate = gate
        reply = api.call("run.stop", {"run_id": rid})
        self.assertEqual(reply.get("code"), "pending", reply)
        self.assertEqual(reply.get("reason"), "cleanup_error:OSError", reply)
        self.assertIs(reply.get("recorded"), False, reply)
        self.assertIn(rid, api._stale_stops)

    def test_m06_m07_m08_the_pending_row_drops_the_refused_proof(self):
        """M06 writes ``stop_pending``. M07 clears the refused attestation.

        M08: a row whose ``stop`` is not a dict still answers pending. The registry
        here stores the payload it was given, so a cleared attestation stays visible.
        """
        row = _running_row()
        registry = _StoresTheRow(row)
        api, _, _ = make_api(registry=registry)
        rec = dict(row)
        rec["state"] = "stopped"
        rec["stop"] = None
        rec["stop_attestation"] = {
            "proof": "empty", "cgroup": row["cgroup"], "cgroup_marker": "cg-1",
            "member_ids": ["child"], "generation": 1,
        }
        api._local.unattested_reason = "cgroup_changed"
        reply = api._refused_attestation(rec)
        self.assertEqual((reply.get("code"), reply.get("reason"), reply.get("state")),
                         ("pending", "cgroup_changed", "pending"), reply)
        self.assertEqual(registry.rec.get("state"), "stop_pending")
        self.assertIsNone(registry.rec.get("stop_attestation"))
        self.assertIsInstance(registry.rec.get("stop"), dict)
        self.assertEqual(registry.rec["members"], [{"member_id": "child", "pid": 4242}])

    def test_f4_the_two_docstrings_match_the_refusal(self):
        module = api_mod.__doc__ or ""
        method = inspect.getdoc(ControlAPI._refused_attestation) or ""
        self.assertIn("not a second look at the process table", module)
        self.assertIn("not a second look at the process table", method)
        self.assertNotIn("process-table error ``_block_stop`` names", method)
        self.assertNotIn("because a member is alive or the process table errored", module)


class _RefuseStopped:
    """Refuses every stopped row and stores any other payload as given. No members."""

    def __init__(self):
        self.rec = _running_row()
        self.rec["members"] = []

    def get(self, run_id):
        if self.rec.get("run_id") != run_id:
            return None
        return dict(self.rec)

    def put(self, record):
        if record.get("state") == "stopped":
            raise StopUnattested(record.get("run_id") or "")
        self.rec = dict(record)
        return dict(self.rec)


class _StoresTheRow:
    """Stores the payload it was given. A stopped row is refused with no reason of its own."""

    def __init__(self, rec):
        self.rec = dict(rec)

    def get(self, run_id):
        if self.rec.get("run_id") != run_id:
            return None
        return dict(self.rec)

    def put(self, record):
        if record.get("state") == "stopped":
            raise StopUnattested(record.get("run_id") or "")
        self.rec = dict(record)
        return dict(self.rec)


class _PendingWriteFails(InMemoryRunRegistry):
    """The in-memory registry, except a ``stop_pending`` write raises ``OSError``."""

    def put(self, record):
        if record.get("state") == "stop_pending":
            raise OSError("synthetic disk error")
        return super().put(record)


def _running_row():
    return {
        "run_id": "run-1", "tenant": "t", "engine": "e", "state": "running",
        "cgroup": "/cg/run-1", "cgroup_marker": "cg-1", "generation": 1,
        "members": [{"member_id": "child", "pid": 4242}],
        "stop": None, "stop_attestation": None, "started_at": 1.0,
    }


def _stopped_row():
    row = _running_row()
    row["state"] = "stopped"
    row["generation"] = 2
    row["stop"] = {"requested_at": 1.0, "checks": 1, "reason": None, "requested": True}
    row["stop_attestation"] = {
        "proof": "empty", "cgroup": "/cg/run-1", "cgroup_marker": "cg-1",
        "member_ids": ["child"], "generation": 2,
    }
    return row


if __name__ == "__main__":
    unittest.main(verbosity=2)
