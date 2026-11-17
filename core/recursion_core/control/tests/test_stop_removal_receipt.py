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
import tempfile
import unittest
from unittest import mock

from recursion_core.control.registry import EMPTY, REMOVED, StaleGeneration, InMemoryRunRegistry
from recursion_core.control.tests.fakes import FakeKillTree, make_api


def _starter(cgroup: str):
    def start(params: dict) -> dict:
        return {"cgroup": cgroup}

    return start


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

        def vanish(path: object) -> None:
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

        def vanish(path: object) -> None:
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

        def busy(path: object) -> None:
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

        def refuse(path: object) -> None:
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
