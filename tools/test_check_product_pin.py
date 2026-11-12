#!/usr/bin/env python3
"""Tests for tools/check_product_pin.py, plus a check of the committed pin (CSL-585)."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("check_product_pin", ROOT / "tools" / "check_product_pin.py")
pin_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pin_check)

AWAITING = {
    "format": "csl-product-pin/v1",
    "status": "awaiting",
    "repository": "CSL-Laboratories/csl-research",
    "path": None,
    "commit": None,
    "sha256": None,
    "claim": None,
    "limitations": "No contract is pinned yet.",
}
PINNED = {
    **AWAITING,
    "status": "pinned",
    "path": "contracts/recursion/PRODUCT.md",
    "commit": "a" * 40,
    "sha256": "b" * 64,
    "claim": "Recursion product contract, candidate.",
}
# The committed contract/PRODUCT.pin.json names exactly these csl-research bytes.
COMMITTED_COMMIT = "d84cdd26a4de2366f3c5d34c4aec5215f822a494"
COMMITTED_SHA256 = "e1d5fb8c02594b2ca88281113ffc14e6f3125daded2a00d45739cfb9d2ed04dd"
# One NUL, one other control character and one lone surrogate: none can be passed to git or printed raw.
UNSAFE_PATHS = ("contracts/PRO\x00DUCT.md", "contracts/PRO\nDUCT.md", "contracts/PRO\ud800DUCT.md")
UNSAFE_PATH = "path must not contain control characters, line breaks or lone surrogates"


class ShapeTests(unittest.TestCase):
    def test_committed_pin_is_valid(self) -> None:
        pin = json.loads(pin_check.PIN.read_text(encoding="utf-8"))
        self.assertEqual(pin_check.errors(pin), [])
        self.assertEqual(pin["status"], "pinned")
        self.assertEqual(pin["repository"], "CSL-Laboratories/csl-research")
        self.assertEqual(pin["path"], "contracts/PRODUCT.md")
        self.assertEqual(pin["commit"], COMMITTED_COMMIT)
        self.assertEqual(pin["sha256"], COMMITTED_SHA256)

    def test_awaiting_and_pinned_examples_pass(self) -> None:
        self.assertEqual(pin_check.errors(AWAITING), [])
        self.assertEqual(pin_check.errors(PINNED), [])

    def test_awaiting_rejects_partial_pin(self) -> None:
        pin = {**AWAITING, "commit": "a" * 40}
        self.assertIn("commit must be null while status is awaiting", pin_check.errors(pin))

    def test_pinned_rejects_short_commit_and_bad_digest(self) -> None:
        pin = {**PINNED, "commit": "abc1234", "sha256": "B" * 64}
        problems = pin_check.errors(pin)
        self.assertIn("commit must be a full 40-hex lowercase SHA", problems)
        self.assertIn("sha256 must be 64 lowercase hex characters", problems)

    def test_pinned_rejects_trailing_newline_in_hex_fields(self) -> None:
        problems = pin_check.errors({**PINNED, "commit": "a" * 40 + "\n", "sha256": "b" * 64 + "\n"})
        self.assertIn("commit must be a full 40-hex lowercase SHA", problems)
        self.assertIn("sha256 must be 64 lowercase hex characters", problems)

    def test_pinned_rejects_escaping_paths(self) -> None:
        for path in ("/etc/passwd", "../x.md", "a/../b.md", "a\\b.md", "./a.md"):
            with self.subTest(path=path):
                self.assertIn(
                    "path must be a relative posix path inside the repository",
                    pin_check.errors({**PINNED, "path": path}),
                )

    def test_pinned_rejects_control_characters_and_lone_surrogates(self) -> None:
        for path in UNSAFE_PATHS:
            with self.subTest(path=ascii(path)):
                problems = pin_check.errors({**PINNED, "path": path})
                self.assertEqual(problems, [UNSAFE_PATH])
                "\n".join(problems).encode("utf-8")  # printable: raises if the bad string leaked in

    def test_pinned_rejects_unicode_line_separators(self) -> None:
        # U+2028/U+2029 are line breaks but not category Cc, so they used to pass shape
        # checks and then split a FAIL line into a second line that starts with PASS.
        for sep in ("\u2028", "\u2029"):
            path = f"contracts/PRO{sep}PASS: product pin is pinned.md"
            with self.subTest(sep=ascii(sep)):
                problems = pin_check.errors({**PINNED, "path": path})
                self.assertEqual(problems, [UNSAFE_PATH])
                self.assertFalse(any(line.startswith("PASS:") for line in "\n".join(problems).splitlines()))

    def test_rejects_other_repository_unknown_keys_and_status(self) -> None:
        pin = copy.deepcopy(PINNED)
        pin["repository"] = "CSL-Laboratories/recursion"
        pin["accepted"] = True
        problems = pin_check.errors(pin)
        self.assertIn("unknown keys: accepted", problems)
        self.assertTrue(any(p.startswith("repository must be") for p in problems))
        self.assertIn("status must be 'awaiting' or 'pinned'", pin_check.errors({**AWAITING, "status": "accepted"}))

    def test_rejects_blank_limitations_and_claim(self) -> None:
        self.assertIn("limitations must be a non-empty string", pin_check.errors({**AWAITING, "limitations": " "}))
        self.assertIn("claim must be a non-empty string", pin_check.errors({**PINNED, "claim": ""}))


class VaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)
        git = ["git", "-C", str(self.vault), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run([*git[:3], "init", "-q"], check=True)
        target = self.vault / "contracts" / "recursion" / "PRODUCT.md"
        target.parent.mkdir(parents=True)
        self.body = b"# Product\n"
        target.write_bytes(self.body)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "contract"], check=True)
        self.commit = subprocess.check_output([*git[:3], "rev-parse", "HEAD"], text=True).strip()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def pin(self, digest: str) -> dict:
        return {**PINNED, "commit": self.commit, "sha256": digest}

    def test_matching_bytes_pass(self) -> None:
        digest = hashlib.sha256(self.body).hexdigest()
        self.assertEqual(pin_check.vault_errors(self.pin(digest), self.vault), [])

    def test_mismatch_and_missing_path_fail(self) -> None:
        self.assertTrue(pin_check.vault_errors(self.pin("0" * 64), self.vault)[0].startswith("sha256 mismatch"))
        missing = {**self.pin("0" * 64), "path": "contracts/recursion/NOPE.md"}
        self.assertTrue(pin_check.vault_errors(missing, self.vault)[0].startswith("cannot read"))

    def test_awaiting_needs_no_vault(self) -> None:
        self.assertEqual(pin_check.vault_errors(AWAITING, self.vault / "absent"), [])

    def test_nul_or_lone_surrogate_fails_cleanly(self) -> None:
        digest = hashlib.sha256(self.body).hexdigest()
        cases = [({**self.pin(digest), "path": path}, self.vault) for path in UNSAFE_PATHS[::2]]
        cases += [(self.pin(digest), Path(f"{self.vault}\x00")), (self.pin(digest), Path(f"{self.vault}\ud800"))]
        for pin, vault in cases:
            with self.subTest(path=ascii(pin["path"]), vault=ascii(str(vault))):
                problems = pin_check.vault_errors(pin, vault)
                self.assertEqual(len(problems), 1)
                self.assertTrue(problems[0].startswith("cannot read"), problems[0])
                self.assertNotIn("\x00", problems[0])
                problems[0].encode("utf-8")

    def test_git_timeout_fails_cleanly(self) -> None:
        expired = subprocess.TimeoutExpired(["git"], pin_check.GIT_TIMEOUT_SECONDS)
        with mock.patch.object(pin_check.subprocess, "run", side_effect=expired) as run:
            problems = pin_check.vault_errors(self.pin("0" * 64), self.vault)
        self.assertEqual(run.call_args.kwargs["timeout"], pin_check.GIT_TIMEOUT_SECONDS)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("cannot read"), problems[0])
        self.assertTrue(problems[0].endswith(f"git timed out after {pin_check.GIT_TIMEOUT_SECONDS} seconds"))

    def write_pin(self, path_escape: str) -> Path:
        """Write a pin whose path holds a raw JSON escape, outside the vault clone."""
        digest = hashlib.sha256(self.body).hexdigest()
        text = json.dumps({**self.pin(digest), "path": "contracts/PRO@DUCT.md"}).replace("@", path_escape)
        pin_dir = tempfile.TemporaryDirectory()
        self.addCleanup(pin_dir.cleanup)
        target = Path(pin_dir.name) / "PRODUCT.pin.json"
        target.write_text(text, encoding="utf-8")
        return target

    def test_script_fails_without_traceback_on_unsafe_path(self) -> None:
        script = str(ROOT / "tools" / "check_product_pin.py")
        for escape in ("\\u0000", "\\ud800"):
            for extra in ([], ["--vault", str(self.vault)]):
                with self.subTest(escape=escape, extra=extra):
                    done = subprocess.run(
                        [sys.executable, "-X", "utf8", "-B", script, "--pin", str(self.write_pin(escape)), *extra],
                        capture_output=True,
                        timeout=120,
                    )
                    output = (done.stdout + done.stderr).decode("utf-8", "replace")
                    self.assertEqual(done.returncode, 1, output)
                    self.assertIn(f"FAIL: {UNSAFE_PATH}", output.splitlines())
                    self.assertNotIn("Traceback", output)

    def test_main_fails_cleanly_on_unsafe_vault(self) -> None:
        pin_file = self.write_pin("")
        for vault in (f"{self.vault}\x00", f"{self.vault}\ud800"):
            with self.subTest(vault=ascii(vault)):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = pin_check.main(["--pin", str(pin_file), "--vault", vault])
                self.assertEqual(code, 1)
                self.assertTrue(out.getvalue().startswith("FAIL: cannot read"), ascii(out.getvalue()))
                self.assertNotIn("\x00", out.getvalue())
                out.getvalue().encode("utf-8")

    def test_replace_ref_cannot_substitute_pinned_bytes(self) -> None:
        """A replace ref must not make git show's substitute pass as the pinned commit."""
        good = self.body
        evil = b"EVIL-BYTES\n"
        target = self.vault / "contracts" / "recursion" / "PRODUCT.md"
        target.write_bytes(evil)
        git = ["git", "-C", str(self.vault), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "evil"], check=True)
        evil_commit = subprocess.check_output([*git[:3], "rev-parse", "HEAD"], text=True).strip()
        subprocess.run([*git[:3], "replace", self.commit, evil_commit], check=True)
        # The bytes git show returns under the replace ref are not the object named by the commit.
        swapped = pin_check.vault_errors(self.pin(hashlib.sha256(evil).hexdigest()), self.vault)
        self.assertTrue(swapped and swapped[0].startswith("sha256 mismatch"), swapped)
        self.assertEqual(pin_check.vault_errors(self.pin(hashlib.sha256(good).hexdigest()), self.vault), [])

    def test_ambient_git_location_cannot_supply_the_blob(self) -> None:
        """--vault must be the object store. Ambient git env vars must not satisfy the pin."""
        empty = Path(self.tmp.name) / "empty-repo"
        empty.mkdir()
        subprocess.run(["git", "-C", str(empty), "init", "-q"], check=True)
        digest = hashlib.sha256(self.body).hexdigest()
        cases = {
            "GIT_DIR": str(self.vault / ".git"),
            "GIT_OBJECT_DIRECTORY": str(self.vault / ".git" / "objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(self.vault / ".git" / "objects"),
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                os.environ[key] = value
                try:
                    problems = pin_check.vault_errors(self.pin(digest), empty)
                finally:
                    os.environ.pop(key, None)
                self.assertTrue(problems and problems[0].startswith("cannot read"), problems)

    def test_main_failure_cannot_print_a_pass_line(self) -> None:
        pin_file = self.write_pin("")
        cases = (
            f"{self.vault}\rPASS: product pin is pinned",
            f"{self.vault}\nPASS: product pin is pinned",
        )
        for vault in cases:
            with self.subTest(vault=ascii(vault)):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = pin_check.main(["--pin", str(pin_file), "--vault", vault])
                text = out.getvalue()
                self.assertEqual(code, 1, ascii(text))
                self.assertFalse(any(line.startswith("PASS:") for line in text.splitlines()), ascii(text))
                self.assertNotIn("\r", text)
                self.assertNotIn("\nPASS:", text)
                text.encode("utf-8")


if __name__ == "__main__":
    unittest.main()
