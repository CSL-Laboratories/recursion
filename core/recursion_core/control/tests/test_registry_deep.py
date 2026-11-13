"""Repros for stop races left after compare-and-swap and attested stop.

Clocks, process tables and cleanup answers are fakes. Nothing starts a provider,
a cgroup, or a real workload.
"""

from __future__ import annotations

import io
import json
import unittest

from recursion_core.control import api as a
from recursion_core.control import cli
from recursion_core.control import mcp_tools
from recursion_core.control.registry import (
    EMPTY, InMemoryRunRegistry, LivenessUnknown, StaleGeneration, StopUnattested,
)
from recursion_core.control.tests.fakes import FakeKillTree, make_api
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


class _BlindBeforeSecondStop(InMemoryRunRegistry):
    """The first stopped put joins a live member, then ``get`` cannot be read.

    The re-read in ``_recorded`` is the one that fails, before any second stopped put.
    """

    def __init__(self) -> None:
        super().__init__(processes=lambda _pid: True)
        self.stopped_puts = 0
        self.blind = False
        self.hidden: dict | None = None

    def put(self, record: dict):
        if record.get("state") == "stopped" and self.stopped_puts < 1:
            self.stopped_puts += 1
            current = super().get(record["run_id"])
            assert current is not None
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


class _PlantStoppedOnFirstPut(InMemoryRunRegistry):
    """The first stopped put loses to a stopped row the registry itself would not have stored."""

    def __init__(self, *, blank_proof: bool, members: list[dict]) -> None:
        super().__init__(processes=lambda _pid: True)
        self._blank_proof = blank_proof
        self._members = members
        self.planted = False

    def put(self, record: dict):
        if record.get("state") == "stopped" and not self.planted:
            current = super().get(record["run_id"])
            assert current is not None
            planted = dict(current)
            planted["state"] = "stopped"
            planted["generation"] = current["generation"] + 1
            planted["members"] = [dict(member) for member in self._members]
            if self._blank_proof:
                planted["stop_attestation"] = {}
            else:
                planted["stop_attestation"] = {
                    "proof": "empty",
                    "cgroup": current.get("cgroup"),
                    "cgroup_marker": current.get("cgroup_marker"),
                    "member_ids": [member["member_id"] for member in self._members],
                    "generation": planted["generation"],
                }
            self._runs[record["run_id"]] = planted
            self.planted = True
            raise StaleGeneration(record["run_id"], record.get("generation"), planted["generation"])
        return super().put(record)


class _PlantStoppedOnRetry(InMemoryRunRegistry):
    """The retry's stopped put loses to a stopped row the registry itself would not have stored.

    The first stopped put only bumps a running row, so ``_recorded``'s own re-read does not see
    ``stopped``. The planted row is what ``_lost_stop_is_pending`` reads first.
    """

    def __init__(self, *, blank_proof: bool, members: list[dict]) -> None:
        super().__init__(processes=lambda _pid: True)
        self._blank_proof = blank_proof
        self._members = members
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
                planted = dict(current)
                planted["state"] = "stopped"
                planted["generation"] = current["generation"] + 1
                planted["members"] = [dict(member) for member in self._members]
                if self._blank_proof:
                    planted["stop_attestation"] = {}
                else:
                    planted["stop_attestation"] = {
                        "proof": "empty",
                        "cgroup": current.get("cgroup"),
                        "cgroup_marker": current.get("cgroup_marker"),
                        "member_ids": [member["member_id"] for member in self._members],
                        "generation": planted["generation"],
                    }
                self._runs[record["run_id"]] = planted
            fresh = super().get(record["run_id"])
            assert fresh is not None
            raise StaleGeneration(record["run_id"], record.get("generation"), fresh["generation"])
        return super().put(record)


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

    def test_a_blind_reread_before_the_second_stopped_put_is_pending(self) -> None:
        # The stopped put loses to a live member, and the re-read before the retry cannot
        # be done. That is not a stopped run, and the CLI must not exit 0.
        reg = _BlindBeforeSecondStop()
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["stop", run], api, out, err)
        res = json.loads(out.getvalue())
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["state"], "pending")
        self.assertEqual(res["reason"], "unknown")
        self.assertIs(res["recorded"], False)
        self.assertNotEqual(code, 0)
        self.assertEqual(code, cli.EXIT_PENDING)
        assert reg.hidden is not None
        self.assertEqual(reg.hidden["state"], "running")
        self.assertEqual(reg.hidden["members"], [{"member_id": "late", "pid": 9}])
        self.assertIs(reg.process_alive(9), True)

    def test_a_lost_stop_does_not_leave_a_stopped_launch_handle(self) -> None:
        # The second loss answers pending, but the handle must not keep a stopped copy.
        # The next stop, while get still fails, must not report stopped over the live member.
        reg = _LoseTwiceThenBlind()
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        held = reg.get(run)
        assert held is not None
        api._launched[run] = dict(held)
        first = api.call("run.stop", {"run_id": run})
        self.assertEqual(first["code"], a.PENDING)
        self.assertEqual(first["state"], "pending")
        self.assertEqual(first["reason"], "unknown")
        self.assertIs(first["recorded"], False)
        self.assertNotEqual(api._launched[run].get("state"), "stopped")
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["stop", run], api, out, err)
        second = json.loads(out.getvalue())
        self.assertEqual(second["code"], a.PENDING)
        self.assertEqual(second["state"], "pending")
        self.assertIs(second["recorded"], False)
        self.assertNotEqual(code, 0)
        self.assertEqual(code, cli.EXIT_PENDING)
        assert reg.hidden is not None
        self.assertEqual(reg.hidden["state"], "running")
        self.assertEqual(reg.hidden["members"], [{"member_id": "late", "pid": 9}])
        self.assertIs(reg.process_alive(9), True)

    def test_a_reread_stopped_row_with_an_empty_proof_is_pending(self) -> None:
        reg = _PlantStoppedOnFirstPut(blank_proof=True, members=[])
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        res = api.call("run.stop", {"run_id": run})
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["state"], "pending")
        self.assertEqual(res["reason"], "generation_conflict")
        self.assertIs(res["recorded"], False)
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual(stored["stop_attestation"], {})
        self.assertNotEqual(res.get("repeat"), True)

    def test_a_reread_stopped_row_that_still_lists_a_live_member_is_pending(self) -> None:
        reg = _PlantStoppedOnFirstPut(blank_proof=False, members=[{"member_id": "late", "pid": 9}])
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        res = api.call("run.stop", {"run_id": run})
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["state"], "pending")
        self.assertEqual(res["reason"], "generation_conflict")
        self.assertIs(res["recorded"], False)
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual(stored["state"], "stopped")
        self.assertEqual(stored["members"], [{"member_id": "late", "pid": 9}])
        self.assertIs(reg.process_alive(9), True)

    def test_a_retry_reread_stopped_row_with_an_empty_proof_is_pending(self) -> None:
        # The first loss leaves the row running. The retry loses to a stopped row whose proof
        # is {}. That re-read is inside _lost_stop_is_pending, and it is not a recorded stop.
        reg = _PlantStoppedOnRetry(blank_proof=True, members=[])
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        res = api.call("run.stop", {"run_id": run})
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["state"], "pending")
        self.assertEqual(res["reason"], "generation_conflict")
        self.assertIs(res["recorded"], False)
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual(stored["stop_attestation"], {})
        self.assertEqual(reg.stopped_puts, 2)

    def test_a_retry_reread_stopped_row_that_still_lists_a_live_member_is_pending(self) -> None:
        reg = _PlantStoppedOnRetry(blank_proof=False, members=[{"member_id": "late", "pid": 9}])
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        res = api.call("run.stop", {"run_id": run})
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["state"], "pending")
        self.assertEqual(res["reason"], "generation_conflict")
        self.assertIs(res["recorded"], False)
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual(stored["state"], "stopped")
        self.assertEqual(stored["members"], [{"member_id": "late", "pid": 9}])
        self.assertIs(reg.process_alive(9), True)
        self.assertEqual(reg.stopped_puts, 2)


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


