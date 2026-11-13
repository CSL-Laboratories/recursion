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
import shutil
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
import zlib
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
            "GIT_COMMON_DIR": str(self.vault / ".git"),
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

    def empty_repo(self, name: str) -> Path:
        path = Path(self.tmp.name) / name
        path.mkdir()
        subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
        return path

    def test_alternates_file_cannot_supply_the_blob(self) -> None:
        """An on-disk alternates file must not make an empty vault pass."""
        digest = hashlib.sha256(self.body).hexdigest()
        for name in ("alternates", "http-alternates"):
            with self.subTest(name=name):
                empty = self.empty_repo(f"empty-{name}")
                alt = empty / ".git" / "objects" / "info" / name
                alt.parent.mkdir(parents=True, exist_ok=True)
                alt.write_text(str(self.vault / ".git" / "objects") + "\n", encoding="utf-8")
                problems = pin_check.vault_errors(self.pin(digest), empty)
                self.assertTrue(problems, problems)
                self.assertTrue(problems[0].startswith("vault object store is not self-contained"), problems)
                self.assertIn(str(alt), problems[0])
                self.assertFalse(any(line.startswith("PASS:") for line in "\n".join(problems).splitlines()))
                problems[0].encode("utf-8")

    def test_commondir_file_cannot_supply_the_blob(self) -> None:
        """A .git/commondir pointer must not make an empty vault pass."""
        empty = self.empty_repo("empty-commondir")
        commondir = empty / ".git" / "commondir"
        commondir.write_text(str(self.vault / ".git") + "\n", encoding="utf-8")
        digest = hashlib.sha256(self.body).hexdigest()
        problems = pin_check.vault_errors(self.pin(digest), empty)
        self.assertTrue(problems, problems)
        self.assertTrue(problems[0].startswith("vault object store is not self-contained"), problems)
        self.assertIn(str(commondir), problems[0])
        self.assertFalse(any(line.startswith("PASS:") for line in "\n".join(problems).splitlines()))
        problems[0].encode("utf-8")

    def test_gitdir_file_cannot_supply_the_blob(self) -> None:
        """A linked-worktree .git file must not pass, and the FAIL line stays one line."""
        digest = hashlib.sha256(self.body).hexdigest()
        linked = Path(self.tmp.name) / f"link\u2028PASS"
        linked.mkdir()
        gitdir = linked / ".git"
        gitdir.write_text(f"gitdir: {self.vault / '.git'}\n", encoding="utf-8")
        problems = pin_check.vault_errors(self.pin(digest), linked)
        self.assertTrue(problems, problems)
        self.assertTrue(problems[0].startswith("vault object store is not self-contained"), problems)
        self.assertIn("\\u2028", problems[0])
        self.assertNotIn("\u2028", problems[0])
        self.assertFalse(any(line.startswith("PASS:") for line in "\n".join(problems).splitlines()))
        problems[0].encode("utf-8")
        pin_file = self.write_pin("")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pin_check.main(["--pin", str(pin_file), "--vault", str(linked)])
        text = out.getvalue()
        self.assertEqual(code, 1, ascii(text))
        self.assertTrue(text.startswith("FAIL:"), ascii(text))
        self.assertIn("\\u2028", text)
        self.assertNotIn("\u2028", text)
        self.assertFalse(any(line.startswith("PASS:") for line in text.splitlines()), ascii(text))

        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        worktree = Path(other.name) / "wt"
        subprocess.run(["git", "-C", str(self.vault), "worktree", "add", "-q", str(worktree), "HEAD"], check=True)
        problems = pin_check.vault_errors(self.pin(digest), worktree)
        self.assertTrue(problems and problems[0].startswith("vault object store is not self-contained"), problems)
        self.assertEqual(pin_check.vault_errors(self.pin(digest), self.vault), [])

    def test_symlinked_git_dir_or_objects_cannot_supply_the_blob(self) -> None:
        """A symlink .git or objects directory is another repo's store."""
        digest = hashlib.sha256(self.body).hexdigest()
        linked = Path(self.tmp.name) / "symlink-git"
        linked.mkdir()
        (linked / ".git").symlink_to(self.vault / ".git", target_is_directory=True)
        problems = pin_check.vault_errors(self.pin(digest), linked)
        self.assertTrue(problems and problems[0].startswith("vault object store is not self-contained"), problems)

        empty = self.empty_repo("symlink-objects")
        objects = empty / ".git" / "objects"
        shutil.rmtree(objects)
        objects.symlink_to(self.vault / ".git" / "objects", target_is_directory=True)
        problems = pin_check.vault_errors(self.pin(digest), empty)
        self.assertTrue(problems and problems[0].startswith("vault object store is not self-contained"), problems)
        self.assertIn(str((self.vault / ".git" / "objects").resolve()), problems[0])

    def test_vault_path_with_spaces_and_unicode_passes(self) -> None:
        vault = Path(self.tmp.name) / "space dir" / "ünicode"
        vault.mkdir(parents=True)
        git = ["git", "-C", str(vault), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run([*git[:3], "init", "-q"], check=True)
        target = vault / "contracts" / "recursion" / "PRODUCT.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(self.body)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "contract"], check=True)
        commit = subprocess.check_output([*git[:3], "rev-parse", "HEAD"], text=True).strip()
        pin = {**PINNED, "commit": commit, "sha256": hashlib.sha256(self.body).hexdigest()}
        self.assertEqual(pin_check.vault_errors(pin, vault), [])

    def test_replace_ref_cannot_swap_pinned_bytes(self) -> None:
        # A replace ref must not make the pinned commit hash as a different blob.
        real = hashlib.sha256(self.body).hexdigest()
        other = b"replaced\n"
        target = self.vault / "contracts" / "recursion" / "PRODUCT.md"
        target.write_bytes(other)
        git = ["git", "-C", str(self.vault), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "replace"], check=True)
        replacement = subprocess.check_output(["git", "-C", str(self.vault), "rev-parse", "HEAD"], text=True).strip()
        subprocess.run(["git", "-C", str(self.vault), "replace", self.commit, replacement], check=True)
        swapped = hashlib.sha256(other).hexdigest()
        with self.subTest(pin="real bytes"):
            self.assertEqual(pin_check.vault_errors(self.pin(real), self.vault), [])
        with self.subTest(pin="replacement bytes"):
            problems = pin_check.vault_errors(self.pin(swapped), self.vault)
            self.assertTrue(problems and problems[0].startswith("sha256 mismatch"), problems)

    def test_vault_is_not_redirected_by_git_env(self) -> None:
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        other_path = Path(other.name)
        subprocess.run(["git", "-C", str(other_path), "init", "-q"], check=True)
        digest = hashlib.sha256(self.body).hexdigest()
        redirects = {
            "GIT_DIR": str(other_path / ".git"),
            "GIT_OBJECT_DIRECTORY": str(other_path / ".git" / "objects"),
            "GIT_COMMON_DIR": str(other_path / ".git"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other_path / ".git" / "objects"),
        }
        for key, value in redirects.items():
            with self.subTest(key=key):
                with mock.patch.dict(os.environ, {key: value}):
                    self.assertEqual(pin_check.vault_errors(self.pin(digest), self.vault), [])

    def test_promisor_blob_is_not_fetched(self) -> None:
        # --vault reads the local clone. A partial clone must not download the blob.
        bare = tempfile.TemporaryDirectory()
        seed = tempfile.TemporaryDirectory()
        part = tempfile.TemporaryDirectory()
        self.addCleanup(bare.cleanup)
        self.addCleanup(seed.cleanup)
        self.addCleanup(part.cleanup)
        bare_path, seed_path, part_path = Path(bare.name), Path(seed.name), Path(part.name)
        body = b"hello-blob\n"
        subprocess.run(["git", "init", "-q", "--bare", str(bare_path)], check=True)
        subprocess.run(["git", "-C", str(bare_path), "config", "uploadpack.allowFilter", "true"], check=True)
        subprocess.run(["git", "-C", str(bare_path), "config", "uploadpack.allowAnySHA1InWant", "true"], check=True)
        subprocess.run(["git", "-C", str(seed_path), "init", "-q"], check=True)
        git = ["git", "-C", str(seed_path), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        (seed_path / "file.md").write_bytes(body)
        subprocess.run([*git, "add", "file.md"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "blob"], check=True)
        subprocess.run([*git, "push", "-q", str(bare_path), "HEAD:refs/heads/master"], check=True)
        subprocess.run(["git", "-C", str(bare_path), "symbolic-ref", "HEAD", "refs/heads/master"], check=True)
        subprocess.run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", "--quiet", bare_path.as_uri(), str(part_path)],
            check=True,
        )
        env = os.environ.copy()
        env["GIT_NO_LAZY_FETCH"] = "1"
        listed = subprocess.check_output(
            ["git", "-C", str(part_path), "rev-list", "--objects", "--missing=print", "HEAD"],
            text=True,
            env=env,
        )
        self.assertIn("?", listed)
        commit = subprocess.check_output(["git", "-C", str(part_path), "rev-parse", "HEAD"], text=True).strip()
        pin = {**PINNED, "path": "file.md", "commit": commit, "sha256": hashlib.sha256(body).hexdigest()}
        problems = pin_check.vault_errors(pin, part_path)
        after = subprocess.check_output(
            ["git", "-C", str(part_path), "rev-list", "--objects", "--missing=print", "HEAD"],
            text=True,
            env=env,
        )
        self.assertIn("?", after)
        self.assertTrue(problems and problems[0].startswith("cannot read"), problems)
        problems[0].encode("utf-8")

    def test_mismatch_message_escapes_lone_surrogate(self) -> None:
        # The path cannot be encoded as a tree name. The cannot-read line must still encode.
        pin = {**self.pin("0" * 64), "path": "contracts/PRO\ud800DUCT.md"}
        problems = pin_check.vault_errors(pin, self.vault)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("cannot read"), problems[0])
        self.assertNotIn("\ud800", problems[0])
        problems[0].encode("utf-8")

    def test_git_failure_escapes_surrogate_from_bytes_stderr(self) -> None:
        # Windows passes a lone surrogate through to git and comes back as CalledProcessError.
        pin = {**self.pin("0" * 64), "path": "contracts/PRO\ud800DUCT.md"}
        vault = Path(f"{self.vault}\ud800")
        exc = subprocess.CalledProcessError(128, ["git", "show"], stderr=b"fatal: bad path\x00\n")
        with mock.patch.object(pin_check.subprocess, "run", side_effect=exc):
            problems = pin_check.vault_errors(pin, vault)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("cannot read"), problems[0])
        self.assertIn("fatal: bad path", problems[0])
        self.assertNotIn("\ud800", problems[0])
        self.assertNotIn("\x00", problems[0])
        problems[0].encode("utf-8")

    def test_git_failure_message_when_stderr_is_not_bytes(self) -> None:
        # CalledProcessError.stderr defaults to None, and a text-mode failure is a str.
        # Either used to raise AttributeError before the message could be escaped.
        pin = self.pin(hashlib.sha256(self.body).hexdigest())
        cases = (
            subprocess.CalledProcessError(128, ["git", "show"], stderr=None),
            subprocess.CalledProcessError(128, ["git", "show"], stderr="fatal: \ud800\n"),
            OSError(2, "No such file or directory", "vault\ud800"),
        )
        for exc in cases:
            with self.subTest(exc=type(exc).__name__, stderr=type(getattr(exc, "stderr", None)).__name__):
                with mock.patch.object(pin_check.subprocess, "run", side_effect=exc):
                    problems = pin_check.vault_errors(pin, self.vault)
                self.assertEqual(len(problems), 1)
                self.assertTrue(problems[0].startswith("cannot read"), problems[0])
                self.assertNotIn("\ud800", problems[0])
                self.assertNotIn("\x00", problems[0])
                problems[0].encode("utf-8")

    def test_git_child_env_drops_every_git_variable(self) -> None:
        with mock.patch.dict(os.environ, {"GIT_COMMON_DIR": "/tmp/elsewhere", "GIT_NO_LAZY_FETCH": "0", "PATH": "/usr/bin"}):
            env = pin_check._git_env()
        self.assertEqual(env.get("GIT_NO_LAZY_FETCH"), "1")
        self.assertNotIn("GIT_COMMON_DIR", env)
        self.assertEqual(env.get("PATH"), "/usr/bin")
        self.assertFalse(any(key.upper().startswith("GIT_") and key != "GIT_NO_LAZY_FETCH" for key in env))

    def _commit_repo(self, path: Path) -> str:
        path.mkdir(parents=True, exist_ok=True)
        git = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
        target = path / "contracts" / "recursion" / "PRODUCT.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(self.body)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "contract"], check=True)
        return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()

    def _blob_oid(self, repo: Path, commit: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", f"{commit}:contracts/recursion/PRODUCT.md"],
            text=True,
        ).strip()

    def _pin_for(self, commit: str) -> dict:
        return {**PINNED, "commit": commit, "sha256": hashlib.sha256(self.body).hexdigest()}

    def _assert_git_reads_body(self, vault: Path, commit: str) -> None:
        """git show from vault returns the donor bytes. No ceiling is set here."""
        done = subprocess.run(
            ["git", "-C", str(vault), "show", f"{commit}:contracts/recursion/PRODUCT.md"],
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, self.body)

    def _assert_refused(self, vault: Path, pin: dict) -> list[str]:
        problems = pin_check.vault_errors(pin, vault)
        self.assertTrue(problems, f"false PASS for {vault}: vault_errors returned {problems!r}")
        self.assertTrue(
            problems[0].startswith("vault object store is not self-contained")
            or problems[0].startswith("cannot read"),
            problems,
        )
        self.assertFalse(any(line.startswith("PASS:") for line in "\n".join(problems).splitlines()))
        problems[0].encode("utf-8")
        return problems

    def _replace_with_symlink(self, entry: Path, absolute: bool) -> None:
        outside = Path(self.tmp.name) / "donor-bytes" / f"{'abs' if absolute else 'rel'}-{entry.parent.name}-{entry.name}"
        outside.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(entry, outside)
        target = outside if absolute else Path(os.path.relpath(outside, entry.parent))
        entry.symlink_to(target, target_is_directory=outside.is_dir())

    def test_child_of_donor_cannot_supply_the_blob(self) -> None:
        """An empty directory inside a donor repo is not that repo."""
        pin = self.pin(hashlib.sha256(self.body).hexdigest())
        child = self.vault / "empty-child"
        child.mkdir()
        for ceiling in (None, "/"):
            with self.subTest(operator_ceiling=ceiling):
                env = {} if ceiling is None else {"GIT_CEILING_DIRECTORIES": ceiling}
                with mock.patch.dict(os.environ, env):
                    self._assert_git_reads_body(child, self.commit)
                    self._assert_refused(child, pin)

    def test_child_after_root_refusal_cannot_supply_the_blob(self) -> None:
        """Refusing the attacked root must also refuse a child of that root."""
        pin = self.pin(hashlib.sha256(self.body).hexdigest())
        for name in ("alternates", "commondir", "gitdir-file", "symlinked-git"):
            with self.subTest(name=name):
                root = Path(self.tmp.name) / f"root-{name}"
                if name == "gitdir-file":
                    root.mkdir()
                    (root / ".git").write_text(f"gitdir: {self.vault / '.git'}\n", encoding="utf-8")
                elif name == "symlinked-git":
                    root.mkdir()
                    (root / ".git").symlink_to(self.vault / ".git", target_is_directory=True)
                else:
                    root = self.empty_repo(f"root-{name}")
                    if name == "alternates":
                        alt = root / ".git" / "objects" / "info" / "alternates"
                        alt.parent.mkdir(parents=True, exist_ok=True)
                        alt.write_text(str(self.vault / ".git" / "objects") + "\n", encoding="utf-8")
                    else:
                        (root / ".git" / "commondir").write_text(str(self.vault / ".git") + "\n", encoding="utf-8")
                child = root / "child"
                child.mkdir()
                root_problems = pin_check.vault_errors(pin, root)
                self.assertTrue(
                    root_problems and root_problems[0].startswith("vault object store is not self-contained"),
                    root_problems,
                )
                self._assert_git_reads_body(child, self.commit)
                self._assert_refused(child, pin)

    def test_linked_worktree_child_cannot_supply_the_blob(self) -> None:
        """worktree/child must not pass, and the main repo with worktrees/ still does."""
        repo = Path(self.tmp.name) / "worktree-main"
        commit = self._commit_repo(repo)
        pin = self._pin_for(commit)
        self.assertEqual(pin_check.vault_errors(pin, repo), [])
        worktree = Path(self.tmp.name) / "linked-wt"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(worktree), "HEAD"], check=True)
        self.assertEqual(pin_check.vault_errors(pin, repo), [])
        child = worktree / "child"
        child.mkdir()
        root_problems = pin_check.vault_errors(pin, worktree)
        self.assertTrue(
            root_problems and root_problems[0].startswith("vault object store is not self-contained"),
            root_problems,
        )
        self._assert_git_reads_body(child, commit)
        self._assert_refused(child, pin)

    def _assert_object_symlink_refused(self, repo: Path, commit: str, entry: Path, absolute: bool) -> None:
        self._replace_with_symlink(entry, absolute)
        pin = self._pin_for(commit)
        self._assert_git_reads_body(repo, commit)
        problems = self._assert_refused(repo, pin)
        self.assertTrue(problems[0].startswith("vault object store is not self-contained"), problems)
        self.assertIn("symlink", problems[0])

    def test_symlinked_loose_object_cannot_supply_the_blob(self) -> None:
        for absolute in (True, False):
            with self.subTest(absolute=absolute):
                repo = Path(self.tmp.name) / f"loose-{absolute}"
                commit = self._commit_repo(repo)
                oid = self._blob_oid(repo, commit)
                entry = repo / ".git" / "objects" / oid[:2] / oid[2:]
                self.assertTrue(entry.is_file(), entry)
                self._assert_object_symlink_refused(repo, commit, entry, absolute)

    def test_symlinked_fanout_dir_cannot_supply_the_blob(self) -> None:
        for absolute in (True, False):
            with self.subTest(absolute=absolute):
                repo = Path(self.tmp.name) / f"fanout-{absolute}"
                commit = self._commit_repo(repo)
                oid = self._blob_oid(repo, commit)
                entry = repo / ".git" / "objects" / oid[:2]
                self.assertTrue(entry.is_dir(), entry)
                self._assert_object_symlink_refused(repo, commit, entry, absolute)

    def test_symlinked_pack_dir_cannot_supply_the_blob(self) -> None:
        for absolute in (True, False):
            with self.subTest(absolute=absolute):
                repo = Path(self.tmp.name) / f"pack-{absolute}"
                commit = self._commit_repo(repo)
                subprocess.run(["git", "-C", str(repo), "repack", "-ad"], check=True)
                entry = repo / ".git" / "objects" / "pack"
                self.assertTrue(entry.is_dir(), entry)
                self._assert_object_symlink_refused(repo, commit, entry, absolute)

    def test_symlinked_objects_info_cannot_supply_the_blob(self) -> None:
        for absolute in (True, False):
            with self.subTest(absolute=absolute):
                repo = Path(self.tmp.name) / f"info-{absolute}"
                commit = self._commit_repo(repo)
                entry = repo / ".git" / "objects" / "info"
                self.assertTrue(entry.is_dir(), entry)
                self._assert_object_symlink_refused(repo, commit, entry, absolute)

    def test_bare_alternates_cannot_supply_the_blob(self) -> None:
        pin = self.pin(hashlib.sha256(self.body).hexdigest())
        donor_objects = self.vault / ".git" / "objects"
        for absolute in (True, False):
            with self.subTest(absolute=absolute):
                bare = Path(self.tmp.name) / f"bare-alt-{absolute}"
                subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
                alt = bare / "objects" / "info" / "alternates"
                alt.parent.mkdir(parents=True, exist_ok=True)
                text = str(donor_objects) if absolute else os.path.relpath(donor_objects, bare / "objects")
                alt.write_text(text + "\n", encoding="utf-8")
                self._assert_git_reads_body(bare, self.commit)
                problems = self._assert_refused(bare, pin)
                self.assertTrue(problems[0].startswith("vault object store is not self-contained"), problems)
                self.assertIn(str(alt), problems[0])

    def test_bare_http_alternates_are_refused(self) -> None:
        pin = self.pin(hashlib.sha256(self.body).hexdigest())
        bare = Path(self.tmp.name) / "bare-http-alternates"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        alt = bare / "objects" / "info" / "http-alternates"
        alt.parent.mkdir(parents=True, exist_ok=True)
        alt.write_text(str(self.vault / ".git" / "objects") + "\n", encoding="utf-8")
        problems = self._assert_refused(bare, pin)
        self.assertTrue(problems[0].startswith("vault object store is not self-contained"), problems)
        self.assertIn(str(alt), problems[0])

    def test_bare_commondir_cannot_supply_the_blob(self) -> None:
        pin = self.pin(hashlib.sha256(self.body).hexdigest())
        bare = Path(self.tmp.name) / "bare-commondir"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        (bare / "commondir").write_text(str(self.vault / ".git") + "\n", encoding="utf-8")
        self._assert_git_reads_body(bare, self.commit)
        problems = self._assert_refused(bare, pin)
        self.assertTrue(problems[0].startswith("vault object store is not self-contained"), problems)

    def test_bare_symlinked_objects_cannot_supply_the_blob(self) -> None:
        pin = self.pin(hashlib.sha256(self.body).hexdigest())
        bare = Path(self.tmp.name) / "bare-objects-symlink"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        donor_objects = self.vault / ".git" / "objects"
        shutil.rmtree(bare / "objects")
        (bare / "objects").symlink_to(os.path.relpath(donor_objects, bare), target_is_directory=True)
        self._assert_git_reads_body(bare, self.commit)
        problems = self._assert_refused(bare, pin)
        self.assertTrue(problems[0].startswith("vault object store is not self-contained"), problems)
        self.assertIn(str(donor_objects.resolve()), problems[0])

    def test_bare_clone_and_shared_repository_pass(self) -> None:
        digest = hashlib.sha256(self.body).hexdigest()
        bare = Path(self.tmp.name) / "bare-clone"
        subprocess.run(
            ["git", "clone", "-q", "--bare", "--no-hardlinks", str(self.vault), str(bare)],
            check=True,
        )
        self.assertEqual(pin_check.vault_errors(self.pin(digest), bare), [])

        shared = Path(self.tmp.name) / "shared repo"
        subprocess.run(["git", "init", "-q", "--shared=group", str(shared)], check=True)
        target = shared / "contracts" / "recursion" / "PRODUCT.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(self.body)
        git = ["git", "-C", str(shared), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "contract"], check=True)
        commit = subprocess.check_output(["git", "-C", str(shared), "rev-parse", "HEAD"], text=True).strip()
        reported = subprocess.check_output(
            ["git", "-C", str(shared), "config", "--get", "core.sharedRepository"],
            text=True,
        ).strip()
        self.assertIn(reported, {"1", "true", "group"})
        self.assertTrue((shared / ".git" / "objects").stat().st_mode & 0o2000)
        self.assertEqual(pin_check.vault_errors(self._pin_for(commit), shared), [])

    def test_git_invocation_bounds_discovery_and_keeps_home(self) -> None:
        digest = hashlib.sha256(self.body).hexdigest()
        seen: list[dict[str, str]] = []
        real_run = pin_check.subprocess.run

        def spy(args, **kwargs):
            env = kwargs.get("env")
            if isinstance(env, dict):
                seen.append(env)
            return real_run(args, **kwargs)

        home = "/tmp/pin-home"
        xdg = "/tmp/pin-xdg"
        with mock.patch.object(pin_check.subprocess, "run", side_effect=spy):
            with mock.patch.dict(
                os.environ,
                {
                    "GIT_CEILING_DIRECTORIES": "/",
                    "GIT_DIR": "/tmp/not-the-vault",
                    "HOME": home,
                    "XDG_CONFIG_HOME": xdg,
                },
            ):
                self.assertEqual(pin_check.vault_errors(self.pin(digest), self.vault), [])
        self.assertTrue(seen)
        parent = str(self.vault.resolve().parent)
        for env in seen:
            self.assertEqual(env.get("GIT_NO_LAZY_FETCH"), "1")
            self.assertNotIn("GIT_DIR", env)
            self.assertEqual(env.get("HOME"), home)
            self.assertEqual(env.get("XDG_CONFIG_HOME"), xdg)
            ceiling = env.get("GIT_CEILING_DIRECTORIES", "")
            self.assertIn(parent, ceiling.split(os.pathsep))
            self.assertNotIn("/", ceiling.split(os.pathsep))

    def _craft_midx(self, names: list[str], version: int = 1) -> bytes:
        """A multi-pack-index whose PNAM chunk lists names, NUL-padded to 4 bytes.

        Version 2 uses the same header and chunk framing as version 1.
        """
        raw = b"".join(name.encode("utf-8") + b"\0" for name in names)
        raw += b"\0" * ((4 - (len(raw) % 4)) % 4)
        num_chunks = 1
        header = b"MIDX" + bytes([version, 1, num_chunks, 0]) + len(names).to_bytes(4, "big")
        pnam_off = 12 + (num_chunks + 1) * 12
        end = pnam_off + len(raw)
        toc = b"PNAM" + pnam_off.to_bytes(8, "big") + (0).to_bytes(4, "big") + end.to_bytes(8, "big")
        body = header + toc + raw
        return body + hashlib.sha1(body).digest()

    def _rewrite_midx_pnam(self, midx: Path, names: list[str]) -> None:
        """Replace the PNAM chunk of a git-written multi-pack-index, shifting later chunks."""
        data = midx.read_bytes()
        if data[:4] != b"MIDX":
            raise AssertionError(f"not a multi-pack-index: {midx}")
        num_chunks = data[6]
        chunks = []
        for index in range(num_chunks + 1):
            off = 12 + index * 12
            chunks.append((data[off : off + 4], int.from_bytes(data[off + 4 : off + 12], "big")))
        raw = b"".join(name.encode("utf-8") + b"\0" for name in names)
        raw += b"\0" * ((4 - (len(raw) % 4)) % 4)
        toc_size = (num_chunks + 1) * 12
        body = bytearray()
        offsets = []
        for index in range(num_chunks):
            cid, start = chunks[index]
            end = chunks[index + 1][1]
            offsets.append(12 + toc_size + len(body))
            body += raw if cid == b"PNAM" else data[start:end]
        end_off = 12 + toc_size + len(body)
        toc = bytearray()
        for index in range(num_chunks):
            toc += chunks[index][0] + offsets[index].to_bytes(8, "big")
        toc += b"\0\0\0\0" + end_off.to_bytes(8, "big")
        rebuilt = bytes(data[:12]) + bytes(toc) + bytes(body)
        midx.write_bytes(rebuilt + hashlib.sha1(rebuilt).digest())

    def test_midx_relative_pnam_cannot_supply_the_blob(self) -> None:
        """A multi-pack-index may name a pack with '..'. The pack file is not in objects/pack."""
        repo = Path(self.tmp.name) / "midx-escape"
        commit = self._commit_repo(repo)
        subprocess.run(["git", "-C", str(repo), "repack", "-ad"], check=True)
        subprocess.run(["git", "-C", str(repo), "multi-pack-index", "write"], check=True)
        packdir = repo / ".git" / "objects" / "pack"
        outside = Path(self.tmp.name) / "midx-escape-out"
        outside.mkdir()
        idx_name = None
        for entry in list(packdir.iterdir()):
            if entry.name.startswith("pack-"):
                if entry.suffix == ".idx":
                    idx_name = entry.name
                shutil.move(str(entry), str(outside / entry.name))
        self.assertIsNotNone(idx_name)
        self.assertEqual([path.name for path in packdir.iterdir()], ["multi-pack-index"])
        relative = os.path.relpath(outside / idx_name, packdir)
        self.assertIn("..", relative)
        self._rewrite_midx_pnam(packdir / "multi-pack-index", [relative])
        self._assert_git_reads_body(repo, commit)
        problems = self._assert_refused(repo, self._pin_for(commit))
        self.assertTrue(problems[0].startswith("vault object store is not self-contained"), problems)
        self.assertIn("multi-pack-index", problems[0])

    def test_incremental_midx_relative_pnam_is_refused(self) -> None:
        """An incremental chain under multi-pack-index.d is the same PNAM rule."""
        repo = Path(self.tmp.name) / "inc-midx-bad"
        commit = self._commit_repo(repo)
        packdir = repo / ".git" / "objects" / "pack"
        packdir.mkdir(parents=True, exist_ok=True)
        layer = self._craft_midx(["../../../../out/pack-" + "ab" * 20 + ".idx"])
        layer_hash = hashlib.sha1(layer[:-20]).hexdigest()
        midx_d = packdir / "multi-pack-index.d"
        midx_d.mkdir()
        (midx_d / f"multi-pack-index-{layer_hash}.midx").write_bytes(layer)
        (midx_d / "multi-pack-index-chain").write_text(layer_hash + "\n", encoding="utf-8")
        self._assert_git_reads_body(repo, commit)
        problems = self._assert_refused(repo, self._pin_for(commit))
        self.assertIn("multi-pack-index", problems[0])

    def test_unknown_midx_is_refused(self) -> None:
        """A multi-pack-index we cannot parse is refused even when loose objects hold the blob."""
        repo = Path(self.tmp.name) / "midx-unknown"
        commit = self._commit_repo(repo)
        packdir = repo / ".git" / "objects" / "pack"
        packdir.mkdir(parents=True, exist_ok=True)
        (packdir / "multi-pack-index").write_bytes(b"MIDX" + bytes([99, 1, 0, 0]) + b"\0" * 8)
        problems = self._assert_refused(repo, self._pin_for(commit))
        self.assertTrue(problems[0].startswith("vault object store is not self-contained"), problems)
        self.assertIn("multi-pack-index", problems[0])

    def test_incremental_midx_with_local_packs_passes(self) -> None:
        repo = Path(self.tmp.name) / "inc-midx-ok"
        commit = self._commit_repo(repo)
        subprocess.run(["git", "-C", str(repo), "repack", "-ad"], check=True)
        subprocess.run(["git", "-C", str(repo), "multi-pack-index", "write"], check=True)
        packdir = repo / ".git" / "objects" / "pack"
        midx = packdir / "multi-pack-index"
        data = midx.read_bytes()
        layer_hash = hashlib.sha1(data[:-20]).hexdigest()
        midx_d = packdir / "multi-pack-index.d"
        midx_d.mkdir()
        (midx_d / f"multi-pack-index-{layer_hash}.midx").write_bytes(data)
        (midx_d / "multi-pack-index-chain").write_text(layer_hash + "\n", encoding="utf-8")
        midx.unlink()
        self.assertEqual(pin_check.vault_errors(self._pin_for(commit), repo), [])

    def test_repack_midx_gc_and_split_commit_graph_still_pass(self) -> None:
        cases = (
            ("repack", [["repack", "-ad"]]),
            ("midx", [["repack", "-ad"], ["multi-pack-index", "write"]]),
            ("gc", [["gc", "--quiet"]]),
            ("split-graph", [["commit-graph", "write", "--split", "--reachable"]]),
        )
        for name, commands in cases:
            with self.subTest(name=name):
                repo = Path(self.tmp.name) / f"keep-{name}"
                commit = self._commit_repo(repo)
                for args in commands:
                    subprocess.run(["git", "-C", str(repo), *args], check=True)
                self.assertEqual(pin_check.vault_errors(self._pin_for(commit), repo), [])

    def test_info_packs_outside_name_is_refused(self) -> None:
        repo = Path(self.tmp.name) / "info-packs-out"
        commit = self._commit_repo(repo)
        info = repo / ".git" / "objects" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "packs").write_text("P ../../../../out/pack-" + "ab" * 20 + ".pack\n\n", encoding="utf-8")
        self._assert_git_reads_body(repo, commit)
        problems = self._assert_refused(repo, self._pin_for(commit))
        self.assertIn("packs", problems[0])

    def test_commit_graph_chain_outside_name_is_refused(self) -> None:
        repo = Path(self.tmp.name) / "cg-chain-out"
        commit = self._commit_repo(repo)
        subprocess.run(
            ["git", "-C", str(repo), "commit-graph", "write", "--split", "--reachable"],
            check=True,
        )
        chain = repo / ".git" / "objects" / "info" / "commit-graphs" / "commit-graph-chain"
        os.chmod(chain, 0o644)
        line = chain.read_text(encoding="utf-8").strip()
        self.assertEqual(pin_check.vault_errors(self._pin_for(commit), repo), [])
        chain.write_text(line + "/../../../../out/evil\n", encoding="utf-8")
        self._assert_git_reads_body(repo, commit)
        problems = self._assert_refused(repo, self._pin_for(commit))
        self.assertIn("commit-graph", problems[0])

    def test_alternates_written_after_walk_cannot_supply_the_blob(self) -> None:
        """The alternates file appears on the first git call, after the object walk."""
        digest = hashlib.sha256(self.body).hexdigest()
        real_run = pin_check.subprocess.run
        for name in ("alternates", "http-alternates"):
            with self.subTest(name=name):
                repo = Path(self.tmp.name) / f"toctou-late-{name}"
                commit = self._commit_repo(repo)
                pin = self._pin_for(commit)
                state = {"armed": True}

                def spy(args, **kwargs):
                    if state["armed"]:
                        state["armed"] = False
                        info = repo / ".git" / "objects" / "info"
                        info.mkdir(parents=True, exist_ok=True)
                        (info / name).write_text(str(self.vault / ".git" / "objects") + "\n", encoding="utf-8")
                    return real_run(args, **kwargs)

                with mock.patch.object(pin_check.subprocess, "run", side_effect=spy):
                    problems = pin_check.vault_errors(pin, repo)
                self.assertFalse(state["armed"], "hook did not run between the walk and the git call")
                self.assertTrue(problems, f"false PASS for {name}: {problems!r}")
                self.assertTrue(problems[0].startswith("vault object store is not self-contained"), problems)
                self.assertIn(name, problems[0])
                self.assertFalse(any(line.startswith("PASS:") for line in "\n".join(problems).splitlines()))
                problems[0].encode("utf-8")
                state["armed"] = True

    def test_alternates_written_before_show_cannot_import_donor_bytes(self) -> None:
        """An empty vault must not pass when alternates is planted on the git show call."""
        empty = self.empty_repo("toctou-donor")
        digest = hashlib.sha256(self.body).hexdigest()
        real_run = pin_check.subprocess.run
        donor = self.vault / ".git" / "objects"
        state = {"planted": False}

        def spy(args, **kwargs):
            if isinstance(args, (list, tuple)) and "show" in args and not state["planted"]:
                state["planted"] = True
                alt = empty / ".git" / "objects" / "info" / "alternates"
                alt.parent.mkdir(parents=True, exist_ok=True)
                alt.write_text(str(donor) + "\n", encoding="utf-8")
            return real_run(args, **kwargs)

        with mock.patch.object(pin_check.subprocess, "run", side_effect=spy):
            problems = pin_check.vault_errors(self.pin(digest), empty)
        self.assertTrue(problems, f"false PASS: vault_errors returned {problems!r}")
        self.assertFalse(any(line.startswith("PASS:") for line in "\n".join(problems).splitlines()))
        problems[0].encode("utf-8")
        alt = empty / ".git" / "objects" / "info" / "alternates"
        if not alt.exists():
            alt.parent.mkdir(parents=True, exist_ok=True)
            alt.write_text(str(donor) + "\n", encoding="utf-8")
        self._assert_git_reads_body(empty, self.commit)

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


