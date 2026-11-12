"""Repros for stop races left after compare-and-swap and attested stop.

Clocks, process tables and cleanup answers are fakes. Nothing starts a provider,
a cgroup, or a real workload.
"""

from __future__ import annotations

import unittest

from recursion_core.control import api as a
from recursion_core.control.registry import InMemoryRunRegistry, StaleGeneration
from recursion_core.control.tests.fakes import make_api
from recursion_core.control.tests.test_registry_cas import _running


class _LoseTwice(InMemoryRunRegistry):
    """The first two stopped puts lose to a later writer. The second writer joins a live member."""

    def __init__(self) -> None:
        super().__init__(processes=lambda _pid: True)
        self.stopped_puts = 0

    def put(self, record: dict):
        if record.get("state") == "stopped" and self.stopped_puts < 2:
            self.stopped_puts += 1
            current = super().get(record["run_id"])
            assert current is not None
            if self.stopped_puts == 1:
                current["note"] = "later-writer"
                super().put(current)
            else:
                super().join_member(record["run_id"], current["generation"], "late", 9)
            fresh = super().get(record["run_id"])
            assert fresh is not None
            raise StaleGeneration(record["run_id"], record.get("generation"), fresh["generation"])
        return super().put(record)


class _LoseTwiceThenBlind(InMemoryRunRegistry):
    """The second lost stopped put joins a live member, then ``get`` cannot be read."""

    def __init__(self) -> None:
        super().__init__(processes=lambda _pid: True)
        self.stopped_puts = 0
        self.blind = False
        self.hidden: dict | None = None

    def put(self, record: dict):
        if record.get("state") == "stopped" and self.stopped_puts < 2:
            self.stopped_puts += 1
            current = super().get(record["run_id"])
            assert current is not None
            if self.stopped_puts == 1:
                current["note"] = "later-writer"
                super().put(current)
            else:
                super().join_member(record["run_id"], current["generation"], "late", 9)
                self.blind = True
            fresh = super().get(record["run_id"])
            assert fresh is not None
            raise StaleGeneration(record["run_id"], record.get("generation"), fresh["generation"])
        return super().put(record)

    def get(self, run_id: str):
        row = super().get(run_id)
        if self.blind:
            self.hidden = row
            raise OSError("disk")
        return row


class _PendingLosesToStopped(InMemoryRunRegistry):
    """Two stopped puts lose. The following pending put loses to a real stopped attestation."""

    def __init__(self) -> None:
        super().__init__(processes=lambda _pid: False)
        self.stopped_puts = 0

    def put(self, record: dict):
        if record.get("state") == "stopped" and self.stopped_puts < 2:
            self.stopped_puts += 1
            current = super().get(record["run_id"])
            assert current is not None
            current["note"] = f"writer-{self.stopped_puts}"
            super().put(current)
            fresh = super().get(record["run_id"])
            assert fresh is not None
            raise StaleGeneration(record["run_id"], record.get("generation"), fresh["generation"])
        if record.get("state") == "stop_pending":
            current = super().get(record["run_id"])
            assert current is not None
            stopped = dict(current)
            stopped["state"] = "stopped"
            stopped["stop_attestation"] = {
                "proof": "empty",
                "cgroup": current["cgroup"],
                "cgroup_marker": current["cgroup_marker"],
                "member_ids": sorted(member["member_id"] for member in current.get("members") or []),
                "generation": current["generation"],
            }
            super().put(stopped)
            fresh = super().get(record["run_id"])
            assert fresh is not None
            raise StaleGeneration(record["run_id"], record.get("generation"), fresh["generation"])
        return super().put(record)


class _FlipAlive:
    """Dead on the first sample, alive on the next. A pid can be reused between the two checks."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _pid: int) -> bool:
        self.calls += 1
        return self.calls > 1


class _DeadThenDenied:
    """The stop's own check sees the member gone. The probe then fails inside the put."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _pid: int) -> bool:
        self.calls += 1
        if self.calls == 1:
            return False
        raise PermissionError("proc")


