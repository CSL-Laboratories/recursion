"""Stop through the control API reaches a grandchild that left its session.

The run's process forks a child that forks a grandchild and exits; the grandchild calls setsid()
and lives on, so a kill of the main's process group misses it. ``run.stop`` answers ``stopped``
(with the record's proof ``empty``) only once that grandchild is gone, and it is gone after the
answer. With the kill of the tree made to spare the grandchild, the answer stays ``pending`` with
``cgroup_populated`` and the record says so.

The daemon composes control's KillTree only over its systemd units (``daemon.launcher.UnitKillTree``
in ``daemon.control_adapter``); nothing composes it over ``cgroup_leaf``. ``LeafKillTree`` here is
this test's own KillTree over ``cgroup_leaf.DelegatedRoot``. On every Linux host it runs over the
inherited-membership fake of the cgroup_leaf tests; with RECURSION_CGROUP_ROOT naming a delegated
cgroup v2 root that holds this process, it runs over the kernel's cgroup as well.
"""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from recursion_core.cgroup_leaf import Cgroup2, DelegatedRoot
from recursion_core.cgroup_leaf.tests.test_cgroup_leaf import (InheritedCgroup, LINUX_ONLY, NOT_LINUX, alive,
                                                               assert_detached, detached_tree_argv, end_own,
                                                               wait_file)
from recursion_core.control import api as a
from recursion_core.control.registry import EMPTY, POPULATED, UNKNOWN, InMemoryRunRegistry

STOP_ANSWER_KEYS = {"code", "run_id", "state", "repeat", "reason", "recorded"}  # stop.result.v1, legacy answer
RUN_NUMBERS = itertools.count(1)  # one leaf name per run in this process, so a kept leaf never blocks a later test


class LeafKillTree:
    """control.registry.KillTree of this test over cgroup_leaf.DelegatedRoot, and the starter that
    launches each run into a leaf of its own. ``request_stop`` kills the leaf (cgroup.kill, then the
    main's pidfd); ``confirm`` answers EMPTY only for the outcome ``leaf_empty`` and POPULATED for
    ``running``. ``empty_with_grandchild_alive`` records, for every EMPTY it answers, whether the
    run's grandchild was alive at that moment."""

    def __init__(self, root: DelegatedRoot, work: Path, kill_timeout: float) -> None:
        self.root, self.work, self.kill_timeout = root, work, kill_timeout
        self.runs: dict = {}
        self.grandchildren: dict[str, int] = {}
        self.requests: list[str] = []
        self.empty_with_grandchild_alive: list[bool] = []

    def start(self, params: dict) -> dict:
        name = params["run_id"]
        leaf = self.root.leaf(name)
        gc_file = self.work / f"{name}.gc"
        run = self.root.launch(name, detached_tree_argv(gc_file, leaf), cwd=self.work,
                               kill_timeout=self.kill_timeout)
        self.runs[str(run.leaf)] = run
        self.grandchildren[str(run.leaf)] = int(wait_file(gc_file))
        return {"cgroup": str(run.leaf)}

    def request_stop(self, cgroup: str) -> None:
        self.requests.append(cgroup)
        self.runs[cgroup].close()

    def confirm(self, cgroup: str, timeout_s: float) -> str:
        outcome = self.runs[cgroup].close()
        answer = {"leaf_empty": EMPTY, "running": POPULATED}.get(outcome.state, UNKNOWN)
        if answer == EMPTY:
            self.empty_with_grandchild_alive.append(alive(self.grandchildren[cgroup]))
        return answer


