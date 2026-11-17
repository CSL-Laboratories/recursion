"""EVAL-E witnesses: archive text, stand-in bytes, record text, and the replacement cap.

Scratch bases are folders under the test's own temporary directory. A base under the real runtime
directory is refused, so the evidence path that joins XDG_RUNTIME_DIR would error first.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import ntpath
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from eval import PACKAGE_ROOT, arms, gitutil, record, runner, scratch, standin_engine, tasks, txtar, validate
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
        errors = record.validate(candidate)
        self.assertTrue(
            any(line.startswith("record.plan.replacements_per_task:") for line in errors),
            errors)


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


# Shapes the review showed still carrying a value into the record. The marker is synthetic.
_MARK = "SYNTHETIC-PRIVATE-VALUE-9f3a"
_SHAPES = (
    ("scheme", f"authorization=Bearer {_MARK}"),
    ("header", f"Authorization: Bearer {_MARK}"),
    ("header-lower", f"authorization: bearer {_MARK}"),
    ("json", '{"token": "' + _MARK + '"}'),
    ("json-space", '{"api_key": "' + _MARK + '"}'),
    ("flag", f"claude --api-key {_MARK}"),
    ("yaml", f"password: {_MARK}"),
    ("env", f"export ANTHROPIC_API_KEY={_MARK}"),
)


class RedactionTests(unittest.TestCase):
    def test_r1_each_named_shape_drops_the_value(self):
        for label, text in _SHAPES:
            with self.subTest(label=label):
                out = arms.redact_detail(text)
                self.assertNotIn(_MARK, out)
                self.assertIn("[redacted]", out)

    def test_r1_parse_stream_json_drops_each_shape_in_its_own_detail(self):
        # This pins the call inside parse_stream_json. The row builder is not involved.
        for label, text in _SHAPES:
            with self.subTest(label=label):
                output = json.dumps({"type": "result", "subtype": "error", "error": text}).encode()
                parsed = arms.parse_stream_json(output, cost_basis="reported_not_billed")
                self.assertNotIn(_MARK, parsed["detail"] or "")
                self.assertIn("[redacted]", parsed["detail"])

    def test_r1_set_error_drops_each_shape_in_the_row(self):
        # This pins the call inside runner._set_error. The detail arrives unredacted.
        for label, text in _SHAPES:
            with self.subTest(label=label):
                row = runner.blank_trial("r.t.1", "t", 1, True, None)
                runner._set_error(row, "setup_failed", text)
                self.assertNotIn(_MARK, row["error"]["detail"] or "")
                self.assertIn("[redacted]", row["error"]["detail"])

    def test_r1_a_scheme_value_does_not_reach_the_record(self):
        output = json.dumps({"type": "result", "subtype": "error",
                             "error": f"engine failed: authorization=Bearer {_MARK}"}).encode()
        parsed = arms.parse_stream_json(output, cost_basis="reported_not_billed")
        self.assertNotIn(_MARK, parsed["detail"] or "")
        row = runner.blank_trial("r.t.1", "t", 1, True, None)
        row["grade"]["verdict"] = "unresolved"
        runner.classify(row, {"verdict": "unresolved", "detail": "tests exit 1"}, SimpleNamespace(**parsed))
        self.assertNotIn(_MARK, record.dumps(row))

    def test_m8_parse_stream_json_redacts_before_the_row_is_built(self):
        marker = "SYNTHETIC_PRIVATE_VALUE_NOT_A_CREDENTIAL"
        output = json.dumps({"type": "result", "subtype": "error", "is_error": True,
                             "error": f"upstream failure; authorization={marker}"}).encode()
        parsed = arms.parse_stream_json(output, cost_basis="reported_not_billed")
        self.assertNotIn(marker, parsed["detail"] or "")
        self.assertIn("[redacted]", parsed["detail"])

    def test_m9_set_error_redacts_the_row_itself(self):
        marker = "SYNTHETIC_PRIVATE_VALUE_NOT_A_CREDENTIAL"
        row = runner.blank_trial("r.t.1", "t", 1, True, None)
        runner._set_error(row, "setup_failed", f"the engine said authorization={marker}")
        self.assertNotIn(marker, row["error"]["detail"] or "")
        self.assertIn("[redacted]", row["error"]["detail"])

    def test_r1_text_that_is_not_a_value_stays(self):
        kept = (
            "tokens=5",
            "the token is missing",
            "password reset requested",
            "api_key rotation",
            "a=b",
            "secret= ",
            "secret =",
            "Authorization= ",
        )
        for text in kept:
            with self.subTest(text=text):
                self.assertEqual(arms.redact_detail(text), text)
        self.assertEqual(arms.redact_detail("token=\nnext"), "token=[redacted]")
        self.assertEqual(arms.redact_detail(f"token={_MARK} other=keep"), "token=[redacted] other=keep")
        self.assertNotIn(_MARK, arms.redact_detail(f"token=abc password={_MARK}"))
        self.assertEqual(arms.redact_detail(f'password="{_MARK}"'), "password=[redacted]")
        self.assertNotIn(_MARK, arms.redact_detail(f"https://x.invalid/v1?api_key={_MARK}"))
        self.assertEqual(arms.redact_detail(f"token = {_MARK}"), "token=[redacted]")
        self.assertIsNone(arms.redact_detail(None))
        self.assertEqual(arms.redact_detail(5), 5)
        self.assertEqual(arms.redact_detail(b"token=x"), b"token=x")
        self.assertEqual(arms.redact_detail(""), "")

    def test_r1_a_second_caller_does_not_see_the_other_value(self):
        marks = ("SYNTHETIC-PRIVATE-VALUE-aaaa", "SYNTHETIC-PRIVATE-VALUE-bbbb")
        out = [None, None]

        def run(slot):
            out[slot] = arms.redact_detail(f"token={marks[slot]}")

        threads = [threading.Thread(target=run, args=(slot,)) for slot in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertNotIn(marks[0], out[0])
        self.assertNotIn(marks[1], out[1])
        self.assertNotIn(marks[1], out[0])
        self.assertNotIn(marks[0], out[1])

    def test_r1_a_long_value_is_one_pass(self):
        marker = "SYNTHETIC-PRIVATE-VALUE-long"
        text = "token=" + ("A" * 200_000) + " " + marker
        out = arms.redact_detail(text)
        self.assertNotIn("A" * 100, out)
        self.assertIn(marker, out)
        self.assertIn("[redacted]", out)


class DigestNameTests(unittest.TestCase):
    def test_d1_a_name_that_is_not_utf8_has_a_digest(self):
        raw_name = b"bad-\xff-name"
        with tempfile.TemporaryDirectory(prefix="eval-e-surrogate-") as temp:
            root = Path(temp) / "tree"
            root.mkdir()
            (root / "plain").write_bytes(b"x")
            self._write_raw(root, raw_name, b"x")
            digest = tasks.dir_digest(root)
            self.assertEqual(len(digest), 64)
            self.assertEqual(digest, tasks.dir_digest(root))
            plain = Path(temp) / "plain-only"
            plain.mkdir()
            (plain / "plain").write_bytes(b"x")
            self.assertNotEqual(digest, tasks.dir_digest(plain))

            task_root = Path(temp) / "tbad"
            task_root.mkdir()
            (task_root / "task.json").write_text(
                json.dumps({"id": "tbad", "test_command": ["{python}", "-m", "unittest"]}) + "\n",
                encoding="utf-8")
            (task_root / "prompt.md").write_text("Do the task.\n", encoding="utf-8")
            (task_root / "hidden").mkdir()
            (task_root / "hidden" / "test_one.py").write_bytes(b"assert True\n")
            self._write_raw(task_root, raw_name, b"x")
            loaded = tasks.load_dir(task_root)
            self.assertEqual(loaded.sha256, tasks.dir_digest(task_root))

    @staticmethod
    def _write_raw(folder, name, data):
        fd = os.open(os.path.join(os.fsencode(folder), name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)


class TxtarAnswerTests(unittest.TestCase):
    def test_t1_cr_only_endings_are_refused(self):
        self.assertEqual(txtar.parse(""), ("", {}))
        self.assertEqual(txtar.parse("-- a.txt --\r\nbody\r\n")[1], {"a.txt": "body\r\n"})
        with self.assertRaises(txtar.TxtarError):
            txtar.parse("-- a.txt --\rbody one\r-- b.txt --\rbody two\r")
        with self.assertRaises(txtar.TxtarError):
            txtar.parse("-- a.txt --\nbody\r")

    def test_t2_format_keeps_a_body_that_ends_in_a_bare_cr(self):
        text = txtar.format("", {"a.txt": "body\r"})
        self.assertTrue(text.endswith("body\r"), text)
        self.assertFalse(text.endswith("body\r\n"), text)
        self.assertEqual(txtar.format("", {"a.txt": "body"}), "-- a.txt --\nbody\n")
        self.assertEqual(txtar.format("", {"a.txt": "body\n"}), "-- a.txt --\nbody\n")
        self.assertEqual(txtar.format("", {"a.txt": "body\r\n"}), "-- a.txt --\nbody\r\n")

    def test_t3_a_colon_is_a_drive_letter_only_in_the_first_component(self):
        self.assertEqual(txtar.check_name("docs/v1:2/notes.md"), "docs/v1:2/notes.md")
        self.assertEqual(txtar.check_name("docs/a:b/c.txt"), "docs/a:b/c.txt")
        self.assertEqual(txtar.check_name("a\u2028b.txt"), "a\u2028b.txt")
        refused = ("a:b.txt", "x:y", "C:/temp/s6-outside.txt", "a\x00b", "a\nb", "a\rb", "a\tb",
                   "a\x0bb", "a\x1fb", "a\x7fb")
        for name in refused:
            with self.subTest(name=name):
                with self.assertRaises(txtar.TxtarError):
                    txtar.check_name(name)
        text = txtar.format("", {"a\u2028b.txt": "x\n"})
        self.assertEqual(txtar.parse(text)[1], {"a\u2028b.txt": "x\n"})


class StandinPlanTests(unittest.TestCase):
    def _run(self, content):
        root = tempfile.TemporaryDirectory(prefix="eval-e-plan-")
        self.addCleanup(root.cleanup)
        work = Path(root.name)
        plan_path = work / "plan.json"
        plan_path.write_text(json.dumps({"outcomes": ["solve"], "solution": {"a.bin": content}}),
                             encoding="utf-8")
        stdin = io.StringIO(json.dumps(
            {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}}) + "\n")
        stdout = io.StringIO()
        old_cwd = os.getcwd()
        old_in, old_out = sys.stdin, sys.stdout
        os.chdir(work)
        sys.stdin, sys.stdout = stdin, stdout
        try:
            with patch.dict(os.environ, {"EVAL_STANDIN_PLAN": str(plan_path), "EVAL_STANDIN_TRIAL": "1"}):
                code = standin_engine.main()
        finally:
            sys.stdin, sys.stdout = old_in, old_out
            os.chdir(old_cwd)
        return code, stdout.getvalue(), work / "a.bin"

    def test_s1_a_bad_plan_entry_is_not_written_and_the_engine_still_speaks(self):
        cases = {
            "missing": {"encoding": "base64"},
            "none": {"encoding": "base64", "data": None},
            "junk": {"encoding": "base64", "data": "QUJD!!!!"},
            "nonascii": {"encoding": "base64", "data": "é"},
            "int": {"encoding": "base64", "data": 5},
        }
        for label, content in cases.items():
            with self.subTest(label=label):
                code, spoken, path = self._run(content)
                self.assertEqual(code, 0, spoken)
                self.assertIn('"type": "result"', spoken)
                self.assertIn("stand_in_bad_file", spoken)
                self.assertFalse(path.exists(), spoken)

    def test_s2_a_mode_other_than_644_or_755_is_written_back(self):
        self.assertEqual(tasks._plan_file(b"text\n", None), "text\n")
        self.assertEqual(tasks._plan_file(b"text\n", 0o644), "text\n")
        self.assertEqual(tasks._plan_file(b"#!/bin/sh\n", 0o755)["mode"], "100755")
        planned = tasks._plan_file(b"#!/bin/sh\n", 0o700)
        self.assertEqual(planned["mode"], "100700")
        code, spoken, path = self._run(planned)
        self.assertEqual(code, 0, spoken)
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_s1_a_valid_base64_entry_is_still_written(self):
        import base64
        content = {"encoding": "base64", "data": base64.b64encode(b"\xff\x00").decode("ascii"),
                   "mode": "100644"}
        code, spoken, path = self._run(content)
        self.assertEqual(code, 0, spoken)
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), b"\xff\x00")


class GitFolderTests(unittest.TestCase):
    def test_c1_a_missing_work_folder_is_a_git_error(self):
        with tempfile.TemporaryDirectory(prefix="eval-e-gitfolder-") as temp:
            missing = Path(temp) / "missing"
            with self.assertRaises(gitutil.GitError):
                gitutil.git(["status"], missing)
            file_cwd = Path(temp) / "a-file"
            file_cwd.write_text("x", encoding="utf-8")
            with self.assertRaises(gitutil.GitError):
                gitutil.git(["status"], file_cwd)
            with patch.dict(os.environ, {"PATH": temp}):
                with self.assertRaises(FileNotFoundError):
                    gitutil.git(["status"], Path(temp))


class StartFileTests(unittest.TestCase):
    def test_c2_a_normalised_name_is_the_entry(self):
        with tempfile.TemporaryDirectory(prefix="eval-e-start-") as temp:
            archive_path = Path(temp) / "start.tar"
            with tarfile.open(archive_path, "w") as archive:
                member = tarfile.TarInfo("src/value.txt")
                member.size = 5
                archive.addfile(member, io.BytesIO(b"value"))
            target = task(start_tar=archive_path)
            self.assertEqual(target.start_file("src/value.txt"), b"value")
            self.assertEqual(target.start_file("src/./value.txt"), b"value")
            self.assertEqual(target.start_file("./src/value.txt"), b"value")
            self.assertEqual(target.start_file("src//value.txt"), b"value")
            self.assertIsNone(target.start_file("src/other.txt"))
            files = task(start_files={"src/value.txt": b"value"})
            self.assertEqual(files.start_file("src/./value.txt"), b"value")


if __name__ == "__main__":
    unittest.main()
