#!/usr/bin/env python3
"""Provider-free tests for land, clean, logs, and the remaining adapters."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v3.harness.cli import main  # noqa: E402
from scripts.v3.harness.config import load_config, starter_config  # noqa: E402
from scripts.v3.harness.doctor import diagnose  # noqa: E402
from scripts.v3.harness.errors import HarnessError, LiveProviderDisabled  # noqa: E402
from scripts.v3.harness.providers import (  # noqa: E402
    LAUNCH_CRITICAL,
    CursorHeadlessProvider,
    GeminiHeadlessProvider,
    OpenCodeHeadlessProvider,
    ProviderRequest,
    adapters,
    open_provider,
)
from scripts.v3.harness.run import run_task  # noqa: E402
from scripts.v3.harness.state import load_run, save_run  # noqa: E402


RUN_ID = "run-aabbccddeeff"
OPEN_ID = "run-001122334455"
TOKEN = "sk-" + "proj-" + "abcdefghijKLMNOP"
BEARER = "Bearer " + "supersecretvalue"


def _completed(argv, code: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-c", "core.longpaths=true", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout or f"git {args[0]} failed")
    return completed


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    created = _git(repo, "init", "-b", "master", check=False)
    if created.returncode != 0:
        _git(repo, "init")
        _git(repo, "checkout", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")


def _on_dev(repo: Path) -> None:
    _git(repo, "checkout", "-b", "dev")


def _accept(state: Path, sha: str, **kwargs) -> None:
    payload = _record(RUN_ID, status="completed", accepted=True, **kwargs)
    payload["reviewed_commit"] = sha
    save_run(state, payload)


def _record(run_id: str, *, status: str, accepted: bool, branch: str | None = "feature") -> dict:
    payload = {
        "accepted": accepted,
        "provider": "fixture",
        "reviewers": ["fixture-b"],
        "reviews": [],
        "run_id": run_id,
        "status": status,
        "task": "task",
        "text": "ok",
        "usage": {"cost": None, "input_tokens": None, "output_tokens": None},
    }
    if branch is not None:
        payload["run_branch"] = branch
    return payload


class _Tree:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.repo = root / "repo"
        self.state = root / "state"
        self.state.mkdir()
        _init_repo(self.repo)

    def close(self) -> None:
        self._tmp.cleanup()


class LandCleanLogTests(unittest.TestCase):
    def test_land_merges_the_reviewed_commit(self) -> None:
        tree = _Tree()
        self.addCleanup(tree.close)
        _on_dev(tree.repo)
        _git(tree.repo, "checkout", "-b", "feature")
        (tree.repo / "extra.txt").write_text("extra\n", encoding="utf-8")
        _git(tree.repo, "add", "extra.txt")
        _git(tree.repo, "commit", "-m", "feature")
        sha = _git(tree.repo, "rev-parse", "HEAD").stdout.strip()
        _git(tree.repo, "checkout", "dev")
        _accept(tree.state, sha)

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["land", RUN_ID, "--state", str(tree.state), "--repo", str(tree.repo), "--into", "dev"])
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertEqual((tree.repo / "extra.txt").read_text(encoding="utf-8"), "extra\n")
        self.assertEqual(_git(tree.repo, "status", "--porcelain").stdout, "")
        subject = _git(tree.repo, "log", "-1", "--format=%s").stdout.strip()
        self.assertEqual(subject, f"recursion: land {RUN_ID}")
        self.assertEqual(_git(tree.repo, "rev-parse", "HEAD^2").stdout.strip(), sha)
        record = json.loads(stdout.getvalue())
        self.assertEqual(record["land"]["status"], "merged")
        self.assertTrue(record["land"]["commit"])

    def test_land_refuses_a_tip_the_reviewer_did_not_approve(self) -> None:
        tree = _Tree()
        self.addCleanup(tree.close)
        _on_dev(tree.repo)
        _git(tree.repo, "checkout", "-b", "feature")
        (tree.repo / "extra.txt").write_text("extra\n", encoding="utf-8")
        _git(tree.repo, "add", "extra.txt")
        _git(tree.repo, "commit", "-m", "feature")
        sha = _git(tree.repo, "rev-parse", "HEAD").stdout.strip()
        _git(tree.repo, "checkout", "dev")
        _accept(tree.state, sha)
        _git(tree.repo, "checkout", "feature")
        (tree.repo / "later.txt").write_text("later\n", encoding="utf-8")
        _git(tree.repo, "add", "later.txt")
        _git(tree.repo, "commit", "-m", "later")
        _git(tree.repo, "checkout", "dev")
        before = _git(tree.repo, "rev-parse", "HEAD").stdout.strip()

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["land", RUN_ID, "--state", str(tree.state), "--repo", str(tree.repo)])
        self.assertEqual(code, 1)
        self.assertIn("branch tip differs from the reviewed commit", stderr.getvalue())
        self.assertFalse((tree.repo / "extra.txt").exists())
        self.assertFalse((tree.repo / "later.txt").exists())
        self.assertEqual(_git(tree.repo, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertEqual(load_run(tree.state, RUN_ID)["land"]["status"], "refused")

    def test_land_refuses_when_acceptance_did_not_record_a_commit(self) -> None:
        tree = _Tree()
        self.addCleanup(tree.close)
        _on_dev(tree.repo)
        _git(tree.repo, "checkout", "-b", "feature")
        (tree.repo / "extra.txt").write_text("extra\n", encoding="utf-8")
        _git(tree.repo, "add", "extra.txt")
        _git(tree.repo, "commit", "-m", "feature")
        _git(tree.repo, "checkout", "dev")
        save_run(tree.state, _record(RUN_ID, status="completed", accepted=True))

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["land", RUN_ID, "--state", str(tree.state), "--repo", str(tree.repo)])
        self.assertEqual(code, 1)
        self.assertIn("reviewed commit is missing", stderr.getvalue())
        self.assertFalse((tree.repo / "extra.txt").exists())
        self.assertEqual(load_run(tree.state, RUN_ID)["land"]["status"], "refused")

    def test_land_refuses_master_and_main(self) -> None:
        for name in ("master", "main"):
            with self.subTest(name=name):
                tree = _Tree()
                self.addCleanup(tree.close)
                if name == "main":
                    _git(tree.repo, "checkout", "-b", "main")
                _git(tree.repo, "checkout", "-b", "feature")
                (tree.repo / "extra.txt").write_text("extra\n", encoding="utf-8")
                _git(tree.repo, "add", "extra.txt")
                _git(tree.repo, "commit", "-m", "feature")
                sha = _git(tree.repo, "rev-parse", "HEAD").stdout.strip()
                _git(tree.repo, "checkout", name)
                before = _git(tree.repo, "rev-parse", "HEAD").stdout.strip()
                _accept(tree.state, sha)

                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    code = main(["land", RUN_ID, "--state", str(tree.state), "--repo", str(tree.repo)])
                self.assertEqual(code, 1)
                self.assertIn(f"refusing to merge into {name}", stderr.getvalue())
                self.assertFalse((tree.repo / "extra.txt").exists())
                self.assertEqual(_git(tree.repo, "rev-parse", "HEAD").stdout.strip(), before)
                self.assertEqual(load_run(tree.state, RUN_ID)["land"]["status"], "refused")

    def test_land_refuses_a_dirty_tree(self) -> None:
        tree = _Tree()
        self.addCleanup(tree.close)
        _on_dev(tree.repo)
        _git(tree.repo, "checkout", "-b", "feature")
        (tree.repo / "extra.txt").write_text("extra\n", encoding="utf-8")
        _git(tree.repo, "add", "extra.txt")
        _git(tree.repo, "commit", "-m", "feature")
        sha = _git(tree.repo, "rev-parse", "HEAD").stdout.strip()
        _git(tree.repo, "checkout", "dev")
        (tree.repo / "base.txt").write_text("dirty\n", encoding="utf-8")
        before = _git(tree.repo, "rev-parse", "HEAD").stdout.strip()
        _accept(tree.state, sha)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["land", RUN_ID, "--state", str(tree.state), "--repo", str(tree.repo)])
        self.assertEqual(code, 1)
        self.assertIn("working tree is dirty", stderr.getvalue())
        self.assertFalse((tree.repo / "extra.txt").exists())
        self.assertEqual(_git(tree.repo, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertEqual((tree.repo / "base.txt").read_text(encoding="utf-8"), "dirty\n")
        self.assertEqual(load_run(tree.state, RUN_ID)["land"]["status"], "refused")

    def test_land_refuses_to_overwrite_an_ignored_file(self) -> None:
        tree = _Tree()
        self.addCleanup(tree.close)
        _on_dev(tree.repo)
        (tree.repo / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
        _git(tree.repo, "add", ".gitignore")
        _git(tree.repo, "commit", "-m", "ignore secret")
        _git(tree.repo, "checkout", "-b", "feature")
        (tree.repo / "secret.txt").write_text("from-branch\n", encoding="utf-8")
        _git(tree.repo, "add", "-f", "secret.txt")
        _git(tree.repo, "commit", "-m", "add secret")
        sha = _git(tree.repo, "rev-parse", "HEAD").stdout.strip()
        _git(tree.repo, "checkout", "dev")
        (tree.repo / "secret.txt").write_text("local-secret\n", encoding="utf-8")
        self.assertEqual(_git(tree.repo, "status", "--porcelain").stdout, "")
        before = _git(tree.repo, "rev-parse", "HEAD").stdout.strip()
        _accept(tree.state, sha)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["land", RUN_ID, "--state", str(tree.state), "--repo", str(tree.repo)])
        self.assertEqual(code, 1)
        self.assertIn("working tree is dirty", stderr.getvalue())
        self.assertEqual((tree.repo / "secret.txt").read_text(encoding="utf-8"), "local-secret\n")
        self.assertEqual(_git(tree.repo, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertFalse((tree.repo / ".git" / "MERGE_HEAD").exists())
        self.assertEqual(load_run(tree.state, RUN_ID)["land"]["status"], "refused")

    def test_an_unrelated_ignored_file_does_not_block_land(self) -> None:
        tree = _Tree()
        self.addCleanup(tree.close)
        _on_dev(tree.repo)
        (tree.repo / ".gitignore").write_text("other.txt\n", encoding="utf-8")
        _git(tree.repo, "add", ".gitignore")
        _git(tree.repo, "commit", "-m", "ignore other")
        _git(tree.repo, "checkout", "-b", "feature")
        (tree.repo / "extra.txt").write_text("extra\n", encoding="utf-8")
        _git(tree.repo, "add", "extra.txt")
        _git(tree.repo, "commit", "-m", "feature")
        sha = _git(tree.repo, "rev-parse", "HEAD").stdout.strip()
        _git(tree.repo, "checkout", "dev")
        (tree.repo / "other.txt").write_text("keep\n", encoding="utf-8")
        self.assertEqual(_git(tree.repo, "status", "--porcelain").stdout, "")
        _accept(tree.state, sha)

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["land", RUN_ID, "--state", str(tree.state), "--repo", str(tree.repo)])
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertEqual((tree.repo / "extra.txt").read_text(encoding="utf-8"), "extra\n")
        self.assertEqual((tree.repo / "other.txt").read_text(encoding="utf-8"), "keep\n")

    def test_land_refuses_a_conflict_and_aborts(self) -> None:
        tree = _Tree()
        self.addCleanup(tree.close)
        _on_dev(tree.repo)
        _git(tree.repo, "checkout", "-b", "feature")
        (tree.repo / "base.txt").write_text("feature\n", encoding="utf-8")
        _git(tree.repo, "commit", "-am", "feature")
        sha = _git(tree.repo, "rev-parse", "HEAD").stdout.strip()
        _git(tree.repo, "checkout", "dev")
        (tree.repo / "base.txt").write_text("main\n", encoding="utf-8")
        _git(tree.repo, "commit", "-am", "main")
        before = _git(tree.repo, "rev-parse", "HEAD").stdout.strip()
        _accept(tree.state, sha)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["land", RUN_ID, "--state", str(tree.state), "--repo", str(tree.repo)])
        self.assertEqual(code, 1)
        self.assertIn("merge conflict", stderr.getvalue())
        self.assertEqual((tree.repo / "base.txt").read_text(encoding="utf-8"), "main\n")
        self.assertEqual(_git(tree.repo, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertFalse((tree.repo / ".git" / "MERGE_HEAD").exists())
        self.assertEqual(_git(tree.repo, "status", "--porcelain").stdout, "")
        self.assertEqual(load_run(tree.state, RUN_ID)["land"]["status"], "conflict")

    def test_land_refuses_a_run_that_is_not_accepted(self) -> None:
        tree = _Tree()
        self.addCleanup(tree.close)
        _git(tree.repo, "checkout", "-b", "feature")
        (tree.repo / "extra.txt").write_text("extra\n", encoding="utf-8")
        _git(tree.repo, "add", "extra.txt")
        _git(tree.repo, "commit", "-m", "feature")
        _git(tree.repo, "checkout", "master")
        save_run(tree.state, _record(RUN_ID, status="running", accepted=False))

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["land", RUN_ID, "--state", str(tree.state), "--repo", str(tree.repo)])
        self.assertEqual(code, 1)
        self.assertIn("not accepted", stderr.getvalue())
        self.assertFalse((tree.repo / "extra.txt").exists())

    def test_clean_removes_finished_state_and_refuses_in_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            save_run(state, _record(RUN_ID, status="completed", accepted=True, branch=None))
            save_run(state, _record(OPEN_ID, status="running", accepted=False, branch=None))
            (state / f"{RUN_ID}.jsonl").write_text('{"event":"run.finished"}\n', encoding="utf-8")
            workspace = state / RUN_ID / "workspace"
            workspace.mkdir(parents=True)
            (workspace / "note.txt").write_text("note\n", encoding="utf-8")
            (state / OPEN_ID / "workspace").mkdir(parents=True)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["clean", "--run", RUN_ID, "--state", directory])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["removed"], [RUN_ID])
            self.assertFalse((state / f"{RUN_ID}.json").exists())
            self.assertFalse((state / f"{RUN_ID}.jsonl").exists())
            self.assertFalse((state / RUN_ID).exists())
            self.assertTrue((state / f"{OPEN_ID}.json").exists())

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                refused = main(["clean", "--run", OPEN_ID, "--state", directory])
            self.assertEqual(refused, 1)
            self.assertIn("still in progress", stderr.getvalue())
            self.assertTrue((state / f"{OPEN_ID}.json").exists())
            self.assertTrue((state / OPEN_ID).exists())

            save_run(state, _record(RUN_ID, status="failed", accepted=False, branch=None))
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                swept = main(["clean", "--state", directory])
            self.assertEqual(swept, 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["removed"], [RUN_ID])
            self.assertEqual(report["refused"], [{"reason": "in progress", "run_id": OPEN_ID}])
            self.assertTrue((state / f"{OPEN_ID}.json").exists())

    def test_logs_prints_events_and_redacts_token_shaped_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            save_run(state, _record(RUN_ID, status="completed", accepted=True, branch=None))
            (state / f"{RUN_ID}.jsonl").write_text(
                json.dumps(
                    {
                        "detail": f"see {TOKEN} and {BEARER} then approve",
                        "event": "provider.result",
                        "run_id": RUN_ID,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["logs", RUN_ID, "--state", directory])
            self.assertEqual(code, 0)
            text = stdout.getvalue()
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("supersecretvalue", text)
            self.assertIn("[redacted]", text)
            self.assertIn("approve", text)
            events = json.loads(text)["events"]
            self.assertEqual(events[0]["event"], "provider.result")

    def test_a_fixture_run_writes_events_without_a_provider_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = run_task(
                "Say hello",
                config=starter_config({"fixture", "fixture-b"}),
                state_dir=Path(directory),
                allow_live=False,
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["logs", record["run_id"], "--state", directory])
            self.assertEqual(code, 0)
            names = [event["event"] for event in json.loads(stdout.getvalue())["events"]]
            self.assertEqual(names[0], "run.started")
            self.assertIn("review.verdict", names)
            self.assertEqual(names[-1], "run.finished")

    def test_acceptance_saves_the_reviewed_commit(self) -> None:
        tree = _Tree()
        self.addCleanup(tree.close)
        _git(tree.repo, "checkout", "-b", "feature")
        (tree.repo / "extra.txt").write_text("extra\n", encoding="utf-8")
        _git(tree.repo, "add", "extra.txt")
        _git(tree.repo, "commit", "-m", "feature")
        sha = _git(tree.repo, "rev-parse", "HEAD").stdout.strip()
        record = run_task(
            "Say hello",
            config=starter_config({"fixture", "fixture-b"}),
            state_dir=tree.state,
            run_branch="feature",
            repo=tree.repo,
        )
        self.assertTrue(record["accepted"])
        self.assertEqual(record["reviewed_commit"], sha)
        self.assertEqual(load_run(tree.state, record["run_id"])["reviewed_commit"], sha)

        missing = run_task(
            "Say hello",
            config=starter_config({"fixture", "fixture-b"}),
            state_dir=tree.state,
            run_branch="does-not-exist",
            repo=tree.repo,
        )
        self.assertTrue(missing["accepted"])
        self.assertNotIn("reviewed_commit", missing)


class AdapterTests(unittest.TestCase):
    def test_launch_critical_names_are_implemented_or_deferred(self) -> None:
        self.assertEqual(
            set(LAUNCH_CRITICAL),
            {"ChatGPT", "Claude", "Cursor", "Gemini", "Grok", "Hermes", "OpenCode"},
        )
        known = adapters()
        for name, row in LAUNCH_CRITICAL.items():
            if "adapter" in row:
                adapter = known[row["adapter"]]
                self.assertFalse(adapter.OFFLINE)
                self.assertTrue(adapter.STATIC_ARGV)
            else:
                self.assertTrue(row["deferred"].strip())
                self.assertNotIn(name.lower(), known)
        self.assertEqual(LAUNCH_CRITICAL["Hermes"]["deferred"], "TUI gateway JSON-RPC")
        self.assertIn("app-server", LAUNCH_CRITICAL["ChatGPT"]["deferred"])
        self.assertIn("stdio", LAUNCH_CRITICAL["ChatGPT"]["deferred"])
        with self.assertRaises(HarnessError):
            open_provider("hermes")
        with self.assertRaises(HarnessError):
            open_provider("chatgpt")
        enabled = {item["name"] for item in load_config()["providers"] if item["enabled"]}
        self.assertEqual(enabled, {"fixture", "fixture-b"})
        for name in ("gemini", "cursor", "opencode"):
            self.assertIn(name, known)

    def test_new_adapters_start_only_with_allow_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = ProviderRequest(task="Say hello", packet_id=RUN_ID, cwd=directory)

            def refuse(argv, stdin_text, timeout, cwd):
                raise AssertionError((argv, stdin_text, timeout, cwd))

            for cls, flag in (
                (GeminiHeadlessProvider, "--approval-mode"),
                (CursorHeadlessProvider, "--trust"),
                (OpenCodeHeadlessProvider, "--auto"),
            ):
                with self.assertRaises(LiveProviderDisabled):
                    cls().execute(request, allow_live=False, runner=refuse)
                seen: dict[str, object] = {}

                def runner(argv, stdin_text, timeout, cwd, seen=seen):
                    del stdin_text, timeout
                    seen["argv"] = tuple(argv)
                    seen["cwd"] = cwd
                    return _completed(argv, 0, stdout="ok")

                cls().execute(request, allow_live=True, runner=runner)
                argv = seen["argv"]
                self.assertIsInstance(argv, tuple)
                self.assertIn(flag, argv)
                self.assertEqual(seen["cwd"], directory)
                self.assertEqual(argv[0], cls.STATIC_ARGV[0])

            gemini = GeminiHeadlessProvider().argv(request)
            self.assertNotIn("Say hello", gemini)
            self.assertEqual(gemini[gemini.index("-p") + 1], " ")
            self.assertIn("yolo", gemini)
            self.assertIn("--skip-trust", gemini)
            captured: dict[str, object] = {}

            def capture(argv, stdin_text, timeout, cwd):
                del timeout, cwd
                captured["argv"] = tuple(argv)
                captured["stdin"] = stdin_text
                return _completed(argv, 0, stdout="ok")

            GeminiHeadlessProvider().execute(request, allow_live=True, runner=capture)
            self.assertEqual(captured["stdin"], "Say hello")
            self.assertNotIn("Say hello", captured["argv"])
            long_request = ProviderRequest(task="y" * 7001, packet_id=RUN_ID, cwd=directory)
            GeminiHeadlessProvider().execute(long_request, allow_live=True, runner=capture)
            self.assertEqual(captured["stdin"], "y" * 7001)
            self.assertNotIn("y" * 7001, captured["argv"])
            cursor = CursorHeadlessProvider().argv(request)
            self.assertEqual(cursor[0], "agent")
            self.assertIn("--force", cursor)
            self.assertIn("--workspace", cursor)
            self.assertEqual(cursor[-2:], ("--", "Say hello"))
            opencode = OpenCodeHeadlessProvider().argv(request)
            self.assertEqual(opencode[:5], ("opencode", "run", "--format", "json", "--auto"))
            self.assertIn("--dir", opencode)
            self.assertNotIn("-p", opencode)

    def test_a_long_opencode_prompt_is_a_private_file_and_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = ProviderRequest(task="x" * 7001, packet_id=RUN_ID, cwd=directory)
            seen: dict[str, object] = {}

            def runner(argv, stdin_text, timeout, cwd):
                del stdin_text, timeout, cwd
                seen["argv"] = tuple(argv)
                prompt = Path(argv[argv.index("--file") + 1])
                seen["exists"] = prompt.is_file()
                seen["mode"] = prompt.stat().st_mode & 0o777
                return _completed(argv, 0, stdout="ok")

            OpenCodeHeadlessProvider().execute(request, allow_live=True, runner=runner)
            argv = seen["argv"]
            self.assertIsInstance(argv, tuple)
            prompt = Path(argv[argv.index("--file") + 1])
            self.assertTrue(seen["exists"])
            self.assertFalse(prompt.exists())
            self.assertNotIn("x" * 7001, argv)
            if sys.platform != "win32":
                self.assertEqual(seen["mode"], 0o600)

    def test_unknown_sign_in_is_reported_and_does_not_block_unprobed_adapters(self) -> None:
        commands = {"gemini": "gemini", "cursor": "agent", "opencode": "opencode"}
        for name, command in commands.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    bindir = Path(directory) / "bin"
                    home = Path(directory) / "home"
                    bindir.mkdir()
                    home.mkdir()
                    (bindir / command).write_text("", encoding="utf-8")

                    def run(argv, timeout, command=command):
                        del timeout
                        if argv[1:] == ["--version"]:
                            return _completed(argv, 0, stdout=f"{command} 1.0\n")
                        raise AssertionError(argv)

                    report = diagnose(
                        starter_config({name}),
                        environ={},
                        platform="linux",
                        home=home,
                        path=str(bindir),
                        run=run,
                        git_run=lambda argv, timeout: _completed(argv, 0, stdout="git version 2.45.0\n"),
                    )
                    row = {item["name"]: item for item in report["providers"]}[name]
                    self.assertEqual(row["signed_in"], "unknown")
                    self.assertIsNone(row["auth_method"])
                    self.assertEqual(row["warning"], "sign-in is unknown")
                    self.assertTrue(row["installed"])
                    self.assertTrue(row["usable"])
                    self.assertTrue(report["ok"])

        def refuse_probe(argv, timeout):
            del timeout
            raise AssertionError(argv)

        missing = diagnose(
            starter_config({"gemini"}),
            environ={},
            platform="linux",
            home=Path("/tmp"),
            path="",
            run=refuse_probe,
            git_run=lambda argv, timeout: _completed(argv, 0, stdout="git version 2.45.0\n"),
        )
        gemini = {item["name"]: item for item in missing["providers"]}["gemini"]
        self.assertEqual(gemini["signed_in"], "unknown")
        self.assertFalse(gemini["installed"])
        self.assertFalse(gemini["usable"])
        self.assertEqual(gemini["warning"], "sign-in is unknown")

        shipped = diagnose(
            load_config(),
            environ={},
            platform="linux",
            home=Path("/tmp"),
            path="",
            run=refuse_probe,
            git_run=lambda argv, timeout: _completed(argv, 0, stdout="git version 2.45.0\n"),
        )
        rows = {item["name"]: item for item in shipped["providers"]}
        for name in ("gemini", "cursor", "opencode"):
            self.assertEqual(rows[name]["signed_in"], "skipped")
            self.assertFalse(rows[name]["usable"])
            self.assertNotIn("warning", rows[name])


if __name__ == "__main__":
    unittest.main()
