"""Deterministic races for registry compare-and-swap, attested stop, and owner locks.

Writers are threads with a barrier (or an event that orders the commits). Each call mints its own
owner token. Processes here are a fake process table: a pid is alive only while the test says so.
Clocks are fake. Nothing starts a provider, a cgroup, or a real workload.
"""

from __future__ import annotations

import threading
import unittest

from recursion_core.control import api as a
from recursion_core.control.api import ControlAPI
from recursion_core.control.registry import (
    EMPTY, POPULATED, InMemoryRunRegistry, JoinRefused, LockNotHeld, LockOrderError, OwnerLockTable,
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
    proof = {"proof": "empty", "cgroup": rec["cgroup"], "member_ids": member_ids,
             "generation": rec["generation"]}
    if rec.get("cgroup_marker") is not None:
        proof["cgroup_marker"] = rec["cgroup_marker"]
    return proof


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
        errors: list[BaseException] = []

        def other() -> None:
            try:
                seen.append(table.owner_of("k"))
                with self.assertRaises(LockNotHeld):
                    table.release("intruder", ["k"])
                table.release("owner-a", ["k"])
            except BaseException as err:  # unittest does not see failures raised on this thread
                errors.append(err)

        thread = threading.Thread(target=other)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]
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


def _start(api, key: str = "k") -> str:
    res = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p", "client_key": key})
    if res["code"] != a.STARTED:
        raise AssertionError(res)
    return res["run"]["run_id"]


class _Gate:
    """Blocks the first confirm of ``path`` until ``release`` is set."""

    def __init__(self, path: str, answer: str) -> None:
        self.path = path
        self.answer = answer
        self.at_confirm = threading.Event()
        self.release = threading.Event()
        self.calls: list[tuple[str, str]] = []

    def request_stop(self, cgroup: str) -> None:
        self.calls.append(("request", cgroup))

    def confirm(self, cgroup: str, timeout_s: float) -> str:
        self.calls.append(("confirm", cgroup))
        if cgroup == self.path:
            self.at_confirm.set()
            if not self.release.wait(5):
                raise TimeoutError("confirm")
        return self.answer


