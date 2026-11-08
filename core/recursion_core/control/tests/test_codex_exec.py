"""CSL-762 B4 / F60 Arm E: arms A to C under codex exec, plus approve on start, n=5.

No Codex binary, provider, network or real credential is used. The approval function is
the offline reading of openai/codex e0aa0434 recorded in ``codex_exec.py``.
"""

from __future__ import annotations

import unittest

from recursion_core.control import mcp_tools
from recursion_core.control.codex_exec import (
    CODEX_BINARY_VERSION,
    CODEX_SOURCE_COMMIT,
    CODEX_SOURCE_FILE,
    exec_outcome,
    requires_approval_for_mode,
    requires_mcp_tool_approval,
)

N = 5
ARM_A = {"destructiveHint": False}  # openWorldHint unset → defaults true → would prompt
ARM_B = {"destructiveHint": False, "openWorldHint": False}  # closed-world write → no prompt
ARM_C = {"readOnlyHint": True}  # read tools only → no prompt


class SourcePinTest(unittest.TestCase):
    def test_codex_source_commit_is_recorded_and_binary_is_unmeasured(self) -> None:
        self.assertEqual(CODEX_SOURCE_COMMIT, "e0aa0434f415b0c5bc0fb64abd7f03c5510f9ed6")
        self.assertEqual(len(CODEX_SOURCE_COMMIT), 40)
        int(CODEX_SOURCE_COMMIT, 16)
        self.assertEqual(CODEX_SOURCE_FILE, "codex-rs/core/src/mcp_tool_call.rs")
        self.assertIsNone(CODEX_BINARY_VERSION)


class MarkTests(unittest.TestCase):
    def test_destructive_true_prompts_even_when_read_only_is_also_set(self) -> None:
        # Code order at e0aa0434: destructiveHint: true always prompts; docs say the reverse.
        self.assertTrue(requires_mcp_tool_approval({"destructiveHint": True, "readOnlyHint": True}))

    def test_missing_annotations_prompt_because_mcp_defaults_are_true(self) -> None:
        self.assertTrue(requires_mcp_tool_approval(None))
        self.assertTrue(requires_mcp_tool_approval({}))

    def test_approval_modes(self) -> None:
        self.assertFalse(requires_approval_for_mode(ARM_A, "approve"))
        self.assertTrue(requires_approval_for_mode(ARM_C, "prompt"))
        self.assertFalse(requires_approval_for_mode(ARM_C, "writes"))
        self.assertTrue(requires_approval_for_mode(ARM_A, "writes"))
        self.assertTrue(requires_approval_for_mode(ARM_A, "auto"))
        self.assertFalse(requires_approval_for_mode(ARM_B, "auto"))
        with self.assertRaises(ValueError):
            requires_approval_for_mode(ARM_B, "untrusted")


class ArmETests(unittest.TestCase):
    """Arm E: A to C under exec, plus approval_mode=approve on start; n>=5."""

    def _repeat(self, check) -> None:
        results = [check() for _ in range(N)]
        self.assertEqual((N, N), (len(results), results.count(True)), results)

    def test_arm_a_destructive_false_only_is_refused_under_exec(self) -> None:
        self._repeat(lambda: exec_outcome(ARM_A) == "refused")

    def test_arm_b_closed_world_non_destructive_is_allowed_under_exec(self) -> None:
        self._repeat(lambda: exec_outcome(ARM_B) == "allowed")

    def test_arm_c_read_tools_are_allowed_under_exec(self) -> None:
        self._repeat(lambda: exec_outcome(ARM_C) == "allowed")

    def test_start_is_refused_under_exec_unless_approval_mode_is_approve(self) -> None:
        start = next(t["annotations"] for t in mcp_tools.TOOLS if t["name"] == "recursion_run_start")
        self.assertEqual(start["openWorldHint"], True)
        self._repeat(lambda: exec_outcome(start) == "refused")
        self._repeat(lambda: exec_outcome(start, "approve") == "allowed")

    def test_stop_is_refused_under_exec_without_the_operator_approve_setting(self) -> None:
        stop = next(t["annotations"] for t in mcp_tools.TOOLS if t["name"] == "recursion_run_stop")
        self.assertEqual(stop["destructiveHint"], True)
        self._repeat(lambda: exec_outcome(stop) == "refused")
        self._repeat(lambda: exec_outcome(stop, "approve") == "allowed")

    def test_our_read_tools_match_arm_c(self) -> None:
        for name in ("recursion_run_list", "recursion_run_status"):
            anns = next(t["annotations"] for t in mcp_tools.TOOLS if t["name"] == name)
            self._repeat(lambda anns=anns: exec_outcome(anns) == "allowed")


class OverrideAndExceptionTests(unittest.TestCase):
    def test_prompt_override_refuses_a_read_under_exec(self) -> None:
        self.assertEqual(exec_outcome(ARM_C, "prompt"), "refused")

    def test_writes_override_allows_reads_and_refuses_start(self) -> None:
        start = next(t["annotations"] for t in mcp_tools.TOOLS if t["name"] == "recursion_run_start")
        self.assertEqual(exec_outcome(ARM_C, "writes"), "allowed")
        self.assertEqual(exec_outcome(start, "writes"), "refused")

    def test_full_access_profiles_allow_a_call_that_would_otherwise_prompt(self) -> None:
        start = next(t["annotations"] for t in mcp_tools.TOOLS if t["name"] == "recursion_run_start")
        for profile in ("disabled", "external", "managed_full_disk"):
            self.assertEqual(exec_outcome(start, profile=profile), "allowed", profile)

    def test_strict_auto_review_and_managed_approval_refuse_even_approve_or_full_access(self) -> None:
        start = next(t["annotations"] for t in mcp_tools.TOOLS if t["name"] == "recursion_run_start")
        self.assertEqual(exec_outcome(start, "approve", strict_auto_review=True), "refused")
        self.assertEqual(exec_outcome(start, "approve", managed_approval=True), "refused")
        self.assertEqual(exec_outcome(start, profile="disabled", strict_auto_review=True), "refused")


if __name__ == "__main__":
    unittest.main()
