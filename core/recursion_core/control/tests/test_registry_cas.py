"""Deterministic races for registry compare-and-swap, attested stop, and owner locks.

Writers are threads with a barrier (or an event that orders the commits). Each call mints its own
owner token. Processes here are a fake process table: a pid is alive only while the test says so.
Clocks are fake. Nothing starts a provider, a cgroup, or a real workload.
"""

from __future__ import annotations

import threading
import unittest

from recursion_core.control import api as a
from recursion_core.control.registry import (
    EMPTY, InMemoryRunRegistry, JoinRefused, LockNotHeld, LockOrderError, OwnerLockTable,
    StaleGeneration, StopUnattested,
)
from recursion_core.control.tests.fakes import FakeKillTree, make_api


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeProcesses:
    """Pids the test marks alive. ``exit`` is the only way a pid becomes gone."""

    def __init__(self) -> None:
        self._alive: set[int] = set()
        self._next = 1

    def spawn(self) -> int:
        pid = self._next
        self._next += 1
        self._alive.add(pid)
        return pid

    def exit(self, pid: int) -> None:
        self._alive.discard(pid)

    def is_alive(self, pid: int) -> bool:
        return pid in self._alive


def _running(run_id: str = "run-1") -> dict:
    return {"run_id": run_id, "tenant": "t", "engine": "codex", "state": "running",
            "cgroup": f"/cg/{run_id}", "stop": None, "start_key": None}


def _attest(rec: dict, member_ids: list[str]) -> dict:
    return {"proof": "empty", "cgroup": rec["cgroup"], "member_ids": member_ids,
            "generation": rec["generation"]}


class CompareAndSwapTest(unittest.TestCase):
    def test_stale_update_cannot_overwrite_a_stop(self) -> None:
        # The stop commits first. The update still holds the generation it read and must lose.
        reg = InMemoryRunRegistry()
        reg.put(_running())
        snap = reg.get("run-1")
        assert snap is not None
        ready, committed = threading.Barrier(2), threading.Event()
        errors: list[BaseException] = []

        def update() -> None:
            ready.wait(5)
            committed.wait(5)
            try:
                stale = dict(snap)
                stale["engine"] = "other"
                reg.compare_and_swap("run-1", snap["generation"], stale)
            except BaseException as err:  # the assertion below names the type
                errors.append(err)

        thread = threading.Thread(target=update)
        thread.start()
        ready.wait(5)  # the update has the pre-stop snapshot and is waiting to write
        stopped = dict(snap)
        stopped["state"] = "stopped"
        stopped["stop_attestation"] = _attest(snap, [])
        stored = reg.compare_and_swap("run-1", snap["generation"], stopped)
        committed.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StaleGeneration)
        final = reg.get("run-1")
        assert final is not None
        self.assertEqual(final["state"], "stopped")
        self.assertEqual(final["generation"], stored["generation"])
        self.assertEqual(final["stop_attestation"]["proof"], "empty")
        self.assertEqual(final["engine"], "codex")

    def test_two_writers_of_one_generation_commit_exactly_once(self) -> None:
        reg = InMemoryRunRegistry()
        reg.put(_running())
        snap = reg.get("run-1")
        assert snap is not None
        barrier = threading.Barrier(2)
        reg.commit_gate = lambda _snapshot: barrier.wait(5)
        outcomes: list[str] = []

        def write(state: str) -> None:
            rec = dict(snap)
            rec["state"] = state
            if state == "stopped":
                rec["stop_attestation"] = _attest(snap, [])
            try:
                reg.compare_and_swap("run-1", snap["generation"], rec)
                outcomes.append("ok")
            except StaleGeneration:
                outcomes.append("stale")

        threads = [threading.Thread(target=write, args=(state,)) for state in ("running", "stopped")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(outcomes), ["ok", "stale"])
        final = reg.get("run-1")
        assert final is not None
        self.assertEqual(final["generation"], snap["generation"] + 1)
        if final["state"] == "stopped":
            self.assertEqual(final["stop_attestation"]["proof"], "empty")
            self.assertEqual(final["members"], [])
        else:
            self.assertIsNone(final["stop_attestation"])
            self.assertEqual(final["state"], "running")

    def test_blind_put_without_a_generation_does_not_overwrite(self) -> None:
        reg = InMemoryRunRegistry()
        reg.put(_running())
        snap = reg.get("run-1")
        assert snap is not None
        stopped = dict(snap)
        stopped["state"] = "stopped"
        stopped["stop_attestation"] = _attest(snap, [])
        reg.put(stopped)
        with self.assertRaises(StaleGeneration):
            reg.put({"run_id": "run-1", "tenant": "t", "state": "running", "cgroup": "/cg/run-1"})
        self.assertEqual(reg.get("run-1")["state"], "stopped")