class ReviewFindingsTest(unittest.TestCase):
    def test_stopped_generation_cannot_be_reopened_as_running(self) -> None:
        # A writer that holds the stopped generation but a running payload must lose. Stop is terminal,
        # and the compare is the pair (generation, state), not the generation alone.
        reg = InMemoryRunRegistry()
        reg.put(_running())
        snap = reg.get("run-1")
        assert snap is not None
        stopped = dict(snap)
        stopped["state"] = "stopped"
        stopped["stop_attestation"] = _attest(snap, [])
        reg.compare_and_swap("run-1", snap["generation"], stopped)
        current = reg.get("run-1")
        assert current is not None
        reopened = dict(current)
        reopened["state"] = "running"
        reopened["stop_attestation"] = None
        with self.assertRaises(StaleGeneration):
            reg.compare_and_swap("run-1", current["generation"], reopened)
        self.assertEqual(reg.get("run-1")["state"], "stopped")
        self.assertEqual(reg.get("run-1")["stop_attestation"]["proof"], "empty")

    def test_refresh_does_not_pair_a_running_payload_with_a_stop_generation(self) -> None:
        class Launder(InMemoryRunRegistry):
            def __init__(self) -> None:
                super().__init__()
                self.tripped = False

            def put(self, record: dict) -> dict | None:
                stored = super().put(record)
                if record.get("state") == "running" and not self.tripped:
                    self.tripped = True
                    cur = super().get(record["run_id"])
                    assert cur is not None
                    cur["state"] = "stopped"
                    cur["stop_attestation"] = _attest(cur, [])
                    super().put(cur)
                return stored

        reg = Launder()
        api, _, _, _ = make_api(registry=reg)
        res = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p", "client_key": "k"})
        self.assertEqual(res["code"], a.STARTED)
        stored = reg.get(res["run"]["run_id"])
        assert stored is not None
        self.assertEqual(stored["state"], "stopped")
        returned = res["run"]
        self.assertFalse(
            returned["state"] == "running" and returned["generation"] == stored["generation"],
            "refresh paired a running payload with the stopped generation",
        )
        reopen = dict(stored)
        reopen["state"] = "running"
        reopen["stop_attestation"] = None
        with self.assertRaises(StaleGeneration):
            reg.put(reopen)
        self.assertEqual(reg.get(res["run"]["run_id"])["state"], "stopped")

    def test_stopped_record_rejects_a_different_cgroup(self) -> None:
        reg = InMemoryRunRegistry()
        reg.put(_running())
        snap = reg.get("run-1")
        assert snap is not None
        stopped = dict(snap)
        stopped["state"] = "stopped"
        stopped["stop_attestation"] = _attest(snap, [])
        reg.compare_and_swap("run-1", snap["generation"], stopped)
        current = reg.get("run-1")
        assert current is not None
        path, marker = current["cgroup"], current["cgroup_marker"]
        edited = dict(current)
        edited["cgroup"] = "/cg/other"
        with self.assertRaises(StopUnattested):
            reg.compare_and_swap("run-1", current["generation"], edited)
        again = reg.get("run-1")
        assert again is not None
        self.assertEqual(again["state"], "stopped")
        self.assertEqual(again["cgroup"], path)
        self.assertEqual(again["cgroup_marker"], marker)
        self.assertEqual(again["stop_attestation"]["cgroup"], path)
        self.assertEqual(again["stop_attestation"]["cgroup_marker"], marker)

    def test_empty_proof_is_bound_to_the_cgroup_that_was_confirmed(self) -> None:
        gate = _Gate("", EMPTY)
        api, reg, _, _ = make_api(kill=gate)
        run = _start(api)
        gate.path = reg.get(run)["cgroup"]
        errors: list[BaseException] = []
        results: list[dict] = []

        def stop() -> None:
            try:
                results.append(api.call("run.stop", {"run_id": run}))
            except BaseException as err:
                errors.append(err)

        thread = threading.Thread(target=stop)
        thread.start()
        self.assertTrue(gate.at_confirm.wait(5))
        snap = reg.get(run)
        assert snap is not None
        snap["cgroup"] = "/cg/other"
        reg.compare_and_swap(run, snap["generation"], snap)
        gate.release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]
        final = reg.get(run)
        assert final is not None
        self.assertEqual(results[0]["code"], a.STOPPED)
        self.assertEqual(final["state"], "stopped")
        self.assertEqual(final["stop_attestation"]["cgroup"], final["cgroup"])
        confirmed = [cgroup for kind, cgroup in gate.calls if kind == "confirm"]
        self.assertIn(final["cgroup"], confirmed)
        self.assertEqual(final["stop_attestation"]["cgroup_marker"], final["cgroup_marker"])
        self.assertTrue(final["cgroup_marker"])

    def test_stale_populated_result_does_not_undo_a_confirmed_stop(self) -> None:
        gate = _Gate("", POPULATED)
        api, reg, _, _ = make_api(kill=gate)
        run = _start(api)
        gate.path = reg.get(run)["cgroup"]
        errors: list[BaseException] = []
        results: list[dict] = []

        def stop() -> None:
            try:
                results.append(api.call("run.stop", {"run_id": run}))
            except BaseException as err:
                errors.append(err)

        thread = threading.Thread(target=stop)
        thread.start()
        self.assertTrue(gate.at_confirm.wait(5))
        snap = reg.get(run)
        assert snap is not None
        stopped = dict(snap)
        stopped["state"] = "stopped"
        stopped["stop_attestation"] = _attest(snap, [])
        reg.compare_and_swap(run, snap["generation"], stopped)
        gate.release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]
        final = reg.get(run)
        assert final is not None
        self.assertEqual(final["state"], "stopped")
        self.assertEqual(final["stop_attestation"]["proof"], "empty")
        self.assertEqual(results[0]["code"], a.STOPPED)
        with self.assertRaises(JoinRefused):
            reg.join_member(run, final["generation"], "later", 9)

    def test_stop_after_a_registry_outage_is_a_typed_result(self) -> None:
        class DownAfterLaunch(InMemoryRunRegistry):
            def __init__(self) -> None:
                super().__init__()
                self.down = False

            def put(self, record: dict):
                if self.down:
                    raise OSError("disk")
                return super().put(record)

            def get(self, run_id: str):
                if self.down:
                    raise OSError("disk")
                return super().get(run_id)

            def list(self):
                if self.down:
                    raise OSError("disk")
                return super().list()

        reg = DownAfterLaunch()

        def starter(params: dict) -> dict:
            reg.down = True
            return {"cgroup": f"/cg/{params['run_id']}"}

        from recursion_core.control.tests.fakes import run_ids

        api = ControlAPI(reg, _Gate("", EMPTY), starter, clock=lambda: 1000.0, new_run_id=run_ids())
        res = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p", "client_key": "k"})
        self.assertEqual(res["code"], a.START_UNCONFIRMED)
        stop = api.call("run.stop", {"run_id": "run-1"})
        self.assertIn(stop["code"], {a.STOPPED, a.PENDING, a.REGISTRY_UNAVAILABLE})

    def test_liveness_errors_stay_inside_the_typed_api(self) -> None:
        def deny(_pid: int) -> bool:
            raise PermissionError("proc")

        reg = InMemoryRunRegistry(processes=deny)
        api, _, _, _ = make_api(registry=reg)
        run = _start(api)
        snap = reg.get(run)
        assert snap is not None
        reg.join_member(run, snap["generation"], "m", 42)
        out = api.call("run.stop", {"run_id": run})
        self.assertEqual(out["code"], a.PENDING)
        self.assertTrue(str(out.get("reason", "")).startswith("cleanup_error:"))
        stored = reg.get(run)
        assert stored is not None
        self.assertNotEqual(stored["state"], "stopped")
        self.assertIsNone(stored.get("stop_attestation"))
        self.assertEqual([member["member_id"] for member in stored["members"]], ["m"])

    def test_a_failed_stop_write_on_retry_is_not_started(self) -> None:
        class StaleRunningThenFailStop(InMemoryRunRegistry):
            def __init__(self) -> None:
                super().__init__()
                self.running_puts = 0

            def put(self, record: dict):
                if record.get("state") == "running" and self.running_puts == 0:
                    self.running_puts += 1
                    cur = super().get(record["run_id"])
                    assert cur is not None
                    cur["stop"] = {"requested_at": 1.0, "checks": 0, "reason": None, "requested": False}
                    cur["state"] = "stop_pending"
                    super().put(cur)
                    raise StaleGeneration(record["run_id"], record.get("generation"), cur["generation"])
                if record.get("state") == "stopped":
                    raise OSError("disk")
                return super().put(record)

        reg = StaleRunningThenFailStop()
        api, _, _, _ = make_api(registry=reg)
        res = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p", "client_key": "k"})
        self.assertEqual(res["code"], a.START_UNCONFIRMED)
        stored = reg.get("run-1")
        assert stored is not None
        self.assertNotEqual(stored["state"], "stopped")

    def test_a_partly_invalid_release_does_not_drop_locks_or_strand_waiters(self) -> None:
        table = OwnerLockTable()
        table.acquire("A", ["a", "z"])
        table.acquire("B", ["b"])
        entered = threading.Event()
        real_wait = table._cv.wait

        def wait(timeout: float | None = None):
            entered.set()
            return real_wait(timeout)

        table._cv.wait = wait  # type: ignore[method-assign]
        thread = threading.Thread(target=lambda: table.acquire("C", ["a"]), daemon=True)
        thread.start()
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaises(LockNotHeld):
                table.release("A", ["a", "b", "z"])
            self.assertEqual(table.owner_of("a"), "A")
            self.assertEqual(table.owner_of("z"), "A")
            self.assertEqual(table.owner_of("b"), "B")
            table.release("A", ["a", "z"])
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(table.owner_of("a"), "C")
        finally:
            with table._cv:
                table._cv.notify_all()
            thread.join(1)

    def test_reap_samples_each_pid_once_and_reports_that_partition(self) -> None:
        reg = InMemoryRunRegistry(processes=lambda _pid: False)
        reg.put(_running())
        snap = reg.get("run-1")
        assert snap is not None
        reg.join_member("run-1", snap["generation"], "gone", 1)
        snap = reg.get("run-1")
        assert snap is not None
        reg.join_member("run-1", snap["generation"], "stays", 2)
        # The first sample is the only one cleanup may use. A second sample flips both answers, which
        # used to report a member that remained and drop a member it did not report.
        answers = {1: iter([False, True]), 2: iter([True, False])}

        def is_alive(pid: int) -> bool:
            return next(answers[pid])

        reaped = reg.reap_orphans(is_alive)
        stored = reg.get("run-1")
        assert stored is not None
        stored_ids = {member["member_id"] for member in stored["members"]}
        reaped_ids = {item["member_id"] for item in reaped if "member_id" in item}
        self.assertTrue(reaped_ids.isdisjoint(stored_ids))
        self.assertEqual(reaped_ids, {"gone", "stays"} - stored_ids)


