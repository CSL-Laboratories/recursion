"""Each attempt's scratch runtime: new folders, never reused, never in the home or the real runtime folder."""
from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path

from eval import scratch
from eval.tests import scratch_bases


class ScratchTests(unittest.TestCase):
    def test_folders_are_new_private_and_removed(self):
        with scratch_bases() as (tmp, xdg):
            with scratch.attempt("a-1", tmp, xdg) as s:
                self.assertEqual(s.tmpdir, tmp / "a-1")
                self.assertEqual(s.xdg_runtime_dir, xdg / "a-1")
                for folder in (s.tmpdir, s.xdg_runtime_dir):
                    self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
                env = scratch.environment(s)
                self.assertEqual(env["TMPDIR"], str(s.tmpdir))
                self.assertEqual(env["XDG_RUNTIME_DIR"], str(s.xdg_runtime_dir))
                self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)
            self.assertTrue(s.removed)
            self.assertEqual(os.listdir(tmp), [])
            self.assertEqual(os.listdir(xdg), [])

    def test_a_folder_is_never_reused(self):
        with scratch_bases() as (tmp, xdg):
            (tmp / "a-2").mkdir()
            with self.assertRaises(scratch.ScratchError):
                with scratch.attempt("a-2", tmp, xdg):
                    pass
            self.assertTrue((tmp / "a-2").exists())  # not ours: left alone

    def test_read_only_leftovers_are_removed(self):
        with scratch_bases() as (tmp, xdg):
            with scratch.attempt("a-3", tmp, xdg) as s:
                locked = s.tmpdir / "work" / "locked"
                locked.mkdir(parents=True)
                (locked / "f").write_text("x", encoding="utf-8")
                locked.chmod(0o500)
            self.assertFalse((tmp / "a-3").exists())

    def test_removed_even_when_the_attempt_raises(self):
        with scratch_bases() as (tmp, xdg):
            with self.assertRaises(RuntimeError):
                with scratch.attempt("a-4", tmp, xdg):
                    raise RuntimeError("boom")
            self.assertEqual(os.listdir(tmp), [])

    def test_bases_in_the_home_or_the_real_runtime_folder_are_refused(self):
        home = Path(os.path.expanduser("~"))
        with self.assertRaises(scratch.ScratchError):
            scratch.check_bases(home / "eval-scratch", Path("/dev/shm/x"))
        with self.assertRaises(scratch.ScratchError):
            scratch.check_bases(Path("/var/tmp/x"), Path(f"/run/user/{os.getuid()}"))
        with self.assertRaises(scratch.ScratchError):
            scratch.check_bases(Path("relative"), Path("/dev/shm/x"))

    def test_bad_attempt_ids(self):
        with scratch_bases() as (tmp, xdg):
            for bad in ("../x", "", "a/b", ".hidden"):
                with self.assertRaises(scratch.ScratchError):
                    with scratch.attempt(bad, tmp, xdg):
                        pass


if __name__ == "__main__":
    unittest.main()
