"""EVAL-E witnesses: archive text, stand-in bytes, record text, and the replacement cap.

Scratch bases are folders under the test's own temporary directory. A base under the real runtime
directory is refused, so the evidence path that joins XDG_RUNTIME_DIR would error first.
"""
from __future__ import annotations

import hashlib
import io
import math
import ntpath
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from eval import PACKAGE_ROOT, arms, gitutil, record, runner, scratch, tasks, txtar, validate
from eval.tests import build_record, trial


class WindowsOs:
    """Only the module-local os facade changes; stdlib and host paths stay POSIX."""
    name = "nt"
    missing = {"getuid", "geteuid", "getsid", "killpg", "fork", "setsid"}

    def __getattr__(self, name):
        if name in self.missing:
            raise AttributeError(f"module 'os' has no attribute '{name}'")
        return getattr(os, name)


def task(**kwargs):
    return tasks.Task(id="s6", prompt="ordinary prompt", test_command=[], hidden={},
                      solution={}, sha256="0" * 64, **kwargs)


class TxtarTests(unittest.TestCase):
    def test_f07_txtar_recognizes_crlf_headers_and_preserves_body(self):
        _, files = txtar.parse("-- a.txt --\r\nbody\r\n")
        self.assertEqual(files, {"a.txt": "body\r\n"})

    def test_f07_txtar_unicode_separator_inside_body_is_not_a_header(self):
        body = "hello\u2028-- b.txt --\nworld\n"
        _, files = txtar.parse("-- a.txt --\n" + body)
        self.assertEqual(files, {"a.txt": body})

    def test_F2_crlf_archive_markers_are_recognized(self):
        _, files = txtar.parse("A task\r\n-- prompt.md --\r\nDo the task.\r\n")
        self.assertEqual(files, {"prompt.md": "Do the task.\r\n"})

    def test_F1_drive_paths_are_rejected_before_a_windows_join(self):
        with self.assertRaises(txtar.TxtarError):
            txtar.check_name("C:/temp/s6-outside.txt")


class DigestTests(unittest.TestCase):
    def test_f08_task_directory_digest_is_unambiguous_for_newline_paths(self):
        digest = hashlib.sha256(b"x").hexdigest()
        with tempfile.TemporaryDirectory(prefix="eval-e-digest-") as temp:
            left, right = Path(temp) / "left", Path(temp) / "right"
            left.mkdir()
            right.mkdir()
            (left / "a").write_bytes(b"x")
            (left / "b").write_bytes(b"x")
            (right / ("a\n" + digest + "  b")).write_bytes(b"x")
            self.assertNotEqual(tasks.dir_digest(left), tasks.dir_digest(right))


class StandinRoundTripTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(prefix="eval-e-standin-")
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.tmp_base = self.root / "attempts"
        self.xdg_base = self.root / "xdg"
        self.tmp_base.mkdir()
        self.xdg_base.mkdir()

    def task(self):
        return tasks.Task(
            id="tiny", prompt="Return one.",
            test_command=["{python}", "-B", "-m", "unittest", "{hidden_modules}"],
            hidden={"test_one.py": b"import unittest\nclass T(unittest.TestCase):\n def test_one(self): self.assertEqual(1,1)\n"},
            solution={}, sha256="0" * 64, start_files={"a.py": b"x=0\n"}, parent_tree="0" * 40,
            timeout_s=2, grading_timeout_s=2)

    def reference_trial(self, start, solution, modes, assertion):
        subject = self.task()
        subject.start_files, subject.solution, subject.modes = start, solution, modes
        subject.hidden = {"test_ref.py": (
            "import os, unittest\nfrom pathlib import Path\n"
            "class T(unittest.TestCase):\n def test_reference(self): " + assertion + "\n").encode()}
        work = self.root / "pin"
        work.mkdir()
        subject.materialize_start(work)
        _, subject.parent_tree = gitutil.snapshot(work)
        checked = validate.validate(subject, reruns=2, tmp_base=self.tmp_base, xdg_base=self.xdg_base)
        self.assertTrue(checked.ok, checked.reason)
        return runner.run_attempt(runner.Context("reference", self.tmp_base, self.xdg_base, None),
                                  arms.DirectArm("standin"), subject, 1)

    def test_f08_standin_can_apply_a_binary_reference(self):
        row = self.reference_trial({"data.bin": b"\x00"}, {"data.bin": b"\xff"}, {},
                                   "self.assertEqual(Path('data.bin').read_bytes(), bytes([255]))")
        self.assertEqual(row["grade"]["verdict"], "resolved", row["error"])

    def test_f08_standin_preserves_reference_executable_mode(self):
        content = b"#!/bin/sh\nexit 0\n"
        row = self.reference_trial({"tool": content}, {"tool": content}, {"tool": 0o755},
                                   "self.assertTrue(os.access('tool', os.X_OK))")
        self.assertEqual(row["grade"]["verdict"], "resolved", row["error"])