class _SwapKill:
    """On confirm, move the stored run to the next cgroup, then return ``answer``."""

    def __init__(self, registry: InMemoryRunRegistry, swaps: list[str], answer: str) -> None:
        self.registry = registry
        self.swaps = list(swaps)
        self.answer = answer
        self.requests: list[str] = []
        self.confirms: list[str] = []

    def request_stop(self, cgroup: str) -> None:
        self.requests.append(cgroup)

    def confirm(self, cgroup: str, timeout_s: float) -> str:
        self.confirms.append(cgroup)
        if self.swaps:
            current = self.registry.list()[0]
            current["cgroup"] = self.swaps.pop(0)
            self.registry.compare_and_swap(current["run_id"], current["generation"], current)
        return self.answer


class RoundTwoTest(unittest.TestCase):
    def test_a_second_cgroup_change_does_not_inherit_an_unsent_request(self) -> None:
        reg = InMemoryRunRegistry()
        kill = _SwapKill(reg, ["/cg/B", "/cg/C"], EMPTY)
        api, _, _, _ = make_api(kill=kill, registry=reg)
        run = _start(api)
        first = api.call("run.stop", {"run_id": run})
        self.assertEqual(first["code"], a.PENDING)
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual(stored["cgroup"], "/cg/C")
        self.assertIs(stored["stop"]["requested"], False)
        second = api.call("run.stop", {"run_id": run})
        self.assertEqual(second["code"], a.STOPPED)
        self.assertEqual(kill.requests[-1], "/cg/C")
        self.assertEqual(kill.confirms[-1], "/cg/C")

    def test_a_populated_confirm_does_not_carry_requested_onto_a_new_cgroup(self) -> None:
        reg = InMemoryRunRegistry()
        kill = _SwapKill(reg, ["/cg/B"], POPULATED)
        api, _, _, _ = make_api(kill=kill, registry=reg)
        run = _start(api)
        first = api.call("run.stop", {"run_id": run})
        self.assertEqual((first["code"], first["reason"]), (a.PENDING, "cgroup_populated"))
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual(stored["cgroup"], "/cg/B")
        self.assertIs(stored["stop"]["requested"], False)
        api.call("run.stop", {"run_id": run})
        self.assertIn("/cg/B", kill.requests)

    def test_stop_records_a_recovered_launch_with_the_fresh_generation(self) -> None:
        class HideSnapshot(InMemoryRunRegistry):
            """``put`` stores but returns None, which the registry protocol still allows."""

            def __init__(self) -> None:
                super().__init__()
                self.puts = 0
                self.reads_down = False

            def put(self, record: dict):
                self.puts += 1
                super().put(record)
                if record.get("state") == "starting":
                    self.reads_down = True
                return None

            def get(self, run_id: str):
                if self.reads_down:
                    raise OSError("disk")
                return super().get(run_id)

        reg = HideSnapshot()
        api, _, _, _ = make_api(registry=reg)
        started = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p", "client_key": "k"})
        self.assertEqual(started["code"], a.START_UNCONFIRMED)
        self.assertNotIn("generation", api._launched["run-1"])
        reg.reads_down = False
        reg.puts = 0
        stop = api.call("run.stop", {"run_id": "run-1"})
        self.assertLess(reg.puts, 5)
        self.assertEqual(stop["code"], a.STOPPED)
        self.assertIsNot(stop.get("recorded"), False)
        self.assertEqual(reg.get("run-1")["state"], "stopped")
        self.assertEqual(reg.get("run-1")["cgroup"], "/sys/fs/cgroup/fake/run-1")

    def test_a_stale_launch_handle_cannot_retry_without_a_bound(self) -> None:
        reg = InMemoryRunRegistry()
        reg.put({"run_id": "run-1", "tenant": "t", "engine": "e", "state": "starting",
                 "cgroup": None, "stop": None, "start_key": None})
        current = reg.get("run-1")
        assert current is not None
        current["engine"] = "e2"
        reg.put(current)
        puts = {"n": 0}
        real_put = reg.put

        def counting_put(record: dict):
            puts["n"] += 1
            if puts["n"] > 20:
                raise AssertionError("stop retried without a bound")
            return real_put(record)

        reg.put = counting_put  # type: ignore[method-assign]
        api, _, _, _ = make_api(registry=reg)
        api._launched["run-1"] = {"run_id": "run-1", "tenant": "t", "engine": "e", "state": "running",
                                  "cgroup": "/cg/run-1", "stop": None, "start_key": None, "generation": 1}
        stop = api.call("run.stop", {"run_id": "run-1"})
        self.assertLess(puts["n"], 5)
        self.assertEqual(stop["code"], a.STOPPED)
        self.assertIsNot(stop.get("recorded"), False)
        stored = reg.get("run-1")
        assert stored is not None
        self.assertEqual(stored["state"], "stopped")
        self.assertEqual(stored["cgroup"], "/cg/run-1")

    def test_a_loaded_stopped_snapshot_without_a_marker_can_be_updated(self) -> None:
        legacy = {"run_id": "run-1", "tenant": "t", "engine": "codex", "state": "stopped",
                  "cgroup": "/cg/run-1", "generation": 2, "members": [],
                  "stop_attestation": {"proof": "empty", "cgroup": "/cg/run-1", "member_ids": [],
                                       "generation": 1}}
        reg = InMemoryRunRegistry([legacy])
        snap = reg.get("run-1")
        assert snap is not None
        self.assertTrue(snap.get("cgroup_marker"))
        self.assertEqual(snap["stop_attestation"]["cgroup_marker"], snap["cgroup_marker"])
        snap["engine"] = "other"
        stored = reg.put(snap)
        self.assertEqual(stored["state"], "stopped")
        self.assertEqual(stored["engine"], "other")
        self.assertEqual(stored["cgroup_marker"], snap["cgroup_marker"])
        self.assertEqual(reg.get("run-1")["stop_attestation"]["cgroup"], "/cg/run-1")

    def test_a_loaded_stopped_snapshot_with_a_foreign_cgroup_is_rejected(self) -> None:
        legacy = {"run_id": "run-1", "tenant": "t", "engine": "codex", "state": "stopped",
                  "cgroup": "/cg/run-1", "generation": 2, "members": [],
                  "stop_attestation": {"proof": "empty", "cgroup": "/cg/other", "member_ids": [],
                                       "generation": 1}}
        with self.assertRaises(StopUnattested):
            InMemoryRunRegistry([legacy])


