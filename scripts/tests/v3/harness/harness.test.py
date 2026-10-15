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
from scripts.v3.harness.doctor import diagnose, resolve_executable  # noqa: E402
from scripts.v3.harness.errors import HarnessError, LiveProviderDisabled  # noqa: E402
from scripts.v3.harness.providers import (  # noqa: E402
    ClaudeHeadlessProvider,
    GrokHeadlessProvider,
    ProviderRequest,
    ProviderResult,
    grok_prompt_path,
)
from scripts.v3.harness.review import parse_verdict  # noqa: E402
from scripts.v3.harness.routing import capable_providers, select_route  # noqa: E402
from scripts.v3.harness.run import run_task  # noqa: E402
from scripts.v3.harness.state import load_run  # noqa: E402
from scripts.v3.harness.usage import (  # noqa: E402
    DEFAULT_MAX_INPUT_TOKENS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    exceeded,
    parse_usage,
)


SECRET = "SECRET-TOKEN-VALUE"
CODEX_SESSION = (
    '{"type":"turn.completed","usage":{"input_tokens":1000,"output_tokens":10}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":6900000,"output_tokens":4000}}\n'
)
GROK_APPROVE = "\n".join(
    (
        json.dumps({"type": "thought", "data": "Checking the diff"}),
        json.dumps({"type": "text", "data": "The change matches the task.\n"}),
        json.dumps({"type": "text", "data": "VERDICT approve\n"}),
        json.dumps({"type": "usage", "usage": {"input_tokens": 12, "output_tokens": 4}}),
    )
)


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
        request = ProviderRequest(task="Say hello", packet_id="run-aabbccddeeff", kind="review")
        path = grok_prompt_path(request.packet_id)
        seen: dict = {}

        def runner(argv, stdin_text, timeout, cwd):
            del stdin_text, timeout, cwd
            self.assertIn("--prompt-file", argv)
            prompt = Path(argv[argv.index("--prompt-file") + 1])
            self.assertEqual(prompt.name, path.name)
            self.assertNotEqual(prompt, path)
            self.assertEqual(prompt.read_text(encoding="utf-8"), "Say hello")
            seen["during"] = True
            seen["path"] = prompt
            return _completed(argv, 0, stdout=GROK_APPROVE)

        result = GrokHeadlessProvider().execute(request, allow_live=True, runner=runner)
        self.assertTrue(seen["during"])
        self.assertFalse(seen["path"].parent.exists())
        self.assertEqual(parse_verdict(result.text)[0], "approve")
        self.assertTrue(result.live)

        def missing(argv, stdin_text, timeout, cwd):
            del argv, stdin_text, timeout, cwd
            raise FileNotFoundError("missing")

        with self.assertRaises(HarnessError):
            GrokHeadlessProvider().execute(request, allow_live=True, runner=missing)
        self.assertFalse(path.exists())
        with self.assertRaises(LiveProviderDisabled):
            ClaudeHeadlessProvider().execute(request, allow_live=False)

    def test_claude_logged_in_json_wins_over_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bindir = Path(directory) / "bin"
            home = Path(directory) / "home"
            bindir.mkdir()
            home.mkdir()
            (bindir / "claude").write_text("", encoding="utf-8")
            body = json.dumps({"authMethod": "claude.ai", "loggedIn": True, "token": SECRET})

            def run(argv, timeout):
                del timeout
                if argv[1:] == ["--version"]:
                    return _completed(argv, 0, stdout=f"claude 2.1.0 {SECRET}\n")
                if argv[1:] == ["auth", "status"]:
                    return _completed(argv, 1, stdout=body + "\n", stderr=f"refresh notice {SECRET}\n")
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
            with patch.dict(os.environ, {"PATH": str(directory)}):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = main(["init", str(target)])
            self.assertEqual(code, 0)
            written = json.loads(target.read_text(encoding="utf-8"))
            enabled = {item["name"] for item in written["providers"] if item["enabled"]}
            self.assertEqual(enabled, {"fixture", "fixture-b"})
            self.assertEqual(written["budgets"]["max_input_tokens"], DEFAULT_MAX_INPUT_TOKENS)
            self.assertIn(str(target), stdout.getvalue())

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
        for name in ("doctor", "init", "run", "status"):
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
