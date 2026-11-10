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


if __name__ == "__main__":
    unittest.main()
