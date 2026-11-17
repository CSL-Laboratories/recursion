"""A Stop that removes the run's cgroup records proof ``removed`` beside proof ``empty``.

The leaf here is a real empty directory. On the tip before the receipt, Stop still
stores proof ``empty`` and leaves that directory in place.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from recursion_core.control.registry import EMPTY
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

    def test_a_stop_that_removes_an_empty_leaf_records_removed(self) -> None:
        leaf = self.tmp / "leaf"
        leaf.mkdir()
        answer, stored = self._stop(str(leaf))
        proof = (stored or {}).get("stop_attestation") or {}
        self.assertEqual(answer["code"], "stopped", answer)
        self.assertEqual(proof.get("proof"), "removed", proof)
        self.assertFalse(leaf.exists(), "the empty leaf is still on disk")
        self.assertNotIn("exit_code", answer)
        self.assertNotIn("attestation", answer)

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

        A plain directory can be removed while a process still has it as its working
        directory, so this uses the errno a populated cgroup returns.
        """
        leaf = self.tmp / "busy"
        leaf.mkdir()

        def refuse(path: object) -> None:
            raise OSError(errno.EBUSY, "Device or resource busy")

        with mock.patch("recursion_core.control.api.os.rmdir", refuse):
            answer, stored = self._stop(str(leaf))
        self.assertEqual((answer["code"], answer.get("reason")), ("pending", "cgroup_populated"), answer)
        self.assertNotEqual((stored or {}).get("state"), "stopped")
        self.assertIsNone((stored or {}).get("stop_attestation"))
        self.assertTrue(leaf.is_dir())


if __name__ == "__main__":
    unittest.main()