class LastWriterStopTest(unittest.TestCase):
    def test_a_later_member_that_wins_the_retry_keeps_stop_pending(self) -> None:
        # Stop confirms empty, then loses the generation twice. The second winner is a live
        # member. The answer must not be stopped: that member is still on the row.
        reg = _LoseTwice()
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        res = api.call("run.stop", {"run_id": run})
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["reason"], "member_alive")
        self.assertNotEqual(stored["state"], "stopped")
        self.assertEqual([member["member_id"] for member in stored["members"]], ["late"])
        self.assertIsNone(stored.get("stop_attestation"))

    def test_a_lost_stop_whose_reread_fails_is_not_reported_stopped(self) -> None:
        # The retry loses to a live member, then the registry cannot be read. That is not a
        # stopped run: the member is still on the running row.
        reg = _LoseTwiceThenBlind()
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        res = api.call("run.stop", {"run_id": run})
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["state"], "pending")
        self.assertEqual(res["reason"], "unknown")
        self.assertIs(res["recorded"], False)
        assert reg.hidden is not None
        self.assertEqual(reg.hidden["state"], "running")
        self.assertEqual([member["member_id"] for member in reg.hidden["members"]], ["late"])

    def test_a_pending_put_that_loses_to_a_stopped_row_answers_stopped(self) -> None:
        # The pending write loses to a writer who stored a real stopped attestation.
        # Re-read that row and answer stopped, instead of pending/generation_conflict.
        reg = _PendingLosesToStopped()
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        res = api.call("run.stop", {"run_id": run})
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual(stored["state"], "stopped")
        self.assertEqual(stored["stop_attestation"]["proof"], "empty")
        self.assertEqual(res["code"], a.STOPPED)
        self.assertEqual(res["state"], "stopped")
        self.assertIs(res["repeat"], True)
        self.assertIsNot(res.get("recorded"), False)


class StopAttestationTest(unittest.TestCase):
    def test_a_member_alive_at_commit_is_not_reported_stopped(self) -> None:
        # The API's check sees the member gone. The registry's check, still on that generation,
        # sees the pid alive and refuses the proof. That refusal is not a stopped run.
        reg = InMemoryRunRegistry(processes=_FlipAlive())
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        reg.join_member(run, reg.get(run)["generation"], "m", 4)
        res = api.call("run.stop", {"run_id": run})
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual(res["code"], a.PENDING)
        self.assertNotEqual(stored["state"], "stopped")
        self.assertEqual(stored["members"], [{"member_id": "m", "pid": 4}])
        self.assertIsNone(stored.get("stop_attestation"))

    def test_a_liveness_error_inside_the_stop_put_is_not_stopped(self) -> None:
        # The API's check saw the member gone. The registry's probe then raises. That is not
        # proof the member is dead, and it is not a stopped run.
        reg = InMemoryRunRegistry(processes=_DeadThenDenied())
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        reg.join_member(run, reg.get(run)["generation"], "m", 4)
        res = api.call("run.stop", {"run_id": run})
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual(res["code"], a.PENDING)
        self.assertIn(res["reason"], {"member_alive", "liveness_unknown"})
        self.assertNotEqual(res["code"], a.STOPPED)
        self.assertNotEqual(stored["state"], "stopped")
        self.assertEqual(stored["members"], [{"member_id": "m", "pid": 4}])
        self.assertIsNone(stored.get("stop_attestation"))

    def test_a_fresh_confirm_is_stored_when_the_old_marker_was_retired(self) -> None:
        # The row dropped its cgroup, so the retired marker cannot be put back. This stop confirms
        # the path now. The stored proof has to name the marker this put actually bound.
        reg = InMemoryRunRegistry()
        reg.put({"run_id": "r", "tenant": "t", "engine": "e", "state": "running",
                 "cgroup": "A", "stop": None, "start_key": None})
        held = reg.get("r")["cgroup_marker"]
        current = reg.get("r")
        assert current is not None
        current["cgroup"] = None
        reg.put(current)
        api, _, _, _ = make_api(registry=reg)
        api._launched["r"] = {"run_id": "r", "tenant": "t", "engine": "e", "state": "running",
                              "cgroup": "A", "cgroup_marker": held, "stop": None, "start_key": None}
        res = api.call("run.stop", {"run_id": "r"})
        stored = reg.get("r")
        assert stored is not None
        self.assertEqual(res["code"], a.STOPPED)
        self.assertIsNot(res.get("recorded"), False)
        self.assertEqual(stored["state"], "stopped")
        self.assertEqual(stored["cgroup"], "A")
        self.assertNotEqual(stored["cgroup_marker"], held)
        self.assertEqual(stored["stop_attestation"]["cgroup"], "A")
        self.assertEqual(stored["stop_attestation"]["cgroup_marker"], stored["cgroup_marker"])