class AttestedStopTest(unittest.TestCase):
    def test_stopped_without_attestation_is_refused(self) -> None:
        reg = InMemoryRunRegistry()
        reg.put(_running())
        rec = reg.get("run-1")
        assert rec is not None
        rec["state"] = "stopped"
        rec.pop("stop_attestation", None)
        with self.assertRaises(StopUnattested):
            reg.put(rec)
        self.assertEqual(reg.get("run-1")["state"], "running")

    def test_live_member_blocks_attestation_and_a_dead_one_does_not(self) -> None:
        procs = FakeProcesses()
        reg = InMemoryRunRegistry(processes=procs.is_alive)
        reg.put(_running())
        pid = procs.spawn()
        joined = reg.join_member("run-1", reg.get("run-1")["generation"], "m1", pid)
        live = dict(joined)
        live["state"] = "stopped"
        live["stop_attestation"] = _attest(joined, ["m1"])
        with self.assertRaises(StopUnattested):
            reg.put(live)
        self.assertEqual(reg.get("run-1")["state"], "running")
        self.assertEqual(reg.get("run-1")["members"], [{"member_id": "m1", "pid": pid}])
        procs.exit(pid)
        gone = reg.get("run-1")
        assert gone is not None
        gone["state"] = "stopped"
        gone["stop_attestation"] = _attest(gone, ["m1"])
        stored = reg.compare_and_swap("run-1", gone["generation"], gone)
        self.assertEqual(stored["state"], "stopped")
        self.assertEqual(stored["members"], [])  # proved gone, so the row is not left behind
        self.assertEqual(stored["stop_attestation"]["member_ids"], ["m1"])

    def test_api_stop_records_attestation_at_the_fake_clock(self) -> None:
        clock = FakeClock()
        api, reg, _, _ = make_api(FakeKillTree([EMPTY]))
        api._clock = clock
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        before = reg.get(run)["generation"]
        clock.advance(5)
        res = api.call("run.stop", {"run_id": run})
        final = reg.get(run)
        self.assertEqual(res["code"], a.STOPPED)
        self.assertEqual(final["state"], "stopped")
        self.assertEqual(final["stop"]["requested_at"], 1005.0)
        self.assertEqual(final["stop_attestation"]["proof"], "empty")
        self.assertEqual(final["stop_attestation"]["generation"], before)
        self.assertEqual(final["stop_attestation"]["member_ids"], [])
        self.assertEqual(final["members"], [])
        self.assertEqual(final["generation"], before + 1)

    def test_confirm_empty_with_a_live_member_stays_pending(self) -> None:
        procs = FakeProcesses()
        reg = InMemoryRunRegistry(processes=procs.is_alive)
        api, _, _, _ = make_api(FakeKillTree([EMPTY]), registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        reg.join_member(run, reg.get(run)["generation"], "m1", procs.spawn())
        res = api.call("run.stop", {"run_id": run})
        self.assertEqual((res["code"], res["reason"]), (a.PENDING, "member_alive"))
        self.assertNotEqual(reg.get(run)["state"], "stopped")
        self.assertIsNone(reg.get(run)["stop_attestation"])


class MemberJoinTest(unittest.TestCase):
    def test_join_after_attested_stop_is_refused(self) -> None:
        procs = FakeProcesses()
        reg = InMemoryRunRegistry(processes=procs.is_alive)
        reg.put(_running())
        snap = reg.get("run-1")
        assert snap is not None
        snap["state"] = "stopped"
        snap["stop_attestation"] = _attest(snap, [])
        reg.put(snap)
        with self.assertRaises(JoinRefused) as caught:
            reg.join_member("run-1", reg.get("run-1")["generation"], "late", procs.spawn())
        self.assertEqual(caught.exception.detail, "stopped")
        final = reg.get("run-1")
        self.assertEqual(final["state"], "stopped")
        self.assertEqual(final["members"], [])

    def test_join_during_confirm_keeps_the_stop_unattested(self) -> None:
        # Stop has already read the record. The join commits before confirm returns, so the
        # empty attestation is stale and the live member stays.
        procs = FakeProcesses()
        reg = InMemoryRunRegistry(processes=procs.is_alive)
        in_confirm, release = threading.Event(), threading.Event()

        class Blocking(FakeKillTree):
            def __init__(self) -> None:
                super().__init__([EMPTY])

            def confirm(self, cgroup: str, timeout_s: float) -> str:
                in_confirm.set()
                if not release.wait(5):
                    raise TimeoutError("confirm released")
                return EMPTY

        api, _, _, _ = make_api(Blocking(), registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        pid = procs.spawn()
        out: list[dict] = []
        thread = threading.Thread(target=lambda: out.append(api.call("run.stop", {"run_id": run})))
        thread.start()
        self.assertTrue(in_confirm.wait(5))
        reg.join_member(run, reg.get(run)["generation"], "late", pid)
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual((out[0]["code"], out[0]["reason"]), (a.PENDING, "member_alive"))
        final = reg.get(run)
        self.assertNotEqual(final["state"], "stopped")
        self.assertEqual(final["members"], [{"member_id": "late", "pid": pid}])

    def test_join_and_stop_of_one_generation_leave_no_stopped_member(self) -> None:
        reg = InMemoryRunRegistry()
        reg.put(_running())
        snap = reg.get("run-1")
        assert snap is not None
        barrier = threading.Barrier(2)
        reg.commit_gate = lambda _snapshot: barrier.wait(5)
        errors: list[BaseException] = []

        def stop() -> None:
            rec = dict(snap)
            rec["state"] = "stopped"
            rec["stop_attestation"] = _attest(snap, [])
            try:
                reg.compare_and_swap("run-1", snap["generation"], rec)
            except BaseException as err:
                errors.append(err)

        def join() -> None:
            try:
                reg.join_member("run-1", snap["generation"], "late", 9)
            except BaseException as err:
                errors.append(err)

        threads = [threading.Thread(target=stop), threading.Thread(target=join)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StaleGeneration)
        final = reg.get("run-1")
        assert final is not None
        self.assertFalse(final["state"] == "stopped" and final["members"])
        self.assertEqual(final["generation"], snap["generation"] + 1)

    def test_update_during_confirm_does_not_drop_the_stop(self) -> None:
        reg = InMemoryRunRegistry()
        in_confirm, release = threading.Event(), threading.Event()

        class Blocking(FakeKillTree):
            def confirm(self, cgroup: str, timeout_s: float) -> str:
                in_confirm.set()
                release.wait(5)
                return EMPTY

        api, _, _, _ = make_api(Blocking([EMPTY]), registry=reg)
        run = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})["run"]["run_id"]
        out: list[dict] = []
        thread = threading.Thread(target=lambda: out.append(api.call("run.stop", {"run_id": run})))
        thread.start()
        self.assertTrue(in_confirm.wait(5))
        snap = reg.get(run)
        snap["engine"] = "other"
        reg.compare_and_swap(run, snap["generation"], snap)
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        final = reg.get(run)
        self.assertEqual(out[0]["code"], a.STOPPED)
        self.assertEqual(final["state"], "stopped")
        self.assertEqual(final["engine"], "other")
        self.assertEqual(final["stop_attestation"]["proof"], "empty")