def _encode_size(value: int) -> bytes:
    out = bytearray()
    while True:
        piece = value & 0x7F
        value >>= 7
        if value:
            out.append(piece | 0x80)
        else:
            out.append(piece)
            return bytes(out)


def _single_blob_pack(content: bytes) -> bytes:
    payload = zlib.compress(content)
    body = b"PACK" + (2).to_bytes(4, "big") + (1).to_bytes(4, "big")
    body += _encode_obj_header(3, len(content)) + payload
    return body + hashlib.sha1(body).digest()


def _content_oid(content: bytes) -> bytes:
    header = b"blob " + str(len(content)).encode("ascii") + b"\0"
    return hashlib.sha1(header + content).digest()


def _append_delta(base: bytes, suffix: bytes) -> bytes:
    """Copy `base` and insert `suffix`. The result is base + suffix."""
    if len(base) > 255 or len(suffix) > 127:
        raise AssertionError("test append delta is limited to a short base and suffix")
    body = _encode_size(len(base)) + _encode_size(len(base) + len(suffix))
    if base:
        body += bytes([0x90, len(base)])
    if suffix:
        body += bytes([len(suffix)]) + suffix
    return body


def _ref_append_pack(base: bytes, suffix: bytes) -> tuple[bytes, bytes, bytes]:
    """Undeltified base plus a type-7 REF delta that appends `suffix`."""
    delta = _append_delta(base, suffix)
    base_oid = _content_oid(base)
    delta_oid = _content_oid(base + suffix)
    buf = bytearray(b"PACK" + (2).to_bytes(4, "big") + (2).to_bytes(4, "big"))
    buf += _encode_obj_header(3, len(base)) + zlib.compress(base)
    buf += _encode_obj_header(7, len(delta)) + base_oid + zlib.compress(delta)
    pack = bytes(buf) + hashlib.sha1(bytes(buf)).digest()
    return pack, base_oid, delta_oid