class RecordTextTests(unittest.TestCase):
    def test_f06_arbitrary_authorization_text_is_not_copied_to_record(self):
        marker = "SYNTHETIC_PRIVATE_VALUE_NOT_A_CREDENTIAL"
        output = ('{"type":"result","subtype":"error","error":"upstream failure; authorization='
                  + marker + '\\nnext line"}').encode()
        parsed = arms.parse_stream_json(output, cost_basis="reported_not_billed")
        row = runner.blank_trial("r.t.1", "t", 1, True, None)
        row["grade"]["verdict"] = "unresolved"
        runner.classify(row, {"verdict": "unresolved", "detail": "tests exit 1"}, SimpleNamespace(**parsed))
        self.assertNotIn(marker, record.dumps(row))


class ReplacementLimitTests(unittest.TestCase):
    def test_the_plan_refuses_more_than_two_replacements_per_task_and_arm(self):
        loaded = SimpleNamespace(split="dev", frozen=False)
        with self.assertRaises(runner.RunRefused):
            runner.check_plan(loaded, [arms.DirectArm("standin")], k=1, attempts=1,
                              replacements=3, label="dev", artifacts=None)

    def test_a_record_cannot_authorize_more_than_the_protocols_two_replacements(self):
        rows = [trial("t", 1, "vendor_limit"),
                trial("t", 2, "vendor_limit", planned=False, replacement_for="r.t.1"),
                trial("t", 3, "vendor_limit", planned=False, replacement_for="r.t.2"),
                trial("t", 4, "pass", planned=False, replacement_for="r.t.3")]
        candidate = build_record(rows, k=1, attempts=1, replacements=3)
        self.assertTrue(record.validate(candidate))


class GitDeadlineTests(unittest.TestCase):
    def test_F7_git_helper_supplies_a_finite_deadline(self):
        with tempfile.TemporaryDirectory(prefix="eval-e-git-") as folder:
            folder = Path(folder)
            binary = folder / "git"
            binary.write_text(f"#!{sys.executable} -B\nimport time\ntime.sleep(0.04)\n", encoding="utf-8")
            binary.chmod(0o700)
            seen = []
            real_run = subprocess.run

            def run(*args, **kwargs):
                seen.append(kwargs.get("timeout"))
                return real_run(*args, **kwargs)

            with patch.dict(os.environ, {"PATH": str(folder)}), patch.object(subprocess, "run", side_effect=run):
                gitutil.git(["status"], folder)
            self.assertTrue(seen)
            self.assertTrue(all(value is not None and math.isfinite(value) for value in seen))


class WindowsPathTests(unittest.TestCase):
    def test_F3_tar_member_names_do_not_use_host_path_normalization(self):
        with tempfile.TemporaryDirectory(prefix="eval-e-tar-") as temp:
            archive_path = Path(temp) / "start.tar"
            with tarfile.open(archive_path, "w") as archive:
                member = tarfile.TarInfo("src/value.txt")
                member.size = 5
                archive.addfile(member, io.BytesIO(b"value"))
            target = task(start_tar=archive_path)
            with mock.patch.object(tasks.os.path, "normpath", ntpath.normpath):
                observed = target.start_file("src/value.txt")
            self.assertEqual(observed, b"value")

    def test_F4_scratch_reports_its_own_platform_refusal(self):
        with tempfile.TemporaryDirectory(prefix="eval-e-base-") as temp:
            with mock.patch.object(scratch, "os", WindowsOs()):
                with self.assertRaisesRegex(scratch.ScratchError, "platform_unsupported"):
                    scratch.check_bases(Path(temp) / "tmp", Path(temp) / "runtime")

    def test_F4_process_refuses_before_launching_on_windows(self):
        original_popen = subprocess.Popen
        with tempfile.TemporaryDirectory(prefix="eval-e-process-") as temp:
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
                   "TMPDIR": temp, "TEMP": temp, "TMP": temp, "XDG_RUNTIME_DIR": temp}
            with mock.patch.object(arms, "os", WindowsOs()), \
                    mock.patch.object(arms.subprocess, "Popen", wraps=original_popen) as started:
                with self.assertRaisesRegex(arms.ArmRefused, "platform_unsupported"):
                    arms.run_process([sys.executable, "-B", "-c", "pass"], cwd=Path(temp),
                                     env=env, stdin=None, timeout_s=2)
                started.assert_not_called()


class DocSentenceTests(unittest.TestCase):
    def test_docs_name_the_operator_and_the_unbuilt_arm(self):
        self.assertIn("operator's job", tasks.__doc__)
        self.assertIn("not sandboxed", tasks.__doc__)
        self.assertIn("NOT_BUILT", arms.__doc__)
        readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("NOT_BUILT", readme)


if __name__ == "__main__":
    unittest.main()
