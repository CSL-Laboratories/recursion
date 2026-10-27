#!/usr/bin/env python3
"""Both fake-driver paths through the Control API: Claude-Code-style and Codex-style.

Each engine is driven end to end through ``ControlService.handle`` over the
Route Contract v0 reference adapter and the AT1 fake driver. No process is
spawned and no provider is contacted.
"""

from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v3.control_api import errors as err  # noqa: E402
from scripts.v3.control_api.auth import SCOPE_CONTROL, SCOPE_OBSERVE  # noqa: E402
from scripts.v3.control_api.fake_engines import (  # noqa: E402
    BEHAVIORS,
    ENGINES,
    FAKE_MODEL,
    VERDICT_SCHEMA,
    FakeEngineTransport,
    build_fake_stack,
)
from scripts.v3.route_contract import argv as argv_mod  # noqa: E402
from scripts.v3.route_contract import schema as contract_schema  # noqa: E402
from scripts.v3.route_contract.errors import ToolPolicyRefused  # noqa: E402

PACKAGE = ROOT / "scripts" / "v3" / "control_api"
PROMPT = "Review the change and answer with a verdict."


def validator(name: str) -> jsonschema.Draft202012Validator:
    defs = contract_schema.load_schema()["$defs"]
    return jsonschema.Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": defs})


class Driver:
    def __init__(self, scripts=None) -> None:
        self.stack = build_fake_stack(scripts=scripts)
        self.token = self.stack.issue("op", "tenant-demo", {SCOPE_CONTROL, SCOPE_OBSERVE})

    def call(self, method: str, params: dict | None = None) -> dict:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        return self.stack.service.handle(body, f"Bearer {self.token}")

    def run(self, engine: str, session: str = "s-one", prompt: str = PROMPT) -> dict:
        self.call("session.open", {"engine": engine, "session": session})
        return self.call("session.run", {"engine": engine, "session": session, "prompt": prompt})