def _idx_v2_one(oid: bytes, offset: int, pack: bytes) -> bytes:
    return _idx_v2_entries([(oid, offset)], pack)


def _idx_v2_entries(entries: list[tuple[bytes, int]], pack: bytes) -> bytes:
    """Version-2 index. Names must be unique; fanout counts every first byte."""
    entries = sorted(entries)
    if len({oid for oid, _offset in entries}) != len(entries):
        raise AssertionError("duplicate pack index names")
    counts = [0] * 256
    for oid, _offset in entries:
        counts[oid[0]] += 1
    fanout = bytearray(256 * 4)
    running = 0
    for index, count in enumerate(counts):
        running += count
        fanout[index * 4 : index * 4 + 4] = running.to_bytes(4, "big")
    names = b"".join(oid for oid, _offset in entries)
    crc = b"\0" * (len(entries) * 4)
    offsets = b"".join(offset.to_bytes(4, "big") for _oid, offset in entries)
    body = b"\xfftOc" + (2).to_bytes(4, "big") + bytes(fanout) + names + crc + offsets + pack[-20:]
    return body + hashlib.sha1(body).digest()


def _encode_obj_header(obj_type: int, size: int) -> bytes:
    byte = (obj_type << 4) | (size & 0x0F)
    size >>= 4
    out = bytearray()
    while size:
        out.append(byte | 0x80)
        byte = size & 0x7F
        size >>= 7
    out.append(byte)
    return bytes(out)


