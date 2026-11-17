"""Answers to the Sonnet read of draft #475.

The redactor must drop ``Authorization: token <value>`` and ``AWS_SECRET_ACCESS_KEY``,
and the scan of engine text must stay one forward read. A bad stand-in plan entry must
refuse before it removes anything that is already in the work tree.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from eval import arms, standin_engine

_MARK = "SYNTHETIC-PRIVATE-VALUE-9f3a"


class RedactorGapTests(unittest.TestCase):
    def test_authorization_token_and_aws_secret_access_key_are_redacted(self):
        # The exact shapes named for this answer. The value must not remain.
        shapes = (
            f"Authorization: token {_MARK}",
            f"Authorization: Token {_MARK}",
            f"AWS_SECRET_ACCESS_KEY={_MARK}",
            f"AWS_SECRET_ACCESS_KEY: {_MARK}",
        )
        for text in shapes:
            with self.subTest(text=text):
                out = arms.redact_detail(text)
                self.assertNotIn(_MARK, out)
                self.assertIn("[redacted]", out)

    def test_authorization_keeps_the_following_line(self):
        text = f"Authorization: token {_MARK}\nkeep-this"
        out = arms.redact_detail(text)
        self.assertNotIn(_MARK, out)
        self.assertIn("keep-this", out)
        self.assertIn("[redacted]", out)

    def test_a_header_scheme_does_not_leave_the_rest_of_its_line(self):
        text = f"Authorization: Digest username=a response={_MARK}"
        out = arms.redact_detail(text)
        self.assertNotIn(_MARK, out)
        self.assertIn("[redacted]", out)

    def test_names_the_read_listed_are_redacted(self):
        shapes = (
            f"secret_key={_MARK}",
            f"private_key={_MARK}",
            f"access_key={_MARK}",
            f"cookie: {_MARK}",
            f"session={_MARK}",
            f"--authorization token {_MARK}",
        )
        for text in shapes:
            with self.subTest(text=text):
                out = arms.redact_detail(text)
                self.assertNotIn(_MARK, out)
                self.assertIn("[redacted]", out)

    def test_an_escaped_quoted_value_does_not_keep_the_part_after_a_space(self):
        text = 'token=\\"abc ' + _MARK + '\\"'
        out = arms.redact_detail(text)
        self.assertNotIn(_MARK, out)
        self.assertIn("[redacted]", out)
        self.assertNotIn("abc", out)

    def test_a_name_that_only_shares_a_prefix_stays(self):
        kept = (
            "tokens=5",
            "sessions=5",
            "cookiejar=5",
            "secret_recipe=keep",
            "Authorization= ",
            "secret= ",
            f"token={_MARK} other=keep",
        )
        for text in kept:
            with self.subTest(text=text):
                if text.startswith("token=") and _MARK in text:
                    self.assertEqual(arms.redact_detail(text), "token=[redacted] other=keep")
                else:
                    self.assertEqual(arms.redact_detail(text), text)

    def test_redact_detail_on_many_separators_stays_under_the_time_bound(self):
        # Many separators and no "--". A scan that restarts from each one is quadratic.
        # 160_000 colons is about two seconds that way on this machine, and a few
        # hundredths of a second as one forward read. The bound sits between them.
        count = 160_000
        text = (":" * count) + f"token={_MARK}"
        started = time.perf_counter()
        out = arms.redact_detail(text)
        elapsed = time.perf_counter() - started
        self.assertNotIn(_MARK, out)
        self.assertIn("[redacted]", out)
        self.assertLess(elapsed, 0.50, f"redact_detail took {elapsed:.3f}s on {count} separators")


class StandinRefusalTests(unittest.TestCase):
    def _run(self, content, prepare, files=None):
        root = tempfile.TemporaryDirectory(prefix="eval-475-plan-")
        self.addCleanup(root.cleanup)
        work = Path(root.name)
        prepare(work)
        plan_path = work / "plan.json"
        plan_path.write_text(
            json.dumps({"outcomes": ["solve"], "solution": files if files is not None else {"a.bin": content}}),
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
        return code, stdout.getvalue(), work

    def test_a_bad_plan_entry_does_not_delete_the_start_file(self):
        def prepare(work):
            (work / "a.bin").write_bytes(b"KEEP-START\n")

        code, spoken, work = self._run({"encoding": "base64", "data": "QUJD!!!!"}, prepare)
        self.assertEqual(code, 0, spoken)
        self.assertIn("stand_in_bad_file", spoken)
        self.assertIn('"type": "result"', spoken)
        self.assertEqual((work / "a.bin").read_bytes(), b"KEEP-START\n")

    def test_a_bad_plan_entry_does_not_delete_the_start_folder(self):
        def prepare(work):
            (work / "a.bin").mkdir()
            (work / "a.bin" / "keep.txt").write_bytes(b"KEEP-START\n")

        code, spoken, work = self._run({"encoding": "base64", "data": "QUJD!!!!"}, prepare)
        self.assertEqual(code, 0, spoken)
        self.assertIn("stand_in_bad_file", spoken)
        self.assertEqual((work / "a.bin" / "keep.txt").read_bytes(), b"KEEP-START\n")

    def test_a_bad_plan_entry_does_not_remove_a_link_at_the_path(self):
        def prepare(work):
            target = work / "kept.txt"
            target.write_bytes(b"KEEP-START\n")
            os.symlink(target, work / "a.bin")

        code, spoken, work = self._run({"encoding": "base64", "data": "QUJD!!!!"}, prepare)
        self.assertEqual(code, 0, spoken)
        self.assertIn("stand_in_bad_file", spoken)
        self.assertTrue((work / "a.bin").is_symlink())
        self.assertEqual((work / "kept.txt").read_bytes(), b"KEEP-START\n")

    def test_a_bad_plan_entry_does_not_remove_a_file_standing_in_a_parent(self):
        def prepare(work):
            (work / "nested").write_bytes(b"KEEP-START\n")

        # The plan names nested/a.bin. ``nested`` is a file, so it stands where a folder must be.
        code, spoken, work = self._run(
            None, prepare,
            files={"nested/a.bin": {"encoding": "base64", "data": "QUJD!!!!"}})
        self.assertEqual(code, 0, spoken)
        self.assertIn("stand_in_bad_file", spoken)
        self.assertEqual((work / "nested").read_bytes(), b"KEEP-START\n")

    def test_a_good_plan_entry_still_replaces_the_start_file(self):
        def prepare(work):
            (work / "a.bin").write_bytes(b"KEEP-START\n")

        code, spoken, work = self._run("new-bytes\n", prepare)
        self.assertEqual(code, 0, spoken)
        self.assertEqual((work / "a.bin").read_bytes(), b"new-bytes\n")


if __name__ == "__main__":
    unittest.main()