class BothPathsTests(unittest.TestCase):
    def test_each_engine_completes_a_run_with_its_own_output_shape(self) -> None:
        driver = Driver()
        sessions = {}
        for engine in ENGINES:
            response = driver.run(engine)
            self.assertNotIn("error", response, engine)
            result = response["result"]["result"]
            self.assertEqual("ok", result["status"], engine)
            self.assertFalse(result["repaired"])
            self.assertEqual("pass", result["validated"]["verdict"])
            self.assertEqual([], list(validator("RouteExecutionResult").iter_errors(result)))
            self.assertEqual([], list(validator("RouteExecutionRequest").iter_errors(response["result"]["request"])))
            self.assertEqual(engine, response["result"]["request"]["engine_id"])
            kinds = [e["kind"] for e in response["result"]["adapter_events"]]
            self.assertEqual(["started", "sent"], kinds)
            sessions[engine] = response["result"]["adapter_events"][0]["credential"]
        self.assertEqual({"claude", "codex"}, set(sessions))

    def test_claude_path_reads_a_json_envelope_and_codex_path_reads_event_lines(self) -> None:
        claude = FakeEngineTransport("claude")
        out = claude.run(argv_mod.build_argv("claude", cli_model=FAKE_MODEL), PROMPT, 5.0, None)
        envelope = json.loads(out.stdout)
        self.assertEqual("result", envelope["type"])
        self.assertTrue(envelope["session_id"].startswith("fake-claude-"))
        self.assertEqual("pass", json.loads(envelope["result"])["verdict"])

        codex = FakeEngineTransport("codex")
        out = codex.run(argv_mod.build_argv("codex", cli_model=FAKE_MODEL), PROMPT, 5.0, None)
        lines = [json.loads(line) for line in out.stdout.splitlines()]
        self.assertEqual(["thread.started", "item.completed", "turn.completed"], [l["type"] for l in lines])
        self.assertEqual("agent_message", lines[1]["item"]["type"])
        self.assertEqual("pass", json.loads(lines[1]["item"]["text"])["verdict"])

    def test_output_is_a_pure_function_of_the_prompt(self) -> None:
        first = Driver().run("claude")["result"]["result"]
        second = Driver().run("claude")["result"]["result"]
        self.assertEqual(first, second)
        other = Driver().run("claude", prompt="a different prompt")["result"]["result"]
        self.assertNotEqual(first["output_digest"], other["output_digest"])

    def test_every_launch_is_the_exact_zero_tools_profile_and_the_prompt_stays_off_argv(self) -> None:
        driver = Driver()
        for engine in ENGINES:
            driver.run(engine)
            (call,) = driver.stack.transports[engine].calls
            self.assertEqual("zero-tools", call["argv_profile"])
            self.assertFalse(call["prompt_on_argv"])
            self.assertEqual(1, len(driver.stack.transports[engine].calls))

    def test_the_fake_transport_refuses_any_argv_that_is_not_zero_tools(self) -> None:
        transport = FakeEngineTransport("claude")
        base = argv_mod.build_argv("claude", cli_model=FAKE_MODEL)
        for argv in (
            [*base, "--add-dir", "/tmp"],
            [*base, "--dangerously-skip-permissions"],
            [*base, "--mcp-config", "x.json"],
            argv_mod.build_argv("codex", cli_model=FAKE_MODEL),
        ):
            with self.assertRaises(ToolPolicyRefused):
                transport.run(argv, PROMPT, 5.0, None)
        self.assertEqual([], transport.calls)

    def test_concurrent_sessions_on_both_engines_are_independent(self) -> None:
        driver = Driver()
        for engine in ENGINES:
            for session in ("s-one", "s-two"):
                response = driver.run(engine, session)
                self.assertEqual("ok", response["result"]["result"]["status"])
        views = [driver.call("session.get", {"engine": e, "session": s})["result"] for e in ENGINES for s in ("s-one", "s-two")]
        self.assertEqual([1, 1, 1, 1], [v["runs"] for v in views])

    def test_route_list_reports_both_engines_as_fake_and_not_live(self) -> None:
        routes = Driver().call("route.list")["result"]["routes"]
        self.assertEqual(["claude", "codex"], [r["engine_id"] for r in routes])
        for route in routes:
            self.assertEqual(("fake", "supported", False), (route["adapter_kind"], route["state"], route["live_calls"]))


class FaultPathTests(unittest.TestCase):
    def test_one_repair_attempt_then_ok(self) -> None:
        for engine in ENGINES:
            for first in ("not_json", "wrong_schema"):
                driver = Driver({engine: [first]})
                result = driver.run(engine)["result"]["result"]
                self.assertEqual("ok", result["status"], (engine, first))
                self.assertTrue(result["repaired"], (engine, first))
                self.assertEqual(2, len(driver.stack.transports[engine].calls))

    def test_two_invalid_outputs_need_a_human_and_never_loop(self) -> None:
        for engine in ENGINES:
            driver = Driver({engine: ["not_json", "wrong_schema", "ok"]})
            result = driver.run(engine)["result"]["result"]
            self.assertEqual("needs_human_review", result["status"])
            self.assertEqual("repair_still_invalid", result["error"])
            self.assertEqual(2, len(driver.stack.transports[engine].calls))
            self.assertEqual([], list(validator("RouteExecutionResult").iter_errors(result)))

    def test_a_limit_opens_the_breaker_for_that_engine_only(self) -> None:
        driver = Driver({"codex": ["limit"]})
        limited = driver.run("codex")["result"]["result"]
        self.assertEqual(("limit_hit", "limit_or_error", True), (limited["status"], limited["error"], limited["dispatched"]))
        self.assertEqual("exhausted", limited["health_event"]["state"])
        blocked = driver.call("session.run", {"engine": "codex", "session": "s-one", "prompt": PROMPT})["result"]["result"]
        self.assertEqual(("limit_hit", "breaker_open", False), (blocked["status"], blocked["error"], blocked["dispatched"]))
        self.assertEqual(1, len(driver.stack.transports["codex"].calls))
        self.assertEqual("ok", driver.run("claude")["result"]["result"]["status"])

    def test_vendor_error_and_timeout_are_typed_results(self) -> None:
        for engine in ENGINES:
            vendor = Driver({engine: ["vendor_error"]}).run(engine)["result"]["result"]
            self.assertEqual("vendor_error_envelope", vendor["error"])
            self.assertEqual("degraded", vendor["usage_state"])
            timeout = Driver({engine: ["timeout"]}).run(engine)["result"]["result"]
            self.assertEqual(("timeout", "timeout"), (timeout["status"], timeout["error"]))
            self.assertEqual("terminated", timeout["termination"]["tree_status"])
            for result in (vendor, timeout):
                self.assertEqual([], list(validator("RouteExecutionResult").iter_errors(result)))

    def test_unknown_behavior_or_engine_is_rejected_at_build_time(self) -> None:
        with self.assertRaises(ValueError):
            FakeEngineTransport("claude", script=["explode"])
        with self.assertRaises(ValueError):
            FakeEngineTransport("grok")
        with self.assertRaises(ValueError):
            build_fake_stack(scripts={"grok": []})
        self.assertIn("ok", BEHAVIORS)