def _encode_ofs(relative: int) -> bytes:
    out = [relative & 0x7F]
    relative >>= 7
    while relative:
        relative -= 1
        out.append(0x80 | (relative & 0x7F))
        relative >>= 7
    return bytes(reversed(out))


def _insert_delta(base: bytes, result: bytes) -> bytes:
    if len(result) > 127:
        raise AssertionError("test insert delta is limited to 127 bytes")
    body = _encode_size(len(base)) + _encode_size(len(result))
    if result:
        body += bytes([len(result)]) + result
    return body


def _ofs_chain_pack(tip: bytes, depth: int) -> tuple[bytes, int]:
    """Undeltified base plus `depth` OFS deltas. The last object resolves to `tip`."""
    contents = [index.to_bytes(2, "big") for index in range(depth)]
    contents.append(tip)
    buf = bytearray(b"PACK" + (2).to_bytes(4, "big") + len(contents).to_bytes(4, "big"))
    offsets = []
    for index, content in enumerate(contents):
        offsets.append(len(buf))
        if index == 0:
            payload = zlib.compress(content)
            buf += _encode_obj_header(3, len(content)) + payload
            continue
        delta = _insert_delta(contents[index - 1], content)
        payload = zlib.compress(delta)
        relative = len(buf) - offsets[index - 1]
        buf += _encode_obj_header(6, len(delta)) + _encode_ofs(relative) + payload
    pack = bytes(buf)
    return pack + hashlib.sha1(pack).digest(), offsets[-1]


