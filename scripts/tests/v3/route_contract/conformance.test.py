#!/usr/bin/env python3
"""Route Contract v0 conformance, run against the AT1 fake driver.

Provider-free. No live provider, no network, no real credential. The only real
processes are local helper scripts started with ``sys.executable``. Behavioral
expectations come from golden vectors captured from an existing production
runner (see ``oracle_vectors.json`` provenance), not from this repository's own
output.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import jsonschema

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v3.route_adapter.credentials import CredentialRef, LocalCredentialResolver  # noqa: E402
from scripts.v3.route_adapter.fake import FakeRouteDriver  # noqa: E402
from scripts.v3.route_adapter.session import SessionKey, SessionOwner  # noqa: E402
from scripts.v3.route_contract import argv as argv_mod  # noqa: E402
from scripts.v3.route_contract import live as live_mod  # noqa: E402
from scripts.v3.route_contract import schema as schema_mod  # noqa: E402
from scripts.v3.route_contract.errors import (  # noqa: E402
    CwdRefused,
    RouteRefusal,
    SecretRefused,
    ToolPolicyRefused,
    Unsupported,
)
from scripts.v3.route_contract.executor import ReferenceAdapter  # noqa: E402
from scripts.v3.route_contract.health import InMemoryHealth  # noqa: E402
from scripts.v3.route_contract.model import ExecutionRequest, sha256_digest  # noqa: E402
from scripts.v3.route_contract.transport import (  # noqa: E402
    ProcResult,
    SubprocessTransport,
)

CONTRACT_DIR = ROOT / "scripts" / "v3" / "route_contract"
VECTORS = json.loads(
    (Path(__file__).with_name("oracle_vectors.json")).read_text(encoding="utf-8")
)
FROZEN_FIXTURES = ROOT / "scripts" / "tests" / "v3" / "execution" / "fixtures"
CANARY = "canary-secret-RC0-4d1e8b"
DIGEST = "sha256:" + "1" * 64
MODEL = VECTORS["_provenance"]["model_id"]
OUTPUT_SCHEMA = VECTORS["schema"]

# Inputs the oracle accepts but this contract deliberately refuses. The contract
# inspector accepts only the exact profile in the exact order.
STRICTER_THAN_ORACLE = {"claude_reordered"}
ENGINE_FOR_CASE = {"claude": "claude", "codex": "codex"}


def contract_defs() -> dict:
    return schema_mod.load_schema()["$defs"]


def validator_for(name: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(
        {"$ref": f"#/$defs/{name}", "$defs": contract_defs()},
        format_checker=jsonschema.FormatChecker(),
    )


def assert_valid(testcase: unittest.TestCase, name: str, record: object) -> None:
    errors = sorted(validator_for(name).iter_errors(record), key=lambda e: list(e.path))
    testcase.assertEqual([], [e.message for e in errors], f"{name} must validate")


def is_valid(name: str, record: object) -> bool:
    return validator_for(name).is_valid(record)


class ScriptedTransport:
    """Fake process transport: replays scripted results and records every call."""

    def __init__(self, results: list[ProcResult]) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    def run(self, argv, input_text, timeout_s, cwd):
        self.calls.append({"argv": list(argv), "input": input_text, "timeout_s": timeout_s, "cwd": cwd})
        return self.results.pop(0)


def proc(raw: dict) -> ProcResult:
    return ProcResult(raw["returncode"], raw["stdout"], raw["stderr"], raw["timed_out"])


def credential(tenant: str = "tenant-a", name: str = "route-ref") -> CredentialRef:
    return CredentialRef(tenant_id=tenant, provider="fake", name=name)


def make_driver(*, register: bool = True) -> FakeRouteDriver:
    resolver = LocalCredentialResolver()
    if register:
        resolver.register(credential(), CANARY)
    return FakeRouteDriver(owner=SessionOwner(), resolver=resolver)


def make_request(engine: str = "claude", *, prompt: str = "PROMPT", **overrides) -> ExecutionRequest:
    fields = dict(
        request_id="req-1",
        route_id="route-fake",
        engine_id=engine,
        route_descriptor_digest=DIGEST,
        process_requirement_id="proc-req-1",
        workload_id="workload-a",
        session=SessionKey("tenant-a", "route-fake", "session-1"),
        credential=credential(),
        cli_model=MODEL,
        prompt=prompt,
        output_schema=OUTPUT_SCHEMA,
        timeout_s=30.0,
    )
    fields.update(overrides)
    return ExecutionRequest(**fields)


def make_adapter(engine="claude", transport=None, *, health=None, driver=None, **kwargs):
    validator = jsonschema.Draft202012Validator(OUTPUT_SCHEMA)

    def validate(body):
        errors = sorted(validator.iter_errors(body), key=lambda e: list(e.path))
        return errors[0].message if errors else None

    return ReferenceAdapter(
        engine,
        transport=transport,
        driver=driver or make_driver(),
        health=health or InMemoryHealth(),
        validate=validate,
        **kwargs,
    )


class SchemaProjectionTests(unittest.TestCase):
    def test_committed_schema_is_the_generated_schema(self) -> None:
        expected = schema_mod.render(schema_mod.build_schema())
        actual = schema_mod.CONTRACT_PATH.read_text(encoding="utf-8")
        self.assertEqual(expected, actual, "run: python3 -X utf8 -B -m scripts.v3.route_contract.schema --write")

    def test_schema_is_valid_draft_2020_12(self) -> None:
        jsonschema.Draft202012Validator.check_schema(schema_mod.load_schema())

    def test_frozen_shapes_are_projected_verbatim(self) -> None:
        frozen = json.loads(schema_mod.FROZEN_PATH.read_text(encoding="utf-8"))["$defs"]
        defs = contract_defs()
        for name in schema_mod.PROJECTED:
            self.assertEqual(frozen[name], defs[name], f"{name} must equal the frozen M1B shape")
        copied = set(defs) - set(schema_mod._new_defs())
        self.assertTrue(set(schema_mod.PROJECTED) <= copied)
        for name in copied:
            self.assertEqual(frozen[name], defs[name], f"{name} drifted from the frozen schema")

    def test_frozen_fixture_records_validate_the_same_way(self) -> None:
        names = set(schema_mod.PROJECTED)
        frozen_defs = json.loads(schema_mod.FROZEN_PATH.read_text(encoding="utf-8"))["$defs"]
        seen: set[str] = set()

        def frozen_valid(name: str, record: object) -> bool:
            return jsonschema.Draft202012Validator(
                {"$ref": f"#/$defs/{name}", "$defs": frozen_defs}
            ).is_valid(record)

        def walk(node: object) -> None:
            if isinstance(node, dict):
                kind = node.get("type")
                if isinstance(kind, str) and kind.endswith("@1") and kind[:-2] in names:
                    name = kind[:-2]
                    seen.add(name)
                    self.assertEqual(frozen_valid(name, node), is_valid(name, node), name)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        for path in sorted(FROZEN_FIXTURES.glob("F-M1B-*.json")):
            walk(json.loads(path.read_text(encoding="utf-8")))
        self.assertIn("RouteCapabilityDescriptor", seen)
        self.assertIn("RouteRequirement", seen)

    def test_no_property_can_carry_secret_material(self) -> None:
        banned = ("secret", "password", "api_key", "apikey", "bearer", "material", "raw_output", "prompt")
        for name in schema_mod._new_defs():
            props = contract_defs()[name].get("properties", {})
            for prop in props:
                if prop in {"prompt_channel", "prompt_digest", "raw_output_retained"}:
                    continue
                self.assertFalse(any(word in prop.lower() for word in banned), f"{name}.{prop}")

    def test_request_record_validates_and_carries_digests_only(self) -> None:
        request = make_request(cwd="/work/dir", cwd_ref="sandbox-a")
        record = request.to_record()
        assert_valid(self, "RouteExecutionRequest", record)
        blob = json.dumps(record)
        self.assertNotIn(request.prompt, blob)
        self.assertNotIn(CANARY, blob)
        self.assertNotIn("/work/dir", blob)
        self.assertEqual(sha256_digest(request.prompt), record["prompt_digest"])

    def test_request_record_refuses_tools_secret_fields_and_argv_prompts(self) -> None:
        good = make_request().to_record()
        mutations = {
            "tools granted": lambda r: r.update(allowed_tools=["Bash"]),
            "other tool profile": lambda r: r.update(tool_profile="allowlist"),
            "prompt on argv": lambda r: r.update(prompt_channel="argv"),
            "unknown engine": lambda r: r.update(engine_id="grok-build"),
            "secret in credential ref": lambda r: r["credential_ref"].update(secret="x"),
            "raw output retained": lambda r: r["output"].update(raw_output_retained=True),
            "no repair bound": lambda r: r["output"].update(repair_attempts=2),
            "non-json output": lambda r: r["output"].update(format="text"),
            "extra field": lambda r: r.update(prompt="x"),
            "bad digest": lambda r: r.update(prompt_digest="sha256:zz"),
            "session separator": lambda r: r["session"].update(session="a/b"),
        }
        for label, mutate in mutations.items():
            record = json.loads(json.dumps(good))
            mutate(record)
            self.assertFalse(is_valid("RouteExecutionRequest", record), label)

    def test_result_invariants(self) -> None:
        ok = {
            "type": "RouteExecutionResult@0", "request_id": "req-1", "status": "ok", "error": None,
            "dispatched": True, "repaired": False, "usage_state": "ok", "output_digest": DIGEST,
            "validated": {"label": "a"}, "health_event": None, "termination": None,
        }
        assert_valid(self, "RouteExecutionResult", ok)
        event = {"type": "RouteHealthEvent@0", "engine_id": "claude", "state": "exhausted", "error": "timeout"}
        bad = {
            "ok with an error": {**ok, "error": "timeout"},
            "ok without a payload": {**ok, "validated": None},
            "ok with a health event": {**ok, "health_event": event},
            "failure with a payload": {**ok, "status": "limit_hit", "error": "limit_or_error",
                                       "usage_state": "exhausted", "health_event": event},
            "timeout without a tree observation": {**ok, "status": "timeout", "error": "timeout",
                                                   "validated": None, "usage_state": "exhausted",
                                                   "health_event": event},
            "unknown error token": {**ok, "status": "error", "error": "mystery", "validated": None},
            "undispatched fault carrying a health event": {
                **ok, "status": "limit_hit", "error": "breaker_open", "dispatched": False,
                "validated": None, "usage_state": "exhausted", "health_event": event},
        }
        for label, record in bad.items():
            self.assertFalse(is_valid("RouteExecutionResult", record), label)

    def test_adapter_support_invariants(self) -> None:
        live = {"type": "AdapterSupport@0", "engine_id": "claude", "adapter_kind": "live",
                "state": "unsupported", "reason": "awaiting-csl-460", "live_calls": False}
        assert_valid(self, "AdapterSupport", live)
        self.assertFalse(is_valid("AdapterSupport", {**live, "state": "supported"}))
        self.assertFalse(is_valid("AdapterSupport", {**live, "reason": "fake-driver"}))
        self.assertFalse(is_valid("AdapterSupport", {**live, "live_calls": True}))
        fake = {**live, "adapter_kind": "fake", "state": "supported", "reason": "fake-driver"}
        assert_valid(self, "AdapterSupport", fake)
        self.assertFalse(is_valid("AdapterSupport", {**fake, "state": "unsupported"}))


class ZeroToolsArgvOracleTests(unittest.TestCase):
    def test_argv_matches_the_oracle_byte_for_byte(self) -> None:
        claude = argv_mod.build_argv("claude", cli_model=MODEL, max_turns=6)
        codex = argv_mod.build_argv("codex", cli_model=MODEL)
        codex_schema = argv_mod.build_argv("codex", cli_model=MODEL, schema_path="schema.json")
        self.assertEqual(VECTORS["argv"]["claude"], claude)
        self.assertEqual(VECTORS["argv"]["codex_no_schema_path"], codex)
        self.assertEqual(VECTORS["argv"]["codex_schema_path"], codex_schema)

    def test_inspector_agrees_with_the_oracle_on_every_vector(self) -> None:
        self.assertGreaterEqual(len(VECTORS["inspect"]), 17)
        for name, case in VECTORS["inspect"].items():
            try:
                argv_mod.inspect_argv(case["argv"])
                refused = False
            except ToolPolicyRefused:
                refused = True
            if name in STRICTER_THAN_ORACLE:
                self.assertFalse(case["refused"], f"{name}: oracle accepts")
                self.assertTrue(refused, f"{name}: contract must refuse")
            else:
                self.assertEqual(case["refused"], refused, name)

    def test_oracle_refusals_include_every_tool_granting_class(self) -> None:
        refused = {name for name, case in VECTORS["inspect"].items() if case["refused"]}
        for required in (
            "claude_grant_bash", "claude_mcp_config", "claude_skip_permissions",
            "claude_add_dir", "claude_system_prompt", "claude_resume",
            "codex_enable_feature", "codex_missing_disable", "codex_full_sandbox", "codex_mcp",
            "unknown_runtime",
        ):
            self.assertIn(required, refused)

    def test_tool_grants_never_reach_the_transport(self) -> None:
        transport = ScriptedTransport([])
        adapter = make_adapter(transport=transport)
        with self.assertRaises(ToolPolicyRefused):
            adapter.execute(make_request(allowed_tools=("Bash",)))
        self.assertEqual([], transport.calls)
        self.assertFalse(adapter._driver.owner.is_busy(make_request().session))

    def test_prompt_travels_on_stdin_only(self) -> None:
        prompt = "do not leak: UNIQUE-PROMPT-MARKER"
        transport = ScriptedTransport([proc(_ok_envelope())])
        make_adapter(transport=transport).execute(make_request(prompt=prompt))
        call = transport.calls[0]
        self.assertEqual(prompt, call["input"])
        self.assertFalse(any("UNIQUE-PROMPT-MARKER" in token for token in call["argv"]))
        self.assertEqual(VECTORS["argv"]["claude"], call["argv"])

    def test_pinned_cwd_is_enforced_before_spawn(self) -> None:
        transport = ScriptedTransport([proc(_ok_envelope())])
        adapter = make_adapter(transport=transport, pinned_cwd="/pinned")
        with self.assertRaises(CwdRefused):
            adapter.execute(make_request(cwd="/elsewhere"))
        with self.assertRaises(CwdRefused):
            adapter.execute(make_request(cwd=None))
        self.assertEqual([], transport.calls)
        adapter.execute(make_request(cwd="/pinned"))
        self.assertEqual(1, len(transport.calls))


def _ok_envelope() -> dict:
    body = json.dumps(VECTORS["good"])
    return {
        "returncode": 0,
        "stdout": json.dumps({"session_id": "s1", "result": body}),
        "stderr": "",
        "timed_out": False,
    }


class SecretRefusalTests(unittest.TestCase):
    def test_oracle_vectors(self) -> None:
        for name, expected in VECTORS["secret"].items():
            transport = ScriptedTransport([proc(_ok_envelope())])
            prompt = "key is sk-LEAK123456 ok" if name == "in_prompt" else "nothing here"
            secrets = {"in_prompt": ["sk-LEAK123456"], "absent": ["sk-LEAK123456"],
                       "empty_secret_ignored": [""], "no_secrets": None}[name]
            adapter = make_adapter(transport=transport, secret_values=secrets)
            if expected["refused"]:
                with self.assertRaises(SecretRefused):
                    adapter.execute(make_request(prompt=prompt))
            else:
                adapter.execute(make_request(prompt=prompt))
            self.assertEqual(expected["transport_calls"], len(transport.calls), name)

    def test_canary_in_prompt_is_refused_and_leaves_no_trace(self) -> None:
        driver = make_driver()
        transport = ScriptedTransport([proc(_ok_envelope())])
        adapter = make_adapter(transport=transport, driver=driver, secret_values=lambda: [CANARY])
        request = make_request(prompt=f"please use {CANARY}")
        with self.assertRaises(SecretRefused) as caught:
            adapter.execute(request)
        self.assertEqual("secret_refused", str(caught.exception))
        self.assertEqual([], transport.calls)
        record = caught.exception.to_record(request.request_id)
        assert_valid(self, "RouteRefusal", record)
        blob = json.dumps([record, driver.snapshot(), str(caught.exception), request.to_record()])
        self.assertNotIn(CANARY, blob)
        self.assertEqual([], driver.snapshot()["sessions"], "refusal precedes session start")

    def test_canary_in_argv_is_refused(self) -> None:
        transport = ScriptedTransport([proc(_ok_envelope())])
        adapter = make_adapter(transport=transport, secret_values=[MODEL])
        with self.assertRaises(SecretRefused):
            adapter.execute(make_request())
        self.assertEqual([], transport.calls)

    def test_secret_values_are_read_per_call(self) -> None:
        values = ["old-secret"]
        transport = ScriptedTransport([proc(_ok_envelope()), proc(_ok_envelope())])
        adapter = make_adapter(transport=transport, secret_values=lambda: list(values))
        adapter.execute(make_request(prompt="carries rotated-secret"))
        values[:] = ["rotated-secret"]
        with self.assertRaises(SecretRefused):
            adapter.execute(make_request(prompt="carries rotated-secret"))
        self.assertEqual(1, len(transport.calls))

    def test_repair_prompt_is_checked_too(self) -> None:
        bad = {"returncode": 0, "stdout": json.dumps({"result": json.dumps({"label": CANARY, "score": 1})}),
               "stderr": "", "timed_out": False}
        transport = ScriptedTransport([proc(bad), proc(_ok_envelope())])
        driver = make_driver()
        adapter = make_adapter(transport=transport, driver=driver, secret_values=[CANARY])
        request = make_request()
        with self.assertRaises(SecretRefused):
            adapter.execute(request)
        self.assertEqual(1, len(transport.calls), "the repair call must not spawn")
        self.assertFalse(driver.owner.is_busy(request.session), "session released after refusal")

    def test_custody_refuses_unknown_and_cross_tenant_refs(self) -> None:
        transport = ScriptedTransport([proc(_ok_envelope())])
        adapter = make_adapter(transport=transport, driver=make_driver(register=False))
        with self.assertRaises(RouteRefusal) as caught:
            adapter.execute(make_request())
        self.assertEqual("custody_denied", caught.exception.code)
        adapter = make_adapter(transport=transport)
        with self.assertRaises(RouteRefusal) as caught:
            adapter.execute(make_request(credential=credential(tenant="tenant-b")))
        self.assertEqual("custody_denied", caught.exception.code)
        self.assertEqual([], transport.calls)


class HealthInjectionTests(unittest.TestCase):
    def run_case(self, case: dict, *, health=None):
        engine = ENGINE_FOR_CASE[case["runtime"]]
        health = health or InMemoryHealth()
        transport = ScriptedTransport([proc(item) for item in case["script"]])
        request = make_request(engine, prompt=case["prompt"])
        adapter = make_adapter(engine, transport, health=health)
        result = adapter.execute(request)
        return engine, request, transport, health, result

    def test_every_oracle_health_vector(self) -> None:
        self.assertGreaterEqual(len(VECTORS["health"]), 20)
        for name, case in VECTORS["health"].items():
            expect = case["expect"]
            engine, request, transport, health, result = self.run_case(case)
            with self.subTest(name):
                self.assertEqual(expect["status"], result.status)
                self.assertEqual(expect["error"], result.error)
                self.assertEqual(expect["usage_state"], result.usage_state)
                self.assertEqual(expect["repaired"], result.repaired)
                self.assertEqual(expect["dispatched"], result.dispatched)
                self.assertEqual(expect["validated"], result.validated)
                self.assertEqual(expect["session_id"], result.session_id)
                self.assertEqual(expect["transport_calls"], len(transport.calls))
                self.assertEqual([list(f) for f in health.faults], expect["faults"])
                self.assertEqual(expect["used"], health.used(engine))
                assert_valid(self, "RouteExecutionResult", result.to_record(request))

    def test_faults_never_return_output_and_open_the_breaker(self) -> None:
        for name, case in VECTORS["health"].items():
            if not case["expect"]["faults"]:
                continue
            engine, _, _, health, result = self.run_case(case)
            with self.subTest(name):
                self.assertNotEqual("ok", result.status)
                self.assertIsNone(result.validated)
                self.assertTrue(health.is_open(engine))
                self.assertEqual(result.health_event["state"], result.usage_state)
                self.assertEqual(result.health_event["error"], result.error)

    def test_breaker_open_refuses_without_spawning(self) -> None:
        expect = VECTORS["breaker_open"]
        health = InMemoryHealth()
        first = ScriptedTransport([ProcResult(1, "", "boom")])
        adapter = make_adapter(transport=first, health=health)
        adapter.execute(make_request())
        second = ScriptedTransport([proc(_ok_envelope())])
        adapter = make_adapter(transport=second, health=health)
        request = make_request()
        result = adapter.execute(request)
        self.assertEqual(
            (expect["status"], expect["error"], expect["usage_state"], expect["dispatched"]),
            (result.status, result.error, result.usage_state, result.dispatched),
        )
        self.assertEqual(expect["transport_calls"], len(second.calls))
        self.assertEqual([list(f) for f in health.faults], expect["faults"])
        assert_valid(self, "RouteExecutionResult", result.to_record(request))

    def test_health_port_is_injected_not_owned(self) -> None:
        class Spy:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def is_open(self, engine_id):
                self.calls.append(("is_open", engine_id))
                return False

            def record_ok(self, engine_id):
                self.calls.append(("record_ok", engine_id))

            def state(self, engine_id):
                return "ok"

            def fault(self, engine_id, state, error):
                self.calls.append(("fault", engine_id, state, error))
                return "exhausted"

        spy = Spy()
        transport = ScriptedTransport([ProcResult(124, "", "\n[timeout]", True)])
        result = make_adapter(transport=transport, health=spy).execute(make_request())
        self.assertEqual(("fault", "claude", "exhausted", "timeout"), spy.calls[-1])
        self.assertEqual(1, sum(1 for call in spy.calls if call[0] == "fault"))
        self.assertEqual("exhausted", result.usage_state)
        spy.calls.clear()
        make_adapter(transport=ScriptedTransport([proc(_ok_envelope())]), health=spy).execute(make_request())
        self.assertIn(("record_ok", "claude"), spy.calls)

    def test_model_text_cannot_trip_the_breaker(self) -> None:
        case = VECTORS["health"]["model_text_mentions_limit_words"]
        engine, _, _, health, result = self.run_case(case)
        self.assertEqual("ok", result.status)
        self.assertFalse(health.is_open(engine))

    def test_engine_channels_vs_model_channel(self) -> None:
        for name in ("stderr_rate_limit", "stdout_raw_limit_text", "nonzero_exit"):
            _, _, _, health, result = self.run_case(VECTORS["health"][name])
            self.assertEqual(("limit_hit", "limit_or_error"), (result.status, result.error), name)
            self.assertTrue(health.is_open("claude"), name)


_HELPER_TREE = textwrap.dedent(
    """
    import os, subprocess, sys, time
    pid_file = sys.argv[1]
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(pid_file, "w") as handle:
        handle.write(str(child.pid))
    sys.stdin.read()
    time.sleep(60)
    """
)
_HELPER_OK = textwrap.dedent(
    """
    import json, sys
    sys.stdin.read()
    print(json.dumps({"session_id": "s-local", "result": json.dumps({"label": "b", "score": 2})}))
    """
)


class LocalHelperTransport:
    """Rewrites provider argv to a local helper. The real kill path still runs."""

    def __init__(self, helper_args: list[str]) -> None:
        self.helper_args = helper_args
        self.inner = SubprocessTransport()
        self.seen_argv: list[list[str]] = []

    def run(self, argv, input_text, timeout_s, cwd):
        self.seen_argv.append(list(argv))
        return self.inner.run([sys.executable, *self.helper_args], input_text, timeout_s, cwd)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@unittest.skipUnless(os.name == "posix", "process-group kill is asserted on POSIX; Windows taskkill path is unverified")
class TreeKillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)

    def write(self, name: str, text: str) -> str:
        path = self.tmpdir / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_transport_timeout_kills_the_whole_tree(self) -> None:
        helper = self.write("tree.py", _HELPER_TREE)
        pid_file = str(self.tmpdir / "child.pid")
        started = time.monotonic()
        result = SubprocessTransport().run([sys.executable, helper, pid_file], "", 1.5, None)
        elapsed = time.monotonic() - started
        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.returncode)
        self.assertLess(elapsed, 1.5 + 10.0, "a surviving descendant must not hold the pipes open")
        child = int(Path(pid_file).read_text(encoding="utf-8"))
        self.assertFalse(pid_alive(child), "the grandchild must be dead")
        observation = result.termination
        self.assertEqual("terminated", observation["tree_status"])
        self.assertEqual([], observation["remaining_process_refs"])
        self.assertEqual("not-evaluated", observation["effect_conclusion"])

    def test_adapter_timeout_injects_health_and_reports_the_tree(self) -> None:
        helper = self.write("tree.py", _HELPER_TREE)
        pid_file = str(self.tmpdir / "child.pid")
        transport = LocalHelperTransport([helper, pid_file])
        health = InMemoryHealth()
        driver = make_driver()
        adapter = make_adapter(transport=transport, health=health, driver=driver)
        request = make_request(timeout_s=1.5)
        result = adapter.execute(request)
        self.assertEqual(("timeout", "timeout"), (result.status, result.error))
        self.assertEqual([("claude", "exhausted", "timeout")], health.faults)
        self.assertFalse(pid_alive(int(Path(pid_file).read_text(encoding="utf-8"))))
        record = result.to_record(request)
        assert_valid(self, "RouteExecutionResult", record)
        self.assertEqual("terminated", record["termination"]["tree_status"])
        self.assertEqual("not-evaluated", record["termination"]["effect_conclusion"])
        self.assertFalse(driver.owner.is_busy(request.session), "session released after the kill")
        again = adapter.execute(make_request(timeout_s=1.5))
        self.assertEqual("breaker_open", again.error)
        self.assertEqual(VECTORS["argv"]["claude"], transport.seen_argv[0])

    def test_real_process_success_path(self) -> None:
        helper = self.write("ok.py", _HELPER_OK)
        transport = LocalHelperTransport([helper])
        result = make_adapter(transport=transport).execute(make_request())
        self.assertEqual("ok", result.status)
        self.assertEqual({"label": "b", "score": 2}, result.validated)

    def test_partial_tree_is_reported_not_hidden(self) -> None:
        class Stubborn:
            pid = 4242

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("x", timeout)

            def kill(self):
                pass

        from scripts.v3.route_contract import transport as transport_mod

        with mock.patch.object(transport_mod.os, "getpgid", return_value=4242), \
                mock.patch.object(transport_mod.os, "killpg"), \
                mock.patch.object(transport_mod, "_group_alive", return_value=True), \
                mock.patch.object(transport_mod, "TREE_POLL_SECONDS", 0.05):
            observation = transport_mod.kill_tree(Stubborn())
        self.assertEqual("partial", observation["tree_status"])
        self.assertEqual(["pgid:4242"], observation["remaining_process_refs"])


class SessionOwnershipTests(unittest.TestCase):
    def test_second_request_on_a_held_session_is_busy(self) -> None:
        driver = make_driver()
        outcome: dict = {}

        class Reentrant:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, argv, input_text, timeout_s, cwd):
                self.calls += 1
                try:
                    make_adapter(transport=ScriptedTransport([]), driver=driver).execute(make_request())
                except RouteRefusal as refusal:
                    outcome["code"] = refusal.code
                return proc(_ok_envelope())

        make_adapter(transport=Reentrant(), driver=driver).execute(make_request())
        self.assertEqual("busy", outcome["code"])
        self.assertFalse(driver.owner.is_busy(make_request().session))

    def test_other_sessions_are_independent_and_release_on_every_outcome(self) -> None:
        driver = make_driver()
        for index, script in enumerate(
            [[proc(_ok_envelope())], [ProcResult(1, "", "boom")], [ProcResult(124, "", "", True)]]
        ):
            session = SessionKey("tenant-a", "route-fake", f"session-{index}")
            request = make_request(session=session)
            make_adapter(transport=ScriptedTransport(script), driver=driver).execute(request)
            self.assertFalse(driver.owner.is_busy(session), index)

    def test_prompt_body_is_not_stored_in_driver_state(self) -> None:
        driver = make_driver()
        make_adapter(transport=ScriptedTransport([proc(_ok_envelope())]), driver=driver).execute(
            make_request(prompt="UNIQUE-BODY-MARKER")
        )
        self.assertNotIn("UNIQUE-BODY-MARKER", json.dumps(driver.snapshot()))
        self.assertNotIn(CANARY, json.dumps(driver.snapshot()))
        kinds = [e["kind"] for e in driver.snapshot()["sessions"][0]["events"]]
        self.assertEqual(["started", "sent", "completed"], kinds)


class LiveAdaptersUnsupportedTests(unittest.TestCase):
    def test_live_enablement_is_a_constant_that_is_off(self) -> None:
        self.assertIs(False, live_mod.LIVE_ADAPTERS_ENABLED)

    def test_every_live_adapter_reports_unsupported_until_csl_460(self) -> None:
        self.assertGreaterEqual(len(live_mod.LIVE_ENGINE_IDS), 5)
        for engine_id in live_mod.LIVE_ENGINE_IDS:
            adapter = live_mod.live_adapter(engine_id)
            support = adapter.support()
            assert_valid(self, "AdapterSupport", support)
            self.assertEqual(("live", "unsupported", "awaiting-csl-460", False),
                             (support["adapter_kind"], support["state"], support["reason"],
                              support["live_calls"]), engine_id)
            with self.assertRaises(Unsupported):
                adapter.execute(make_request())

    def test_the_fake_adapter_is_the_only_supported_kind(self) -> None:
        support = make_adapter(transport=ScriptedTransport([])).support()
        assert_valid(self, "AdapterSupport", support)
        self.assertEqual(("fake", "supported", False),
                         (support["adapter_kind"], support["state"], support["live_calls"]))

    def test_unknown_engines_cannot_be_given_an_adapter(self) -> None:
        with self.assertRaises(Unsupported):
            make_adapter("grok-build", ScriptedTransport([]))

    def test_the_real_transport_refuses_provider_executables_without_spawning(self) -> None:
        for binary in sorted(live_mod.LIVE_ENGINE_BINARIES):
            with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("spawned")):
                with self.assertRaises(Unsupported):
                    SubprocessTransport().run([binary, "-p"], "", 1.0, None)

    def test_provider_cli_through_the_real_transport_is_unsupported_and_releases_the_session(self) -> None:
        driver = make_driver()
        request = make_request()
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("spawned")):
            with self.assertRaises(Unsupported):
                make_adapter(transport=SubprocessTransport(), driver=driver).execute(request)
        self.assertFalse(driver.owner.is_busy(request.session))


class StaticBoundaryTests(unittest.TestCase):
    ALLOWED_IMPORTS = {
        "__future__", "collections", "dataclasses", "hashlib", "json", "os", "pathlib", "re",
        "shutil", "signal", "subprocess", "sys", "threading", "time", "typing", "scripts",
    }

    def sources(self) -> list[Path]:
        return sorted(CONTRACT_DIR.glob("*.py"))

    def test_only_standard_library_and_the_adapter_package_are_imported(self) -> None:
        for path in self.sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules = [node.module]
                for module in modules:
                    top = module.split(".")[0]
                    self.assertIn(top, self.ALLOWED_IMPORTS, f"{path.name}: {module}")
                    if top == "scripts":
                        self.assertTrue(
                            module.startswith(("scripts.v3.route_contract", "scripts.v3.route_adapter")),
                            f"{path.name}: {module}",
                        )

    def test_subprocess_is_confined_to_the_transport_module(self) -> None:
        for path in self.sources():
            if path.name != "transport.py":
                self.assertNotIn("import subprocess", path.read_text(encoding="utf-8"), path.name)

    def test_contract_never_writes_the_process_environment(self) -> None:
        for path in self.sources():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("os.environ[", text, path.name)
            self.assertNotIn("putenv", text, path.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