class CustodyTests(unittest.TestCase):
    def test_credential_material_is_random_per_stack_and_never_leaves_it(self) -> None:
        one, two = Driver(), Driver()
        self.assertNotEqual(one.stack._material, two.stack._material)
        blob = ""
        for engine in ENGINES:
            blob += json.dumps(one.run(engine, prompt=f"echo {engine}"))
        blob += json.dumps(one.stack.journal.poll("tenant-demo", 0))
        blob += repr(one.stack._resolver) + repr(one.stack.service.__dict__.keys())
        self.assertNotIn(one.stack._material, blob)

    def test_a_prompt_carrying_the_credential_is_refused_before_the_transport(self) -> None:
        driver = Driver()
        driver.call("session.open", {"engine": "claude", "session": "s-one"})
        response = driver.call(
            "session.run",
            {"engine": "claude", "session": "s-one", "prompt": f"leak {driver.stack._material}"},
        )
        self.assertEqual(err.REFUSED, response["error"]["code"])
        self.assertEqual("secret_refused", response["error"]["data"]["code"])
        self.assertEqual([], driver.stack.transports["claude"].calls)

    def test_a_tenant_without_a_provisioned_credential_is_refused(self) -> None:
        driver = Driver()
        outsider = driver.stack.tokens.issue("outsider", "tenant-other", {SCOPE_CONTROL})
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "session.open", "params": {"engine": "claude", "session": "s-one"}})
        driver.stack.service.handle(body, f"Bearer {outsider}")
        run = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session.run", "params": {"engine": "claude", "session": "s-one", "prompt": PROMPT}})
        response = driver.stack.service.handle(run, f"Bearer {outsider}")
        self.assertEqual("custody_denied", response["error"]["data"]["code"])
        self.assertEqual([], driver.stack.transports["claude"].calls)


class StaticBoundaryTests(unittest.TestCase):
    def test_fake_engines_import_no_process_or_network_api(self) -> None:
        tree = ast.parse((PACKAGE / "fake_engines.py").read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots.add((node.module or "").split(".")[0])
        self.assertLessEqual(
            roots,
            {"__future__", "hashlib", "json", "secrets", "threading", "collections", "dataclasses", "pathlib", "scripts"},
        )

    def test_verdict_schema_is_valid_and_closed(self) -> None:
        jsonschema.Draft202012Validator.check_schema(VERDICT_SCHEMA)
        self.assertIs(False, VERDICT_SCHEMA["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
