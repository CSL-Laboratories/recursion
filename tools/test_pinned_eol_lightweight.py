#!/usr/bin/env python3
"""CSL-282: docs-only CI enforces the shared pinned-path checkout policy."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("eol_checker", HERE / "check_program_control.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class PinnedEolLightweightTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.ledger = self.root / "docs/v3/program-control/program.json"
        self.ledger.parent.mkdir(parents=True)
        self.ledger.write_text('{"contracts": []}', encoding="utf-8")
        checker._git_check_attr.cache_clear()

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def run_check(self):
        output = io.StringIO()
        with mock.patch.object(checker, "ROOT", self.root), \
                mock.patch.object(checker, "LEDGER", self.ledger), \
                mock.patch.object(sys, "argv", ["check_program_control.py", "--pinned-eol-only"]), \
                mock.patch.object(checker, "validate_program", side_effect=AssertionError("full validator invoked")), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = checker.main()
        return result, output.getvalue()

    def test_valid_lf_and_binary_pins_do_not_invoke_full_validator(self):
        self.write("docs/packet/payload.md", "payload\n")
        self.write("docs/packet/blob.bin", "binary\n")
        self.write("docs/packet/SHA256SUMS.txt", "a" * 64 + "  payload.md\n" + "b" * 64 + "  blob.bin\n")
        self.write(".gitattributes", "*.md text eol=lf\n*.bin -text\n")
        result, output = self.run_check()
        self.assertEqual(result, 0, output)
        self.assertIn("PINNED_EOL_PASS checkout_policy_only=true", output)
        self.assertNotIn("PROGRAM_CONTROL_PASS", output)

    def test_uncovered_contract_manifest_and_audit_paths_fail(self):
        for source in ("contract", "sha256sums", "manifest", "audit"):
            with self.subTest(source=source):
                self.write("docs/packet/payload.md", "payload\n")
                if source == "contract":
                    self.ledger.write_text(json.dumps({"contracts": [{"path": "docs/packet/payload.md"}]}), encoding="utf-8")
                elif source == "sha256sums":
                    self.write("docs/packet/SHA256SUMS.txt", "a" * 64 + "  payload.md\n")
                elif source == "manifest":
                    self.write("docs/packet/MANIFEST.json", json.dumps({"files": [{"path": "payload.md", "sha256": "a" * 64}]}))
                else:
                    self.write("docs/audits/receipt.txt", "receipt\n")
                result, output = self.run_check()
                self.assertEqual(result, 1, output)
                self.assertIn("lacks eol=lf", output)
                self.assertNotIn("_PASS", output)
                self.ledger.write_text('{"contracts": []}', encoding="utf-8")
                for path in ("docs/packet/SHA256SUMS.txt", "docs/packet/MANIFEST.json", "docs/audits/receipt.txt"):
                    (self.root / path).unlink(missing_ok=True)

    def test_invalid_ledger_json_and_pin_shapes_fail_closed(self):
        for data in ("{", "[]", "{}", '{"contracts": {}}', '{"contracts": [null]}',
                     '{"contracts": [{}]}', '{"contracts": [{"path": 3}]}',
                     '{"contracts": [{"path": "../outside"}]}'):
            with self.subTest(data=data):
                self.ledger.write_text(data, encoding="utf-8")
                result, output = self.run_check()
                self.assertEqual(result, 1, output)
                self.assertIn("PINNED_EOL_FAIL", output)
                self.assertNotIn("_PASS", output)

    def test_missing_or_non_utf8_ledger_fails_closed(self):
        self.ledger.unlink()
        self.assertEqual(self.run_check()[0], 1)
        self.ledger.write_bytes(b"\xff")
        self.assertEqual(self.run_check()[0], 1)

    def test_invalid_manifests_fail_closed(self):
        for name, payload in (("SHA256SUMS.txt", b"bad digest  payload.md\n"),
                              ("SHA256SUMS.txt", b"\xff"),
                              ("MANIFEST.json", b"{"),
                              ("MANIFEST.json", b'{"files":[{}]}'),
                              ("MANIFEST.json", b'{"files":[],"note":"\xff"}')):
            with self.subTest(name=name, payload=payload):
                manifest = self.root / name
                manifest.write_bytes(payload)
                result, output = self.run_check()
                self.assertEqual(result, 1, output)
                self.assertIn("PINNED_EOL_FAIL", output)
                manifest.unlink()

    def test_case_insensitive_glob_keeps_the_same_inventory_on_windows(self):
        for name in ("manifest.json", "sha256sums.txt"):
            self.write(name, "unrelated schema\n")
        glob = Path.glob
        def insensitive_glob(path, pattern):
            # Model Windows matching on every platform.
            return glob(path, pattern.lower())
        with mock.patch.object(Path, "glob", insensitive_glob):
            self.assertEqual(self.run_check()[0], 0)

    def test_git_failure_and_missing_output_fail_closed(self):
        self.write("docs/audits/receipt.txt", "receipt\n")
        for failure in (FileNotFoundError("git unavailable"), subprocess.TimeoutExpired("git", 60),
                        subprocess.CompletedProcess([], 1, "", "not a repository"),
                        subprocess.CompletedProcess([], 0, "", ""),
                        subprocess.CompletedProcess([], 0, "docs/audits/receipt.txt: eol: lf\n", ""),
                        subprocess.CompletedProcess([], 0, "malformed\n", ""),
                        subprocess.CompletedProcess([], 0, "docs/audits/receipt.txt: eol: lf\n" * 2, "")):
            with self.subTest(failure=failure):
                checker._git_check_attr.cache_clear()
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with mock.patch.object(checker.subprocess, "run", **kwargs):
                    result, output = self.run_check()
                self.assertEqual(result, 1, output)
                self.assertIn("PINNED_EOL_FAIL", output)

    def test_docs_only_gate_rejects_uncovered_pin_then_passes_with_lf(self):
        for name in ("ci_gate.py", "check_program_control.py", "check_public_scope.py"):
            destination = self.root / "tools" / name
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(HERE / name, destination)
        self.write(".gitattributes", "docs/covered/** text eol=lf\n")
        env = {**os.environ, "GIT_AUTHOR_NAME": "eol-test", "GIT_AUTHOR_EMAIL": "eol@example.com",
               "GIT_COMMITTER_NAME": "eol-test", "GIT_COMMITTER_EMAIL": "eol@example.com"}
        def commit():
            subprocess.run(["git", "add", "."], cwd=self.root, env=env, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-qm", "test"], cwd=self.root, env=env, check=True)
            return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        base = commit()
        self.write("docs/packet/payload.md", "payload\n")
        self.write("docs/packet/SHA256SUMS.txt", "a" * 64 + "  payload.md\n")
        commit()
        env = {**env, "CI_BASE_SHA": base, "CI_HEAD_SHA": "", "GITHUB_OUTPUT": ""}
        def run_gate():
            return subprocess.run([sys.executable, "-X", "utf8", "-B", "tools/ci_gate.py"],
                                  cwd=self.root, env=env, text=True, capture_output=True)
        failed = run_gate()
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertIn("docs-only diff", failed.stdout)
        self.assertIn("lacks eol=lf", failed.stderr)
        self.assertNotIn("CI_GATE_PASS", failed.stdout)
        (self.root / "docs/packet").rename(self.root / "docs/covered")
        commit()
        passed = run_gate()
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.assertIn("docs-only diff", passed.stdout)
        self.assertIn("PINNED_EOL_PASS", passed.stdout)
        self.assertNotIn("CI_GATE_PASS", passed.stdout)
        self.assertNotIn("test_program_control.py", passed.stdout)


if __name__ == "__main__":
    unittest.main()