class LoadedStopProbeTest(unittest.TestCase):
    def test_loading_a_stopped_snapshot_whose_probe_raises_is_liveness_unknown(self) -> None:
        legacy = {"run_id": "run-1", "tenant": "t", "engine": "codex", "state": "stopped",
                  "cgroup": "/cg/run-1", "generation": 2,
                  "members": [{"member_id": "m", "pid": 7}],
                  "stop_attestation": {"proof": "empty", "cgroup": "/cg/run-1", "member_ids": ["m"],
                                       "generation": 1}}

        def deny(_pid: int) -> bool:
            raise PermissionError("proc")

        with self.assertRaises(LivenessUnknown):
            InMemoryRunRegistry([legacy], processes=deny)


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


class _NoneAfterLiveJoin(InMemoryRunRegistry):
    """A stopped put joins a live member and returns None, which the protocol allows.

    Nothing is stored for that put. The row the re-read sees is the join, not a stop.
    """

    def __init__(self) -> None:
        super().__init__(processes=lambda _pid: True)
        self.blind_stops = 0

    def put(self, record: dict):
        if record.get("state") == "stopped":
            current = super().get(record["run_id"])
            assert current is not None
            if not any(member.get("member_id") == "late" for member in current.get("members") or []):
                super().join_member(record["run_id"], current["generation"], "late", 9)
            self.blind_stops += 1
            return None
        return super().put(record)


class _OsErrorAfterLiveJoin(InMemoryRunRegistry):
    """A stopped put joins a live member, then raises ``OSError`` and stores no stop."""

    def __init__(self, *, processes=None) -> None:
        super().__init__(processes=processes or (lambda _pid: True))
        self.joined = False

    def put(self, record: dict):
        if record.get("state") == "stopped" and not self.joined:
            current = super().get(record["run_id"])
            assert current is not None
            super().join_member(record["run_id"], current["generation"], "late", 9)
            self.joined = True
            raise OSError("disk")
        return super().put(record)


class _BlindAfterStoppedPut(InMemoryRunRegistry):
    """The stopped put raises ``OSError``, and every later read fails.

    ``members`` is the member list planted on the running row before that put, or empty.
    """

    def __init__(self, *, processes=None) -> None:
        super().__init__(processes=processes or (lambda _pid: False))
        self.blind = False
        self.reads_while_blind = 0

    def put(self, record: dict):
        if record.get("state") == "stopped":
            self.blind = True
            raise OSError("disk")
        return super().put(record)

    def get(self, run_id: str):
        if self.blind:
            self.reads_while_blind += 1
            raise OSError("disk")
        return super().get(run_id)


class _BlankThenRealAttestation(InMemoryRunRegistry):
    """The stopped put loses to a blank stopped row. The pending put loses to a real attestation."""

    def __init__(self) -> None:
        super().__init__(processes=lambda _pid: False)
        self.phase = 0

    def put(self, record: dict):
        if record.get("state") == "stopped" and self.phase == 0:
            current = super().get(record["run_id"])
            assert current is not None
            self.phase = 1
            planted = dict(current)
            planted["state"] = "stopped"
            planted["generation"] = current["generation"] + 1
            planted["members"] = []
            planted["stop_attestation"] = {}
            self._runs[record["run_id"]] = planted
            raise StaleGeneration(record["run_id"], record.get("generation"), planted["generation"])
        if record.get("state") == "stop_pending" and self.phase == 1:
            current = super().get(record["run_id"])
            assert current is not None
            self.phase = 2
            stopped = dict(current)
            stopped["state"] = "stopped"
            stopped["members"] = []
            stopped["generation"] = current["generation"]
            stopped["stop_attestation"] = {
                "proof": "empty",
                "cgroup": current.get("cgroup"),
                "cgroup_marker": current.get("cgroup_marker"),
                "member_ids": [],
                "generation": current["generation"],
            }
            self._runs[record["run_id"]] = stopped
            raise StaleGeneration(record["run_id"], record.get("generation"), stopped["generation"])
        return super().put(record)


def _stop_surfaces(api, run: str) -> tuple[dict, int, dict]:
    """One CLI stop, then one MCP stop. Both see the same registry."""
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["stop", run], api, out, err)
    cli_res = json.loads(out.getvalue())
    tool = mcp_tools.call_tool(api, "recursion_run_stop", {"run_id": run})
    return cli_res, code, tool