class RoundThreeTest(unittest.TestCase):
    def test_adopting_a_stopped_launch_rebuilds_the_attestation(self) -> None:
        reg = InMemoryRunRegistry()
        reg.put({"run_id": "run-1", "tenant": "t", "engine": "new", "state": "starting",
                 "cgroup": None, "stop": None, "start_key": None})
        current = reg.get("run-1")
        assert current is not None
        current["engine"] = "new"
        reg.put(current)
        kill = FakeKillTree([EMPTY])
        api, _, _, _ = make_api(kill=kill, registry=reg)
        api._launched["run-1"] = {
            "run_id": "run-1", "tenant": "t", "engine": "old", "state": "stopped",
            "cgroup": "/cg/run-1", "cgroup_marker": "stale", "start_key": None, "generation": 1,
            "stop": {"requested_at": 1.0, "checks": 1, "reason": None, "requested": True},
            "stop_attestation": {"proof": "empty", "cgroup": "/cg/run-1", "cgroup_marker": "stale",
                                 "member_ids": [], "generation": 1},
        }
        seen = reg.get("run-1")
        assert seen is not None
        stop = api.call("run.stop", {"run_id": "run-1"})
        self.assertEqual(stop["code"], a.STOPPED)
        self.assertIsNot(stop.get("recorded"), False)
        self.assertEqual(kill.confirms, ["/cg/run-1"])
        stored = reg.get("run-1")
        assert stored is not None
        self.assertEqual(stored["state"], "stopped")
        self.assertEqual(stored["engine"], "new")
        self.assertEqual(stored["stop_attestation"]["proof"], "empty")
        self.assertEqual(stored["stop_attestation"]["generation"], seen["generation"])
        self.assertEqual(stored["stop_attestation"]["cgroup"], "/cg/run-1")

    def test_a_later_cgroup_change_does_not_reuse_the_previous_request(self) -> None:
        kill = FakeKillTree([POPULATED, EMPTY])
        api, reg, _, _ = make_api(kill=kill)
        run = _start(api)
        first = api.call("run.stop", {"run_id": run})
        self.assertEqual((first["code"], first["reason"]), (a.PENDING, "cgroup_populated"))
        self.assertEqual(len(kill.requests), 1)
        snap = reg.get(run)
        assert snap is not None
        self.assertIs(snap["stop"]["requested"], True)
        snap["cgroup"] = "/cg/B"
        reg.compare_and_swap(run, snap["generation"], snap)
        second = api.call("run.stop", {"run_id": run})
        self.assertEqual(second["code"], a.STOPPED)
        self.assertEqual(kill.requests[-1], "/cg/B")
        self.assertEqual(len(kill.requests), 2)

    def test_adoption_keeps_newer_registry_metadata(self) -> None:
        reg = InMemoryRunRegistry()
        reg.put({"run_id": "run-1", "tenant": "t", "engine": "old", "state": "starting",
                 "cgroup": None, "stop": None, "start_key": None})
        current = reg.get("run-1")
        assert current is not None
        current["engine"] = "new"
        reg.put(current)
        api, _, _, _ = make_api(registry=reg)
        api._launched["run-1"] = {"run_id": "run-1", "tenant": "t", "engine": "old", "state": "running",
                                  "cgroup": "/cg/run-1", "stop": None, "start_key": None, "generation": 1}
        stop = api.call("run.stop", {"run_id": "run-1"})
        self.assertEqual(stop["code"], a.STOPPED)
        stored = reg.get("run-1")
        assert stored is not None
        self.assertEqual(stored["engine"], "new")
        self.assertEqual(stored["state"], "stopped")
        self.assertEqual(stored["cgroup"], "/cg/run-1")

    def test_a_loaded_stop_rejects_a_live_member_missing_from_the_proof(self) -> None:
        legacy = {"run_id": "run-1", "tenant": "t", "engine": "codex", "state": "stopped",
                  "cgroup": "/cg/run-1", "generation": 2,
                  "members": [{"member_id": "live", "pid": 7}],
                  "stop_attestation": {"proof": "empty", "cgroup": "/cg/run-1", "member_ids": [],
                                       "generation": 1}}
        with self.assertRaises(StopUnattested):
            InMemoryRunRegistry([legacy], processes=lambda _pid: True)

    def test_a_loaded_stop_rejects_a_proof_generation_above_the_record(self) -> None:
        legacy = {"run_id": "run-1", "tenant": "t", "engine": "codex", "state": "stopped",
                  "cgroup": "/cg/run-1", "generation": 2, "members": [],
                  "stop_attestation": {"proof": "empty", "cgroup": "/cg/run-1", "member_ids": [],
                                       "generation": 999}}
        with self.assertRaises(StopUnattested):
            InMemoryRunRegistry([legacy])

    def test_a_loaded_stop_keeps_a_historical_proof(self) -> None:
        legacy = {"run_id": "run-1", "tenant": "t", "engine": "codex", "state": "stopped",
                  "cgroup": "/cg/run-1", "generation": 2, "members": [],
                  "stop_attestation": {"proof": "empty", "cgroup": "/cg/run-1", "member_ids": ["former"],
                                       "generation": 1}}
        reg = InMemoryRunRegistry([legacy])
        snap = reg.get("run-1")
        assert snap is not None
        self.assertEqual(snap["state"], "stopped")
        self.assertEqual(snap["generation"], 2)
        self.assertEqual(snap["stop_attestation"]["generation"], 1)
        self.assertEqual(snap["stop_attestation"]["member_ids"], ["former"])

    def test_join_during_retry_commit_keeps_stop_pending_never_stopped(self) -> None:
        """R2-1: the write-back is stale, and a live member joins before the retry's write lands."""
        procs = FakeProcesses()
        reg = InMemoryRunRegistry(processes=procs.is_alive)
        live_pid = procs.spawn()

        class InterleavedKill(FakeKillTree):
            def confirm(self, cgroup: str, timeout_s: float) -> str:
                snap = reg.get("run-1")
                assert snap is not None
                snap["engine"] = "codex-updated"
                reg.compare_and_swap("run-1", snap["generation"], snap)

                def on_commit(info: dict) -> None:
                    if info["op"] == "put" and info["generation"] == 3:
                        reg.commit_gate = None
                        reg.join_member("run-1", 3, "live-child", live_pid)

                reg.commit_gate = on_commit
                return EMPTY

        api, _, _, _ = make_api(InterleavedKill([EMPTY]), registry=reg)
        run = _start(api)
        res = api.call("run.stop", {"run_id": run})
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual((res["code"], res.get("reason")), (a.PENDING, "member_alive"), res)
        self.assertIsNot(res.get("recorded"), False)  # the pending decision landed on the record that holds the member
        self.assertEqual((stored["state"], stored["stop"]["reason"]), ("stop_pending", "member_alive"))
        self.assertIsNone(stored["stop_attestation"])
        self.assertEqual(stored["members"], [{"member_id": "live-child", "pid": live_pid}])

    def test_a_cgroup_swap_during_the_retry_commit_is_cleaned_up_again(self) -> None:
        """R2-1: the write-back is stale, and the run moves to another cgroup before the retry's write lands.
        The empty result was for the first cgroup, so the second one is requested and confirmed itself."""
        for second, code in ((EMPTY, a.STOPPED), (POPULATED, a.PENDING)):
            with self.subTest(second_cgroup=second):
                reg = InMemoryRunRegistry()

                class InterleavedKill(FakeKillTree):
                    def confirm(self, cgroup: str, timeout_s: float) -> str:
                        self.confirms.append(cgroup)
                        if cgroup == "/cg/B":
                            return second
                        snap = reg.get("run-1")
                        assert snap is not None
                        snap["engine"] = "codex-updated"
                        reg.compare_and_swap("run-1", snap["generation"], snap)

                        def on_commit(info: dict) -> None:
                            if info["op"] == "put" and info["generation"] == 3:
                                reg.commit_gate = None
                                moved = reg.get("run-1")
                                assert moved is not None
                                moved["cgroup"] = "/cg/B"
                                reg.compare_and_swap("run-1", moved["generation"], moved)

                        reg.commit_gate = on_commit
                        return EMPTY

                kill = InterleavedKill()
                api, _, _, _ = make_api(kill, registry=reg)
                run = _start(api)
                res = api.call("run.stop", {"run_id": run})
                stored = reg.get(run)
                assert stored is not None
                self.assertEqual(res["code"], code, res)
                self.assertEqual((kill.requests[-1], kill.confirms[-1]), ("/cg/B", "/cg/B"))
                self.assertEqual(stored["cgroup"], "/cg/B")
                if code == a.STOPPED:
                    self.assertEqual((stored["state"], stored["stop_attestation"]["cgroup"]), ("stopped", "/cg/B"))
                else:
                    self.assertEqual((res.get("reason"), stored["state"]), ("cgroup_populated", "stop_pending"))

    def test_repeated_stale_commits_never_answer_stopped(self) -> None:
        """R2-1: every write of the stop is stale. The retries are bounded, and the answer is pending."""
        class AlwaysStaleAfterConfirm(InMemoryRunRegistry):
            def __init__(self) -> None:
                super().__init__()
                self.bump = False
                self.stopped_puts = 0

            def put(self, record: dict):
                if self.bump and record.get("state") == "stopped":
                    self.stopped_puts += 1
                    cur = super().get(record["run_id"])
                    assert cur is not None
                    cur["engine"] = f"e-{cur['generation']}"
                    super().put(cur)
                return super().put(record)

        reg = AlwaysStaleAfterConfirm()

        class ArmKill(FakeKillTree):
            moving = True

            def confirm(self, cgroup: str, timeout_s: float) -> str:
                reg.bump = self.moving
                return EMPTY

        kill = ArmKill([EMPTY])
        api, _, _, _ = make_api(kill, registry=reg)
        run = _start(api)
        res = api.call("run.stop", {"run_id": run})
        self.assertEqual((res["code"], res["state"], res.get("reason")), (a.PENDING, "pending", "cgroup_changed"), res)
        self.assertIs(res.get("recorded"), False)
        self.assertEqual(reg.stopped_puts, 1 + a.STALE_RETRIES)  # the first write, then the bounded retries
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual((stored["state"], stored["stop_attestation"]), ("running", None))
        # Nothing is wedged: once the record holds still, the next Stop cleans up again and is attested.
        kill.moving = reg.bump = False
        again = api.call("run.stop", {"run_id": run})
        self.assertEqual((again["code"], reg.get(run)["state"]), (a.STOPPED, "stopped"))
        self.assertIsNot(again.get("recorded"), False)

    def test_a_member_that_joins_on_the_last_stale_retry_is_the_pending_reason(self) -> None:
        """R2-1: out of retries, the reason is ``member_alive`` when a member is alive on the record."""
        procs = FakeProcesses()
        live_pid = procs.spawn()

        class StaleThenJoin(InMemoryRunRegistry):
            def __init__(self) -> None:
                super().__init__(processes=procs.is_alive)
                self.bump = False
                self.stopped_puts = 0

            def put(self, record: dict):
                if self.bump and record.get("state") == "stopped":
                    self.stopped_puts += 1
                    cur = super().get(record["run_id"])
                    assert cur is not None
                    if self.stopped_puts == 1 + a.STALE_RETRIES:  # the last retry's write loses to a join
                        self.join_member(record["run_id"], cur["generation"], "live-child", live_pid)
                    else:
                        cur["engine"] = f"e-{cur['generation']}"
                        super().put(cur)
                return super().put(record)

        reg = StaleThenJoin()

        class ArmKill(FakeKillTree):
            def confirm(self, cgroup: str, timeout_s: float) -> str:
                reg.bump = True
                return EMPTY

        api, _, _, _ = make_api(ArmKill([EMPTY]), registry=reg)
        run = _start(api)
        res = api.call("run.stop", {"run_id": run})
        self.assertEqual((res["code"], res.get("reason")), (a.PENDING, "member_alive"), res)
        self.assertIs(res.get("recorded"), False)
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual((stored["state"], stored["members"]),
                         ("running", [{"member_id": "live-child", "pid": live_pid}]))

    def test_a_record_that_moved_and_cannot_be_read_again_is_pending(self) -> None:
        """R2-1: the write-back is stale and the re-read fails. What the record now holds is unknown,
        so the empty result is not answered as stopped."""
        class MovedThenUnreadable(InMemoryRunRegistry):
            def __init__(self) -> None:
                super().__init__()
                self.arm = self.blind = False

            def put(self, record: dict):
                if self.arm and record.get("state") == "stopped":
                    self.arm = False
                    cur = super().get(record["run_id"])
                    assert cur is not None
                    cur["engine"] = "moved"
                    super().put(cur)
                    self.blind = True
                return super().put(record)

            def get(self, run_id: str):
                if self.blind:
                    raise OSError("registry read failed")
                return super().get(run_id)

        reg = MovedThenUnreadable()

        class ArmKill(FakeKillTree):
            def confirm(self, cgroup: str, timeout_s: float) -> str:
                reg.arm = True
                return EMPTY

        api, _, _, _ = make_api(ArmKill([EMPTY]), registry=reg)
        run = _start(api)
        res = api.call("run.stop", {"run_id": run})
        self.assertEqual((res["code"], res.get("reason")), (a.PENDING, "cgroup_changed"), res)
        self.assertIs(res.get("recorded"), False)
        reg.blind = False
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual((stored["state"], stored["engine"]), ("running", "moved"))

    def test_a_stale_write_whose_retry_cannot_be_written_is_pending_until_a_write_lands(self) -> None:
        """R2-1: after a stale write-back, ``stopped`` waits for a write that lands. A retry decided on a
        fresh read and then refused by the disk is pending; so is the next Stop while writes still fail.
        The first write that lands ends that, and a plain failed write is again reported as before."""
        class StaleThenDown(InMemoryRunRegistry):
            def __init__(self) -> None:
                super().__init__()
                self.stale_once = self.down = False

            def put(self, record: dict):
                if self.down:
                    raise OSError("disk unavailable")
                if self.stale_once and record.get("state") == "stopped":
                    self.stale_once = False
                    cur = super().get(record["run_id"])
                    assert cur is not None
                    cur["engine"] = "moved"
                    super().put(cur)
                    self.down = True  # the retry reads the moved record, and its write then fails
                return super().put(record)

        reg = StaleThenDown()
        kill = FakeKillTree([EMPTY, EMPTY, POPULATED, EMPTY])
        api, _, _, _ = make_api(kill, registry=reg)
        run = _start(api)
        reg.stale_once = True
        first = api.call("run.stop", {"run_id": run})
        self.assertEqual((first["code"], first.get("reason"), first.get("recorded")),
                         (a.PENDING, "cgroup_changed", False), first)
        second = api.call("run.stop", {"run_id": run})  # the disk is still down, and nothing has landed
        self.assertEqual((second["code"], second.get("reason"), second.get("recorded")),
                         (a.PENDING, "cgroup_changed", False), second)
        reg.down = False
        third = api.call("run.stop", {"run_id": run})  # a pending answer lands: the copy is current again
        self.assertEqual((third["code"], third.get("reason")), (a.PENDING, "cgroup_populated"), third)
        self.assertIsNot(third.get("recorded"), False)
        reg.down = True
        fourth = api.call("run.stop", {"run_id": run})  # an ordinary failed write, with no stale write before it
        self.assertEqual((fourth["code"], fourth.get("recorded")), (a.STOPPED, False), fourth)


