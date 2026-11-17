"""#376 next round: the permission mode is passed and read back, a denial is not success,
and the route label carries the sub-agent flags.

B3-2.4 with B3-13.3, B3-28.3 and B3-40.3: ``ClaudeCodeAdapter.argv`` names
``--permission-mode`` with the fail-closed value, and a ``system/init`` that reports any
other mode ends the run refused. B1-8.6: a final ``result/success`` after a permission
denial or an error event in the same turn is not success, and a route that asks for the
permission bridge requires PreToolUse to be confirmed at init. B2-15.3: a declaration
that leaves out the sub-agent flags is refused, and the route label shows them.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from recursion_core.routes import claude_code as cc
from recursion_core.routes import contract as c
from recursion_core.routes import driver as d
from recursion_core.routes.claude_code import ClaudeCodeAdapter, _classify_result
from recursion_core.routes.driver import Declaration, DriverInputError, Flags, one_turn
from recursion_core.routes.tests.fakes import request
from recursion_core.routes.tests.test_routes_claude import Harness

# The fail-closed value PRODUCT.md 11.6 names. The adapter publishes the same spelling.
FAIL_CLOSED_PERMISSION_MODE = getattr(cc, "FAIL_CLOSED_PERMISSION_MODE", "dontAsk")
route_label = getattr(d, "route_label", None)

_README = Path(d.__file__).resolve().parent / "README.md"
_OLD_FLAGS = (
    "multi_turn", "mid_turn_input", "interrupt", "resume", "fork",
    "permission_bridge", "typed_limits", "usage",
)
_SUBAGENT_FLAGS = ("observes_subagents", "cancels_subagents", "usage_complete")


def _init(mode=FAIL_CLOSED_PERMISSION_MODE, **extra):
    return {"type": "system", "subtype": "init", "permissionMode": mode, **extra}


def _success(text="ran"):
    return {"type": "result", "subtype": "success", "is_error": False, "result": text}


def _confirmed(**extra):
    return _init(hooks_applied=True, hook_on_failure_supported=True, **extra)


class PermissionModeTest(unittest.TestCase):
    def test_argv_passes_the_fail_closed_permission_mode(self) -> None:
        adapter = ClaudeCodeAdapter(lambda argv, **kwargs: None, lambda: 0.0, lambda *a: "", {})
        argv = adapter.argv(request())
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], FAIL_CLOSED_PERMISSION_MODE)
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        # The model stays last: fallback tests read argv[-1] as the model.
        self.assertEqual(argv[-1], "claude-sonnet")
        named = adapter.argv(request(permission_mode="plan"))
        self.assertEqual(named[named.index("--permission-mode") + 1], "plan")
        self.assertNotIn("--permission-prompts", named)

    def test_bypass_needs_a_full_access_acknowledgement(self) -> None:
        adapter = ClaudeCodeAdapter(lambda argv, **kwargs: None, lambda: 0.0, lambda *a: "", {})
        with self.assertRaises(c.RouteError) as caught:
            adapter.argv(request(permission_mode="bypassPermissions"))
        self.assertEqual(caught.exception.code, c.LAUNCH_REFUSED)
        argv = adapter.argv(request(permission_mode="bypassPermissions", permission_ack="full_access"))
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")

    def test_an_unknown_permission_mode_is_refused_before_launch(self) -> None:
        h = Harness([[_success()]])
        res = h.handle(request(permission_mode="default"))
        self.assertEqual(res["code"], c.REQUEST_INVALID)
        self.assertEqual(h.launcher.calls, [])

    def test_a_reported_mode_other_than_the_one_passed_ends_the_run_refused(self) -> None:
        for reported in ("bypassPermissions", "acceptEdits", "plan", "manual", "auto"):
            with self.subTest(reported=reported):
                h = Harness([[_init(reported), _success()]])
                res = h.handle(request())
                self.assertEqual(res["code"], c.LAUNCH_REFUSED)
                self.assertNotEqual(res["status"], "ok")
                self.assertEqual(h.launcher.processes[0].fenced, ["permission_mode"])

    def test_the_passed_mode_is_read_back_and_a_second_caller_uses_its_own(self) -> None:
        h = Harness([[_init(FAIL_CLOSED_PERMISSION_MODE), _success("first")],
                     [_init(FAIL_CLOSED_PERMISSION_MODE), _success("second")]])
        first = h.handle(request())
        self.assertEqual((first["status"], first["text"]), ("ok", "first"))
        second = h.handle(request(rid="r2", permission_mode="plan"))
        self.assertEqual(second["code"], c.LAUNCH_REFUSED)
        self.assertEqual(h.launcher.calls[0]["argv"][h.launcher.calls[0]["argv"].index("--permission-mode") + 1],
                         FAIL_CLOSED_PERMISSION_MODE)
        self.assertEqual(h.launcher.calls[1]["argv"][h.launcher.calls[1]["argv"].index("--permission-mode") + 1],
                         "plan")

    def test_a_matching_full_access_report_is_not_a_mode_refusal(self) -> None:
        h = Harness([[_init("bypassPermissions"), _success("full")]])
        res = h.handle(request(permission_mode="bypassPermissions", permission_ack="full_access"))
        self.assertEqual((res["status"], res["text"]), ("ok", "full"))


class PermissionBridgeAndClassifyTest(unittest.TestCase):
    def test_success_after_a_permission_denial_or_error_is_not_success(self) -> None:
        denial = {"type": "system", "subtype": "permission_denial", "tool": "Bash"}
        error = {"type": "system", "subtype": "error", "error": "boom"}
        for name, event in (("denial", denial), ("error", error)):
            with self.subTest(name=name):
                h = Harness([[_init(), event, _success()]])
                res = h.handle(request())
                self.assertEqual(res["status"], "error")
                self.assertEqual(res["code"], c.ENGINE_ERROR)
                self.assertNotEqual(res["code"], None)

    def test_a_success_result_that_carries_permission_denials_is_not_success(self) -> None:
        result = _success()
        result["permission_denials"] = [{"tool_name": "Bash"}]
        out = _classify_result("r1", result, None, [], "claude-sonnet", lambda value: None)
        self.assertEqual(out["code"], c.ENGINE_ERROR)
        self.assertNotEqual(out["status"], "ok")

    def test_classify_sees_a_denial_recorded_earlier_in_the_turn(self) -> None:
        events = [c.event("denial", "claude.stream", {"subtype": "permission_denial"})]
        out = _classify_result("r1", _success(), None, events, "claude-sonnet", lambda value: None)
        self.assertEqual(out["code"], c.ENGINE_ERROR)

    def test_a_retry_then_success_stays_success(self) -> None:
        retry = {"type": "system", "subtype": "api_retry", "attempt": 1, "error_status": 529, "error": "overloaded"}
        h = Harness([[retry, *_init_and_success()]])
        res = h.handle(request())
        self.assertEqual((res["status"], res["code"]), ("ok", None))

    def test_the_permission_bridge_fails_closed_unless_pretooluse_is_confirmed(self) -> None:
        cases = (
            _init(),
            _init(hooks_applied=False, hook_on_failure_supported=True),
            _init(hooks_applied=True, hook_on_failure_supported=False),
            _init(hooks_applied=True),
            _success(),  # no init at all: the bridge was never confirmed
        )
        for script in cases:
            with self.subTest(script=script):
                lines = script if isinstance(script, list) else [script, _success()]
                if lines[-1].get("type") != "result":
                    lines = [*lines, _success()]
                h = Harness([lines])
                res = h.handle(request(permission_bridge=True))
                self.assertEqual(res["code"], c.LAUNCH_REFUSED)
                self.assertNotEqual(res["status"], "ok")

    def test_a_confirmed_pretooluse_bridge_allows_the_run(self) -> None:
        h = Harness([[_confirmed(), _success("bridged")]])
        res = h.handle(request(permission_bridge=True))
        self.assertEqual((res["status"], res["text"]), ("ok", "bridged"))

    def test_an_interruption_while_reading_still_leaves(self) -> None:
        class Interrupt:
            def send(self, line: str) -> None:
                return None

            def close_stdin(self) -> None:
                return None

            def read_line(self, timeout_s: float):
                raise KeyboardInterrupt

            def fence(self, reason: str) -> None:
                return None

        clock = type("Clock", (), {"now": 0.0, "__call__": lambda self: self.now})()
        adapter = ClaudeCodeAdapter(lambda argv, **kwargs: Interrupt(), clock, lambda *a: "", {}, wall_clock=clock)
        from recursion_core.routes.breaker import ConnectionBreaker
        from recursion_core.routes.service import RouteService
        service = RouteService({"claude_code": adapter}, ConnectionBreaker(clock), clock)
        with self.assertRaises(KeyboardInterrupt):
            service.handle(request())


def _init_and_success():
    return [_init(), _success()]


class SubagentFlagsTest(unittest.TestCase):
    def test_a_record_with_no_subagent_flags_is_refused(self) -> None:
        record = {"engine": "claude_code", "driver": 2, "input_ack": False,
                  "flags": {name: False for name in _OLD_FLAGS}}
        with self.assertRaises(DriverInputError) as caught:
            Declaration.from_record(record)
        self.assertEqual(caught.exception.field, "flags")

    def test_the_new_flags_default_false_and_round_trip(self) -> None:
        declared = one_turn("claude_code")
        for name in _SUBAGENT_FLAGS:
            self.assertIs(declared.flags.as_dict()[name], False)
        self.assertEqual(Declaration.from_record(declared.as_record()).as_record(), declared.as_record())
        self.assertIs(Flags(observes_subagents=True).observes_subagents, True)
        self.assertIs(Flags(cancels_subagents=True, multi_turn=True).cancels_subagents, True)
        self.assertIs(Flags(usage_complete=True).usage_complete, True)

    def test_the_route_label_shows_the_subagent_flags(self) -> None:
        self.assertIsNotNone(route_label)
        label = route_label(one_turn("claude_code"))
        for name in _SUBAGENT_FLAGS:
            self.assertIn(f"{name}=false", label)
        turned = route_label(Declaration("claude_code", Flags(observes_subagents=True), input_ack=False))
        self.assertIn("observes_subagents=true", turned)
        text = _README.read_text(encoding="utf-8")
        for name in _SUBAGENT_FLAGS:
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