class DetachedGrandchildStopCases:
    """The cases, over whichever cgroup filesystem ``make_root`` gives."""

    kill_timeout = 0.5  # test value: how long one kill of the leaf waits for it to read empty

    def make_root(self, base: Path) -> DelegatedRoot:
        raise NotImplementedError

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.temp.name))
        self.work = base / "work"
        self.work.mkdir()
        self.root = self.make_root(base)
        self.tree = LeafKillTree(self.root, self.work, self.kill_timeout)
        self.registry = InMemoryRunRegistry()
        self.api = a.ControlAPI(self.registry, self.tree, self.tree.start, time.time,
                                new_run_id=lambda: f"run-gc{os.getpid()}x{next(RUN_NUMBERS)}")

    def tearDown(self) -> None:
        for cgroup, run in self.tree.runs.items():
            try:
                run.close()
            finally:
                gc = self.tree.grandchildren.get(cgroup)
                if gc is not None:
                    end_own(gc, Path(cgroup))
        self.temp.cleanup()

    def start(self) -> tuple[str, str, int, int]:
        run_id = self.api.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p"})["run"]["run_id"]
        cgroup = self.registry.get(run_id)["cgroup"]
        gc, main = self.tree.grandchildren[cgroup], self.tree.runs[cgroup].pid
        assert_detached(self, gc, main)
        self.assertTrue(alive(gc))
        return run_id, cgroup, gc, main

    def test_stopped_only_once_the_detached_grandchild_is_gone(self) -> None:
        run_id, cgroup, gc, main = self.start()
        res = self.api.call("run.stop", {"run_id": run_id})
        self.assertEqual((res["code"], res["state"], res["repeat"]), (a.STOPPED, "stopped", False))
        self.assertLessEqual(set(res), STOP_ANSWER_KEYS)
        self.assertNotIn("reason", res)
        rec = self.registry.get(run_id)
        self.assertEqual(rec["state"], "stopped")
        self.assertEqual((rec["stop_attestation"]["proof"], rec["stop_attestation"]["cgroup"]), ("empty", cgroup))
        self.assertFalse(alive(gc))
        self.assertFalse(alive(main))
        self.assertEqual(self.tree.empty_with_grandchild_alive, [False])
        self.assertFalse(Path(cgroup).exists())
        self.assertEqual(self.tree.requests, [cgroup])

    def test_spared_grandchild_keeps_stop_pending_until_it_is_gone(self) -> None:
        # The cut: the kill of the tree spares the grandchild; only the main's pidfd is signalled.
        run_id, cgroup, gc, main = self.start()
        with patch.object(self.root.fs, "kill", lambda leaf: None), \
                self.assertLogs("recursion_core.cgroup_leaf", level="ERROR"):
            for _ in range(2):
                res = self.api.call("run.stop", {"run_id": run_id})
                self.assertEqual((res["code"], res["state"], res["reason"]), (a.PENDING, "pending", "cgroup_populated"))
                self.assertNotIn("stopped", json.dumps(res))
                rec = self.registry.get(run_id)
                self.assertEqual((rec["state"], rec["stop"]["reason"]), ("stop_pending", "cgroup_populated"))
                self.assertFalse(rec.get("stop_attestation"))
                self.assertTrue(alive(gc))
                self.assertFalse(alive(main))
                self.assertTrue(Path(cgroup).is_dir())
        self.assertEqual(self.tree.empty_with_grandchild_alive, [])
        res = self.api.call("run.stop", {"run_id": run_id})  # the kill restored
        self.assertEqual((res["code"], res["state"], res["repeat"]), (a.STOPPED, "stopped", True))
        self.assertEqual(self.registry.get(run_id)["stop_attestation"]["proof"], "empty")
        self.assertFalse(alive(gc))
        self.assertEqual(self.tree.empty_with_grandchild_alive, [False])
        self.assertEqual(self.tree.requests, [cgroup])  # one request; later Stops confirm again


@unittest.skipIf(NOT_LINUX, LINUX_ONLY)
class FakeCgroupStopTests(DetachedGrandchildStopCases, unittest.TestCase):
    """Every Linux host: the inherited-membership fake keeps the setsid() grandchild in the leaf."""

    def make_root(self, base: Path) -> DelegatedRoot:
        mount = base / "cgroup"
        root_path = mount / "user.slice" / "delegated"
        root_path.mkdir(parents=True)
        for name in ("cgroup.procs", "cgroup.kill", "cgroup.controllers"):
            (root_path / name).touch()
        return DelegatedRoot(root_path, InheritedCgroup(mount))


@unittest.skipIf(NOT_LINUX, LINUX_ONLY)
@unittest.skipUnless(os.environ.get("RECURSION_CGROUP_ROOT"), "no delegated cgroup root")
class RealCgroupStopTests(DetachedGrandchildStopCases, unittest.TestCase):
    """Live cell: the kernel's cgroup under the delegated root RECURSION_CGROUP_ROOT names."""

    def make_root(self, base: Path) -> DelegatedRoot:
        return DelegatedRoot(os.environ["RECURSION_CGROUP_ROOT"], Cgroup2())


if __name__ == "__main__":
    unittest.main()
