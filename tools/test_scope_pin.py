"""Scope and product-pin gates fail closed when a path cannot be certified."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pin = load_tool("check_product_pin")
scope = load_tool("check_public_scope")


def foreign_word() -> str:
    """The gate's own term, built at runtime so this file stays scannable."""
    return "".join(chr(code) for code in (99, 104, 105, 114, 111, 110))


def candidate(**changes):
    value = dict(
        format=pin.FORMAT,
        status="pinned",
        repository=pin.REPOSITORY,
        path="contracts/PRODUCT.md",
        commit="a" * 40,
        sha256="b" * 64,
        claim="Candidate product contract",
        limitations="Not acceptance",
    )
    value.update(changes)
    return value


def git_env():
    return dict(
        os.environ,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="fixture",
        GIT_AUTHOR_EMAIL="fixture@example.invalid",
        GIT_COMMITTER_NAME="fixture",
        GIT_COMMITTER_EMAIL="fixture@example.invalid",
        GIT_TERMINAL_PROMPT="0",
    )


def git(*args, env):
    return subprocess.check_output(
        ["git", "-c", "credential.helper=", "-c", "core.hooksPath=" + os.devnull,
         "-c", "commit.gpgsign=false", *args],
        env=env,
        stderr=subprocess.PIPE,
        timeout=15,
    )