class ReviewDeepFourTest(unittest.TestCase):
    def test_a_none_put_after_a_live_join_is_not_stopped(self) -> None:
        # put may return None. Joining late/9 and returning None is not a stored stop.
        # CLI must not exit 0, and MCP must set isError.
        reg = _NoneAfterLiveJoin()
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        res, code, tool = _stop_surfaces(api, run)
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["state"], "pending")
        self.assertEqual(res["reason"], "member_alive")
        self.assertNotEqual(res["code"], a.STOPPED)
        self.assertNotEqual(code, 0)
        self.assertEqual(code, cli.EXIT_PENDING)
        self.assertTrue(tool["isError"])
        self.assertEqual(tool["structuredContent"]["code"], a.PENDING)
        self.assertNotEqual(tool["structuredContent"]["code"], a.STOPPED)
        stored = reg.get(run)
        assert stored is not None
        self.assertNotEqual(stored["state"], "stopped")
        self.assertEqual(stored["members"], [{"member_id": "late", "pid": 9}])
        self.assertIs(reg.process_alive(9), True)
        self.assertGreaterEqual(reg.blind_stops, 1)

    def test_an_oserror_after_a_live_join_is_not_stopped(self) -> None:
        # A non-typed put error is not 'stale', so the old path kept code stopped
        # with recorded false. The re-read shows late/9 alive.
        reg = _OsErrorAfterLiveJoin()
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        res, code, tool = _stop_surfaces(api, run)
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["state"], "pending")
        self.assertEqual(res["reason"], "member_alive")
        self.assertIs(res["recorded"], False)
        self.assertNotEqual(code, 0)
        self.assertEqual(code, cli.EXIT_PENDING)
        self.assertTrue(tool["isError"])
        self.assertNotEqual(tool["structuredContent"]["code"], a.STOPPED)
        stored = reg.get(run)
        assert stored is not None
        self.assertNotEqual(stored["state"], "stopped")
        self.assertEqual(stored["members"], [{"member_id": "late", "pid": 9}])
        self.assertIs(reg.process_alive(9), True)

    def test_an_oserror_after_a_join_whose_probe_raises_is_liveness_unknown(self) -> None:
        # The re-read sees the member, and the probe raises. That is not proof the
        # member is gone, so the answer is pending / liveness_unknown.
        def deny(_pid: int) -> bool:
            raise PermissionError("proc")

        reg = _OsErrorAfterLiveJoin(processes=deny)
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["stop", run], api, out, err)
        res = json.loads(out.getvalue())
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["reason"], "liveness_unknown")
        self.assertIs(res["recorded"], False)
        self.assertNotEqual(code, 0)
        self.assertNotIn(res["code"], a.SUCCESS_CODES)
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual(stored["members"], [{"member_id": "late", "pid": 9}])
        self.assertNotEqual(stored["state"], "stopped")

    def test_a_failed_reread_after_a_confirmed_stop_with_members_is_pending(self) -> None:
        # The stopped put raises, and the re-read raises too. The pre-put row already
        # listed a member, so the confirmed-stop contract does not apply.
        reg = _BlindAfterStoppedPut()
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        reg.join_member(run, reg.get(run)["generation"], "late", 9)
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["stop", run], api, out, err)
        res = json.loads(out.getvalue())
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["state"], "pending")
        self.assertEqual(res["reason"], "liveness_unknown")
        self.assertIs(res["recorded"], False)
        self.assertNotEqual(code, 0)
        self.assertNotIn(res["code"], a.SUCCESS_CODES)

    def test_a_failed_reread_after_a_confirmed_stop_with_no_members_stays_stopped(self) -> None:
        # Choice: when the re-read itself fails, keep the documented contract
        # (test_failed_write_after_confirmed_stop_still_answers_stopped) only if the
        # pre-put row had no members and the stop was confirmed. A member joined
        # inside the failed put is invisible, and this path does not invent one.
        # The code/recorded pair also passed on 387e5ea3 without observing the re-read:
        # a failed put of a confirmed stop with no pre-put members answers stopped
        # either way. ``reads_while_blind`` is that re-read. It has to run, and fail,
        # before the stopped answer is kept.
        reg = _BlindAfterStoppedPut()
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["stop", run], api, out, err)
        res = json.loads(out.getvalue())
        self.assertGreaterEqual(reg.reads_while_blind, 1)
        self.assertEqual((res["code"], res["recorded"]), (a.STOPPED, False))
        self.assertEqual(code, 0)
        self.assertIn(res["code"], a.SUCCESS_CODES)

    def test_a_second_stop_of_a_planted_stopped_row_with_a_live_member_is_pending(self) -> None:
        # The first stop plants a stopped row that still lists late/9. A second stop
        # used to return stopped from the early return, with no recorded-stop check.
        reg = _PlantStoppedOnFirstPut(blank_proof=False, members=[{"member_id": "late", "pid": 9}])
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        first = api.call("run.stop", {"run_id": run})
        self.assertEqual(first["code"], a.PENDING)
        res, code, tool = _stop_surfaces(api, run)
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["state"], "pending")
        self.assertNotEqual(res["code"], a.STOPPED)
        self.assertIsNot(res.get("repeat"), True)
        self.assertNotEqual(code, 0)
        self.assertEqual(code, cli.EXIT_PENDING)
        self.assertTrue(tool["isError"])
        self.assertNotEqual(tool["structuredContent"]["code"], a.STOPPED)
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual(stored["members"], [{"member_id": "late", "pid": 9}])
        self.assertIs(reg.process_alive(9), True)

    def test_a_pending_rewrite_that_loses_to_a_recorded_stop_answers_stopped(self) -> None:
        # The re-read is a blank stopped row, so the pending rewrite runs. That put
        # loses to a real attestation. The answer follows the stored row.
        reg = _BlankThenRealAttestation()
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["stop", run], api, out, err)
        res = json.loads(out.getvalue())
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual(stored["state"], "stopped")
        self.assertEqual(stored["stop_attestation"]["proof"], "empty")
        self.assertEqual(stored["members"], [])
        self.assertTrue(a._recorded_stop(stored))
        self.assertEqual(res["code"], a.STOPPED)
        self.assertEqual(res["state"], "stopped")
        self.assertIs(res["repeat"], True)
        self.assertIsNot(res.get("recorded"), False)
        self.assertEqual(code, 0)
        self.assertIn(res["code"], a.SUCCESS_CODES)
        self.assertEqual(reg.phase, 2)


class _PlantStoppedMemberOnRunningPut(InMemoryRunRegistry):
    """The running put loses to a stored stopped row that still lists late/9.

    Start still returns started, and the id stays in ``_launched``. The first stop
    must not answer stopped or clear that member.
    """

    def __init__(self, *, processes) -> None:
        super().__init__(processes=processes)
        self.planted = False

    def put(self, record: dict):
        if record.get("state") == "running" and not self.planted:
            current = super().get(record["run_id"])
            assert current is not None
            planted = dict(current)
            planted["state"] = "stopped"
            planted["generation"] = current["generation"] + 1
            planted["cgroup"] = record.get("cgroup")
            planted["cgroup_marker"] = record.get("cgroup_marker")
            planted["members"] = [{"member_id": "late", "pid": 9}]
            planted["stop_attestation"] = {
                "proof": "empty",
                "cgroup": planted.get("cgroup"),
                "cgroup_marker": planted.get("cgroup_marker"),
                "member_ids": ["late"],
                "generation": planted["generation"],
            }
            self._runs[record["run_id"]] = planted
            self.planted = True
            raise StaleGeneration(record["run_id"], record.get("generation"), planted["generation"])
        return super().put(record)


class _ReturnRunningRowOnStop(InMemoryRunRegistry):
    """A stopped put returns the stored running row, with late/9, and stores no stop."""

    def __init__(self) -> None:
        super().__init__(processes=lambda _pid: True)
        self.returned_running = 0

    def put(self, record: dict):
        if record.get("state") == "stopped":
            current = super().get(record["run_id"])
            assert current is not None
            if not any(member.get("member_id") == "late" for member in current.get("members") or []):
                super().join_member(record["run_id"], current["generation"], "late", 9)
            self.returned_running += 1
            fresh = super().get(record["run_id"])
            assert fresh is not None
            return fresh
        return super().put(record)