def _pack_bytes(objects: list[bytes]) -> tuple[bytes, list[int]]:
    buf = bytearray(b"PACK" + (2).to_bytes(4, "big") + len(objects).to_bytes(4, "big"))
    offsets = []
    for obj in objects:
        offsets.append(len(buf))
        buf += obj
    pack = bytes(buf)
    return pack + hashlib.sha1(pack).digest(), offsets


def _blob_obj(content: bytes) -> bytes:
    return _encode_obj_header(3, len(content)) + zlib.compress(content)


def _ref_obj(base_oid: bytes, delta: bytes) -> bytes:
    return _encode_obj_header(7, len(delta)) + base_oid + zlib.compress(delta)


def _ofs_obj(relative: int, delta: bytes) -> bytes:
    return _encode_obj_header(6, len(delta)) + _encode_ofs(relative) + zlib.compress(delta)


def _write_store(root: Path, packs: list[tuple[str, bytes, bytes]]) -> None:
    packdir = root / "pack"
    packdir.mkdir(parents=True, exist_ok=True)
    for name, pack, idx in packs:
        (packdir / f"{name}.pack").write_bytes(pack)
        (packdir / f"{name}.idx").write_bytes(idx)


def _write_loose(objects: Path, oid: bytes, content: bytes) -> None:
    raw = zlib.compress(b"blob " + str(len(content)).encode("ascii") + b"\0" + content)
    path = objects / oid.hex()[:2] / oid.hex()[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def _poison_ref_chain(depth: int, claimed_tip: bytes) -> tuple[bytes, bytes]:
    """`depth` REF deltas named as `claimed_tip`, ending in a blob with the wrong hash.

    The same chain in two packs is what makes a resolver that forgets a failed
    base re-walk every copy: ``2**(depth + 2) - 1`` pack reads.
    """
    leaf = b"poison-leaf"
    delta = _insert_delta(leaf, b"Z")
    bases = [bytes([index + 1]) + b"\x5a" * 19 for index in range(depth)]
    if claimed_tip in bases:
        raise AssertionError("claimed tip collides with a synthetic base")
    objects = []
    entries_at = []
    for index in range(depth):
        entries_at.append(len(objects))
        objects.append(_ref_obj(bases[index], delta))
    entries_at.append(len(objects))
    objects.append(_blob_obj(leaf))
    pack, offsets = _pack_bytes(objects)
    entries = []
    for index in range(depth):
        name = claimed_tip if index == 0 else bases[index - 1]
        entries.append((name, offsets[entries_at[index]]))
    entries.append((bases[-1], offsets[entries_at[-1]]))
    return pack, _idx_v2_entries(entries, pack)


def _ref_chain_onto(depth: int, leaf: bytes) -> tuple[bytes, bytes, bytes, bytes]:
    """`depth` REF deltas that rebuild ``i.to_bytes(2)`` on top of `leaf`.

    Returns the pack, its index, the tip oid and the leaf oid. The leaf itself
    is not in the pack.
    """
    contents = [leaf]
    for index in range(1, depth + 1):
        contents.append(index.to_bytes(2, "big"))
    oids = [_content_oid(content) for content in contents]
    objects = []
    for index in range(1, depth + 1):
        delta = _insert_delta(contents[index - 1], contents[index])
        objects.append(_ref_obj(oids[index - 1], delta))
    pack, offsets = _pack_bytes(objects)
    entries = [(oids[index], offsets[index - 1]) for index in range(1, depth + 1)]
    return pack, _idx_v2_entries(entries, pack), oids[-1], oids[0]


def _count_pack_reads(call) -> tuple[int, object]:
    calls = 0
    real = pin_check._pack_object_at

    def wrapped(pack: bytes, offset: int, real=real) -> tuple:
        nonlocal calls
        calls += 1
        return real(pack, offset)

    with mock.patch.object(pin_check, "_pack_object_at", wrapped):
        result = call()
    return calls, result


def _peak(call) -> tuple[BaseException | None, int]:
    tracemalloc.start()
    caught = None
    try:
        call()
    except BaseException as exc:  # noqa: BLE001 - the caller asserts the type
        caught = exc
    finally:
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return caught, peak


class ObjectReaderBoundsTests(unittest.TestCase):
    def test_delta_copy_overflow_stays_under_a_megabyte(self) -> None:
        """A short declared result must not materialise every copy command."""
        base = b"x" * (64 * 1024)
        delta = _encode_size(len(base)) + _encode_size(1) + bytes([0x80]) * 80

        def call() -> None:
            pin_check._apply_delta(base, delta)

        caught, peak = _peak(call)
        self.assertIsInstance(caught, pin_check._ObjectMissing)
        self.assertIn("delta result size", caught.message)
        self.assertLess(peak, 1 << 20, f"peak {peak}")

    def test_pack_inflate_stays_under_a_megabyte(self) -> None:
        """A zlib stream larger than its pack header must not be fully inflated."""
        comp = zlib.compress(b"\0" * (8 * 1024 * 1024))
        header = _encode_obj_header(3, 1)
        body = b"PACK" + (2).to_bytes(4, "big") + (1).to_bytes(4, "big") + header + comp
        pack = body + hashlib.sha1(body).digest()

        def call() -> None:
            pin_check._pack_object_at(pack, 12)

        caught, peak = _peak(call)
        self.assertIsInstance(caught, pin_check._ObjectMissing)
        self.assertLess(peak, 1 << 20, f"peak {peak}")

    def test_delta_copy_past_the_base_is_refused(self) -> None:
        delta = _encode_size(3) + _encode_size(1) + bytes([0x91, 5, 1])
        with self.assertRaises(pin_check._ObjectMissing) as caught:
            pin_check._apply_delta(b"abc", delta)
        self.assertIn("extends past the base", caught.exception.message)

    def _resolve(self, pack: bytes, offset: int) -> tuple[bytes, bytes]:
        store = pin_check._ObjectStore.__new__(pin_check._ObjectStore)
        store.cache = {}
        store.packs = []
        store.objects = Path(".")
        index = pin_check._PackIndex.__new__(pin_check._PackIndex)
        index.pack = pack
        return store._resolve(index, offset)

    def test_delta_depth_matches_git_maximum_and_cycles_stop(self) -> None:
        """4095 OFS deltas resolve; 4096 and a self-delta stop without recursing."""
        pack, tip = _ofs_chain_pack(b"tip-bytes", 4095)
        _typ, content = self._resolve(pack, tip)
        self.assertEqual(content, b"tip-bytes")
        pack, tip = _ofs_chain_pack(b"tip-bytes", 4096)
        with self.assertRaises(pin_check._ObjectMissing) as caught:
            self._resolve(pack, tip)
        self.assertEqual(caught.exception.message, "delta chain is too deep")
        delta = _insert_delta(b"\x00", b"\x01")
        body = (
            b"PACK"
            + (2).to_bytes(4, "big")
            + (1).to_bytes(4, "big")
            + _encode_obj_header(6, len(delta))
            + b"\x00"
            + zlib.compress(delta)
        )
        self_pack = body + hashlib.sha1(body).digest()
        with self.assertRaises(pin_check._ObjectMissing) as caught:
            self._resolve(self_pack, 12)
        self.assertEqual(caught.exception.message, "delta chain is too deep")


class DeepObjectStoreTests(VaultTests):
    def _install_pack(self, repo: Path, pack: bytes) -> None:
        packdir = repo / ".git" / "objects" / "pack"
        packdir.mkdir(parents=True, exist_ok=True)
        path = packdir / "deep.pack"
        path.write_bytes(pack)
        done = subprocess.run(["git", "-C", str(repo), "index-pack", str(path)], capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_ofs_delta_chain_of_65_matches_git(self) -> None:
        """git reads 65 OFS deltas (pack.depth allows 4095); the checker must too."""
        repo = Path(self.tmp.name) / "delta-65"
        commit = self._commit_repo(repo)
        oid = self._blob_oid(repo, commit)
        pack, _tip = _ofs_chain_pack(self.body, 65)
        self._install_pack(repo, pack)
        loose = repo / ".git" / "objects" / oid[:2] / oid[2:]
        self.assertTrue(loose.is_file(), loose)
        loose.unlink()
        got = subprocess.check_output(["git", "-C", str(repo), "cat-file", "blob", oid])
        self.assertEqual(got, self.body)
        fsck = subprocess.run(["git", "-C", str(repo), "fsck"], capture_output=True)
        self.assertEqual(fsck.returncode, 0, fsck.stderr)
        shown = subprocess.check_output(
            ["git", "-C", str(repo), "show", f"{commit}:contracts/recursion/PRODUCT.md"]
        )
        self.assertEqual(shown, self.body)
        self.assertEqual(pin_check.vault_errors(self._pin_for(commit), repo), [])

    def test_corrupt_loose_object_falls_back_to_pack(self) -> None:
        """A loose file whose bytes are not the object must not hide the packed copy."""
        cases = {
            "hash": zlib.compress(b"blob 4\0nope"),
            "inflate": b"this is not a zlib stream",
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                repo = Path(self.tmp.name) / f"loose-fallback-{name}"
                commit = self._commit_repo(repo)
                oid = self._blob_oid(repo, commit)
                subprocess.run(["git", "-C", str(repo), "repack", "-ad"], check=True)
                loose = repo / ".git" / "objects" / oid[:2] / oid[2:]
                self.assertFalse(loose.exists(), loose)
                loose.parent.mkdir(parents=True, exist_ok=True)
                loose.write_bytes(raw)
                got = subprocess.check_output(["git", "-C", str(repo), "cat-file", "blob", oid])
                self.assertEqual(got, self.body)
                shown = subprocess.check_output(
                    ["git", "-C", str(repo), "show", f"{commit}:contracts/recursion/PRODUCT.md"]
                )
                self.assertEqual(shown, self.body)
                self.assertEqual(pin_check.vault_errors(self._pin_for(commit), repo), [])

    def test_loose_trailing_bytes_match_git(self) -> None:
        """Bytes after a finished loose zlib stream are not a size error.

        Git reads them. Bodies of 60, 200 and 65536 bytes all spill past the
        first 64-byte inflate window, which is the path that used to treat
        Python's unconsumed tail as a header mismatch.
        """
        for size in (60, 200, 65536):
            with self.subTest(size=size):
                repo = Path(self.tmp.name) / f"loose-tail-{size}"
                repo.mkdir()
                git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
                subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
                body = b"P" * size
                target = repo / "contracts" / "recursion" / "PRODUCT.md"
                target.parent.mkdir(parents=True)
                target.write_bytes(body)
                subprocess.run([*git, "add", "-A"], check=True)
                subprocess.run([*git, "commit", "-q", "-m", "contract"], check=True)
                commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
                oid = subprocess.check_output(
                    ["git", "-C", str(repo), "rev-parse", f"{commit}:contracts/recursion/PRODUCT.md"],
                    text=True,
                ).strip()
                loose = repo / ".git" / "objects" / oid[:2] / oid[2:]
                os.chmod(loose, 0o644)
                loose.write_bytes(loose.read_bytes() + b"GARBAGE")
                got = subprocess.check_output(["git", "-C", str(repo), "cat-file", "blob", oid])
                self.assertEqual(got, body)
                pin = {**PINNED, "commit": commit, "sha256": hashlib.sha256(body).hexdigest()}
                self.assertEqual(pin_check.vault_errors(pin, repo), [])

    def test_second_pack_copy_is_used_when_the_first_fails_verify(self) -> None:
        """An oid whose first packed copy does not hash is read from a later pack."""
        repo = Path(self.tmp.name) / "second-pack"
        commit = self._commit_repo(repo)
        oid = self._blob_oid(repo, commit)
        subprocess.run(["git", "-C", str(repo), "repack", "-ad"], check=True)
        packdir = repo / ".git" / "objects" / "pack"
        bad = _single_blob_pack(b"not-the-product-bytes")
        (packdir / "aaa.pack").write_bytes(bad)
        (packdir / "aaa.idx").write_bytes(_idx_v2_one(bytes.fromhex(oid), 12, bad))
        store = pin_check._ObjectStore(repo / ".git" / "objects")
        oid_b = bytes.fromhex(oid)
        offsets = [pack.offset(oid_b) for pack in store.packs]
        self.assertEqual(offsets[0], 12, "the corrupt copy must be the first pack searched")
        self.assertGreaterEqual(sum(offset is not None for offset in offsets), 2)
        typ, content = store.get(oid_b)
        self.assertEqual((typ, content), (b"blob", self.body))
        self.assertEqual(pin_check.vault_errors(self._pin_for(commit), repo), [])

    def test_ref_delta_skips_same_length_poison_base_in_an_earlier_pack(self) -> None:
        """A REF delta whose base is wrong in an earlier pack still matches git.

        The good pack is an undeltified base plus a type-7 delta that appends
        ``-TAIL``, and ``git index-pack`` accepts it. ``aaa.pack`` sorts first.
        Its index maps the base oid at the poison object: different bytes, same
        length, so the delta would apply and then fail the hash. A fresh store
        has not cached the base. ``get`` on the delta must return the bytes
        ``git cat-file --batch`` returns.
        """
        base = b"base-bytes"
        suffix = b"-TAIL"
        poison = b"BASE-BYTES"
        self.assertEqual(len(poison), len(base))
        self.assertNotEqual(poison, base)
        delta = _append_delta(base, suffix)
        self.assertEqual(pin_check._apply_delta(base, delta), base + suffix)
        self.assertNotEqual(pin_check._apply_delta(poison, delta), base + suffix)
        pack, base_oid, delta_oid = _ref_append_pack(base, suffix)
        repo = Path(self.tmp.name) / "ref-poison"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self._install_pack(repo, pack)
        bad = _single_blob_pack(poison)
        packdir = repo / ".git" / "objects" / "pack"
        (packdir / "aaa.pack").write_bytes(bad)
        (packdir / "aaa.idx").write_bytes(_idx_v2_one(base_oid, 12, bad))
        raw = subprocess.check_output(
            ["git", "-C", str(repo), "cat-file", "--batch"],
            input=delta_oid.hex().encode() + b"\n",
        )
        header, _, rest = raw.partition(b"\n")
        git_oid, git_type, git_size = header.split()
        size = int(git_size)
        self.assertEqual(git_oid, delta_oid.hex().encode())
        self.assertEqual((git_type, rest[:size]), (b"blob", base + suffix))
        store = pin_check._ObjectStore(repo / ".git" / "objects")
        self.assertEqual(len(store.packs), 2)
        self.assertEqual(store.packs[0].offset(base_oid), 12)
        self.assertIsNone(store.packs[0].offset(delta_oid))
        self.assertIsNotNone(store.packs[1].offset(delta_oid))
        self.assertEqual(store.cache, {})
        self.assertEqual(store.get(delta_oid), (b"blob", rest[:size]))

    def test_corrupt_loose_without_a_pack_is_refused(self) -> None:
        repo = Path(self.tmp.name) / "loose-only-bad"
        commit = self._commit_repo(repo)
        oid = self._blob_oid(repo, commit)
        loose = repo / ".git" / "objects" / oid[:2] / oid[2:]
        os.chmod(loose, 0o644)
        loose.write_bytes(zlib.compress(b"blob 4\0nope"))
        problems = self._assert_refused(repo, self._pin_for(commit))
        self.assertIn("does not match its name", problems[0])

    def test_midx_version_2_local_packs_pass_and_escape_is_refused(self) -> None:
        """Version 2 is the current default and uses the same chunk framing as version 1."""
        repo = Path(self.tmp.name) / "midx-v2"
        commit = self._commit_repo(repo)
        subprocess.run(["git", "-C", str(repo), "repack", "-ad"], check=True)
        packdir = repo / ".git" / "objects" / "pack"
        idx_name = next(path.name for path in packdir.iterdir() if path.suffix == ".idx")
        midx = packdir / "multi-pack-index"
        midx.write_bytes(self._craft_midx([idx_name], version=2))
        self.assertEqual(pin_check.vault_errors(self._pin_for(commit), repo), [])
        midx.write_bytes(self._craft_midx(["../" + idx_name], version=2))
        problems = self._assert_refused(repo, self._pin_for(commit))
        self.assertIn("multi-pack-index", problems[0])

    def test_no_delta_base_offset_pack_matches_git(self) -> None:
        """A pack written with REF deltas only resolves to the same blob git reads."""
        repo = Path(self.tmp.name) / "no-delta-base"
        repo.mkdir()
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        body = b"A" * 200
        target = repo / "contracts" / "recursion" / "PRODUCT.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(body + b"-ONE")
        (repo / "other.txt").write_bytes(body + b"-TWO")
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "contract"], check=True)
        commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        oid = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", f"{commit}:contracts/recursion/PRODUCT.md"],
            text=True,
        ).strip()
        packdir = repo / ".git" / "objects" / "pack"
        packdir.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "pack-objects",
                "--all",
                "--no-delta-base-offset",
                "--window=10",
                "--depth=5",
                str(packdir / "nodelta"),
            ],
            capture_output=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        packs = list(packdir.glob("nodelta-*.pack"))
        self.assertEqual(len(packs), 1, packs)
        self.assertTrue(_pack_has_ref_delta(packs[0].read_bytes()))
        for loose in (repo / ".git" / "objects").glob("[0-9a-f][0-9a-f]/*"):
            loose.unlink()
        got = subprocess.check_output(["git", "-C", str(repo), "cat-file", "blob", oid])
        self.assertEqual(got, body + b"-ONE")
        pin = {**PINNED, "commit": commit, "sha256": hashlib.sha256(body + b"-ONE").hexdigest()}
        self.assertEqual(pin_check.vault_errors(pin, repo), [])