class PublicScopeRegressions(unittest.TestCase):
    def scan(self, root):
        output = io.StringIO()
        with mock.patch.object(scope, "ROOT", root), contextlib.redirect_stdout(output):
            result = scope.main()
        return result, output.getvalue()

    def test_F3_utf16_markdown_cannot_get_a_clean_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text(foreign_word() + "\n", encoding="utf-16")
            self.assertEqual(self.scan(root)[0], 1)

    def test_F3_unreadable_markdown_cannot_get_a_clean_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text(foreign_word() + "\n", encoding="utf-8")
            with mock.patch.object(Path, "read_text", side_effect=PermissionError("synthetic denial")):
                self.assertEqual(self.scan(root)[0], 1)

    @unittest.skipIf(os.name == "nt", "symlink creation requires platform privileges")
    def test_F4_external_symlink_is_not_opened_or_echoed(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            target = Path(outside) / "synthetic.txt"
            target.write_text(foreign_word() + " SYNTHETIC_PRIVATE_CONTEXT\n", encoding="utf-8")
            (root / "external.txt").symlink_to(target)
            original_read = Path.read_text

            def confined_read(path, *args, **kwargs):
                self.assertNotEqual(path.resolve(), target.resolve(), "opened an external target")
                return original_read(path, *args, **kwargs)

            with mock.patch.object(Path, "read_text", new=confined_read):
                result, output = self.scan(root)
            self.assertEqual(result, 1)
            self.assertIn("symlink leaves the repository", output)
            self.assertNotIn("SYNTHETIC_PRIVATE_CONTEXT", output)
            self.assertNotIn("PASS:", output)

    @unittest.skipIf(os.name == "nt", "symlink creation requires platform privileges")
    def test_directory_symlink_outside_the_tree_is_refused(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            (root / "ok.txt").write_text("hello\n", encoding="utf-8")
            secret = Path(outside) / "secret.md"
            secret.write_text(foreign_word() + " SYNTHETIC_PRIVATE_CONTEXT\n", encoding="utf-8")
            (root / "escape").symlink_to(Path(outside), target_is_directory=True)
            result, output = self.scan(root)
            self.assertEqual(result, 1, output)
            self.assertIn("escape", output)
            self.assertIn("symlink leaves the repository", output)
            self.assertNotIn("SYNTHETIC_PRIVATE_CONTEXT", output)
            self.assertNotIn("PASS:", output)

    @unittest.skipIf(os.name == "nt", "symlink loops are a POSIX witness")
    def test_symlink_loop_is_refused_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "a.txt"
            second = root / "b.txt"
            first.symlink_to(second)
            second.symlink_to(first)
            loop = root / "dirloop"
            loop.symlink_to(loop)
            result, output = self.scan(root)
            self.assertEqual(result, 1, output)
            self.assertNotIn("Traceback", output)
            self.assertIn("not a regular file", output)
            self.assertNotIn("PASS:", output)

    def test_unreadable_directory_is_not_a_clean_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            locked = root / "locked"
            locked.mkdir()
            (locked / "hidden.md").write_text(foreign_word() + " SYNTHETIC_PRIVATE_CONTEXT\n", encoding="utf-8")
            locked.chmod(0)
            try:
                result, output = self.scan(root)
            finally:
                locked.chmod(0o755)
            self.assertEqual(result, 1, output)
            self.assertIn("unreadable", output)
            self.assertNotIn("SYNTHETIC_PRIVATE_CONTEXT", output)
            self.assertNotIn("PASS:", output)

    def test_scope_scan_of_this_repository_passes(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = scope.main()
        self.assertEqual(result, 0, output.getvalue())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFO support")
    def test_F5_fifo_does_not_block_the_scope_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.mkfifo(root / "build.log")
            code = (
                "import sys; from pathlib import Path; "
                f"sys.path.insert(0, {str(ROOT / 'tools')!r}); "
                "import check_public_scope as s; "
                f"s.ROOT=Path({str(root)!r}); raise SystemExit(s.main())"
            )
            done = subprocess.run(
                [sys.executable, "-B", "-c", code],
                timeout=5,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            self.assertIn("not a regular file", done.stdout)
            self.assertNotIn("PASS:", done.stdout)

    @unittest.skipIf(os.name == "nt", "newline filename is a POSIX witness")
    def test_F7_scope_diagnostics_escape_terminal_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "note\nFORGED.txt").write_text(foreign_word() + " \x1b[2J\n", encoding="utf-8")
            result, output = self.scan(root)
            self.assertEqual(result, 1)
            self.assertNotIn("\x1b", output)
            self.assertNotIn("\nFORGED.txt", output)
            for splitter in ("\u2028", "\u2029"):
                forged = root / f"note{splitter}PASS: forged.txt"
                forged.write_text(foreign_word() + "\n", encoding="utf-8")
                result, output = self.scan(root)
                self.assertEqual(result, 1, output)
                self.assertNotIn(splitter, output)
                self.assertFalse(any(line.startswith("PASS:") for line in output.splitlines()), output)
                forged.unlink()

    @unittest.skipIf(os.name == "nt", "surrogate bytes in a file name are a POSIX witness")
    def test_scope_lone_surrogate_in_a_file_name_is_escaped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_name = os.fsencode(root) + b"/note\xed\xa0\x80.txt"
            with open(raw_name, "wb") as handle:
                handle.write((foreign_word() + "\n").encode())
            result, output = self.scan(root)
            self.assertEqual(result, 1, output)
            output.encode("utf-8")
            self.assertFalse(any(0xD800 <= ord(char) <= 0xDFFF for char in output), output)


class ProductPinRegressions(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "local upload-pack command is a POSIX fixture")
    def test_F8_partial_clone_cannot_trigger_network_access(self):
        # The promisor is a local file repository. A lazy fetch would fill the
        # missing blob and the pin would match, with no socket opened by this test.
        env = git_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, clone = root / "source", root / "clone"
            source.mkdir()
            body = b"# Synthetic product contract\n"
            (source / "PRODUCT.md").write_bytes(body)
            git("-C", str(source), "init", "-q", env=env)
            git("-C", str(source), "add", "PRODUCT.md", env=env)
            git("-C", str(source), "commit", "-qm", "synthetic contract fixture", env=env)
            commit = git("-C", str(source), "rev-parse", "HEAD", env=env).decode().strip()
            git(
                "clone", "-q", "--filter=blob:none",
                "--upload-pack=git -c uploadpack.allowFilter=true upload-pack",
                "--no-checkout", source.resolve().as_uri(), str(clone),
                env=env,
            )
            missing = git("-C", str(clone), "rev-list", "--objects", "--missing=print", "HEAD", env=env).decode()
            self.assertTrue(any(line.startswith("?") for line in missing.splitlines()), missing)
            document = candidate(
                path="PRODUCT.md",
                commit=commit,
                sha256=hashlib.sha256(body).hexdigest(),
            )
            with mock.patch.dict(os.environ, env):
                problems = pin.vault_errors(document, clone)
            self.assertTrue(problems, "a missing promisor blob was accepted")
            still = git("-C", str(clone), "rev-list", "--objects", "--missing=print", "HEAD", env=env).decode()
            self.assertTrue(
                any(line.startswith("?") for line in still.splitlines()),
                "the offline check fetched the missing blob from the promisor",
            )

    def test_F6_directory_listing_is_not_a_pinned_blob(self):
        env = git_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "contracts").mkdir()
            (root / "contracts/PRODUCT.md").write_text("# Candidate\n", encoding="utf-8")
            git("-C", str(root), "init", "-q", env=env)
            git("-C", str(root), "add", "contracts/PRODUCT.md", env=env)
            git("-C", str(root), "commit", "-qm", "synthetic contract fixture", env=env)
            commit = git("-C", str(root), "rev-parse", "HEAD", env=env).decode().strip()
            listing = git("-C", str(root), "show", f"{commit}:contracts", env=env)
            document = candidate(
                path="contracts",
                commit=commit,
                sha256=hashlib.sha256(listing).hexdigest(),
            )
            self.assertEqual(pin.errors(document), [])
            problems = pin.vault_errors(document, root)
            self.assertTrue(problems)
            self.assertIn("not a pinned blob", problems[0])

    def test_tag_or_tree_object_is_not_a_commit_pin(self):
        env = git_env()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = b"# Synthetic product contract\n"
            (root / "PRODUCT.md").write_bytes(body)
            git("-C", str(root), "init", "-q", env=env)
            git("-C", str(root), "add", "PRODUCT.md", env=env)
            git("-C", str(root), "commit", "-qm", "synthetic contract fixture", env=env)
            git("-C", str(root), "tag", "-a", "synthetic-pin", "-m", "synthetic tag", env=env)
            tag = git("-C", str(root), "rev-parse", "synthetic-pin", env=env).decode().strip()
            tree = git("-C", str(root), "rev-parse", "HEAD^{tree}", env=env).decode().strip()
            digest = hashlib.sha256(body).hexdigest()
            for label, object_id in (("tag", tag), ("tree", tree)):
                with self.subTest(label=label):
                    document = candidate(path="PRODUCT.md", commit=object_id, sha256=digest)
                    self.assertEqual(pin.errors(document), [])
                    problems = pin.vault_errors(document, root)
                    self.assertTrue(problems, label)
                    self.assertIn("not a commit", problems[0])

    def test_vault_errors_refuses_a_malformed_pin_without_raising(self):
        self.assertEqual(pin.vault_errors(["not-a-pin"], Path(".")), ["pin must be a JSON object"])
        problems = pin.vault_errors({"status": "pinned"}, Path("."))
        self.assertTrue(problems)
        self.assertIn("missing keys", problems[0])
        self.assertEqual(pin.vault_errors({"status": "awaiting"}, Path(".")), [])

    def test_vault_errors_escapes_a_line_separator_in_the_commit(self):
        document = {
            "status": "pinned",
            "commit": "abc\u2028PASS: forged",
            "path": "PRODUCT.md",
            "sha256": "ab",
        }
        problems = pin.vault_errors(document, Path("."))
        self.assertTrue(problems)
        self.assertNotIn("\u2028", problems[0])
        self.assertFalse(any(line.startswith("PASS:") for line in problems[0].splitlines()), problems)

    def test_object_label_is_capped_at_forty_characters(self):
        self.assertEqual(pin._object_label("k" * 200), "k" * 40)
        self.assertEqual(pin._object_label("   "), "unknown object")

    def test_F7_unknown_key_cannot_inject_a_diagnostic_line(self):
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "pin.json"
            document.write_text(
                json.dumps({**candidate(), "extra\nPASS: forged\u001b[2J": 1}),
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = pin.main(["--pin", str(document)])
            self.assertEqual(result, 1)
            self.assertNotIn("\x1b", output.getvalue())
            self.assertNotIn("\nPASS: forged", output.getvalue())
            for splitter in ("\u2028", "\u2029"):
                document.write_text(
                    json.dumps({**candidate(), f"extra{splitter}PASS: forged": 1}),
                    encoding="utf-8",
                )
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = pin.main(["--pin", str(document)])
                text = output.getvalue()
                self.assertEqual(result, 1, text)
                self.assertNotIn(splitter, text)
                self.assertFalse(any(line.startswith("PASS:") for line in text.splitlines()), text)


if __name__ == "__main__":
    unittest.main()