class ReviewDeepFiveTest(unittest.TestCase):
    def test_a_launched_stop_does_not_clear_a_live_member(self) -> None:
        # The running put loses to a stopped row that still lists late/9. Start returns
        # started and the id stays in _launched. The first stop used to take the
        # _launched shortcut, and the already-stopped put cleared the member with no
        # liveness check. CLI must not exit 0, and MCP must set isError.
        reg = _PlantStoppedMemberOnRunningPut(processes=lambda _pid: True)
        api, _, _, _ = make_api(registry=reg)
        started = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})
        run = started["run"]["run_id"]
        self.assertEqual(started["code"], a.STARTED)
        self.assertIn(run, api._launched)
        self.assertEqual(reg.get(run)["members"], [{"member_id": "late", "pid": 9}])
        res, code, tool = _stop_surfaces(api, run)
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["state"], "pending")
        self.assertEqual(res["reason"], "member_alive")
        self.assertIs(res["recorded"], False)
        self.assertNotEqual(res["code"], a.STOPPED)
        self.assertIsNot(res.get("repeat"), True)
        self.assertNotEqual(code, 0)
        self.assertEqual(code, cli.EXIT_PENDING)
        self.assertTrue(tool["isError"])
        self.assertEqual(tool["structuredContent"]["code"], a.PENDING)
        self.assertNotEqual(tool["structuredContent"]["code"], a.STOPPED)
        self.assertNotIn(res["code"], a.SUCCESS_CODES)
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual(stored["members"], [{"member_id": "late", "pid": 9}])
        self.assertIs(reg.process_alive(9), True)
        self.assertTrue(reg.planted)

    def test_a_launched_stop_treats_a_probe_error_as_pending(self) -> None:
        # PermissionError is not proof the member is gone. The already-stopped put
        # must not clear late/9, and the answer is pending, not stopped.
        def deny(_pid: int) -> bool:
            raise PermissionError("proc")

        reg = _PlantStoppedMemberOnRunningPut(processes=deny)
        api, _, _, _ = make_api(registry=reg)
        started = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})
        run = started["run"]["run_id"]
        self.assertEqual(started["code"], a.STARTED)
        self.assertIn(run, api._launched)
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["stop", run], api, out, err)
        res = json.loads(out.getvalue())
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["reason"], "liveness_unknown")
        self.assertIs(res["recorded"], False)
        self.assertNotEqual(code, 0)
        self.assertNotIn(res["code"], a.SUCCESS_CODES)
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual(stored["members"], [{"member_id": "late", "pid": 9}])
        self.assertNotEqual(stored["state"], "running")

    def test_a_returned_running_row_is_not_a_recorded_stop(self) -> None:
        # put may return any dict. A running row that still lists late/9 is not the
        # stop that was written, so the answer is pending and the member stays.
        reg = _ReturnRunningRowOnStop()
        api, _, _, _ = make_api(registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        res, code, tool = _stop_surfaces(api, run)
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["state"], "pending")
        self.assertEqual(res["reason"], "member_alive")
        self.assertNotEqual(res["code"], a.STOPPED)
        self.assertNotEqual(code, 0)
        self.assertEqual(code, cli.EXIT_PENDING)
        self.assertTrue(tool["isError"])
        self.assertNotEqual(tool["structuredContent"]["code"], a.STOPPED)
        self.assertNotIn(res["code"], a.SUCCESS_CODES)
        stored = reg.get(run)
        assert stored is not None
        self.assertNotEqual(stored["state"], "stopped")
        self.assertEqual(stored["members"], [{"member_id": "late", "pid": 9}])
        self.assertIs(reg.process_alive(9), True)
        self.assertGreaterEqual(reg.returned_running, 1)


def _stopped_row_with_member(reg: InMemoryRunRegistry) -> dict:
    reg.put({"run_id": "r", "tenant": "t", "engine": "e", "state": "running",
             "cgroup": "/cg/r", "stop": None, "start_key": None})
    row = reg._runs["r"]
    row["state"] = "stopped"
    row["members"] = [{"member_id": "late", "pid": 9}]
    row["stop_attestation"] = {
        "proof": "empty",
        "cgroup": row["cgroup"],
        "cgroup_marker": row.get("cgroup_marker"),
        "member_ids": ["late"],
        "generation": row["generation"],
    }
    fresh = reg.get("r")
    assert fresh is not None
    fresh["engine"] = "other"
    return fresh


class AlreadyStoppedMemberTest(unittest.TestCase):
    def test_an_already_stopped_put_does_not_drop_a_live_member(self) -> None:
        reg = InMemoryRunRegistry(processes=lambda _pid: True)
        update = _stopped_row_with_member(reg)
        with self.assertRaises(StopUnattested):
            reg.put(update)
        stored = reg.get("r")
        assert stored is not None
        self.assertEqual(stored["members"], [{"member_id": "late", "pid": 9}])
        self.assertIs(reg.process_alive(9), True)
        self.assertEqual(stored["state"], "stopped")

    def test_an_already_stopped_put_does_not_drop_a_member_when_the_probe_fails(self) -> None:
        def deny(_pid: int) -> bool:
            raise PermissionError("proc")

        reg = InMemoryRunRegistry(processes=deny)
        update = _stopped_row_with_member(reg)
        with self.assertRaises(LivenessUnknown):
            reg.put(update)
        stored = reg.get("r")
        assert stored is not None
        self.assertEqual(stored["members"], [{"member_id": "late", "pid": 9}])
        self.assertEqual(stored["state"], "stopped")

    def test_an_already_stopped_put_drops_a_member_that_is_gone(self) -> None:
        reg = InMemoryRunRegistry(processes=lambda _pid: False)
        update = _stopped_row_with_member(reg)
        stored = reg.put(update)
        self.assertEqual(stored["members"], [])
        self.assertEqual(stored["state"], "stopped")
        self.assertEqual(stored["engine"], "other")
        self.assertEqual(reg.get("r")["members"], [])


