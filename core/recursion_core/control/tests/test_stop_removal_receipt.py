"""A Stop that removes the run's cgroup records proof ``removed`` beside proof ``empty``.

The leaf here is a real empty directory. On the tip before the receipt, Stop still
stores proof ``empty`` and leaves that directory in place.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from recursion_core.control.registry import EMPTY, REMOVED, StaleGeneration, InMemoryRunRegistry
from recursion_core.control.tests.fakes import FakeKillTree, make_api

LINUX = sys.platform.startswith("linux")


def _starter(cgroup: str):
    def start(params: dict) -> dict:
        return {"cgroup": cgroup}

    return start


@unittest.skipUnless(LINUX, "the daemon is Linux only")
class RemovalReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rc-stop-receipt-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(self._wipe)

    def _wipe(self) -> None:
        if not self.tmp.exists():
            return
        for path in sorted(self.tmp.rglob("*"), reverse=True):
            if path.is_symlink() or not path.is_dir():
                path.unlink()
            else:
                path.rmdir()
        self.tmp.rmdir()

    def _stop(self, cgroup: str) -> tuple[dict, dict | None]:
        api, reg, _, _ = make_api(FakeKillTree([EMPTY]), starter=_starter(cgroup))
        started = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})
        run_id = started["run"]["run_id"]
        answer = api.call("run.stop", {"run_id": run_id})
        stored = reg.get(run_id)
        return answer, stored

    def test_an_arbitrary_empty_directory_outside_the_slice_is_never_removed(self) -> None:
        """A recorded host path is not a cgroup. Stop must not remove it.

        The directory sits in this test's own temporary directory, outside any
        supervised slice and outside any cgroup root the cleanup layer named.
        """
        outside = self.tmp / "outside-the-slice"
        outside.mkdir()
        answer, stored = self._stop(str(outside))
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertEqual(answer["code"], "stopped", answer)
        self.assertNotEqual(proof.get("proof"), "removed", proof)
        self.assertEqual(proof.get("proof"), "empty", proof)
        self.assertTrue(outside.is_dir(), "an arbitrary empty directory was removed")
        self.assertNotIn("exit_code", answer)
        self.assertNotIn("attestation", answer)

    def test_a_proved_empty_directory_under_the_slice_records_removed(self) -> None:
        """Removal is allowed only for this run's unit under the cgroup root and the slice."""
        root = self.tmp / "cg"
        root.mkdir()
        holder: dict = {}

        def starter(params: dict) -> dict:
            unit = f"recursion-run-{params['run_id']}.service"
            directory = root / "app.slice" / unit
            directory.mkdir(parents=True)
            info = directory.lstat()
            holder["directory"] = directory
            holder["identity"] = {"st_dev": info.st_dev, "st_ino": info.st_ino}
            return {"cgroup": f"/app.slice/{unit}"}

        kill = FakeKillTree([EMPTY])
        kill.cgroup_root = root
        api, reg, _, _ = make_api(kill, starter=starter)
        started = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})
        run_id = started["run"]["run_id"]
        rec = reg.get(run_id)
        assert rec is not None
        rec["cgroup_identity"] = holder["identity"]
        reg.put(rec)
        answer = api.call("run.stop", {"run_id": run_id})
        stored = reg.get(run_id)
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertEqual(answer["code"], "stopped", answer)
        self.assertEqual(proof.get("proof"), "removed", proof)
        self.assertFalse(holder["directory"].exists())

    def test_a_proved_directory_whose_inode_is_not_the_recorded_one_stays(self) -> None:
        root = self.tmp / "cg"
        root.mkdir()
        holder: dict = {}

        def starter(params: dict) -> dict:
            unit = f"recursion-run-{params['run_id']}.service"
            directory = root / "app.slice" / unit
            directory.mkdir(parents=True)
            holder["directory"] = directory
            return {"cgroup": f"/app.slice/{unit}"}

        kill = FakeKillTree([EMPTY])
        kill.cgroup_root = root
        api, reg, _, _ = make_api(kill, starter=starter)
        started = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})
        run_id = started["run"]["run_id"]
        rec = reg.get(run_id)
        assert rec is not None
        rec["cgroup_identity"] = {"st_dev": 0, "st_ino": 1}
        reg.put(rec)
        answer = api.call("run.stop", {"run_id": run_id})
        stored = reg.get(run_id)
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertEqual(answer["code"], "stopped", answer)
        self.assertNotEqual(proof.get("proof"), "removed", proof)
        self.assertTrue(holder["directory"].is_dir())

    def test_rmdir_enoent_on_a_proved_directory_is_not_recorded_as_removed(self) -> None:
        """ENOENT from rmdir is someone else. A proved directory still on disk stays."""
        root = self.tmp / "cg-enoent"
        root.mkdir()
        holder: dict = {}

        def starter(params: dict) -> dict:
            unit = f"recursion-run-{params['run_id']}.service"
            directory = root / "app.slice" / unit
            directory.mkdir(parents=True)
            holder["directory"] = directory
            return {"cgroup": f"/app.slice/{unit}"}

        def vanish(path: object, dir_fd: int | None = None) -> None:
            raise OSError(errno.ENOENT, "gone")

        kill = FakeKillTree([EMPTY])
        kill.cgroup_root = root
        api, reg, _, _ = make_api(kill, starter=starter)
        started = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})
        with mock.patch("recursion_core.control.api.os.rmdir", vanish):
            answer = api.call("run.stop", {"run_id": started["run"]["run_id"]})
        stored = reg.get(started["run"]["run_id"])
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertNotEqual(proof.get("proof"), "removed", proof)
        self.assertTrue(holder["directory"].is_dir())
        self.assertEqual(answer["code"], "stopped", answer)

    def test_rmdir_enoent_is_not_recorded_as_removed(self) -> None:
        """The directory vanished under someone else. This call did not remove it."""
        leaf = self.tmp / "raced"
        leaf.mkdir()

        def vanish(path: object, dir_fd: int | None = None) -> None:
            raise OSError(errno.ENOENT, "gone")

        with mock.patch("recursion_core.control.api.os.rmdir", vanish):
            answer, stored = self._stop(str(leaf))
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertNotEqual(proof.get("proof"), "removed", proof)
        self.assertTrue(leaf.is_dir())
        self.assertEqual(answer["code"], "stopped", answer)

    def test_ebusy_on_a_clear_cgroup_does_not_stay_pending(self) -> None:
        """Empty child cgroups make cgroupfs answer EBUSY. That is not a live process."""
        root = self.tmp / "cg-clear"
        root.mkdir()
        holder: dict = {}

        def starter(params: dict) -> dict:
            unit = f"recursion-run-{params['run_id']}.service"
            directory = root / "app.slice" / unit
            directory.mkdir(parents=True)
            (directory / "cgroup.procs").write_text("")
            (directory / "cgroup.events").write_text("populated 0\n")
            child = directory / "child"
            child.mkdir()
            (child / "cgroup.procs").write_text("")
            (child / "cgroup.events").write_text("populated 0\n")
            holder["directory"] = directory
            holder["child"] = child
            return {"cgroup": f"/app.slice/{unit}"}

        def busy(path: object, dir_fd: int | None = None) -> None:
            raise OSError(errno.EBUSY, "Device or resource busy")

        kill = FakeKillTree([EMPTY])
        kill.cgroup_root = root
        api, reg, _, _ = make_api(kill, starter=starter)
        started = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})
        with mock.patch("recursion_core.control.api.os.rmdir", busy):
            answer = api.call("run.stop", {"run_id": started["run"]["run_id"]})
        stored = reg.get(started["run"]["run_id"])
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertEqual(answer["code"], "stopped", answer)
        self.assertNotEqual(answer.get("reason"), "cgroup_populated", answer)
        self.assertEqual(proof.get("proof"), "empty", proof)
        self.assertTrue(holder["directory"].is_dir())
        self.assertTrue(holder["child"].is_dir())

    def test_a_leaf_that_was_never_there_stays_proof_empty(self) -> None:
        missing = self.tmp / "missing"
        answer, stored = self._stop(str(missing))
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertEqual((answer["code"], proof.get("proof")), ("stopped", "empty"), (answer, proof))
        self.assertFalse(missing.exists())

    def test_a_leaf_that_still_holds_a_file_stays_proof_empty(self) -> None:
        leaf = self.tmp / "held"
        leaf.mkdir()
        (leaf / "cgroup.procs").write_text("")
        answer, stored = self._stop(str(leaf))
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertEqual((answer["code"], proof.get("proof")), ("stopped", "empty"), (answer, proof))
        self.assertTrue(leaf.is_dir())

    def test_a_symlink_leaf_is_not_removed(self) -> None:
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "link"
        link.symlink_to(real, target_is_directory=True)
        answer, stored = self._stop(str(link))
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertEqual((answer["code"], proof.get("proof")), ("stopped", "empty"), (answer, proof))
        self.assertTrue(link.is_symlink())
        self.assertTrue(stat.S_ISDIR(real.lstat().st_mode))

    def test_a_kernel_refusal_to_remove_is_not_attested_stopped(self) -> None:
        """A cgroup the kernel still holds answers EBUSY. That Stop stays pending.

        The directory is this run's unit under the supervised slice, and the re-read
        shows it populated. A plain directory can be removed while a process still has
        it as its working directory, so this uses the errno a populated cgroup returns.
        The pid is only text in this test's directory. Nothing signals a real process.
        """
        root = self.tmp / "cg-busy"
        root.mkdir()
        holder: dict = {}

        def starter(params: dict) -> dict:
            unit = f"recursion-run-{params['run_id']}.service"
            directory = root / "app.slice" / unit
            directory.mkdir(parents=True)
            (directory / "cgroup.procs").write_text("424242\n")
            (directory / "cgroup.events").write_text("populated 1\n")
            holder["directory"] = directory
            return {"cgroup": f"/app.slice/{unit}"}

        def refuse(path: object, dir_fd: int | None = None) -> None:
            raise OSError(errno.EBUSY, "Device or resource busy")

        kill = FakeKillTree([EMPTY])
        kill.cgroup_root = root
        api, reg, _, _ = make_api(kill, starter=starter)
        started = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})
        with mock.patch("recursion_core.control.api.os.rmdir", refuse):
            answer = api.call("run.stop", {"run_id": started["run"]["run_id"]})
        stored = reg.get(started["run"]["run_id"])
        self.assertEqual((answer["code"], answer.get("reason")), ("pending", "cgroup_populated"), answer)
        self.assertNotEqual((stored or {}).get("state"), "stopped")
        self.assertIsNone((stored or {}).get("stop_attestation"))
        self.assertTrue(holder["directory"].is_dir())

    def test_a_directory_under_another_slice_is_not_removed(self) -> None:
        """A unit under system.slice is not this run's supervised cgroup."""
        root = self.tmp / "cg-system"
        root.mkdir()
        holder: dict = {}

        def starter(params: dict) -> dict:
            unit = f"recursion-run-{params['run_id']}.service"
            directory = root / "system.slice" / unit
            directory.mkdir(parents=True)
            holder["directory"] = directory
            return {"cgroup": f"/system.slice/{unit}"}

        kill = FakeKillTree([EMPTY])
        kill.cgroup_root = root
        api, reg, _, _ = make_api(kill, starter=starter)
        started = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})
        answer = api.call("run.stop", {"run_id": started["run"]["run_id"]})
        stored = reg.get(started["run"]["run_id"])
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertEqual(answer["code"], "stopped", answer)
        self.assertNotEqual(proof.get("proof"), "removed", proof)
        self.assertTrue(holder["directory"].is_dir())

    def test_a_dotdot_path_is_not_removed(self) -> None:
        """A recorded path that climbs out of the cgroup root is not removed."""
        root = self.tmp / "cg-dot"
        root.mkdir()
        (root / "app.slice").mkdir()
        holder: dict = {}

        def starter(params: dict) -> dict:
            unit = f"recursion-run-{params['run_id']}.service"
            outside = self.tmp / unit
            outside.mkdir()
            holder["directory"] = outside
            return {"cgroup": f"/app.slice/../../{unit}"}

        kill = FakeKillTree([EMPTY])
        kill.cgroup_root = root
        api, reg, _, _ = make_api(kill, starter=starter)
        started = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})
        answer = api.call("run.stop", {"run_id": started["run"]["run_id"]})
        stored = reg.get(started["run"]["run_id"])
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertEqual(answer["code"], "stopped", answer)
        self.assertNotEqual(proof.get("proof"), "removed", proof)
        self.assertTrue(holder["directory"].is_dir())

    def test_a_symlink_in_the_slice_path_is_not_removed(self) -> None:
        root = self.tmp / "cg-link"
        root.mkdir()
        real = self.tmp / "real-slice"
        holder: dict = {}

        def starter(params: dict) -> dict:
            unit = f"recursion-run-{params['run_id']}.service"
            directory = real / unit
            directory.mkdir(parents=True)
            (root / "app.slice").symlink_to(real, target_is_directory=True)
            holder["directory"] = directory
            return {"cgroup": f"/app.slice/{unit}"}

        kill = FakeKillTree([EMPTY])
        kill.cgroup_root = root
        api, reg, _, _ = make_api(kill, starter=starter)
        started = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})
        answer = api.call("run.stop", {"run_id": started["run"]["run_id"]})
        stored = reg.get(started["run"]["run_id"])
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertNotEqual(proof.get("proof"), "removed", proof)
        self.assertTrue(holder["directory"].is_dir())
        self.assertTrue((root / "app.slice").is_symlink())

    def test_an_empty_marker_is_not_removed(self) -> None:
        """A proved path with no marker is doubt. The registry cannot store an empty marker."""
        root = self.tmp / "cg-marker"
        root.mkdir()
        holder: dict = {}

        def starter(params: dict) -> dict:
            unit = f"recursion-run-{params['run_id']}.service"
            directory = root / "app.slice" / unit
            directory.mkdir(parents=True)
            holder["directory"] = directory
            return {"cgroup": f"/app.slice/{unit}"}

        kill = FakeKillTree([EMPTY])
        kill.cgroup_root = root
        api, reg, _, _ = make_api(kill, starter=starter)
        started = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})
        run_id = started["run"]["run_id"]
        reg._runs[run_id]["cgroup_marker"] = ""
        api.call("run.stop", {"run_id": run_id})
        self.assertTrue(holder["directory"].is_dir())

    def _proved_unit(self, root_name: str):
        """A proved unit under ``app.slice``. The caller fills the directory."""
        root = self.tmp / root_name
        root.mkdir()
        holder: dict = {}

        def starter(params: dict) -> dict:
            unit = f"recursion-run-{params['run_id']}.service"
            directory = root / "app.slice" / unit
            directory.mkdir(parents=True)
            holder["root"] = root
            holder["directory"] = directory
            holder["unit"] = unit
            return {"cgroup": f"/app.slice/{unit}"}

        kill = FakeKillTree([EMPTY])
        kill.cgroup_root = root
        api, reg, _, _ = make_api(kill, starter=starter)
        started = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})
        return api, reg, started["run"]["run_id"], holder

    def _nest(self, directory: Path, depth: int) -> None:
        """``depth`` child levels under ``directory``. Each level reads clear."""
        current = directory
        (current / "cgroup.procs").write_text("")
        (current / "cgroup.events").write_text("populated 0\n")
        for index in range(depth):
            current = current / f"n{index}"
            current.mkdir()
            (current / "cgroup.procs").write_text("")
            (current / "cgroup.events").write_text("populated 0\n")

    def test_enotempty_with_a_live_pid_is_not_stored_as_empty(self) -> None:
        """R3. A child makes rmdir answer ENOTEMPTY here. That is not an empty unit.

        No rmdir stand-in. ``cgroup.procs`` names this process as text. Nothing is signalled.
        Confirm already said empty, which is the path that used to store proof empty.
        """
        api, reg, run_id, holder = self._proved_unit("cg-enotempty")
        directory = holder["directory"]
        (directory / "cgroup.procs").write_text(f"{os.getpid()}\n")
        (directory / "cgroup.events").write_text("populated 1\n")
        child = directory / "child"
        child.mkdir()
        (child / "cgroup.procs").write_text(f"{os.getpid()}\n")
        (child / "cgroup.events").write_text("populated 1\n")
        answer = api.call("run.stop", {"run_id": run_id})
        stored = reg.get(run_id)
        self.assertEqual((answer["code"], answer.get("reason")), ("pending", "cgroup_populated"), answer)
        self.assertNotEqual((stored or {}).get("state"), "stopped")
        self.assertIsNone((stored or {}).get("stop_attestation"))
        self.assertTrue(directory.is_dir())
        self.assertTrue(child.is_dir())

    def test_an_rmdir_error_other_than_enoent_with_a_live_pid_is_not_empty(self) -> None:
        """R3. Any rmdir error other than ENOENT is re-read. EIO is not proof empty."""
        api, reg, run_id, holder = self._proved_unit("cg-eio")
        directory = holder["directory"]
        (directory / "cgroup.procs").write_text(f"{os.getpid()}\n")
        (directory / "cgroup.events").write_text("populated 1\n")

        def refuse(path: object, dir_fd: int | None = None) -> None:
            raise OSError(errno.EIO, "Input/output error")

        with mock.patch("recursion_core.control.api.os.rmdir", refuse):
            answer = api.call("run.stop", {"run_id": run_id})
        stored = reg.get(run_id)
        self.assertEqual((answer["code"], answer.get("reason")), ("pending", "cgroup_populated"), answer)
        self.assertNotEqual((stored or {}).get("state"), "stopped")
        self.assertIsNone((stored or {}).get("stop_attestation"))
        self.assertTrue(directory.is_dir())

    def test_enotempty_with_a_dead_pid_is_proof_empty(self) -> None:
        """A pid that is already gone, with populated 0, is a clear re-read.

        The child is the only reason rmdir answers ENOTEMPTY. The pid is a process this
        test created and has already waited on. Nothing else is signalled.
        """
        child_proc = subprocess.Popen(["sleep", "60"])
        child_proc.kill()
        child_proc.wait()
        api, reg, run_id, holder = self._proved_unit("cg-dead-pid")
        directory = holder["directory"]
        (directory / "cgroup.procs").write_text(f"{child_proc.pid}\n")
        (directory / "cgroup.events").write_text("populated 0\n")
        child = directory / "child"
        child.mkdir()
        (child / "cgroup.procs").write_text("")
        (child / "cgroup.events").write_text("populated 0\n")
        answer = api.call("run.stop", {"run_id": run_id})
        stored = reg.get(run_id)
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertEqual(answer["code"], "stopped", answer)
        self.assertNotEqual(answer.get("reason"), "cgroup_populated", answer)
        self.assertEqual(proof.get("proof"), "empty", proof)
        self.assertTrue(directory.is_dir())
        self.assertTrue(child.is_dir())

    def test_a_pid_whose_proc_read_does_not_say_is_not_proof_empty(self) -> None:
        """An unfinished /proc read is doubt. That rmdir error is not stored as proof empty."""
        api, reg, run_id, holder = self._proved_unit("cg-proc-unread")
        directory = holder["directory"]
        (directory / "cgroup.procs").write_text("424242\n")
        (directory / "cgroup.events").write_text("populated 0\n")
        child = directory / "child"
        child.mkdir()
        real_read = Path.read_text

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            if str(path).startswith("/proc/"):
                raise OSError(errno.EIO, "Input/output error")
            return real_read(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", read_text):
            answer = api.call("run.stop", {"run_id": run_id})
        stored = reg.get(run_id)
        self.assertEqual((answer["code"], answer.get("reason")), ("pending", "cgroup_populated"), answer)
        self.assertNotEqual((stored or {}).get("state"), "stopped")
        self.assertIsNone((stored or {}).get("stop_attestation"))
        self.assertTrue(directory.is_dir())

    def test_enotempty_after_a_clear_reread_keeps_proof_empty(self) -> None:
        """A clear child directory stays. Real rmdir is ENOTEMPTY on this host, and the re-read is clear."""
        api, reg, run_id, holder = self._proved_unit("cg-enotempty-clear")
        directory = holder["directory"]
        (directory / "cgroup.procs").write_text("")
        (directory / "cgroup.events").write_text("populated 0\n")
        child = directory / "child"
        child.mkdir()
        (child / "cgroup.procs").write_text("")
        (child / "cgroup.events").write_text("populated 0\n")
        answer = api.call("run.stop", {"run_id": run_id})
        stored = reg.get(run_id)
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertEqual(answer["code"], "stopped", answer)
        self.assertNotEqual(answer.get("reason"), "cgroup_populated", answer)
        self.assertEqual(proof.get("proof"), "empty", proof)
        self.assertTrue(directory.is_dir())
        self.assertTrue(child.is_dir())

    def test_a_parent_swapped_between_the_check_and_rmdir_is_not_followed(self) -> None:
        """R2. The parent becomes a symlink in the rmdir call. The outside directory stays.

        The swap sits in the rmdir the product calls, after the path was checked. The real
        removal then runs with the arguments the product passed.
        """
        api, reg, run_id, holder = self._proved_unit("cg-swap")
        root = holder["root"]
        unit = holder["unit"]
        outside = self.tmp / "outside-swap"
        victim = outside / unit
        victim.mkdir(parents=True)
        real_rmdir = os.rmdir
        swapped = {"done": False}

        def attack(path: object, dir_fd: int | None = None) -> None:
            slice_dir = root / "app.slice"
            if not swapped["done"] and slice_dir.is_dir() and not slice_dir.is_symlink():
                swapped["done"] = True
                slice_dir.rename(root / "app.slice.held")
                slice_dir.symlink_to(outside, target_is_directory=True)
            if dir_fd is None:
                real_rmdir(path)
            else:
                real_rmdir(path, dir_fd=dir_fd)

        with mock.patch("recursion_core.control.api.os.rmdir", attack):
            answer = api.call("run.stop", {"run_id": run_id})
        stored = reg.get(run_id)
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertTrue(victim.is_dir(), "a directory outside the unit was removed")
        self.assertNotEqual(proof.get("proof"), "removed", proof)
        self.assertNotEqual(answer["code"], "stopped", answer)
        self.assertTrue(swapped["done"])

    def test_a_clear_ebusy_nest_between_the_old_depth_bounds_is_stopped(self) -> None:
        """R1. Depth 9 sits between the old re-read bound of 8 and the walk bound of 64.

        rmdir answers EBUSY. The nest is clear, so the Stop is stopped and the directories stay.
        A second Stop of that same tree is stopped as well.
        """
        api, reg, run_id, holder = self._proved_unit("cg-depth")
        directory = holder["directory"]
        self._nest(directory, 9)

        def busy(path: object, dir_fd: int | None = None) -> None:
            raise OSError(errno.EBUSY, "Device or resource busy")

        with mock.patch("recursion_core.control.api.os.rmdir", busy):
            answer = api.call("run.stop", {"run_id": run_id})
            again = api.call("run.stop", {"run_id": run_id})
        stored = reg.get(run_id)
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertEqual(answer["code"], "stopped", answer)
        self.assertEqual(proof.get("proof"), "empty", proof)
        self.assertEqual(again["code"], "stopped", again)
        self.assertTrue(directory.is_dir())
        self.assertTrue((directory / "n0").is_dir())

    def test_a_clear_ebusy_nest_past_the_old_node_bound_is_stopped(self) -> None:
        """R1. 64 child directories are 65 nodes, past the old re-read bound and under the walk bound."""
        api, reg, run_id, holder = self._proved_unit("cg-wide")
        directory = holder["directory"]
        (directory / "cgroup.procs").write_text("")
        (directory / "cgroup.events").write_text("populated 0\n")
        for index in range(64):
            child = directory / f"c{index}"
            child.mkdir()
            (child / "cgroup.procs").write_text("")
            (child / "cgroup.events").write_text("populated 0\n")

        def busy(path: object, dir_fd: int | None = None) -> None:
            raise OSError(errno.EBUSY, "Device or resource busy")

        with mock.patch("recursion_core.control.api.os.rmdir", busy):
            answer = api.call("run.stop", {"run_id": run_id})
        stored = reg.get(run_id)
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertEqual(answer["code"], "stopped", answer)
        self.assertEqual(proof.get("proof"), "empty", proof)
        self.assertTrue((directory / "c0").is_dir())
        self.assertTrue((directory / "c63").is_dir())

    def test_a_live_pid_past_the_old_node_bound_stays_pending(self) -> None:
        """R1. The same wide tree with a pid in one child is not stored stopped."""
        api, reg, run_id, holder = self._proved_unit("cg-wide-pid")
        directory = holder["directory"]
        (directory / "cgroup.procs").write_text("")
        (directory / "cgroup.events").write_text("populated 0\n")
        for index in range(64):
            child = directory / f"c{index}"
            child.mkdir()
            text = f"{os.getpid()}\n" if index == 0 else ""
            events = "populated 1\n" if index == 0 else "populated 0\n"
            (child / "cgroup.procs").write_text(text)
            (child / "cgroup.events").write_text(events)

        def busy(path: object, dir_fd: int | None = None) -> None:
            raise OSError(errno.EBUSY, "Device or resource busy")

        with mock.patch("recursion_core.control.api.os.rmdir", busy):
            answer = api.call("run.stop", {"run_id": run_id})
        stored = reg.get(run_id)
        self.assertEqual((answer["code"], answer.get("reason")), ("pending", "cgroup_populated"), answer)
        self.assertNotEqual((stored or {}).get("state"), "stopped")
        self.assertIsNone((stored or {}).get("stop_attestation"))

    def test_the_ebusy_reread_uses_the_one_walk_bound(self) -> None:
        """R1. The re-read bound is the walk's own constant, not a second copy of the numbers.

        Depth 3 is inside the old re-read bound. Lowering the shared constant to 2 must
        make this Stop unproved. A private copy of the old numbers would still stop it.
        """
        api, reg, run_id, holder = self._proved_unit("cg-bound")
        self._nest(holder["directory"], 3)

        def busy(path: object, dir_fd: int | None = None) -> None:
            raise OSError(errno.EBUSY, "Device or resource busy")

        with mock.patch("recursion_core.daemon.CGROUP_WALK_MAX_DEPTH", 2), \
                mock.patch("recursion_core.control.api.os.rmdir", busy):
            answer = api.call("run.stop", {"run_id": run_id})
        stored = reg.get(run_id)
        self.assertEqual((answer["code"], answer.get("reason")), ("pending", "cgroup_populated"), answer)
        self.assertNotEqual((stored or {}).get("state"), "stopped")
        self.assertIsNone((stored or {}).get("stop_attestation"))

    def _sleeper(self) -> subprocess.Popen:
        """A ``sleep`` child this test starts and ends itself. Nothing else is signalled."""
        proc = subprocess.Popen(["sleep", "300"])

        def end() -> None:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

        self.addCleanup(end)
        return proc

    def _swap_in_rmdir(self, directory: Path, child: subprocess.Popen, move, busy: bool):
        """An rmdir stand-in. On its first call the leaf lists ``child`` and ``move`` changes the path."""
        real_rmdir = os.rmdir
        state = {"done": False}

        def swap(path: object, dir_fd: int | None = None) -> None:
            if not state["done"]:
                state["done"] = True
                (directory / "cgroup.procs").write_text(f"{child.pid}\n")
                (directory / "cgroup.events").write_text("populated 1\n")
                move()
            if busy:
                raise OSError(errno.EBUSY, "Device or resource busy")
            if dir_fd is None:
                real_rmdir(path)
            else:
                real_rmdir(path, dir_fd=dir_fd)

        return swap, state

    def _r1(self, root_name: str, busy: bool) -> None:
        api, reg, run_id, holder = self._proved_unit(root_name)
        root, unit, directory = holder["root"], holder["unit"], holder["directory"]
        decoy = self.tmp / f"decoy-{root_name}"
        (decoy / unit).mkdir(parents=True)
        child = self._sleeper()

        def move() -> None:
            (root / "app.slice").rename(root / "app.slice.held")
            (root / "app.slice").symlink_to(decoy, target_is_directory=True)

        swap, state = self._swap_in_rmdir(directory, child, move, busy)
        with mock.patch("recursion_core.control.api.os.rmdir", swap):
            answer = api.call("run.stop", {"run_id": run_id})
        stored = reg.get(run_id)
        held = root / "app.slice.held" / unit
        self.assertTrue(state["done"])
        self.assertEqual((answer["code"], answer.get("reason")), ("pending", "cgroup_populated"), answer)
        self.assertNotEqual((stored or {}).get("state"), "stopped")
        self.assertIsNone((stored or {}).get("stop_attestation"))
        self.assertEqual((held / "cgroup.procs").read_text().split(), [str(child.pid)])
        self.assertIsNone(child.poll(), "the sleep child was signalled")
        self.assertTrue((decoy / unit).is_dir())

    def test_r1_a_live_pid_and_a_swapped_slice_inside_rmdir_stay_pending(self) -> None:
        """R1. Inside the rmdir call the leaf gains a live pid and app.slice is renamed aside,
        a link to a clear decoy put in its place. rmdir answers ENOTEMPTY.

        The second look reads the leaf through the parent folder the removal held, so the
        Stop is pending with no proof. The decoy is not read and not removed.
        """
        self._r1("cg-r1", busy=False)

    def test_r1_with_ebusy_as_a_cgroup_mount_answers_stays_pending(self) -> None:
        """R1 with rmdir answering EBUSY, as a cgroup mount answers for a populated cgroup."""
        self._r1("cg-r1-busy", busy=True)

    def test_r2_a_live_pid_and_a_swapped_folder_above_the_root_stay_pending(self) -> None:
        """R2. Inside the rmdir call the leaf gains a live pid and the folder above the cgroup
        root is renamed aside, a link to a decoy tree put in its place.

        The second look reads the held folder, so the Stop is pending with no proof and the
        decoy tree's leaf of the same name stays.
        """
        top = self.tmp / "top-r2"
        top.mkdir()
        api, reg, run_id, holder = self._proved_unit("top-r2/cg")
        unit, directory = holder["unit"], holder["directory"]
        decoytop = self.tmp / "decoytop-r2"
        (decoytop / "cg" / "app.slice" / unit).mkdir(parents=True)
        child = self._sleeper()

        def move() -> None:
            top.rename(self.tmp / "top-r2.held")
            top.symlink_to(decoytop, target_is_directory=True)

        swap, state = self._swap_in_rmdir(directory, child, move, busy=False)
        with mock.patch("recursion_core.control.api.os.rmdir", swap):
            answer = api.call("run.stop", {"run_id": run_id})
        stored = reg.get(run_id)
        held = self.tmp / "top-r2.held" / "cg" / "app.slice" / unit
        self.assertTrue(state["done"])
        self.assertEqual((answer["code"], answer.get("reason")), ("pending", "cgroup_populated"), answer)
        self.assertNotEqual((stored or {}).get("state"), "stopped")
        self.assertIsNone((stored or {}).get("stop_attestation"))
        self.assertEqual((held / "cgroup.procs").read_text().split(), [str(child.pid)])
        self.assertIsNone(child.poll(), "the sleep child was signalled")
        self.assertTrue((decoytop / "cg" / "app.slice" / unit).is_dir())


class StaleRemovalProofTest(unittest.TestCase):
    """The removed proof has to survive the one stale write-back retry."""

    def test_a_removed_proof_survives_the_stale_write_retry(self) -> None:
        class StaleOnce(InMemoryRunRegistry):
            def __init__(self) -> None:
                super().__init__()
                self.once = True

            def put(self, record: dict):
                if self.once and record.get("state") == "stopped":
                    self.once = False
                    current = super().get(record["run_id"])
                    assert current is not None
                    current["engine"] = "moved"
                    super().put(current)
                    raise StaleGeneration(record["run_id"], record.get("generation"), None)
                return super().put(record)

        reg = StaleOnce()
        api, _, _, _ = make_api(FakeKillTree([REMOVED]), registry=reg)
        started = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "p"})
        run_id = started["run"]["run_id"]
        answer = api.call("run.stop", {"run_id": run_id})
        stored = reg.get(run_id)
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertEqual(answer["code"], "stopped", answer)
        self.assertEqual(proof.get("proof"), "removed", proof)
        self.assertEqual((stored or {}).get("engine"), "moved")


class SchemaRemovalProofTest(unittest.TestCase):
    """The stored-record schema that accepts proof removed carries the next draft."""

    def _schema(self, name: str) -> dict:
        path = Path(__file__).resolve().parents[4] / "schemas" / "control" / "v1" / name
        return json.loads(path.read_text())

    def test_run_record_and_run_view_bump_for_the_removed_proof(self) -> None:
        record = self._schema("run.record.v1.json")
        view = self._schema("run.view.v1.json")
        self.assertEqual(record["x-version"], "1.0-draft.5")
        self.assertEqual(view["x-version"], "1.0-draft.5")
        self.assertNotIn("whose proof is 'removed'", record["description"])
        changes = (Path(__file__).resolve().parents[4] / "schemas" / "control" / "v1" / "CHANGES.md").read_text()
        self.assertIn("| `run.record.v1.json` | 1.0-draft.5 |", changes)
        self.assertIn("| `run.view.v1.json` | 1.0-draft.5 |", changes)


if __name__ == "__main__":
    unittest.main()