class OrphanTest(unittest.TestCase):
    def test_reap_drops_dead_members_and_dangling_index_ids(self) -> None:
        procs = FakeProcesses()
        reg = InMemoryRunRegistry(processes=procs.is_alive)
        reg.put(_running())
        generation = reg.get("run-1")["generation"]
        live, dead = procs.spawn(), procs.spawn()
        reg.join_member("run-1", generation, "live", live)
        reg.join_member("run-1", reg.get("run-1")["generation"], "dead", dead)
        procs.exit(dead)
        reg._starts[("t", "ghost")] = ["missing-run"]
        before = reg.get("run-1")["generation"]
        reaped = reg.reap_orphans(procs.is_alive)
        final = reg.get("run-1")
        self.assertEqual(reaped[0], {"run_id": "run-1", "member_id": "dead", "pid": dead})
        self.assertEqual(reaped[1]["run_id"], "missing-run")
        self.assertEqual(final["members"], [{"member_id": "live", "pid": live}])
        self.assertEqual(final["state"], "running")  # reap is not an attested stop
        self.assertGreater(final["generation"], before)
        self.assertNotIn(("t", "ghost"), reg._starts)
        with self.assertRaises(StaleGeneration):
            stale = dict(final)
            stale["generation"] = before
            stale["state"] = "running"
            reg.put(stale)