class _ForeignStopOnEmptyCgroup(InMemoryRunRegistry):
    """A stopped put with no cgroup stores a live member and returns another cgroup's stop.

    ``get`` is the stored row. The dict ``put`` returns is not that row.
    """

    def __init__(self) -> None:
        super().__init__(processes=lambda _pid: True)

    def put(self, record: dict):
        cgroup = record.get("cgroup")
        if record.get("state") == "stopped" and not (isinstance(cgroup, str) and cgroup):
            current = self._runs.get(record["run_id"])
            assert current is not None
            planted = dict(current)
            planted["state"] = "running"
            planted["cgroup"] = "/cg/real"
            planted["members"] = [{"member_id": "late", "pid": 9}]
            planted["stop"] = None
            planted["stop_attestation"] = None
            self._runs[record["run_id"]] = planted
            return {
                "run_id": record["run_id"],
                "tenant": current.get("tenant"),
                "engine": current.get("engine"),
                "state": "stopped",
                "cgroup": "/cg/other",
                "cgroup_marker": "cg-other",
                "generation": 4,
                "members": [],
                "stop_attestation": {
                    "proof": "empty",
                    "cgroup": "/cg/other",
                    "cgroup_marker": "cg-other",
                    "member_ids": [],
                    "generation": 4,
                },
            }
        return super().put(record)


class _SameCgroupStopHidesMember(InMemoryRunRegistry):
    """A stopped put returns a recorded stop of the written cgroup and stores late/9 alive."""

    def __init__(self) -> None:
        super().__init__(processes=lambda _pid: True)

    def put(self, record: dict):
        if record.get("state") == "stopped":
            current = self._runs.get(record["run_id"])
            assert current is not None
            planted = dict(current)
            planted["state"] = "running"
            planted["members"] = [{"member_id": "late", "pid": 9}]
            planted["stop_attestation"] = None
            self._runs[record["run_id"]] = planted
            cgroup = record.get("cgroup")
            marker = record.get("cgroup_marker")
            generation = record.get("generation")
            if not isinstance(generation, int):
                generation = 1
            return {
                "run_id": record["run_id"],
                "tenant": current.get("tenant"),
                "engine": current.get("engine"),
                "state": "stopped",
                "cgroup": cgroup,
                "cgroup_marker": marker,
                "generation": generation,
                "members": [],
                "stop_attestation": {
                    "proof": "empty",
                    "cgroup": cgroup,
                    "cgroup_marker": marker,
                    "member_ids": [],
                    "generation": generation,
                },
            }
        return super().put(record)


def _stopped_row_without_cgroup(reg: InMemoryRunRegistry, cgroup: str | None) -> dict:
    """A stopped row whose proof names no cgroup. Members are empty, so liveness is clear."""
    reg.put({"run_id": "r1", "tenant": "t", "engine": "e", "state": "running",
             "cgroup": "/cg/r1", "stop": None, "start_key": None})
    row = reg._runs["r1"]
    row["state"] = "stopped"
    row["cgroup"] = cgroup
    row["cgroup_marker"] = None
    row["members"] = []
    row["stop_attestation"] = {
        "proof": "empty", "cgroup": None, "cgroup_marker": None,
        "member_ids": [], "generation": row["generation"],
    }
    fresh = reg.get("r1")
    assert fresh is not None
    return fresh


class ReviewDeepSixTest(unittest.TestCase):
    def test_an_empty_written_cgroup_does_not_accept_a_foreign_recorded_stop(self) -> None:
        # The row is stopped, the proof names no cgroup, and the id is in _launched
        # with no cgroup. Stop must not call request_stop and must not put: that
        # put only bumps generation, and a registry that answered it with another
        # cgroup's stop must not be consulted. The row stays stopped, with no
        # member planted. CLI must not exit 0, and MCP must set isError. Each
        # surface is the first stop on its own registry.
        for written in (None, ""):
            with self.subTest(written=written):
                reg = _ForeignStopOnEmptyCgroup()
                api, _, kill, _ = make_api(FakeKillTree([EMPTY]), registry=reg)
                fresh = _stopped_row_without_cgroup(reg, written)
                before = fresh["generation"]
                api._launched["r1"] = dict(fresh)
                api._launched["r1"]["cgroup"] = written
                out, err = io.StringIO(), io.StringIO()
                code = cli.main(["stop", "r1"], api, out, err)
                res = json.loads(out.getvalue())
                self.assertEqual(res["code"], a.PENDING)
                self.assertEqual(res["state"], "pending")
                self.assertEqual(res["reason"], "cgroup_unknown")
                self.assertNotEqual(res["code"], a.STOPPED)
                self.assertIsNot(res.get("repeat"), True)
                self.assertNotEqual(code, 0)
                self.assertEqual(code, cli.EXIT_PENDING)
                self.assertNotIn(res["code"], a.SUCCESS_CODES)
                self.assertEqual(kill.requests, [])
                stored = reg.get("r1")
                assert stored is not None
                self.assertEqual(stored["generation"], before)
                self.assertEqual(stored["state"], "stopped")
                self.assertEqual(stored["members"], [])
                self.assertFalse(a._recorded_stop(stored))

                reg = _ForeignStopOnEmptyCgroup()
                api, _, kill, _ = make_api(FakeKillTree([EMPTY]), registry=reg)
                fresh = _stopped_row_without_cgroup(reg, written)
                before = fresh["generation"]
                api._launched["r1"] = dict(fresh)
                api._launched["r1"]["cgroup"] = written
                tool = mcp_tools.call_tool(api, "recursion_run_stop", {"run_id": "r1"})
                self.assertTrue(tool["isError"])
                self.assertEqual(tool["structuredContent"]["code"], a.PENDING)
                self.assertEqual(tool["structuredContent"]["reason"], "cgroup_unknown")
                self.assertNotEqual(tool["structuredContent"]["code"], a.STOPPED)
                self.assertEqual(kill.requests, [])
                stored = reg.get("r1")
                assert stored is not None
                self.assertEqual(stored["generation"], before)
                self.assertEqual(stored["state"], "stopped")
                self.assertEqual(stored["members"], [])

    def test_a_same_cgroup_snapshot_is_checked_against_the_stored_members(self) -> None:
        # put returns a recorded stop of the cgroup that was written, with no
        # members. The stored row still lists late/9 alive. The returned members
        # are not that row.
        reg = _SameCgroupStopHidesMember()
        api, _, _, _ = make_api(FakeKillTree([EMPTY]), registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["stop", run], api, out, err)
        res = json.loads(out.getvalue())
        self.assertEqual(res["code"], a.PENDING)
        self.assertEqual(res["state"], "pending")
        self.assertEqual(res["reason"], "member_alive")
        self.assertNotEqual(res["code"], a.STOPPED)
        self.assertNotEqual(code, 0)
        self.assertEqual(code, cli.EXIT_PENDING)
        self.assertTrue(mcp_tools.call_tool(api, "recursion_run_stop", {"run_id": run})["isError"])
        stored = reg.get(run)
        assert stored is not None
        self.assertNotEqual(stored["state"], "stopped")
        self.assertEqual(stored["members"], [{"member_id": "late", "pid": 9}])
        self.assertIs(reg.process_alive(9), True)

    def test_repeated_pending_calls_do_not_bump_generation(self) -> None:
        # A stopped row whose proof names no cgroup, with no members. Each
        # pending answer is cgroup_unknown. A put here only bumps generation
        # and rewrites the stop dict.
        reg = InMemoryRunRegistry(processes=lambda _pid: False)
        _stopped_row_without_cgroup(reg, None)
        api, _, _, _ = make_api(registry=reg)
        before = reg.get("r1")["generation"]
        for _ in range(4):
            res = api.call("run.stop", {"run_id": "r1"})
            self.assertEqual(res["code"], a.PENDING)
            self.assertEqual(res["state"], "pending")
            self.assertEqual(res["reason"], "cgroup_unknown")
            self.assertNotEqual(res["code"], a.STOPPED)
            self.assertNotIn(res["code"], a.SUCCESS_CODES)
        stored = reg.get("r1")
        assert stored is not None
        self.assertEqual(stored["generation"], before)
        self.assertEqual(stored["state"], "stopped")
        self.assertIsNone(stored.get("cgroup"))
        self.assertEqual(stored["members"], [])
        self.assertFalse(a._recorded_stop(stored))

    def test_an_ahead_proof_cannot_become_stopped_by_catching_up(self) -> None:
        # The proof generation is ahead of the row. A stopped put keeps that
        # proof and bumps the row. Once the row generation catches the proof,
        # the old proof must not answer stopped.
        reg = InMemoryRunRegistry(processes=lambda _pid: False)
        reg.put({"run_id": "r1", "tenant": "t", "engine": "e", "state": "running",
                 "cgroup": "/cg/r1", "stop": None, "start_key": None})
        row = reg._runs["r1"]
        row["state"] = "stopped"
        row["members"] = []
        ahead = row["generation"] + 4
        row["stop_attestation"] = {
            "proof": "empty",
            "cgroup": row["cgroup"],
            "cgroup_marker": row.get("cgroup_marker"),
            "member_ids": [],
            "generation": ahead,
        }
        api, _, _, _ = make_api(FakeKillTree([EMPTY]), registry=reg)
        start = reg.get("r1")["generation"]
        self.assertGreater(ahead, start)
        self.assertFalse(a._recorded_stop(reg.get("r1")))
        for _ in range(6):
            res = api.call("run.stop", {"run_id": "r1"})
            self.assertEqual(res["code"], a.PENDING)
            self.assertEqual(res["state"], "pending")
            self.assertNotEqual(res["code"], a.STOPPED)
            self.assertIsNot(res.get("repeat"), True)
            self.assertNotIn(res["code"], a.SUCCESS_CODES)
        stored = reg.get("r1")
        assert stored is not None
        self.assertEqual(stored["generation"], start)
        self.assertGreater(stored["stop_attestation"]["generation"], stored["generation"])
        self.assertFalse(a._recorded_stop(stored))
        self.assertEqual(stored["state"], "stopped")
        self.assertEqual(stored["members"], [])
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["stop", "r1"], api, out, err)
        self.assertNotEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["code"], a.PENDING)
        self.assertTrue(mcp_tools.call_tool(api, "recursion_run_stop", {"run_id": "r1"})["isError"])