class StaleReconfirmTest(unittest.TestCase):
    """Found by the review of this PR that ran at e8038043 (its ``s3-stale-reconfirm-down.py``): the ways
    out of a Stop that the "until a write lands" rule did not cover."""

    def test_a_reconfirm_after_a_stale_write_keeps_the_rule_until_a_write_lands(self) -> None:
        """The stale write found the run on a second cgroup, so the Stop goes round once more and confirms
        that one. That cleanup starts from a stale write too: when the disk refuses its own write, the
        answer is pending, not stopped, whatever the record holds by then."""
        for joins in (True, False):
            with self.subTest(a_member_joins=joins):
                procs = FakeProcesses()
                live_pid = procs.spawn()

                class MovesThenFails(InMemoryRunRegistry):
                    def __init__(self) -> None:
                        super().__init__(processes=procs.is_alive)
                        self.stopped_writes = 0

                    def put(self, record: dict):
                        if record.get("state") == "stopped":
                            self.stopped_writes += 1
                            cur = super().get(record["run_id"])
                            assert cur is not None
                            if self.stopped_writes == 1:  # another writer moved the run: this write is stale
                                cur["cgroup"] = "/cg/B"
                                super().put(cur)
                            elif self.stopped_writes == 2:  # the disk refuses this one
                                if joins:
                                    self.join_member(record["run_id"], cur["generation"], "live-child", live_pid)
                                raise OSError(28, "No space left on device")
                        return super().put(record)

                reg = MovesThenFails()
                kill = FakeKillTree([EMPTY])
                api, _, _, _ = make_api(kill, registry=reg)
                run = _start(api)
                res = api.call("run.stop", {"run_id": run})
                self.assertEqual((kill.requests[-1], kill.confirms[-1]), ("/cg/B", "/cg/B"))  # it did go round
                self.assertEqual((res["code"], res.get("reason"), res.get("recorded")),
                                 (a.PENDING, "member_alive" if joins else "cgroup_changed", False), res)
                stored = reg.get(run)
                assert stored is not None
                self.assertEqual((stored["state"], stored["stop_attestation"]), ("running", None))
                # The disk is back. With a live member the next Stop lands as pending; once it is gone
                # (or if none ever joined) the Stop is attested, for the second cgroup.
                if joins:
                    again = api.call("run.stop", {"run_id": run})
                    self.assertEqual((again["code"], again.get("reason")), (a.PENDING, "member_alive"), again)
                    self.assertIsNot(again.get("recorded"), False)
                    procs.exit(live_pid)
                last = api.call("run.stop", {"run_id": run})
                self.assertEqual((last["code"], reg.get(run)["stop_attestation"]["cgroup"]), (a.STOPPED, "/cg/B"), last)
                self.assertIsNot(last.get("recorded"), False)

    def test_a_kept_launch_with_no_generation_is_pending_after_an_unlanded_stale_stop(self) -> None:
        """A kept launch record that cannot name a generation skips the write altogether. Its empty result
        is still the cleanup's answer on its own, but not once an earlier write of that run's Stop came
        back stale and nothing has landed since: that copy may be behind the record."""
        class Unreadable(InMemoryRunRegistry):
            def get(self, run_id: str):
                raise OSError("registry read failed")

        handle = {"run_id": "run-1", "tenant": "t", "engine": "e", "state": "running", "cgroup": "/cg/run-1",
                  "stop": None, "start_key": None}  # no generation: the registry never returned one
        api, _, _, _ = make_api(FakeKillTree([EMPTY]), registry=Unreadable())
        api._launched["run-1"] = dict(handle)
        plain = api.call("run.stop", {"run_id": "run-1"})
        self.assertEqual((plain["code"], plain.get("recorded")), (a.STOPPED, False), plain)  # unchanged
        api._launched["run-1"] = dict(handle)
        api._stale_stops.add("run-1")
        held = api.call("run.stop", {"run_id": "run-1"})
        self.assertEqual((held["code"], held.get("reason"), held.get("recorded")),
                         (a.PENDING, "cgroup_changed", False), held)
        self.assertEqual(api._launched["run-1"]["state"], "stop_pending")


