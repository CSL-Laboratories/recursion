"""The arms: what the direct arm reads from engine output, and the Recursion arm's refusal."""
from __future__ import annotations

import json
import unittest

from eval import REPO_ROOT, arms


def lines(*messages) -> bytes:
    return b"".join(json.dumps(m).encode() + b"\n" for m in messages)


class StreamJsonTests(unittest.TestCase):
    def test_success_with_usage_maps_every_token_field(self):
        out = arms.parse_stream_json(lines(
            {"type": "system", "subtype": "init"},
            {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.25,
             "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30,
                       "output_tokens": 40}}), cost_basis="reported_not_billed")
        self.assertTrue(out["claimed_done"])
        self.assertIsNone(out["failure"])
        self.assertEqual({k: out["usage"][k] for k in ("uncached_input", "cache_write", "cache_read", "output")},
                         {"uncached_input": 10, "cache_write": 20, "cache_read": 30, "output": 40})
        self.assertEqual((out["usage"]["cost"], out["usage"]["cost_unit"], out["usage"]["cost_basis"]),
                         (0.25, "USD", "reported_not_billed"))

    def test_absent_or_bad_fields_are_null_never_zero(self):
        out = arms.parse_stream_json(lines(
            {"type": "result", "subtype": "success", "is_error": False,
             "usage": {"input_tokens": 5, "output_tokens": "many", "cache_read_input_tokens": -1}}),
            cost_basis="reported_not_billed")
        self.assertEqual(out["usage"]["uncached_input"], 5)
        self.assertIsNone(out["usage"]["output"])
        self.assertIsNone(out["usage"]["cache_read"])
        self.assertIsNone(out["usage"]["cache_write"])
        self.assertIsNone(out["usage"]["cost"])
        self.assertEqual(out["usage"]["cost_basis"], "unknown")
        nothing = arms.parse_stream_json(lines({"type": "result", "subtype": "success", "is_error": False}),
                                         cost_basis="billed")
        self.assertIsNone(nothing["usage"]["source"])
        self.assertIsNone(nothing["usage"]["uncached_input"])

    def test_typed_vendor_failures_are_infra_classes(self):
        cases = [
            ({"error": "rate_limit"}, "vendor_limit"),
            ({"api_error_params": {"rate_limit_info": {"status": "rejected"}}}, "vendor_limit"),
            ({"error": "authentication_failed"}, "auth_failed"),
            ({"error": "oauth_token_expired"}, "auth_failed"),
            ({"api_error_status": 401}, "auth_failed"),
            ({"error": "something_else"}, "engine_error"),
            ({}, "engine_error"),
        ]
        for extra, expected in cases:
            with self.subTest(expected=expected, extra=extra):
                out = arms.parse_stream_json(lines({"type": "result", "subtype": "error_during_execution",
                                                    "is_error": True, **extra}), cost_basis="billed")
                self.assertFalse(out["claimed_done"])
                self.assertEqual(out["failure"], expected)

    def test_no_result_line(self):
        out = arms.parse_stream_json(b'{"type": "assistant"}\nnot json\n', cost_basis="billed")
        self.assertFalse(out["has_result"])
        self.assertFalse(out["claimed_done"])

    def test_the_last_result_line_counts(self):
        out = arms.parse_stream_json(lines({"type": "result", "subtype": "success", "is_error": False},
                                           {"type": "result", "subtype": "error_during_execution", "is_error": True}),
                                     cost_basis="billed")
        self.assertFalse(out["claimed_done"])


class RecursionArmTests(unittest.TestCase):
    def refusal(self, found):
        arm = arms.RecursionArm("standin", probe=lambda _root: found)
        with self.assertRaises(arms.ArmRefused) as caught:
            arm.preflight()
        return str(caught.exception)

    def test_refuses_naming_workspace_when_it_is_absent(self):
        text = self.refusal({"control_hooks": True, "workspace": False, "usage_events": True})
        self.assertIn("recursion arm refused", text)
        self.assertIn("Missing here: WORKSPACE's `workspace` at start (seam S6).", text)
        self.assertNotIn("Missing here: EVENTS", text)

    def test_refuses_naming_usage_events_when_they_are_absent(self):
        text = self.refusal({"control_hooks": True, "workspace": True, "usage_events": False})
        self.assertIn("Missing here: EVENTS' usage events through events.read.", text)

    def test_refuses_naming_both_and_the_seams_on_this_base(self):
        text = self.refusal(arms.probe_base(REPO_ROOT))
        found = arms.probe_base(REPO_ROOT)
        if not found["control_hooks"]:
            self.assertIn("SPINE's seams under it", text)
        if not (found["workspace"] and found["usage_events"]):
            self.assertIn("The other arms run.", text)

    def test_with_both_present_it_still_refuses_until_slice_3(self):
        text = self.refusal({"control_hooks": True, "workspace": True, "usage_events": True})
        self.assertIn("slice 3", text)
        self.assertNotIn("Missing here", text)

    def test_probe_reads_the_files_of_the_evaluated_checkout(self):
        from eval.tests import outside_folder
        with outside_folder() as root:
            pkg = root / "core" / "recursion_core"
            self.assertEqual(arms.probe_base(root), {"control_hooks": False, "workspace": False, "usage_events": False})
            for rel in ("control/hooks.py", "workspace/hooks.py"):
                (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
                (pkg / rel).write_text("", encoding="utf-8")
            self.assertEqual(arms.probe_base(root), {"control_hooks": True, "workspace": True, "usage_events": False})


class DirectArmTests(unittest.TestCase):
    def test_claude_code_needs_a_model_and_the_binary(self):
        with self.assertRaises(arms.ArmRefused):
            arms.DirectArm("claude_code").preflight()
        missing = arms.DirectArm("claude_code", model="m",
                                 claude=arms.ClaudeCodeSettings(binary="/nonexistent/claude"))
        with self.assertRaises(arms.ArmRefused):
            missing.preflight()

    def test_claude_code_argv_is_the_adapters(self):
        arm = arms.DirectArm("claude_code", model="m", claude=arms.ClaudeCodeSettings(permission_mode="acceptEdits"))
        self.assertEqual(arm.argv(), ["claude", "-p", "--input-format", "stream-json", "--output-format",
                                      "stream-json", "--verbose", "--model", "m", "--permission-mode", "acceptEdits"])

    def test_unwired_engine_refused(self):
        with self.assertRaises(arms.ArmRefused):
            arms.DirectArm("codex")

    def test_settings_hash_follows_the_settings(self):
        a = arms.DirectArm("claude_code", model="m1").settings_sha256()
        b = arms.DirectArm("claude_code", model="m2").settings_sha256()
        self.assertNotEqual(a, b)
        self.assertEqual(a, arms.DirectArm("claude_code", model="m1").settings_sha256())


if __name__ == "__main__":
    unittest.main()