def _attested_stop_snapshot(record: dict, current: dict, *, cgroup_field: str) -> dict:
    """A stopped dict whose proof names the cgroup ``record`` was writing.

    ``cgroup_field`` is ``keep`` (the row cgroup equals that path), ``empty``,
    ``none``, or ``absent`` (the key is left off).
    """
    generation = record.get("generation")
    if not isinstance(generation, int):
        generation = current.get("generation") if isinstance(current.get("generation"), int) else 1
    written = record.get("cgroup")
    marker = record.get("cgroup_marker")
    snap = {
        "run_id": record["run_id"],
        "tenant": current.get("tenant"),
        "engine": current.get("engine"),
        "state": "stopped",
        "cgroup_marker": marker,
        "generation": generation,
        "members": [],
        "stop_attestation": {
            "proof": "empty",
            "cgroup": written,
            "cgroup_marker": marker,
            "member_ids": [],
            "generation": generation,
        },
    }
    if cgroup_field == "empty":
        snap["cgroup"] = ""
    elif cgroup_field == "none":
        snap["cgroup"] = None
    elif cgroup_field == "keep":
        snap["cgroup"] = written
    return snap


class _UnreadableAfterStoppedPut(InMemoryRunRegistry):
    """A stopped put stores running late/9 and returns a recorded-stop snapshot.

    Later reads either raise ``OSError`` or return None. The snapshot is not the row.
    """

    def __init__(self, mode: str) -> None:
        super().__init__(processes=lambda _pid: True)
        self.mode = mode
        self.blind = False
        self.hidden: dict | None = None
        self.stopped_puts = 0

    def put(self, record: dict):
        if record.get("state") == "stopped":
            self.stopped_puts += 1
            current = self._runs.get(record["run_id"])
            assert current is not None
            planted = dict(current)
            planted["state"] = "running"
            planted["members"] = [{"member_id": "late", "pid": 9}]
            planted["stop"] = None
            planted["stop_attestation"] = None
            self._runs[record["run_id"]] = planted
            self.hidden = planted
            self.blind = True
            return _attested_stop_snapshot(record, current, cgroup_field="keep")
        return super().put(record)

    def get(self, run_id: str):
        row = super().get(run_id)
        if self.blind:
            self.hidden = row
            if self.mode == "oserror":
                raise OSError("disk")
            return None
        return row


class _BlankCgroupSnapshot(InMemoryRunRegistry):
    """A stopped put stores running late/9 and returns a stop whose row cgroup is blank.

    The attestation still names the cgroup that was written. Later reads return that
    same snapshot, so a check that only trusts the proof does not see late/9.
    """

    def __init__(self, cgroup_field: str) -> None:
        super().__init__(processes=lambda _pid: True)
        self._cgroup_field = cgroup_field
        self._lie: dict | None = None
        self.hidden: dict | None = None
        self.stopped_puts = 0

    def put(self, record: dict):
        if record.get("state") == "stopped":
            self.stopped_puts += 1
            current = self._runs.get(record["run_id"])
            assert current is not None
            planted = dict(current)
            planted["state"] = "running"
            planted["members"] = [{"member_id": "late", "pid": 9}]
            planted["stop"] = None
            planted["stop_attestation"] = None
            self._runs[record["run_id"]] = planted
            self.hidden = planted
            self._lie = _attested_stop_snapshot(record, current, cgroup_field=self._cgroup_field)
            return _attested_stop_snapshot(record, current, cgroup_field=self._cgroup_field)
        return super().put(record)

    def get(self, run_id: str):
        if self._lie is not None:
            self.hidden = super().get(run_id)
            return _attested_stop_snapshot(
                {"run_id": run_id, "cgroup": self._lie["stop_attestation"]["cgroup"],
                 "cgroup_marker": self._lie.get("cgroup_marker"),
                 "generation": self._lie.get("generation")},
                self._lie, cgroup_field=self._cgroup_field)
        return super().get(run_id)


