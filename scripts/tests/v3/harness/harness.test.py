#!/usr/bin/env python3
"""Provider-free harness tests. No live CLI is started."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v3.harness.cli import main  # noqa: E402
from scripts.v3.harness.config import load_config, starter_config, validate_config, write_starter  # noqa: E402
from scripts.v3.harness.doctor import diagnose, discover_installed, resolve_executable  # noqa: E402
from scripts.v3.harness.errors import HarnessError, LiveProviderDisabled, ProviderTimeout  # noqa: E402
from scripts.v3.harness.providers import (  # noqa: E402
    ClaudeHeadlessProvider,
    CodexExecProvider,
    GrokHeadlessProvider,
    ProviderRequest,
    ProviderResult,
)
from scripts.v3.harness.review import parse_verdict  # noqa: E402
from scripts.v3.harness.routing import capable_providers, select_route  # noqa: E402
from scripts.v3.harness.run import run_task  # noqa: E402
from scripts.v3.harness.state import list_runs, load_run  # noqa: E402
from scripts.v3.harness.usage import (  # noqa: E402
    DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    exceeded,
    parse_usage,
)


SECRET = "SECRET-TOKEN-VALUE"
FIXTURE_DIR = Path(__file__).with_name("fixtures")
_EVIDENCE = {"source-documented", "locally observed", "synthetic"}
_PROVIDERS = {"grok", "codex", "claude"}


def _load_fixture(name: str) -> tuple[dict[str, str], str]:
    """Return the header map and the stream body. Header lines are not events."""

    raw = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    header: dict[str, str] = {}
    body: list[str] = []
    in_header = True
    for line in raw.splitlines():
        if in_header and line.startswith("# "):
            key, separator, value = line[2:].partition(":")
            if separator:
                header[key.strip()] = value.strip()
            continue
        if in_header and not line.strip():
            continue
        in_header = False
        body.append(line)
    text = "\n".join(body)
    if raw.endswith("\n"):
        text += "\n"
    return header, text


CODEX_SESSION = _load_fixture("codex-cumulative.txt")[1]
GROK_APPROVE = _load_fixture("grok-review.txt")[1]


def _completed(argv, code: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


class _Stub:
    def __init__(self, name: str, text: str) -> None:
        self.name = name
        self.text = text
        self.kinds: list[str] = []

    def execute(self, request, *, allow_live: bool = False, timeout_seconds: int | None = None, runner=None):
        del allow_live, timeout_seconds, runner
        self.kinds.append(request.kind)
        return ProviderResult(self.name, ("stub",), self.text, False)


class HarnessTests(unittest.TestCase):
    def test_codex_jsonl_agent_message_is_the_assistant_text(self) -> None:
        from scripts.v3.harness.streams import assistant_text

        raw = (FIXTURE_DIR / "codex-exec-jsonl.txt").read_text(encoding="utf-8")
        self.assertEqual(
            assistant_text(raw),
            "Repo contains docs, sdk, and examples directories.",
        )

    def test_shipped_config_enables_fixtures_and_raises_the_codex_ceiling(self) -> None:
        config = load_config()
        enabled = {item["name"] for item in config["providers"] if item["enabled"]}
        self.assertEqual(enabled, {"fixture", "fixture-b"})
        self.assertEqual(config["budgets"]["max_input_tokens"], DEFAULT_MAX_INPUT_TOKENS)
        self.assertEqual(config["budgets"]["max_output_tokens"], DEFAULT_MAX_OUTPUT_TOKENS)
        self.assertGreater(DEFAULT_MAX_INPUT_TOKENS, 6_900_000)
        self.assertNotEqual(DEFAULT_MAX_INPUT_TOKENS, 1_000_000)
        self.assertEqual(select_route(config), ("fixture", ["fixture-b"]))

    def test_config_rejects_credential_fields_without_echoing_them(self) -> None:
        payload = starter_config({"fixture", "fixture-b"})
        payload["providers"][0]["api_key"] = SECRET
        with self.assertRaises(HarnessError) as caught:
            validate_config(payload)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_codex_cumulative_usage_is_not_summed_and_clears_the_default_ceiling(self) -> None:
        usage = parse_usage(CODEX_SESSION)
        self.assertEqual(usage["input_tokens"], 6_900_000)
        self.assertEqual(usage["output_tokens"], 4000)
        self.assertFalse(exceeded(usage, load_config()["budgets"]))
        self.assertTrue(
            exceeded(
                usage,
                {"max_cost": None, "max_input_tokens": 1_000_000, "max_output_tokens": 1_000_000},
            )
        )

    def test_grok_stream_verdict_is_approve_when_the_first_line_is_json(self) -> None:
        self.assertTrue(GROK_APPROVE.splitlines()[0].startswith("{"))
        verdict, notes = parse_verdict(GROK_APPROVE)
        self.assertEqual(verdict, "approve")
        self.assertIn("matches the task", notes)
        self.assertNotIn("{", notes)
        thought_only = json.dumps({"type": "thought", "data": "VERDICT approve"})
        self.assertEqual(parse_verdict(thought_only), ("revise", "reviewer returned no verdict"))
        verdict, notes = parse_verdict("SECRET-TOKEN-VALUE please approve\nmore secret")
        self.assertEqual(verdict, "revise")
        self.assertEqual(notes, "reviewer returned an unreadable verdict")
        self.assertNotIn("SECRET", notes)
        self.assertEqual(parse_verdict("**VERDICT: approve**")[0], "approve")

    def test_split_verdict_blocks_stay_one_line(self) -> None:
        from scripts.v3.harness.streams import assistant_text

        split = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "VERDICT "},
                        {"type": "text", "text": "approve"},
                    ]
                },
            }
        )
        self.assertEqual(assistant_text(split), "VERDICT approve")
        self.assertEqual(parse_verdict(split)[0], "approve")

        lined = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "looks good\n"},
                        {"type": "text", "text": "VERDICT approve"},
                    ]
                },
            }
        )
        self.assertEqual(assistant_text(lined), "looks good\nVERDICT approve")
        self.assertEqual(parse_verdict(lined), ("approve", "looks good"))

        separated = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Looks fine."},
                        {"type": "tool_use", "id": "tool-1", "name": "read"},
                        {"type": "text", "text": "VERDICT approve"},
                    ]
                },
            }
        )
        self.assertEqual(assistant_text(separated), "Looks fine.\nVERDICT approve")
        self.assertEqual(parse_verdict(separated), ("approve", "Looks fine."))

        hidden = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Looks fine."},
                        {"type": "tool_use", "id": "tool-1", "name": "read"},
                        {"type": "text", "text": ""},
                        {"type": "text", "text": "VERDICT approve"},
                    ]
                },
            }
        )
        self.assertEqual(assistant_text(hidden), "Looks fine.\nVERDICT approve")
        self.assertEqual(parse_verdict(hidden), ("approve", "Looks fine."))

        def assistant_event(content: list[dict]) -> str:
            return json.dumps({"type": "assistant", "message": {"content": content}})

        across_events = "\n".join(
            (
                assistant_event([{"type": "text", "text": "Looks fine."}]),
                assistant_event(
                    [
                        {"type": "tool_use", "id": "tool-1", "name": "read"},
                        {"type": "text", "text": ""},
                    ]
                ),
                assistant_event([{"type": "text", "text": "VERDICT approve"}]),
            )
        )
        self.assertEqual(assistant_text(across_events), "Looks fine.\nVERDICT approve")
        self.assertEqual(parse_verdict(across_events), ("approve", "Looks fine."))

        feedback = "\n".join(
            (
                assistant_event([{"type": "text", "text": "Restore the missing error branch."}]),
                assistant_event([{"type": "text", "text": "VERDICT revise"}]),
            )
        )
        self.assertEqual(parse_verdict(feedback), ("revise", "Restore the missing error branch."))

        crowded = "\n".join(
            (
                assistant_event([{"type": "text", "text": "x" * 1100}]),
                assistant_event(
                    [{"type": "text", "text": "Change the error branch.\nVERDICT revise"}]
                ),
            )
        )
        verdict, notes = parse_verdict(crowded)
        self.assertEqual(verdict, "revise")
        self.assertTrue(notes.startswith("Change the error branch."))

        stale = "\n".join(
            (
                assistant_event([{"type": "text", "text": "VERDICT approve"}]),
                assistant_event([{"type": "text", "text": "Need another pass."}]),
            )
        )
        self.assertEqual(parse_verdict(stale)[0], "revise")

        changing = assistant_event(
            [
                {
                    "type": "text",
                    "text": "Fix X.\nVERDICT approve\nActually no.\nVERDICT revise",
                }
            ]
        )
        verdict, notes = parse_verdict(changing)
        self.assertEqual(verdict, "revise")
        self.assertEqual(notes, "Fix X.\nActually no.")
        self.assertNotIn("VERDICT", notes)

        content_before_result = "\n".join(
            (
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "VERDICT approve"}]},
                    }
                ),
                json.dumps({"type": "result", "result": "session finished without a verdict line"}),
            )
        )
        self.assertEqual(assistant_text(content_before_result), "VERDICT approve")
        self.assertEqual(parse_verdict(content_before_result)[0], "approve")

    def test_fixture_run_reviews_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["run", "--task", "Say hello", "--state", directory])
            self.assertEqual(code, 0)
            record = json.loads(stdout.getvalue())
            self.assertEqual(record["provider"], "fixture")
            self.assertEqual(record["status"], "completed")
            self.assertTrue(record["accepted"])
            self.assertEqual(record["reviews"][0]["provider"], "fixture-b")
            self.assertEqual(record["reviews"][0]["verdict"], "approve")
            self.assertIn("Say hello", record["text"])
            again = io.StringIO()
            with redirect_stdout(again):
                status_code = main(["status", "--run", record["run_id"], "--state", directory])
            self.assertEqual(status_code, 0)
            self.assertEqual(json.loads(again.getvalue())["run_id"], record["run_id"])

    def test_fixture_revise_once_then_approves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["run", "--task", "fixture: revise once", "--state", directory])
            self.assertEqual(code, 0)
            record = json.loads(stdout.getvalue())
            self.assertEqual([item["verdict"] for item in record["reviews"]], ["revise", "approve"])

    def test_pinned_live_provider_does_not_start_without_allow_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "harness.json"
            write_starter(config_path, {"fixture", "fixture-b", "claude"})
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [
                        "run",
                        "--task",
                        "Say hello",
                        "--provider",
                        "claude",
                        "--config",
                        str(config_path),
                        "--state",
                        str(root / "state"),
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("--allow-live", stderr.getvalue())
            self.assertNotIn(SECRET, stderr.getvalue())

    def test_grok_reviewer_stub_approves_and_old_ceiling_stops_codex(self) -> None:
        config = starter_config({"codex", "grok"})
        codex = _Stub("codex", CODEX_SESSION)
        grok = _Stub("grok", GROK_APPROVE)

        def open_adapter(name: str):
            return {"codex": codex, "grok": grok}[name]

        with tempfile.TemporaryDirectory() as directory:
            finished = run_task(
                "Say hello",
                config=config,
                state_dir=Path(directory),
                open_adapter=open_adapter,
            )
            self.assertEqual(finished["status"], "completed")
            self.assertEqual(finished["provider"], "codex")
            self.assertEqual(finished["reviews"][0]["verdict"], "approve")
            self.assertEqual(finished["reviews"][0]["provider"], "grok")
            self.assertEqual(finished["usage"]["input_tokens"], 6_900_012)
            self.assertEqual(codex.kinds, ["execute"])
            self.assertEqual(grok.kinds, ["review"])

            low = starter_config({"codex"})
            low["review"]["max_rounds"] = 0
            low["budgets"]["max_input_tokens"] = 1_000_000
            stopped = run_task(
                "Say hello",
                config=low,
                state_dir=Path(directory),
                provider="codex",
                open_adapter=lambda name: _Stub("codex", CODEX_SESSION),
            )
            self.assertEqual(stopped["status"], "budget-exceeded")
            self.assertFalse(stopped["accepted"])

    def test_grok_adapter_writes_the_prompt_and_returns_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = ProviderRequest(
                task="Say hello",
                packet_id="run-aabbccddeeff",
                kind="review",
                cwd=directory,
            )
            predictable = Path(tempfile.gettempdir()) / "recursion-grok-run-aabbccddeeff.txt"
            predictable.write_text("sentinel", encoding="utf-8")
            seen: dict[str, Path] = {}

            def runner(argv, stdin_text, timeout, cwd):
                del stdin_text, timeout
                self.assertEqual(cwd, directory)
                self.assertEqual(argv[argv.index("--cwd") + 1], directory)
                prompt = Path(argv[argv.index("--prompt-file") + 1])
                self.assertNotEqual(prompt, predictable)
                self.assertTrue(prompt.name.startswith("recursion-grok-"))
                self.assertEqual(prompt.read_text(encoding="utf-8"), "Say hello")
                self.assertFalse(prompt.is_symlink())
                if os.name == "posix":
                    self.assertEqual(prompt.stat().st_mode & 0o777, 0o600)
                seen["prompt"] = prompt
                return _completed(argv, 0, stdout=GROK_APPROVE)

            try:
                result = GrokHeadlessProvider().execute(request, allow_live=True, runner=runner)
                self.assertIn("prompt", seen)
                self.assertFalse(seen["prompt"].exists())
                self.assertEqual(predictable.read_text(encoding="utf-8"), "sentinel")
                self.assertEqual(parse_verdict(result.text)[0], "approve")
                self.assertTrue(result.live)
            finally:
                predictable.unlink(missing_ok=True)

        def missing(argv, stdin_text, timeout, cwd):
            del stdin_text, timeout, cwd
            prompt = Path(argv[argv.index("--prompt-file") + 1])
            self.assertTrue(prompt.is_file())
            raise FileNotFoundError("missing")

        with tempfile.TemporaryDirectory() as directory:
            bounded = ProviderRequest(
                task="Say hello",
                packet_id="run-aabbccddeeff",
                kind="review",
                cwd=directory,
            )
            with self.assertRaises(HarnessError):
                GrokHeadlessProvider().execute(bounded, allow_live=True, runner=missing)
        bare = ProviderRequest(task="Say hello", packet_id="run-aabbccddeeff", kind="review")

        def refuse(argv, stdin_text, timeout, cwd):
            raise AssertionError((argv, stdin_text, timeout, cwd))

        with self.assertRaises(HarnessError) as caught:
            GrokHeadlessProvider().execute(bare, allow_live=True, runner=refuse)
        self.assertIn("working folder", str(caught.exception))
        with self.assertRaises(LiveProviderDisabled):
            ClaudeHeadlessProvider().execute(bare, allow_live=False)

    def test_claude_logged_in_json_wins_over_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bindir = Path(directory) / "bin"
            home = Path(directory) / "home"
            bindir.mkdir()
            home.mkdir()
            (bindir / "claude").write_text("", encoding="utf-8")
            header, body = _load_fixture("claude-auth-status-redaction.txt")
            self.assertIn(SECRET, body)

            def run(argv, timeout):
                del timeout
                if argv[1:] == ["--version"]:
                    return _completed(argv, 0, stdout=f"claude 2.1.0 {SECRET}\n")
                if argv[1:] == ["auth", "status"]:
                    return _completed(
                        argv,
                        int(header["exit-code"]),
                        stdout=body,
                        stderr=f"refresh notice {SECRET}\n",
                    )
                raise AssertionError(argv)

            report = diagnose(
                starter_config({"claude"}),
                environ={"ANTHROPIC_API_KEY": SECRET},
                platform="linux",
                home=home,
                path=str(bindir),
                run=run,
                git_run=lambda argv, timeout: _completed(argv, 0, stdout="git version 2.45.0\n"),
            )
            rendered = json.dumps(report)
            self.assertNotIn(SECRET, rendered)
            claude = {item["name"]: item for item in report["providers"]}["claude"]
            self.assertEqual(claude["signed_in"], "signed-in")
            self.assertEqual(claude["auth_method"], "claude.ai")
            self.assertTrue(claude["usable"])
            self.assertTrue(report["ok"])
            self.assertIn("[redacted]", claude["version"])

            def signed_out(argv, timeout):
                del timeout
                if argv[1:] == ["--version"]:
                    return _completed(argv, 0, stdout="claude 2.1.0\n")
                if argv[1:] == ["auth", "status"]:
                    return _completed(
                        argv,
                        0,
                        stdout=json.dumps({"authMethod": "none", "loggedIn": False}) + "\n",
                    )
                raise AssertionError(argv)

            out = diagnose(
                starter_config({"claude"}),
                environ={},
                platform="linux",
                home=home,
                path=str(bindir),
                run=signed_out,
                git_run=lambda argv, timeout: _completed(argv, 0, stdout="git version 2.45.0\n"),
            )
            row = {item["name"]: item for item in out["providers"]}["claude"]
            self.assertEqual(row["signed_in"], "signed-out")
            self.assertTrue(row["installed"])
            self.assertFalse(row["usable"])
            self.assertFalse(out["ok"])

            def stderr_only(argv, timeout):
                del timeout
                if argv[1:] == ["--version"]:
                    return _completed(argv, 0, stdout="claude 2.1.0\n")
                if argv[1:] == ["auth", "status"]:
                    return _completed(
                        argv,
                        1,
                        stdout="warning before status\n",
                        stderr=json.dumps({"authMethod": "claude.ai", "loggedIn": True}) + "\n",
                    )
                raise AssertionError(argv)

            via_stderr = diagnose(
                starter_config({"claude"}),
                environ={},
                platform="linux",
                home=home,
                path=str(bindir),
                run=stderr_only,
                git_run=lambda argv, timeout: _completed(argv, 0, stdout="git version 2.45.0\n"),
            )
            stderr_row = {item["name"]: item for item in via_stderr["providers"]}["claude"]
            self.assertEqual(stderr_row["signed_in"], "signed-in")
            self.assertEqual(stderr_row["auth_method"], "claude.ai")

    def test_disabled_providers_are_not_probed_and_init_writes_the_starter(self) -> None:
        def boom(argv, timeout):
            del timeout
            raise AssertionError(argv)

        report = diagnose(
            load_config(),
            environ={},
            platform="linux",
            home=Path("/tmp"),
            path="",
            run=boom,
            git_run=lambda argv, timeout: _completed(argv, 0, stdout="git version 2.45.0\n"),
        )
        self.assertTrue(report["ok"])
        rows = {item["name"]: item for item in report["providers"]}
        self.assertEqual(rows["claude"]["signed_in"], "skipped")
        self.assertEqual(rows["fixture"]["signed_in"], "not-applicable")

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "harness.json"
            bindir = Path(directory) / "bin"
            bindir.mkdir()
            claude = bindir / "claude"
            claude.write_text("#!/bin/sh\n", encoding="utf-8")
            claude.chmod(0o755)
            with patch.dict(os.environ, {"PATH": str(bindir)}):
                self.assertIn("claude", discover_installed())
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = main(["init", str(target)])
            self.assertEqual(code, 0)
            written = json.loads(target.read_text(encoding="utf-8"))
            enabled = {item["name"] for item in written["providers"] if item["enabled"]}
            self.assertEqual(enabled, {"fixture", "fixture-b"})
            self.assertEqual(written["budgets"]["max_input_tokens"], DEFAULT_MAX_INPUT_TOKENS)
            emitted = json.loads(stdout.getvalue())
            self.assertEqual(set(emitted["enabled"]), {"fixture", "fixture-b"})
            self.assertEqual(Path(emitted["path"]), target)

    def test_windows_shim_resolves_and_posix_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "claude.CMD").write_text("", encoding="utf-8")
            found = resolve_executable("claude", path=str(root), platform="win32")
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "claude.CMD")
            self.assertIsNone(resolve_executable("claude", path=str(root), platform="linux"))
            (root / "claude").write_text("", encoding="utf-8")
            posix = resolve_executable("claude", path=str(root), platform="linux")
            self.assertIsNotNone(posix)
            self.assertEqual(posix.name, "claude")

    @unittest.skipUnless(sys.platform == "win32", "runs on the ci:full Windows job")
    def test_doctor_windows_path_shim_does_not_start_a_provider_cli(self) -> None:
        """Resolve a Windows shim through doctor. The stub probe never execs it."""

        raw = (FIXTURE_DIR / "claude-auth-status-exit-1.txt").read_text(encoding="utf-8")
        header, _body = _load_fixture("claude-auth-status-exit-1.txt")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            shim = root / "claude.cmd"
            shim.write_bytes(b"@echo off\r\nexit /b 1\r\n")

            def run(argv, timeout):
                del timeout
                self.assertEqual(Path(argv[0]).name.casefold(), "claude.cmd")
                if argv[1:] == ["--version"]:
                    return _completed(argv, 0, stdout="claude 2.1.300\n")
                if argv[1:] == ["auth", "status"]:
                    return _completed(argv, int(header["exit-code"]), stdout=raw)
                raise AssertionError(argv)

            real_run = subprocess.run

            def guarded_run(argv, *args, **kwargs):
                text = " ".join(str(part) for part in argv)
                if any(name in text.casefold() for name in ("claude", "codex", "grok")):
                    raise AssertionError(text)
                return real_run(argv, *args, **kwargs)

            with patch("subprocess.run", guarded_run):
                report = diagnose(
                    starter_config({"claude"}),
                    environ={},
                    home=home,
                    path=str(root),
                    run=run,
                    git_run=lambda argv, timeout: _completed(argv, 0, stdout="git version 2.45.0\n"),
                )
            row = {item["name"]: item for item in report["providers"]}["claude"]
            self.assertTrue(row["installed"])
            self.assertTrue(str(row["resolved"]).casefold().endswith("claude.cmd"))
            self.assertEqual(row["signed_in"], "signed-in")
            self.assertEqual(row["auth_method"], "claude.ai")
            self.assertEqual(shim.read_bytes(), b"@echo off\r\nexit /b 1\r\n")

    def test_checked_in_stream_fixtures_match_the_parsers(self) -> None:
        from scripts.v3.harness.streams import assistant_text, claude_last_message

        names = sorted(path.name for path in FIXTURE_DIR.glob("*.txt"))
        self.assertEqual(
            names,
            [
                "claude-auth-status-exit-1.txt",
                "claude-auth-status-redaction.txt",
                "claude-auth-status.txt",
                "claude-stream-json.txt",
                "codex-cumulative.txt",
                "codex-exec-jsonl.txt",
                "grok-review.txt",
                "grok-streaming-json.txt",
            ],
        )
        for name in names:
            header, body = _load_fixture(name)
            self.assertIn(header["provider"], _PROVIDERS)
            self.assertIn(header["evidence"], _EVIDENCE)
            self.assertTrue(header["cli-version"])
            self.assertTrue(body.strip())
            if header["evidence"] == "synthetic":
                self.assertIn("synthetic", header["note"].casefold())

        grok_review = (FIXTURE_DIR / "grok-review.txt").read_text(encoding="utf-8")
        verdict, notes = parse_verdict(grok_review)
        self.assertEqual(verdict, "approve")
        self.assertIn("matches the task", notes)
        self.assertEqual(parse_usage(grok_review)["input_tokens"], 12)

        grok_doc = (FIXTURE_DIR / "grok-streaming-json.txt").read_text(encoding="utf-8")
        self.assertEqual(parse_verdict(grok_doc)[0], "revise")
        self.assertEqual(assistant_text(grok_doc), "Here's a summary")
        documented = parse_usage(grok_doc)
        self.assertEqual(documented["input_tokens"], 812)
        self.assertEqual(documented["output_tokens"], 45)

        codex_doc = (FIXTURE_DIR / "codex-exec-jsonl.txt").read_text(encoding="utf-8")
        published = parse_usage(codex_doc)
        self.assertEqual(published["input_tokens"], 24763)
        self.assertEqual(published["output_tokens"], 122)
        self.assertEqual(
            assistant_text(codex_doc),
            "Repo contains docs, sdk, and examples directories.",
        )
        cumulative = parse_usage((FIXTURE_DIR / "codex-cumulative.txt").read_text(encoding="utf-8"))
        self.assertEqual(cumulative["input_tokens"], 6_900_000)
        self.assertEqual(cumulative["output_tokens"], 4000)

        claude_stream = (FIXTURE_DIR / "claude-stream-json.txt").read_text(encoding="utf-8")
        claude_events = [
            json.loads(line) for line in claude_stream.splitlines() if line.startswith("{")
        ]
        claude_content = claude_events[0]["message"]["content"][0]["text"]
        claude_result = claude_events[1]["result"]
        self.assertNotEqual(claude_content.strip(), claude_result.strip())
        self.assertNotIn("VERDICT", claude_result)
        self.assertEqual(assistant_text(claude_stream), claude_content)
        self.assertEqual(parse_verdict(claude_stream)[0], "approve")
        two_messages = "\n".join(
            json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
            )
            for text in ("Review complete.", "VERDICT approve\n")
        )
        self.assertEqual(parse_verdict(two_messages)[0], "approve")
        feedback_first = "\n".join(
            json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
            )
            for text in ("Restore the missing error branch.", "VERDICT revise\n")
        )
        self.assertEqual(
            parse_verdict(feedback_first),
            ("revise", "Restore the missing error branch."),
        )
        crowded = "\n".join(
            json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
            )
            for text in ("x" * 1100, "Change the error branch.\nVERDICT revise")
        )
        verdict, notes = parse_verdict(crowded)
        self.assertEqual(verdict, "revise")
        self.assertTrue(notes.startswith("Change the error branch."))
        flipped = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "Fix X.\nVERDICT approve\nActually no.\nVERDICT revise",
                        }
                    ]
                },
            }
        )
        flipped_verdict, flipped_notes = parse_verdict(flipped)
        self.assertEqual(flipped_verdict, "revise")
        self.assertEqual(flipped_notes, "Fix X.\nActually no.")
        self.assertNotIn("VERDICT", flipped_notes)
        stale_verdict = "\n".join(
            json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
            )
            for text in ("VERDICT approve\n", "Need another pass.\n")
        )
        self.assertIn("VERDICT approve", stale_verdict)
        self.assertEqual(claude_last_message(stale_verdict), "Need another pass.\n")
        self.assertEqual(parse_verdict(stale_verdict)[0], "revise")
        trailing_empty = "\n".join(
            (
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "VERDICT approve\n"}]},
                    }
                ),
                json.dumps(
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": ""}]}},
                ),
            )
        )
        self.assertEqual(claude_last_message(trailing_empty), "VERDICT approve\n")
        self.assertEqual(parse_verdict(trailing_empty)[0], "approve")
        tool_then_empty = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Review complete."},
                        {"type": "tool_use", "name": "Bash", "input": {}},
                        {"type": "text", "text": ""},
                        {"type": "text", "text": "VERDICT approve\n"},
                    ]
                },
            }
        )
        self.assertEqual(
            assistant_text(tool_then_empty),
            "Review complete.\nVERDICT approve\n",
        )
        self.assertEqual(parse_verdict(tool_then_empty)[0], "approve")
        result_only = json.dumps({"type": "result", "result": "fallback text"})
        self.assertEqual(assistant_text(result_only), "fallback text")
        claude_usage = parse_usage(claude_stream)
        self.assertEqual(claude_usage["input_tokens"], 150)
        self.assertEqual(claude_usage["output_tokens"], 70)
        self.assertEqual(claude_usage["cost"], 0.0123)

        self._doctor_reads_auth_fixture("claude-auth-status.txt")
        self._doctor_reads_auth_fixture("claude-auth-status-exit-1.txt", raw=True)
        self._doctor_reads_auth_fixture("claude-auth-status-redaction.txt")

    def _doctor_reads_auth_fixture(self, name: str, *, raw: bool = False) -> None:
        header, body = _load_fixture(name)
        stdout = (FIXTURE_DIR / name).read_text(encoding="utf-8") if raw else body
        with tempfile.TemporaryDirectory() as directory:
            bindir = Path(directory) / "bin"
            home = Path(directory) / "home"
            bindir.mkdir()
            home.mkdir()
            (bindir / "claude").write_text("", encoding="utf-8")

            def run(argv, timeout):
                del timeout
                if argv[1:] == ["--version"]:
                    return _completed(argv, 0, stdout="claude 2.1.300\n")
                if argv[1:] == ["auth", "status"]:
                    return _completed(argv, int(header["exit-code"]), stdout=stdout)
                raise AssertionError(argv)

            report = diagnose(
                starter_config({"claude"}),
                environ={},
                platform="linux",
                home=home,
                path=str(bindir),
                run=run,
                git_run=lambda argv, timeout: _completed(argv, 0, stdout="git version 2.45.0\n"),
            )
        row = {item["name"]: item for item in report["providers"]}["claude"]
        self.assertEqual(row["signed_in"], "signed-in")
        self.assertEqual(row["auth_method"], "claude.ai")
        self.assertTrue(row["usable"])

    def test_capability_route_skips_disabled_providers(self) -> None:
        config = starter_config({"fixture", "claude", "codex", "grok"})
        config["routing"]["required_capabilities"] = ["execute", "streaming-json"]
        self.assertEqual(capable_providers(config), ["grok"])
        self.assertEqual(select_route(config), ("grok", []))

    def test_run_id_cannot_escape_the_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HarnessError):
                load_run(Path(directory), "../secret")

    def test_help_lists_the_slice_commands(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as caught:
                main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        text = stdout.getvalue()
        for name in ("doctor", "init", "run", "status", "logs", "land", "clean"):
            self.assertIn(name, text)

    def test_doctor_cli_on_the_shipped_config(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["doctor"])
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertTrue(report["ok"])
        rows = {item["name"]: item for item in report["providers"]}
        self.assertEqual(rows["fixture"]["signed_in"], "not-applicable")
        self.assertEqual(rows["claude"]["signed_in"], "skipped")

    def test_live_cli_requires_a_working_folder(self) -> None:
        request = ProviderRequest(task="Say hello", packet_id="run-aabbccddeeff")

        def runner(argv, stdin_text, timeout, cwd):
            raise AssertionError((argv, stdin_text, timeout, cwd))

        with self.assertRaises(HarnessError) as caught:
            CodexExecProvider().execute(request, allow_live=True, runner=runner)
        self.assertIn("working folder", str(caught.exception))

        with tempfile.TemporaryDirectory() as directory:
            seen: dict[str, object] = {}

            def bounded_runner(argv, stdin_text, timeout, cwd):
                del stdin_text, timeout
                seen["argv"] = argv
                seen["cwd"] = cwd
                return _completed(argv, 0, stdout="ok")

            bounded = ProviderRequest(task="Say hello", packet_id="run-aabbccddeeff", cwd=directory)
            CodexExecProvider().execute(bounded, allow_live=True, runner=bounded_runner)
            self.assertEqual(seen["cwd"], directory)
            self.assertIn(directory, seen["argv"])

    def test_live_run_defaults_to_a_private_workspace(self) -> None:
        seen: dict[str, object] = {}

        class Watch(_Stub):
            def execute(self, request, *, allow_live=False, timeout_seconds=None, runner=None):
                seen["cwd"] = request.cwd
                seen["allow_live"] = allow_live
                return super().execute(
                    request,
                    allow_live=allow_live,
                    timeout_seconds=timeout_seconds,
                    runner=runner,
                )

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            config = starter_config({"fixture"})
            config["review"]["max_rounds"] = 0
            record = run_task(
                "Say hello",
                config=config,
                state_dir=state,
                allow_live=True,
                open_adapter=lambda name: Watch("fixture", "hello"),
            )
            cwd = Path(str(seen["cwd"]))
            expected = (state / record["run_id"] / "workspace").resolve()
            self.assertTrue(seen["allow_live"])
            self.assertEqual(cwd.resolve(), expected)
            self.assertEqual(cwd, expected)
            self.assertTrue(cwd.is_dir())
            self.assertEqual(Path(str(record["workspace"])).resolve(), expected)
            self.assertEqual(record["workspace"], str(cwd))
            if os.name == "posix":
                self.assertEqual(cwd.stat().st_mode & 0o777, 0o700)
                self.assertEqual(cwd.parent.stat().st_mode & 0o777, 0o700)

    def test_relative_state_is_an_absolute_folder_for_codex_and_grok(self) -> None:
        seen: dict[str, str] = {}

        class Capture:
            def __init__(self, name: str) -> None:
                self.name = name

            def execute(self, request, *, allow_live=False, timeout_seconds=None, runner=None):
                del allow_live, timeout_seconds, runner
                seen[self.name] = request.cwd
                if request.kind == "review":
                    return ProviderResult(self.name, ("stub",), "VERDICT approve\nok", False)
                return ProviderResult(self.name, ("stub",), "hello", False)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = Path.cwd()
            os.chdir(root)
            try:
                record = run_task(
                    "Say hello",
                    config=starter_config({"codex", "grok"}),
                    state_dir=Path("relative-state"),
                    allow_live=True,
                    open_adapter=lambda name: Capture(name),
                )
            finally:
                os.chdir(previous)
            expected = (root / "relative-state" / record["run_id"] / "workspace").resolve()
            for name in ("codex", "grok"):
                folder = seen[name]
                self.assertTrue(Path(folder).is_absolute())
                self.assertEqual(Path(folder), expected)
            codex_argv = CodexExecProvider().argv(
                ProviderRequest(task="Say hello", packet_id=record["run_id"], cwd=seen["codex"])
            )
            grok_argv = GrokHeadlessProvider().argv(
                ProviderRequest(task="Say hello", packet_id=record["run_id"], cwd=seen["grok"])
            )
            self.assertEqual(codex_argv[codex_argv.index("--cd") + 1], str(expected))
            self.assertEqual(grok_argv[grok_argv.index("--cwd") + 1], str(expected))
            self.assertEqual(record["workspace"], str(expected))

    def test_provider_error_is_saved_for_status(self) -> None:
        class Boom:
            def execute(self, request, *, allow_live=False, timeout_seconds=None, runner=None):
                del request, allow_live, timeout_seconds, runner
                raise HarnessError("fixture exited 1")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(HarnessError):
                run_task(
                    "Say hello",
                    config=starter_config({"fixture"}),
                    state_dir=Path(directory),
                    open_adapter=lambda name: Boom(),
                )
            ids = list_runs(Path(directory))
            self.assertEqual(ids, [load_run(Path(directory), ids[0])["run_id"]])
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["status", "--run", ids[0], "--state", directory])
            self.assertEqual(code, 0)
            record = json.loads(stdout.getvalue())
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["error"], "fixture exited 1")
            self.assertFalse(record["accepted"])
            self.assertEqual(record["reviews"], [])
            self.assertEqual(record["text"], "")

    def test_timeout_keeps_the_work_already_done(self) -> None:
        class Worker:
            def execute(self, request, *, allow_live=False, timeout_seconds=None, runner=None):
                del allow_live, timeout_seconds, runner
                if request.kind == "review":
                    raise ProviderTimeout("grok timed out")
                return ProviderResult(
                    provider="fixture",
                    argv=(),
                    text=(
                        '{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":3}}\n'
                        "hello"
                    ),
                    live=False,
                )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ProviderTimeout):
                run_task(
                    "Say hello",
                    config=starter_config({"fixture", "grok"}),
                    state_dir=Path(directory),
                    open_adapter=lambda name: Worker(),
                )
            ids = list_runs(Path(directory))
            self.assertEqual(len(ids), 1)
            record = load_run(Path(directory), ids[0])
            self.assertEqual(record["status"], "timed-out")
            self.assertEqual(record["error"], "grok timed out")
            self.assertEqual(record["text"], "hello")
            self.assertEqual(record["usage"]["input_tokens"], 12)
            self.assertEqual(record["reviews"], [])
            self.assertFalse(record["accepted"])

    def test_saved_run_scrubs_a_secret_from_the_environment(self) -> None:
        secret = "SECRET-TOKEN-VALUE"

        class Echo(_Stub):
            def execute(self, request, *, allow_live=False, timeout_seconds=None, runner=None):
                del request, allow_live, timeout_seconds, runner
                return ProviderResult(self.name, ("stub",), f"token {secret}", False)

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": secret}):
                record = run_task(
                    "Say hello",
                    config=starter_config({"fixture"}),
                    state_dir=Path(directory),
                    open_adapter=lambda name: Echo("fixture", ""),
                )
            self.assertNotIn(secret, json.dumps(record))
            self.assertIn("[redacted]", record["text"])

    def test_assistant_text_and_provider_errors_are_both_scrubbed(self) -> None:
        secret = "SECRET-TOKEN-VALUE"
        token = "sk-abcdEFGH1234"

        class Pair:
            def __init__(self, name: str) -> None:
                self.name = name

            def execute(self, request, *, allow_live=False, timeout_seconds=None, runner=None):
                del allow_live, timeout_seconds, runner
                if request.kind == "review":
                    return ProviderResult(self.name, ("stub",), f"VERDICT approve\nnote {secret}", False)
                return ProviderResult(self.name, ("stub",), f"answer {token}", False)

        class Boom:
            def execute(self, request, *, allow_live=False, timeout_seconds=None, runner=None):
                del request, allow_live, timeout_seconds, runner
                raise HarnessError(f"codex exited 1 {secret} {token}")

        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": secret}):
                record = run_task(
                    "Say hello",
                    config=starter_config({"fixture", "fixture-b"}),
                    state_dir=Path(directory),
                    open_adapter=lambda name: Pair(name),
                )
            saved = json.dumps(record)
            self.assertNotIn(secret, saved)
            self.assertNotIn(token, saved)
            self.assertIn("[redacted]", record["text"])
            self.assertIn("[redacted]", record["reviews"][0]["notes"])

            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": secret}):
                with self.assertRaises(HarnessError):
                    run_task(
                        "Say hello",
                        config=starter_config({"fixture"}),
                        state_dir=Path(directory) / "failed",
                        open_adapter=lambda name: Boom(),
                    )
            failed = load_run(Path(directory) / "failed", list_runs(Path(directory) / "failed")[0])
            rendered = json.dumps(failed)
            self.assertNotIn(secret, rendered)
            self.assertNotIn(token, rendered)
            self.assertIn("[redacted]", failed["error"])
            self.assertEqual(failed["text"], "")

    def test_grok_prompt_write_finishes_short_counts_and_fsyncs(self) -> None:
        import scripts.v3.harness.providers as providers

        task = "Say hello in full"
        real_write = providers.os.write
        real_fsync = providers.os.fsync
        seen = {"fsync": 0}

        def short_write(fd, data):
            chunk = bytes(data[:1])
            return real_write(fd, chunk)

        def counting_fsync(fd):
            seen["fsync"] += 1
            return real_fsync(fd)

        with tempfile.TemporaryDirectory() as directory:
            request = ProviderRequest(task=task, packet_id="run-aabbccddeeff", cwd=directory)

            def runner(argv, stdin_text, timeout, cwd):
                del stdin_text, timeout, cwd
                prompt = Path(argv[argv.index("--prompt-file") + 1])
                self.assertEqual(prompt.read_text(encoding="utf-8"), task)
                return _completed(argv, 0, stdout="ok")

            with patch.object(providers.os, "write", short_write), patch.object(providers.os, "fsync", counting_fsync):
                GrokHeadlessProvider().execute(request, allow_live=True, runner=runner)
            self.assertEqual(seen["fsync"], 1)

    def test_grok_env_key_counts_as_signed_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bindir = Path(directory) / "bin"
            home = Path(directory) / "home"
            bindir.mkdir()
            home.mkdir()
            (bindir / "grok").write_text("", encoding="utf-8")
            report = diagnose(
                starter_config({"grok"}),
                environ={"XAI_API_KEY": SECRET},
                platform="linux",
                home=home,
                path=str(bindir),
                run=lambda argv, timeout: _completed(argv, 0, stdout="grok 1.0\n"),
                git_run=lambda argv, timeout: _completed(argv, 0, stdout="git version 2.45.0\n"),
            )
            rendered = json.dumps(report)
            self.assertNotIn(SECRET, rendered)
            row = {item["name"]: item for item in report["providers"]}["grok"]
            self.assertEqual(row["signed_in"], "signed-in")
            self.assertEqual(row["auth_method"], "env")
            self.assertTrue(row["usable"])