class LaterMemberOrphanTest(unittest.TestCase):
    def test_a_later_member_reuses_the_one_sample_of_its_pid(self) -> None:
        reg = InMemoryRunRegistry()
        reg.put(_running())
        generation = reg.get("run-1")["generation"]
        reg.join_member("run-1", generation, "early", 7)
        reg.join_member("run-1", reg.get("run-1")["generation"], "late", 7)
        calls: list[int] = []

        def is_alive(pid: int) -> bool:
            calls.append(pid)
            return len(calls) == 1  # a second sample would call the later member dead

        reaped = reg.reap_orphans(is_alive)
        stored = reg.get("run-1")
        assert stored is not None
        self.assertEqual(calls, [7])
        self.assertEqual([member["member_id"] for member in stored["members"]], ["early", "late"])
        self.assertFalse(any(item.get("member_id") == "late" for item in reaped))

    def test_a_liveness_error_does_not_skip_a_later_orphan(self) -> None:
        reg = InMemoryRunRegistry()
        reg.put(_running("run-a"))
        reg.put(_running("run-b"))
        reg.join_member("run-a", reg.get("run-a")["generation"], "bad", 1)
        reg.join_member("run-b", reg.get("run-b")["generation"], "gone", 2)

        def is_alive(pid: int) -> bool:
            if pid == 1:
                raise PermissionError("proc")
            return False

        reaped = reg.reap_orphans(is_alive)
        self.assertEqual(reg.get("run-a")["members"], [{"member_id": "bad", "pid": 1}])
        self.assertEqual(reg.get("run-b")["members"], [])
        self.assertEqual([item["member_id"] for item in reaped if "member_id" in item], ["gone"])

    def test_a_dead_sample_is_not_reused_for_another_run(self) -> None:
        # One false dead sample for pid 42 must not reap the same pid on another run.
        # That run keeps its member, so stop stays pending.
        reg = InMemoryRunRegistry(processes=lambda _pid: True)
        reg.put(_running("run-a"))
        reg.put(_running("run-b"))
        reg.join_member("run-a", reg.get("run-a")["generation"], "a", 42)
        reg.join_member("run-b", reg.get("run-b")["generation"], "b", 42)
        calls: list[int] = []

        def is_alive(pid: int) -> bool:
            calls.append(pid)
            return len(calls) > 1

        reaped = reg.reap_orphans(is_alive)
        self.assertEqual([item["run_id"] for item in reaped if "member_id" in item], ["run-a"])
        self.assertEqual(reg.get("run-a")["members"], [])
        self.assertEqual(reg.get("run-b")["members"], [{"member_id": "b", "pid": 42}])
        api, _, _, _ = make_api(registry=reg)
        res = api.call("run.stop", {"run_id": "run-b"})
        stored = reg.get("run-b")
        assert stored is not None
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["reason"], "member_alive")
        self.assertEqual(stored["members"], [{"member_id": "b", "pid": 42}])
        self.assertNotEqual(stored["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