def _pack_has_ref_delta(pack: bytes) -> bool:
    """True when the pack contains a type-7 REF delta."""
    pos = 12
    count = int.from_bytes(pack[8:12], "big")
    found = False
    for _ in range(count):
        if pos >= len(pack) - 20:
            break
        byte = pack[pos]
        pos += 1
        obj_type = (byte >> 4) & 7
        size = byte & 15
        shift = 4
        while byte & 0x80:
            byte = pack[pos]
            pos += 1
            size |= (byte & 0x7F) << shift
            shift += 7
        if obj_type == 6:
            byte = pack[pos]
            pos += 1
            while byte & 0x80:
                byte = pack[pos]
                pos += 1
        elif obj_type == 7:
            pos += 20
            found = True
        stream = zlib.decompressobj()
        payload = stream.decompress(pack[pos:])
        if len(payload) != size:
            return False
        pos = len(pack) - len(stream.unused_data)
    return found


class DeltaChainBoundTests(unittest.TestCase):
    def test_two_poison_ref_packs_do_not_rewalk_a_failed_base(self) -> None:
        """Two poison copies of one REF chain must not re-search a failed base.

        Forgetting the failure reads ``2**(depth + 2) - 1`` objects (262143 at
        depth 16). Each distinct pack offset is expanded once, then the real
        blob is read.
        """
        depth = 16
        content = b"real-bytes"
        oid = _content_oid(content)
        poison, pidx = _poison_ref_chain(depth, oid)
        valid = _single_blob_pack(content)
        vidx = _idx_v2_one(oid, 12, valid)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_store(root, [("a", poison, pidx), ("b", poison, pidx), ("c", valid, vidx)])
            store = pin_check._ObjectStore(root)
            calls, got = _count_pack_reads(lambda: store.get(oid))
        self.assertEqual(got, (b"blob", content))
        self.assertLessEqual(calls, 2 * (depth + 1) + 1, calls)
        self.assertGreater(calls, 0)

    def test_loose_and_cached_leaf_uses_the_packed_depth_cap(self) -> None:
        """4095 REF deltas onto a loose or cached leaf resolve; 4096 are refused.

        Packed chains already stop at 4096. The leaf after the last increment
        has to use that same cap when it comes from a loose file or the cache.
        """
        leaf = b"L"
        for kind in ("loose", "cached"):
            for depth in (4095, 4096):
                with self.subTest(kind=kind, depth=depth):
                    pack, idx, tip, leaf_oid = _ref_chain_onto(depth, leaf)
                    with tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp)
                        _write_store(root, [("a", pack, idx)])
                        if kind == "loose":
                            _write_loose(root, leaf_oid, leaf)
                        store = pin_check._ObjectStore(root)
                        if kind == "cached":
                            store.cache[leaf_oid] = (b"blob", leaf)
                        if depth <= pin_check._MAX_DELTA_DEPTH:
                            self.assertEqual(store.get(tip), (b"blob", depth.to_bytes(2, "big")))
                        else:
                            with self.assertRaises(pin_check._ObjectMissing) as caught:
                                store.get(tip)
                            self.assertEqual(caught.exception.message, "delta chain is too deep")

    def test_mutual_ref_cycle_uses_a_later_real_blob(self) -> None:
        """A REF cycle in an earlier pack does not hide the real blob in a later pack."""
        content = b"real-bytes"
        oid = _content_oid(content)
        other = b"\x11" * 20
        junk = _insert_delta(b"x", b"y")
        objects = [_ref_obj(other, junk), _ref_obj(oid, junk)]
        pack, offsets = _pack_bytes(objects)
        idx = _idx_v2_entries([(oid, offsets[0]), (other, offsets[1])], pack)
        valid = _single_blob_pack(content)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_store(
                root,
                [("a", pack, idx), ("b", valid, _idx_v2_one(oid, 12, valid))],
            )
            store = pin_check._ObjectStore(root)
            self.assertEqual(store.get(oid), (b"blob", content))

    def test_ref_delta_uses_a_base_stored_in_a_later_pack(self) -> None:
        """The delta may be the only copy of the tip, with its base in a later pack."""
        base = b"base-bytes"
        suffix = b"-TAIL"
        base_oid = _content_oid(base)
        tip_oid = _content_oid(base + suffix)
        delta = _append_delta(base, suffix)
        # Earlier pack also has a REF cycle copy of the base, which must be skipped.
        junk = _insert_delta(base, b"nope")
        objects = [_ref_obj(base_oid, delta), _ref_obj(tip_oid, junk)]
        pack, offsets = _pack_bytes(objects)
        earlier = _idx_v2_entries([(tip_oid, offsets[0]), (base_oid, offsets[1])], pack)
        later_objects = [_blob_obj(base)]
        later, later_offsets = _pack_bytes(later_objects)
        later_idx = _idx_v2_entries([(base_oid, later_offsets[0])], later)
        self.assertLess(offsets[0], offsets[1])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_store(root, [("a", pack, earlier), ("b", later, later_idx)])
            store = pin_check._ObjectStore(root)
            self.assertEqual(store.get(tip_oid), (b"blob", base + suffix))

    def test_forward_ref_delta_matches_its_base(self) -> None:
        """A REF delta may sit at a lower offset than the base it names."""
        base = b"base-bytes"
        suffix = b"-TAIL"
        base_oid = _content_oid(base)
        tip_oid = _content_oid(base + suffix)
        objects = [_ref_obj(base_oid, _append_delta(base, suffix)), _blob_obj(base)]
        pack, offsets = _pack_bytes(objects)
        self.assertLess(offsets[0], offsets[1])
        idx = _idx_v2_entries([(tip_oid, offsets[0]), (base_oid, offsets[1])], pack)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_store(root, [("a", pack, idx)])
            store = pin_check._ObjectStore(root)
            self.assertEqual(store.get(tip_oid), (b"blob", base + suffix))

    def test_ofs_delta_ignores_a_poison_ref_base_in_an_earlier_pack(self) -> None:
        """An OFS delta uses the base at its offset, not a same-named poison REF."""
        base = b"base-bytes"
        suffix = b"-TAIL"
        poison = b"BASE-BYTES"
        self.assertEqual(len(poison), len(base))
        base_oid = _content_oid(base)
        tip_oid = _content_oid(base + suffix)
        delta = _append_delta(base, suffix)
        bad = _single_blob_pack(poison)
        bad_idx = _idx_v2_one(base_oid, 12, bad)
        base_obj = _blob_obj(base)
        # Placeholder so the relative offset can be computed after both sizes are known.
        relative = len(base_obj)
        ofs = _ofs_obj(relative, delta)
        pack, offsets = _pack_bytes([base_obj, ofs])
        self.assertEqual(offsets[1] - offsets[0], relative)
        idx = _idx_v2_entries([(base_oid, offsets[0]), (tip_oid, offsets[1])], pack)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_store(root, [("a", bad, bad_idx), ("b", pack, idx)])
            store = pin_check._ObjectStore(root)
            self.assertEqual(store.packs[0].offset(base_oid), 12)
            self.assertEqual(store.get(tip_oid), (b"blob", base + suffix))

    def test_corrupt_loose_base_falls_through_to_the_packed_base(self) -> None:
        """A loose base that will not inflate does not hide the packed base."""
        base = b"base-bytes"
        suffix = b"-TAIL"
        base_oid = _content_oid(base)
        tip_oid = _content_oid(base + suffix)
        objects = [_ref_obj(base_oid, _append_delta(base, suffix)), _blob_obj(base)]
        pack, offsets = _pack_bytes(objects)
        idx = _idx_v2_entries([(tip_oid, offsets[0]), (base_oid, offsets[1])], pack)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_store(root, [("a", pack, idx)])
            _write_loose(root, base_oid, base)
            loose = root / base_oid.hex()[:2] / base_oid.hex()[2:]
            loose.write_bytes(b"this is not a zlib stream")
            store = pin_check._ObjectStore(root)
            self.assertEqual(store.get(tip_oid), (b"blob", base + suffix))


if __name__ == "__main__":
    unittest.main()
