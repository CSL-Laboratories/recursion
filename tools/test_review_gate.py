#!/usr/bin/env python3
"""Provider-free tests for tools/review_gate.py and review-gate.yml."""
from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "review-gate.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def load_mod():
    spec = importlib.util.spec_from_file_location(
        "review_gate", ROOT / "tools" / "review_gate.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = load_mod()


class WorkflowStaticTests(unittest.TestCase):
    def test_review_gate_is_never_skipped_and_runs_on_ubuntu(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("name: review-gate", text)
        self.assertIn("runs-on: ubuntu-latest", text)
        self.assertNotIn("blacksmith", text.lower())
        self.assertIn("merge_group:", text)
        self.assertIn("types: [checks_requested]", text)
        self.assertIn("status:", text)
        self.assertIn("pull_request:", text)
        on_block = text[text.index("\non:") : text.index("\nconcurrency:")]
        self.assertNotIn("check_run:", on_block)
        jobs = text.split("jobs:", 1)[1]
        job = jobs[jobs.index("  review-gate:") :]
        self.assertNotRegex(job.split("steps:", 1)[0], r"(?m)^\s+if:")
        self.assertIn("github.event_name == 'merge_group'", text)
        self.assertIn("tools/review_gate.py", text)

    def test_ci_yml_merge_group_and_master_skip(self) -> None:
        text = CI.read_text(encoding="utf-8")
        preamble = text.split("jobs:", 1)[0]
        self.assertIn("merge_group:", preamble)
        self.assertIn("types: [checks_requested]", preamble)
        self.assertIn("github.event.merge_group.head_ref", preamble)
        self.assertIn("github.event.merge_group.base_sha", preamble)
        self.assertIn("github.event.merge_group.head_sha", preamble)
        linux = text[text.index("  linux:\n") : text.index("  windows:")]
        self.assertIn("github.event_name != 'push'", linux)
        windows = text[text.index("  windows:") : text.index("  macos:")]
        self.assertIn("github.event_name == 'merge_group'", windows)
        self.assertNotIn("github.event_name == 'push'", windows)
        macos = text[text.index("  macos:") :]
        self.assertIn("github.event_name == 'merge_group'", macos)
        self.assertNotIn("github.event_name == 'push'", macos)


class ReviewGateLogicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sha = "a" * 40
        self.env = {
            "GITHUB_REPOSITORY": "CSL-Laboratories/recursion",
            "HEAD_SHA": self.sha,
            "EVENT_NAME": "pull_request",
            "IS_DRAFT": "false",
            "REVIEW_GATE_POLL_SECONDS": "0",
            "REVIEW_GATE_POLL_INTERVAL": "0",
        }

    def test_merge_group_passes_without_api(self) -> None:
        with mock.patch.dict(os.environ, {**self.env, "EVENT_NAME": "merge_group"}):
            self.assertEqual(mod.main(), 0)

    def test_draft_fails_closed(self) -> None:
        with mock.patch.dict(os.environ, {**self.env, "IS_DRAFT": "true"}):
            self.assertEqual(mod.main(), 1)

    def test_missing_sha_fails(self) -> None:
        env = {**self.env, "HEAD_SHA": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ["HEAD_SHA"] = ""
            self.assertEqual(mod.main(), 1)

    def test_pull_request_poll_succeeds_when_status_is_success(self) -> None:
        with mock.patch.dict(os.environ, self.env), mock.patch.object(
            mod, "devin_review_succeeded", return_value=True
        ) as succeeded:
            self.assertEqual(mod.main(), 0)
            succeeded.assert_called_with(self.sha)

    def test_pull_request_poll_times_out(self) -> None:
        with mock.patch.dict(os.environ, self.env), mock.patch.object(
            mod, "devin_review_succeeded", return_value=False
        ):
            self.assertEqual(mod.main(), 1)

    def test_status_on_non_pr_sha_passes(self) -> None:
        with mock.patch.dict(
            os.environ, {**self.env, "EVENT_NAME": "status"}
        ), mock.patch.object(mod, "associated_pulls", return_value=[]), mock.patch.object(
            mod, "devin_review_succeeded"
        ) as succeeded:
            self.assertEqual(mod.main(), 0)
            succeeded.assert_not_called()

    def test_status_on_pr_head_requires_devin(self) -> None:
        pull = {"state": "open", "draft": False, "head": {"sha": self.sha}}
        with mock.patch.dict(
            os.environ, {**self.env, "EVENT_NAME": "status"}
        ), mock.patch.object(mod, "associated_pulls", return_value=[pull]), mock.patch.object(
            mod, "devin_review_succeeded", return_value=False
        ):
            self.assertEqual(mod.main(), 1)

    def test_devin_review_succeeded_reads_commit_status(self) -> None:
        with mock.patch.object(
            mod,
            "gh_api",
            side_effect=[
                {"statuses": [{"context": "Devin Review", "state": "success"}]},
            ],
        ):
            self.assertTrue(mod.devin_review_succeeded(self.sha))

    def test_devin_review_succeeded_falls_back_to_check_run(self) -> None:
        with mock.patch.object(
            mod,
            "gh_api",
            side_effect=[
                {"statuses": []},
                {
                    "check_runs": [
                        {
                            "name": "Devin Review",
                            "started_at": "2026-10-16T00:00:00Z",
                            "conclusion": "success",
                        }
                    ]
                },
            ],
        ):
            self.assertTrue(mod.devin_review_succeeded(self.sha))


if __name__ == "__main__":
    unittest.main()