class ReviewDeepSevenTest(unittest.TestCase):
    def test_an_unreadable_reread_after_a_stopped_put_is_not_stopped(self) -> None:
        # put returns a recorded stop of the cgroup that was written. The following
        # get raises, or returns None, and the stored row is still running with
        # late/9 alive. Copying the put snapshot answers stopped (CLI exit 0, MCP
        # isError false). The re-read has to succeed. Each surface is the first
        # stop on its own registry. A second stop does not kill again.
        for mode in ("oserror", "none"):
            with self.subTest(mode=mode):
                reg = _UnreadableAfterStoppedPut(mode)
                api, _, kill, _ = make_api(FakeKillTree([EMPTY]), registry=reg)
                run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
                res = api.call("run.stop", {"run_id": run})
                self.assertEqual(res["code"], a.PENDING)
                self.assertEqual(res["state"], "pending")
                self.assertEqual(res["reason"], "stop_unverified")
                self.assertIs(res["recorded"], False)
                self.assertNotEqual(res["code"], a.STOPPED)
                self.assertIsNot(res.get("repeat"), True)
                self.assertNotIn(res["code"], a.SUCCESS_CODES)
                self.assertEqual(len(kill.requests), 1)
                self.assertEqual(reg.stopped_puts, 1)
                assert reg.hidden is not None
                self.assertEqual(reg.hidden["state"], "running")
                self.assertEqual(reg.hidden["members"], [{"member_id": "late", "pid": 9}])
                self.assertIs(reg.process_alive(9), True)
                again = api.call("run.stop", {"run_id": run})
                self.assertNotEqual(again["code"], a.STOPPED)
                self.assertNotIn(again["code"], a.SUCCESS_CODES)
                self.assertEqual(len(kill.requests), 1)
                self.assertEqual(reg.stopped_puts, 1)

                reg = _UnreadableAfterStoppedPut(mode)
                api, _, kill, _ = make_api(FakeKillTree([EMPTY]), registry=reg)
                run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
                out, err = io.StringIO(), io.StringIO()
                code = cli.main(["stop", run], api, out, err)
                body = json.loads(out.getvalue())
                self.assertEqual(body["code"], a.PENDING)
                self.assertEqual(body["reason"], "stop_unverified")
                self.assertNotEqual(code, 0)
                self.assertEqual(code, cli.EXIT_PENDING)
                self.assertEqual(len(kill.requests), 1)
                assert reg.hidden is not None
                self.assertEqual(reg.hidden["members"], [{"member_id": "late", "pid": 9}])

                reg = _UnreadableAfterStoppedPut(mode)
                api, _, kill, _ = make_api(FakeKillTree([EMPTY]), registry=reg)
                run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
                tool = mcp_tools.call_tool(api, "recursion_run_stop", {"run_id": run})
                self.assertTrue(tool["isError"])
                self.assertEqual(tool["structuredContent"]["code"], a.PENDING)
                self.assertEqual(tool["structuredContent"]["reason"], "stop_unverified")
                self.assertNotEqual(tool["structuredContent"]["code"], a.STOPPED)
                self.assertEqual(len(kill.requests), 1)

    def test_a_returned_snapshot_with_a_blank_cgroup_is_not_the_written_stop(self) -> None:
        # The attestation names the cgroup that was written. The returned row's
        # cgroup is "", None, or missing. That is not the written cgroup. The
        # stored row is running with late/9. CLI must not exit 0, and MCP must
        # set isError. One kill, and the stopped put is not repeated.
        for field in ("empty", "none", "absent"):
            with self.subTest(field=field):
                written = {"run_id": "r", "state": "stopped", "cgroup": "/cg/written",
                           "cgroup_marker": "cg-1", "generation": 2}
                current = {"tenant": "t", "engine": "e", "generation": 2}
                stored = _attested_stop_snapshot(written, current, cgroup_field=field)
                self.assertFalse(a._stop_snapshot_ok(written, stored))

                reg = _BlankCgroupSnapshot(field)
                api, _, kill, _ = make_api(FakeKillTree([EMPTY]), registry=reg)
                run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
                res = api.call("run.stop", {"run_id": run})
                self.assertEqual(res["code"], a.PENDING)
                self.assertEqual(res["state"], "pending")
                self.assertEqual(res["reason"], "stop_unverified")
                self.assertIs(res.get("recorded"), False)
                self.assertNotEqual(res["code"], a.STOPPED)
                self.assertIsNot(res.get("repeat"), True)
                self.assertNotIn(res["code"], a.SUCCESS_CODES)
                self.assertEqual(len(kill.requests), 1)
                self.assertEqual(reg.stopped_puts, 1)
                assert reg.hidden is not None
                self.assertEqual(reg.hidden["state"], "running")
                self.assertEqual(reg.hidden["members"], [{"member_id": "late", "pid": 9}])
                self.assertIs(reg.process_alive(9), True)
                again = api.call("run.stop", {"run_id": run})
                self.assertEqual(len(kill.requests), 1)
                self.assertEqual(reg.stopped_puts, 1)

                reg = _BlankCgroupSnapshot(field)
                api, _, _, _ = make_api(FakeKillTree([EMPTY]), registry=reg)
                run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
                out, err = io.StringIO(), io.StringIO()
                code = cli.main(["stop", run], api, out, err)
                body = json.loads(out.getvalue())
                self.assertEqual(body["code"], a.PENDING)
                self.assertEqual(body["reason"], "stop_unverified")
                self.assertNotEqual(code, 0)
                self.assertEqual(code, cli.EXIT_PENDING)
                self.assertNotIn(body["code"], a.SUCCESS_CODES)

                reg = _BlankCgroupSnapshot(field)
                api, _, _, _ = make_api(FakeKillTree([EMPTY]), registry=reg)
                run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
                tool = mcp_tools.call_tool(api, "recursion_run_stop", {"run_id": run})
                self.assertTrue(tool["isError"])
                self.assertEqual(tool["structuredContent"]["code"], a.PENDING)
                self.assertEqual(tool["structuredContent"]["reason"], "stop_unverified")
                self.assertNotEqual(tool["structuredContent"]["code"], a.STOPPED)

    def test_a_missing_cgroup_key_is_pending_and_does_not_raise(self) -> None:
        # request_stop(rec["cgroup"]) raises KeyError when the key is absent, and
        # each pending put bumps generation. Absent is cgroup_unknown. The first
        # call records that intent. Later calls do not put.
        reg = InMemoryRunRegistry(processes=lambda _pid: False)
        reg.put({"run_id": "r", "tenant": "t", "engine": "e", "state": "running",
                 "cgroup": "/cg/r", "stop": None, "start_key": None})
        reg._runs["r"].pop("cgroup", None)
        api, _, kill, _ = make_api(FakeKillTree([EMPTY]), registry=reg)
        api._launched["r"] = {"run_id": "r", "tenant": "t", "engine": "e", "state": "running",
                              "stop": None}
        before = reg.get("r")["generation"]
        for _ in range(4):
            res = api.call("run.stop", {"run_id": "r"})
            self.assertEqual(res["code"], a.PENDING)
            self.assertEqual(res["state"], "pending")
            self.assertEqual(res["reason"], "cgroup_unknown")
            self.assertNotIn("KeyError", res["reason"])
            self.assertNotEqual(res["code"], a.STOPPED)
            self.assertNotIn(res["code"], a.SUCCESS_CODES)
        stored = reg.get("r")
        assert stored is not None
        self.assertEqual(stored["generation"], before + 1)
        self.assertEqual(kill.requests, [])
        self.assertEqual(stored["stop"]["reason"], "cgroup_unknown")
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["stop", "r"], api, out, err)
        self.assertEqual(json.loads(out.getvalue())["reason"], "cgroup_unknown")
        self.assertNotEqual(code, 0)
        self.assertEqual(code, cli.EXIT_PENDING)
        self.assertEqual(reg.get("r")["generation"], before + 1)
        self.assertTrue(mcp_tools.call_tool(api, "recursion_run_stop", {"run_id": "r"})["isError"])

    def test_a_launched_stopped_row_with_no_cgroup_does_not_bump_generation(self) -> None:
        # The handle and the row have no cgroup. Each pending call used to put the
        # already-stopped row and bump generation, or call request_stop on the
        # empty path. Nothing material changes, so there is no put and no kill.
        for kind in ("none", "empty", "absent"):
            with self.subTest(kind=kind):
                reg = InMemoryRunRegistry(processes=lambda _pid: False)
                reg.put({"run_id": "r1", "tenant": "t", "engine": "e", "state": "running",
                         "cgroup": "/cg/r1", "stop": None, "start_key": None})
                row = reg._runs["r1"]
                row["state"] = "stopped"
                row["members"] = []
                row["cgroup_marker"] = None
                row["stop_attestation"] = {
                    "proof": "empty", "cgroup": None, "cgroup_marker": None,
                    "member_ids": [], "generation": row["generation"],
                }
                if kind == "absent":
                    row.pop("cgroup", None)
                elif kind == "empty":
                    row["cgroup"] = ""
                else:
                    row["cgroup"] = None
                api, _, kill, _ = make_api(FakeKillTree([EMPTY]), registry=reg)
                held = {"run_id": "r1", "tenant": "t", "engine": "e", "state": "stopped",
                        "stop": None, "members": []}
                if kind != "absent":
                    held["cgroup"] = row.get("cgroup")
                api._launched["r1"] = held
                before = reg.get("r1")["generation"]
                for _ in range(4):
                    res = api.call("run.stop", {"run_id": "r1"})
                    self.assertEqual(res["code"], a.PENDING)
                    self.assertEqual(res["state"], "pending")
                    self.assertEqual(res["reason"], "cgroup_unknown")
                    self.assertNotEqual(res["code"], a.STOPPED)
                    self.assertIsNot(res.get("repeat"), True)
                    self.assertNotIn(res["code"], a.SUCCESS_CODES)
                stored = reg.get("r1")
                assert stored is not None
                self.assertEqual(stored["generation"], before)
                self.assertEqual(stored["state"], "stopped")
                self.assertEqual(stored["members"], [])
                self.assertEqual(kill.requests, [])
                self.assertFalse(a._recorded_stop(stored))

    def test_an_older_proof_repeats_stopped_without_a_kill(self) -> None:
        # Confirmed. The proof generation is older than the row, it names the
        # cgroup, and no member is listed. Repeat stopped, and do not kill.
        reg = InMemoryRunRegistry(processes=lambda _pid: False)
        reg.put({"run_id": "r1", "tenant": "t", "engine": "e", "state": "running",
                 "cgroup": "/cg/r1", "stop": None, "start_key": None})
        row = reg._runs["r1"]
        row["state"] = "stopped"
        row["members"] = []
        row["generation"] = row["generation"] + 2
        row["stop_attestation"] = {
            "proof": "empty",
            "cgroup": row["cgroup"],
            "cgroup_marker": row.get("cgroup_marker"),
            "member_ids": [],
            "generation": row["generation"] - 2,
        }
        api, _, kill, _ = make_api(FakeKillTree([EMPTY]), registry=reg)
        before = reg.get("r1")["generation"]
        self.assertTrue(a._recorded_stop(reg.get("r1")))
        for _ in range(3):
            res = api.call("run.stop", {"run_id": "r1"})
            self.assertEqual(res["code"], a.STOPPED)
            self.assertEqual(res["state"], "stopped")
            self.assertIs(res["repeat"], True)
            self.assertIsNot(res.get("recorded"), False)
        self.assertEqual(kill.requests, [])
        self.assertEqual(kill.confirms, [])
        self.assertEqual(reg.get("r1")["generation"], before)

    def test_two_apis_do_not_bump_an_ahead_proof_with_a_live_member(self) -> None:
        # Confirmed. Two ControlAPIs share the registry. The proof is ahead of the
        # row and late/9 is alive. Pending, member_alive, and the generation stays.
        reg = InMemoryRunRegistry(processes=lambda _pid: True)
        reg.put({"run_id": "r1", "tenant": "t", "engine": "e", "state": "running",
                 "cgroup": "/cg/r1", "stop": None, "start_key": None})
        row = reg._runs["r1"]
        row["state"] = "stopped"
        row["members"] = [{"member_id": "late", "pid": 9}]
        row["stop_attestation"] = {
            "proof": "empty",
            "cgroup": row["cgroup"],
            "cgroup_marker": row.get("cgroup_marker"),
            "member_ids": ["late"],
            "generation": row["generation"] + 4,
        }
        before = reg.get("r1")["generation"]
        self.assertFalse(a._recorded_stop(reg.get("r1")))
        for _ in range(2):
            api, _, kill, _ = make_api(FakeKillTree([EMPTY]), registry=reg)
            for _call in range(3):
                res = api.call("run.stop", {"run_id": "r1"})
                self.assertEqual(res["code"], a.PENDING)
                self.assertEqual(res["state"], "pending")
                self.assertEqual(res["reason"], "member_alive")
                self.assertIs(res["recorded"], False)
                self.assertNotEqual(res["code"], a.STOPPED)
                self.assertNotIn(res["code"], a.SUCCESS_CODES)
            self.assertEqual(kill.requests, [])
        stored = reg.get("r1")
        assert stored is not None
        self.assertEqual(stored["generation"], before)
        self.assertEqual(stored["members"], [{"member_id": "late", "pid": 9}])
        self.assertIs(reg.process_alive(9), True)
        self.assertEqual(stored["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