class OwnerLockTest(unittest.TestCase):
    def test_opposite_lock_orders_do_not_deadlock(self) -> None:
        table = OwnerLockTable()
        barrier = threading.Barrier(2)
        done: list[str] = []

        def hold(owner: str, keys: list[str]) -> None:
            barrier.wait(5)
            table.acquire(owner, keys)
            done.append(owner)
            table.release(owner, keys)

        threads = [
            threading.Thread(target=hold, args=("owner-a", ["b", "a"])),
            threading.Thread(target=hold, args=("owner-b", ["a", "b"])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive(), "opposite lock orders deadlocked")
        self.assertEqual(sorted(done), ["owner-a", "owner-b"])
        self.assertEqual(table.held_keys(), [])

    def test_nested_acquire_of_an_earlier_key_is_refused(self) -> None:
        table = OwnerLockTable()
        table.acquire("owner-a", ["b-run:1"])
        with self.assertRaises(LockOrderError):
            table.acquire("owner-a", ["a-start:t"])
        table.release("owner-a", ["b-run:1"])
        self.assertEqual(table.held_keys(), [])

    def test_release_requires_the_owner_token_not_the_thread(self) -> None:
        table = OwnerLockTable()
        table.acquire("owner-a", ["k"])
        seen: list[str | None] = []

        def other() -> None:
            seen.append(table.owner_of("k"))
            with self.assertRaises(LockNotHeld):
                table.release("intruder", ["k"])
            table.release("owner-a", ["k"])

        thread = threading.Thread(target=other)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(seen, ["owner-a"])
        self.assertNotEqual(table.owner_of("k"), str(threading.get_ident()))
        self.assertEqual(table.held_keys(), [])

    def test_finished_runs_do_not_keep_locks(self) -> None:
        api, reg, _, _ = make_api()
        for i in range(20):
            res = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p", "client_key": f"k{i}"})
            self.assertEqual(res["code"], a.STARTED)
            self.assertEqual(api.call("run.stop", {"run_id": res["run"]["run_id"]})["code"], a.STOPPED)
        self.assertEqual(api._start_locks, {})
        self.assertEqual(reg.locks.held_keys(), [])


if __name__ == "__main__":
    unittest.main()