class FinalStaleReasonTest(unittest.TestCase):
    """The reason a Stop gives once its stale retries are spent: ``member_alive`` when a member joined,
    else ``cgroup_changed``. From the Codex review of this PR at b74a3a58, its finding B1."""

    def test_member_join_on_last_retry_after_reconfirm_names_member_alive(self) -> None:
        """Written by that review; its assertions are kept as it wrote them. The first empty Stop finds
        the run on a second cgroup and spends its reconfirm. The next empty result finds a third, so
        every retry is held as ``cgroup_changed``, and the last retry's write loses to a member that
        joins. The answer names that member."""
        class Moving(InMemoryRunRegistry):
            def __init__(self) -> None:
                super().__init__(processes=lambda pid: pid == 9999)
                self.stopped = self.pending = 0

            def move(self, group: str) -> None:
                rec = super().get("run-x")
                assert rec is not None
                rec.update(cgroup=group, engine="update-" + str(self.pending))
                super().put(rec)

            def put(self, rec: dict):
                if rec.get("state") == "stopped":
                    self.stopped += 1
                    self.move("/cg/two" if self.stopped == 1 else "/cg/three")
                    raise StaleGeneration("run-x", rec.get("generation"), None)
                if rec.get("state") == "stop_pending":
                    self.pending += 1
                    self.move("/cg/three")
                    if self.pending == 3:
                        latest = super().get("run-x")
                        assert latest is not None
                        super().join_member("run-x", latest["generation"], "joined", 9999)
                    raise StaleGeneration("run-x", rec.get("generation"), None)
                return super().put(rec)

        class Kill:
            def __init__(self) -> None:
                self.requests: list[str] = []

            def request_stop(self, group: str) -> None:
                self.requests.append(group)

            def confirm(self, group: str, timeout: float) -> str:
                return EMPTY

        registry, kill = Moving(), Kill()
        registry.put({"run_id": "run-x", "tenant": "t", "engine": "codex", "state": "running",
                      "cgroup": "/cg/one", "started_at": 1, "stop": None, "start_key": None})
        api = ControlAPI(registry, kill, lambda p: None, lambda: 1000)
        answer = api.call("run.stop", {"run_id": "run-x"})
        self.assertEqual((answer["code"], answer.get("reason")), ("pending", "member_alive"), answer)
        self.assertIs(answer.get("recorded"), False)
        self.assertEqual(kill.requests, ["/cg/one", "/cg/two"])
        self.assertEqual(registry.pending, 3)
        self.assertNotEqual(registry.get("run-x")["state"], "stopped")

    def test_a_member_the_retry_saw_alive_stays_the_reason_when_the_registry_then_fails(self) -> None:
        """The other side of that reason read. A retry that read a live member holds ``member_alive``.
        When the disk refuses its write and the record cannot be read again, the member is still what
        this Stop saw, so the reason is not swapped for ``cgroup_changed`` on the strength of a read
        that failed."""
        procs = FakeProcesses()
        live_pid = procs.spawn()

        class JoinsThenFails(InMemoryRunRegistry):
            def __init__(self) -> None:
                super().__init__(processes=procs.is_alive)
                self.armed = self.down = False
                self.stopped_writes = self.pending_writes = 0

            def get(self, run_id: str):
                if self.down:
                    raise OSError("registry read failed")
                return super().get(run_id)

            def put(self, record: dict):
                if self.armed and record.get("state") == "stopped":
                    self.stopped_writes += 1
                    if self.stopped_writes == 1:  # a member joins first: this write is stale
                        cur = super().get(record["run_id"])
                        assert cur is not None
                        self.join_member(record["run_id"], cur["generation"], "live-child", live_pid)
                elif self.armed and record.get("state") == "stop_pending":
                    self.pending_writes += 1
                    if self.pending_writes == 1:  # the disk refuses the retry's write, then reads fail too
                        self.down = True
                        raise OSError(28, "No space left on device")
                return super().put(record)

        reg = JoinsThenFails()

        class ArmKill(FakeKillTree):
            def confirm(self, cgroup: str, timeout_s: float) -> str:
                reg.armed = True
                return EMPTY

        api, _, _, _ = make_api(ArmKill([EMPTY]), registry=reg)
        run = _start(api)
        res = api.call("run.stop", {"run_id": run})
        self.assertEqual((res["code"], res.get("reason"), res.get("recorded")),
                         (a.PENDING, "member_alive", False), res)
        self.assertEqual((reg.stopped_writes, reg.pending_writes), (1, 1))  # one stale write, one refused retry
        # The registry is back. The member is on the record and alive, so the next Stop lands as pending
        # for the same reason; once it has exited, the Stop is attested.
        reg.down = False
        stored = reg.get(run)
        assert stored is not None
        self.assertEqual((stored["state"], stored["members"]),
                         ("running", [{"member_id": "live-child", "pid": live_pid}]))
        again = api.call("run.stop", {"run_id": run})
        self.assertEqual((again["code"], again.get("reason")), (a.PENDING, "member_alive"), again)
        self.assertIsNot(again.get("recorded"), False)
        procs.exit(live_pid)
        last = api.call("run.stop", {"run_id": run})
        self.assertEqual((last["code"], reg.get(run)["state"]), (a.STOPPED, "stopped"), last)
        self.assertIsNot(last.get("recorded"), False)


