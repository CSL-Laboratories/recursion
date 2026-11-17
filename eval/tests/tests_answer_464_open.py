"""Answers still open on draft #464 after the planted-name reply.

A second interrupt during scratch removal must still remove the other folder and must not
replace the attempt's own error. A FIFO or a socket where a folder of a hidden path must be
is removed, and a parent that still cannot be made is a TaskError. Text passed as stdin is
refused before a process exists.
"""
from __future__ import annotations

import errno
import os
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from eval import arms, scratch, tasks
from eval.tests import outside_folder, scratch_bases


class SecondInterruptCleanupTests(unittest.TestCase):
    def test_an_interrupt_removing_tmp_does_not_skip_the_runtime_folder_or_replace_the_body(self):
        real_remove = scratch._rmtree

        def fault(path):
            if "eval-tmp-" in str(path.parent):
                raise KeyboardInterrupt("injected")
            return real_remove(path)

        with scratch_bases() as (tmp, xdg):
            caught = None
            try:
                with patch.object(scratch, "_rmtree", side_effect=fault):
                    with scratch.attempt("second-interrupt", tmp, xdg):
                        raise RuntimeError("body")
            except BaseException as exc:
                caught = exc
            self.assertIsInstance(caught, RuntimeError, f"cleanup replaced the body: {caught!r}")
            self.assertFalse((xdg / "second-interrupt").exists(), "the second removal was skipped")
            self.assertTrue((tmp / "second-interrupt").exists())

    def test_an_interrupt_removing_tmp_still_removes_the_runtime_folder_when_the_body_succeeded(self):
        real_remove = scratch._rmtree

        def fault(path):
            if "eval-tmp-" in str(path.parent):
                raise KeyboardInterrupt("injected")
            return real_remove(path)

        with scratch_bases() as (tmp, xdg):
            with patch.object(scratch, "_rmtree", side_effect=fault):
                with self.assertRaises(KeyboardInterrupt):
                    with scratch.attempt("cleanup-only", tmp, xdg):
                        pass
            self.assertFalse((xdg / "cleanup-only").exists(), "the second removal was skipped")


class HiddenFolderObstacleTests(unittest.TestCase):
    def _task(self) -> tasks.Task:
        return tasks.Task(id="t", prompt="", test_command=["x"],
                          hidden={"sub/hidden.py": b"HIDDEN\n"}, solution={}, sha256="0" * 64)

    def test_a_fifo_where_a_hidden_folder_must_be_is_removed(self):
        with outside_folder() as folder:
            work = folder / "work"
            work.mkdir()
            os.mkfifo(work / "sub")
            self._task().write_hidden(work)
            written = work / "sub" / "hidden.py"
            self.assertTrue(written.is_file())
            self.assertEqual(written.read_bytes(), b"HIDDEN\n")
            self.assertFalse(stat_is_fifo(work / "sub"))

    def test_a_socket_where_a_hidden_folder_must_be_is_removed(self):
        with outside_folder() as folder:
            work = folder / "work"
            work.mkdir()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.bind(os.fspath(work / "sub"))
                self._task().write_hidden(work)
                written = work / "sub" / "hidden.py"
                self.assertTrue(written.is_file())
                self.assertEqual(written.read_bytes(), b"HIDDEN\n")
            finally:
                sock.close()

    def test_a_parent_that_cannot_be_made_is_a_task_error(self):
        real_mkdir = Path.mkdir

        def boom(path, *args, **kwargs):
            if path.name == "sub":
                raise FileExistsError(errno.EEXIST, "File exists", os.fspath(path))
            return real_mkdir(path, *args, **kwargs)

        with outside_folder() as folder:
            work = folder / "work"
            work.mkdir()
            with patch.object(Path, "mkdir", boom):
                with self.assertRaises(tasks.TaskError):
                    self._task().write_hidden(work)
                with self.assertRaises(tasks.TaskError):
                    tasks.apply_files(work, {"sub/answer.py": b"SOLUTION\n"})


class TextStdinTests(unittest.TestCase):
    def test_text_stdin_is_refused_before_a_process_exists(self):
        with outside_folder() as folder:
            marker = folder / "ran"
            command = [sys.executable, "-B", "-c",
                       f"open({str(marker)!r}, 'w').write('ran')"]
            with self.assertRaises(ValueError):
                arms.run_process(command, cwd=folder, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                                 stdin="text", timeout_s=2)
            self.assertFalse(marker.exists(), "the command ran with the text dropped")


def stat_is_fifo(path: Path) -> bool:
    try:
        return path.is_fifo()
    except OSError:
        return False


if __name__ == "__main__":
    unittest.main()