class StaleOutageTest(unittest.TestCase):
    """From the cross-family review of the R2-1 patch: a stale write-back followed by a registry outage."""

    def exercise(self, cached: bool) -> None:
        live = [True]

        class Registry(InMemoryRunRegistry):
            def __init__(self) -> None:
                super().__init__(processes=lambda pid: live[0])
                self.unreadable = self.down = self.churn = False
                self.fail_running = cached
                self.attempts = 0

            def get(self, run_id: str):
                if self.unreadable:
                    raise OSError("registry read failed")
                return super().get(run_id)

            def put(self, record: dict):
                if self.down or (self.fail_running and record["state"] == "running"):
                    raise OSError("disk unavailable")
                if self.churn and record["state"] == "stopped":
                    self.attempts += 1
                    cur = super().get(record["run_id"])
                    assert cur is not None
                    cur["engine"] = f"bump-{self.attempts}"
                    super().put(cur)
                    if self.attempts == 1 + a.STALE_RETRIES:
                        cur = super().get(record["run_id"])
                        assert cur is not None
                        self.join_member(record["run_id"], cur["generation"], "live-member", 9999)
                return super().put(record)

        reg = Registry()
        confirms: list[str] = []

        class Kill:
            def request_stop(self, cgroup: str) -> None:
                pass

            def confirm(self, cgroup: str, timeout_s: float) -> str:
                confirms.append(cgroup)
                if not cached and len(confirms) == 1:
                    cur = reg.get("run-x")
                    assert cur is not None
                    reg.join_member("run-x", cur["generation"], "live-member", 9999)
                    reg.unreadable = True
                return EMPTY

        api = ControlAPI(reg, Kill(), lambda params: {"cgroup": "/cg/run-x"}, lambda: 1000.0,
                         new_run_id=lambda: "run-x")
        api.call("run.start", {"tenant": "t", "engine": "codex", "prompt": "p"})
        reg.fail_running = False
        if cached:
            rec = reg.get("run-x")
            assert rec is not None
            rec.update(state="running", cgroup="/cg/run-x")
            reg.put(rec)
            reg.churn = True
        answer = api.call("run.stop", {"run_id": "run-x"})
        self.assertEqual(answer["code"], a.PENDING, answer)
        self.assertIs(answer.get("recorded"), False)
        if cached:
            self.assertEqual(answer.get("reason"), "member_alive", answer)  # it joined on the last retry
            self.assertNotEqual(api._launched["run-x"]["state"], "stopped")
            reg.churn = False
            reg.unreadable = reg.down = True
            answer = api.call("run.stop", {"run_id": "run-x"})  # only the kept launch record can be used
            self.assertEqual((answer["code"], answer.get("reason")), (a.PENDING, "cgroup_changed"), answer)
        reg.unreadable = reg.down = False
        answer = api.call("run.stop", {"run_id": "run-x"})
        self.assertEqual((answer["code"], answer.get("reason")), (a.PENDING, "member_alive"), answer)
        live[0] = False
        self.assertEqual(api.call("run.stop", {"run_id": "run-x"})["code"], a.STOPPED)

    def test_stale_then_unreadable_never_certifies_the_old_member_set(self) -> None:
        self.exercise(False)

    def test_cached_exhausted_stop_stays_pending_during_outage_and_recovers(self) -> None:
        self.exercise(True)


if __name__ == "__main__":
    unittest.main()
