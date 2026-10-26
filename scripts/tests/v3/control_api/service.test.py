#!/usr/bin/env python3
"""Control API core: authentication, tenant isolation, protocol and run conformance.

Provider-free. The adapters are the Route Contract v0 reference adapter over the
AT1 fake driver, fed by scripted in-process transports. No socket, no process.
"""

from __future__ import annotations

import ast
import json
import sys
import threading
import unittest
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v3.control_api import errors as err  # noqa: E402
from scripts.v3.control_api.auth import (  # noqa: E402
    SCOPE_CONTROL,
    SCOPE_OBSERVE,
    TokenRegistry,
    parse_bearer,
)
from scripts.v3.control_api.events import EventJournal  # noqa: E402
from scripts.v3.control_api.service import ControlService, make_validator  # noqa: E402
from scripts.v3.route_adapter.credentials import CredentialRef, LocalCredentialResolver  # noqa: E402
from scripts.v3.route_adapter.fake import FakeRouteDriver  # noqa: E402
from scripts.v3.route_adapter.session import SessionOwner  # noqa: E402
from scripts.v3.route_contract import schema as contract_schema  # noqa: E402
from scripts.v3.route_contract.executor import ReferenceAdapter  # noqa: E402
from scripts.v3.route_contract.health import InMemoryHealth  # noqa: E402
from scripts.v3.route_contract.transport import ProcResult  # noqa: E402

PACKAGE = ROOT / "scripts" / "v3" / "control_api"
CANARY = "canary-secret-CA0-7b2f19"
OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict"],
    "properties": {"verdict": {"enum": ["pass", "fail"]}},
}
OK_STDOUT = json.dumps({"session_id": "s1", "result": json.dumps({"verdict": "pass"})})


class StubTransport:
    def __init__(self, stdout: str = OK_STDOUT, *, gate: threading.Event | None = None) -> None:
        self.stdout = stdout
        self.gate = gate
        self.entered = threading.Event()
        self.calls = 0

    def run(self, argv, input_text, timeout_s, cwd, max_output_bytes=None):
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(10)
        return ProcResult(0, self.stdout, "", False)


def contract_validator(name: str) -> jsonschema.Draft202012Validator:
    defs = contract_schema.load_schema()["$defs"]
    return jsonschema.Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": defs})


class Harness:
    def __init__(self, *, transport: StubTransport | None = None, capacity: int = 4096) -> None:
        self.transport = transport or StubTransport()
        resolver = LocalCredentialResolver()
        for tenant in ("tenant-a", "tenant-b"):
            resolver.register(CredentialRef(tenant, "fake", "route-ref"), CANARY)
        driver = FakeRouteDriver(owner=SessionOwner(), resolver=resolver)
        adapter = ReferenceAdapter(
            "claude",
            transport=self.transport,
            driver=driver,
            health=InMemoryHealth(),
            validate=make_validator(OUTPUT_SCHEMA),
            secret_values=[CANARY],
        )
        self.tokens = TokenRegistry()
        self.control_a = self.tokens.issue("op-a", "tenant-a", {SCOPE_CONTROL, SCOPE_OBSERVE})
        self.observe_a = self.tokens.issue("viewer-a", "tenant-a", {SCOPE_OBSERVE})
        self.control_only_a = self.tokens.issue("ctl-a", "tenant-a", {SCOPE_CONTROL})
        self.control_b = self.tokens.issue("op-b", "tenant-b", {SCOPE_CONTROL, SCOPE_OBSERVE})
        self.journal = EventJournal(capacity)
        self.service = ControlService(
            adapters={"claude": adapter},
            tokens=self.tokens,
            output_schema=OUTPUT_SCHEMA,
            credential_provider="fake",
            credential_name="route-ref",
            cli_model="fake-model",
            journal=self.journal,
        )

    def call(self, token: str | None, method: str, params: dict | None = None, **extra):
        message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        message.update(extra)
        header = None if token is None else f"Bearer {token}"
        return self.service.handle(json.dumps(message), header)

    def ok(self, token: str, method: str, params: dict | None = None):
        response = self.call(token, method, params)
        assert "error" not in response, response
        return response["result"]


def code_of(response: dict) -> int:
    return response["error"]["code"]


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def test_every_method_needs_a_valid_token(self) -> None:
        for method in ("service.info", "route.list", "session.get", "events.poll", "session.run"):
            for header in (None, "", "Bearer", "Bearer ", "Basic abc", "bearer x", "Token abc"):
                response = self.h.service.handle(
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}), header
                )
                self.assertEqual(err.UNAUTHENTICATED, code_of(response), (method, header))

    def test_wrong_and_revoked_tokens_are_refused(self) -> None:
        for token in ("x" * 43, self.h.control_a[:-1] + "A" * (1 if self.h.control_a[-1] != "A" else 2)):
            self.assertEqual(err.UNAUTHENTICATED, code_of(self.h.call(token, "service.info")))
        self.assertEqual(1, self.h.tokens.revoke("op-a"))
        self.assertEqual(err.UNAUTHENTICATED, code_of(self.h.call(self.h.control_a, "service.info")))

    def test_header_shapes_are_strict(self) -> None:
        good = self.h.control_a
        for header in (f"Bearer  {good}", f"Bearer {good} ", f"Bearer {good}\n", f"Bearer {good}x" * 20):
            with self.assertRaises(err.Unauthenticated, msg=repr(header)):
                parse_bearer(header)
        self.assertEqual(good, parse_bearer(f"BEARER {good}"))

    def test_authentication_precedes_parsing(self) -> None:
        for body in (b"{not json", b"[]", b"\xff\xfe", b"x" * (70 * 1024)):
            response = self.h.service.handle(body, None)
            self.assertEqual(err.UNAUTHENTICATED, code_of(response))
            self.assertIn(
                response["error"]["message"], {"missing_credentials", "malformed_credentials"}
            )

    def test_scopes_are_enforced_per_method(self) -> None:
        self.h.ok(self.h.control_a, "session.open", {"engine": "claude", "session": "s-one"})
        denied = [
            (self.h.observe_a, "session.open", {"engine": "claude", "session": "s-two"}),
            (self.h.observe_a, "session.run", {"engine": "claude", "session": "s-one", "prompt": "p"}),
            (self.h.observe_a, "session.close", {"engine": "claude", "session": "s-one"}),
            (self.h.control_only_a, "events.poll", {"after": 0}),
            (self.h.control_only_a, "session.get", {"engine": "claude", "session": "s-one"}),
            (self.h.control_only_a, "route.list", {}),
        ]
        for token, method, params in denied:
            response = self.h.call(token, method, params)
            self.assertEqual(err.FORBIDDEN, code_of(response), method)
            self.assertEqual("scope_denied", response["error"]["message"])
        self.assertEqual("open", self.h.ok(self.h.observe_a, "session.get", {"engine": "claude", "session": "s-one"})["state"])

    def test_token_registry_never_stores_or_prints_a_token(self) -> None:
        text = repr(self.h.tokens) + repr(vars(self.h.tokens))
        for token in (self.h.control_a, self.h.observe_a, self.h.control_b):
            self.assertNotIn(token, text)
        with self.assertRaises(ValueError):
            self.h.tokens.issue("bad id", "tenant-a", {SCOPE_CONTROL})
        with self.assertRaises(ValueError):
            self.h.tokens.issue("ok-id", "tenant-a", {"admin"})
        with self.assertRaises(ValueError):
            self.h.tokens.issue("ok-id", "tenant-a", set())


class TenantIsolationTests(unittest.TestCase):
    def test_tenants_do_not_see_each_others_sessions_or_events(self) -> None:
        h = Harness()
        h.ok(h.control_a, "session.open", {"engine": "claude", "session": "shared-name"})
        h.ok(h.control_a, "session.run", {"engine": "claude", "session": "shared-name", "prompt": "hello"})
        response = h.call(h.control_b, "session.get", {"engine": "claude", "session": "shared-name"})
        self.assertEqual(err.UNKNOWN_SESSION, code_of(response))
        self.assertEqual([], h.ok(h.control_b, "events.poll", {"after": 0})["events"])
        h.ok(h.control_b, "session.open", {"engine": "claude", "session": "shared-name"})
        seen_a = h.ok(h.control_a, "events.poll", {"after": 0})["events"]
        self.assertEqual(["session.opened", "run.started", "run.finished"], [e["kind"] for e in seen_a])
        self.assertEqual(0, h.ok(h.control_b, "session.get", {"engine": "claude", "session": "shared-name"})["runs"])

    def test_tenant_comes_from_the_token_never_from_params(self) -> None:
        h = Harness()
        response = h.call(
            h.control_a,
            "session.open",
            {"engine": "claude", "session": "s-one", "tenant": "tenant-b"},
        )
        self.assertEqual(err.INVALID_PARAMS, code_of(response))
        self.assertEqual("tenant-a", h.ok(h.control_a, "service.info")["tenant"])


class ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def raw(self, body: bytes | str) -> dict:
        return self.h.service.handle(body, f"Bearer {self.h.control_a}")

    def test_malformed_envelopes_are_refused_with_fixed_tokens(self) -> None:
        cases = {
            "not json": (b"{nope", err.PARSE_ERROR),
            "invalid utf8": (b"\xff\xfe", err.PARSE_ERROR),
            "nan constant": (b'{"jsonrpc":"2.0","id":1,"method":"service.info","params":{"x":NaN}}', err.PARSE_ERROR),
            "batch": (b"[]", err.INVALID_REQUEST),
            "scalar": (b"1", err.INVALID_REQUEST),
            "wrong version": (b'{"jsonrpc":"1.0","id":1,"method":"service.info"}', err.INVALID_REQUEST),
            "no version": (b'{"id":1,"method":"service.info"}', err.INVALID_REQUEST),
            "extra key": (b'{"jsonrpc":"2.0","id":1,"method":"service.info","admin":true}', err.INVALID_REQUEST),
            "bool id": (b'{"jsonrpc":"2.0","id":true,"method":"service.info"}', err.INVALID_REQUEST),
            "object id": (b'{"jsonrpc":"2.0","id":{},"method":"service.info"}', err.INVALID_REQUEST),
            "method not string": (b'{"jsonrpc":"2.0","id":1,"method":7}', err.INVALID_REQUEST),
            "params not object": (b'{"jsonrpc":"2.0","id":1,"method":"service.info","params":[]}', err.INVALID_REQUEST),
            "unknown method": (b'{"jsonrpc":"2.0","id":1,"method":"admin.shell"}', err.METHOD_NOT_FOUND),
            "too large": (b" " * (65 * 1024), err.TOO_LARGE),
        }
        for label, (body, code) in cases.items():
            response = self.raw(body)
            self.assertEqual(code, code_of(response), label)
            self.assertIn(response["error"]["message"], {
                "parse_error", "invalid_request", "batch_unsupported", "method_not_found", "body_too_large",
            }, label)

    def test_params_are_a_closed_typed_set(self) -> None:
        self.h.ok(self.h.control_a, "session.open", {"engine": "claude", "session": "s-one"})
        bad = [
            ("session.open", {"engine": "claude"}),
            ("session.open", {"engine": "claude", "session": "s-one", "x": 1}),
            ("session.open", {"engine": 3, "session": "s-one"}),
            ("session.open", {"engine": "grok", "session": "s-one"}),
            ("session.open", {"engine": "claude", "session": "Bad_Id"}),
            ("session.open", {"engine": "claude", "session": "a/b"}),
            ("session.open", {"engine": "claude", "session": "x" * 80}),
            ("session.run", {"engine": "claude", "session": "s-one", "prompt": ""}),
            ("session.run", {"engine": "claude", "session": "s-one", "prompt": "p" * (33 * 1024)}),
            ("session.run", {"engine": "claude", "session": "s-one", "prompt": "p", "timeout_s": True}),
            ("session.run", {"engine": "claude", "session": "s-one", "prompt": "p", "timeout_s": 0}),
            ("session.run", {"engine": "claude", "session": "s-one", "prompt": "p", "timeout_s": 9999}),
            ("session.run", {"engine": "claude", "session": "s-one", "prompt": "p", "allowed_tools": ["Bash"]}),
            ("session.run", {"engine": "claude", "session": "s-one", "prompt": "p", "cwd": "/"}),
            ("events.poll", {"after": -1}),
            ("events.poll", {"after": "0"}),
            ("events.poll", {"after": True}),
            ("service.info", {"verbose": True}),
        ]
        for method, params in bad:
            response = self.h.call(self.h.control_a, method, params)
            self.assertEqual(err.INVALID_PARAMS, code_of(response), (method, params))
        self.assertEqual(0, self.h.transport.calls)

    def test_response_echoes_the_request_id(self) -> None:
        for request_id in (7, "abc", None):
            response = self.h.call(self.h.control_a, "service.info", id=request_id)
            self.assertEqual(request_id, response["id"])

    def test_internal_faults_are_not_echoed(self) -> None:
        h = Harness()
        secret_text = "leaky-detail-should-not-appear"

        class Exploding:
            engine_id = "claude"

            def support(self):
                return {"engine_id": "claude"}

            def execute(self, request):
                raise RuntimeError(secret_text)

        h.service._adapters["claude"] = Exploding()
        h.ok(h.control_a, "session.open", {"engine": "claude", "session": "s-one"})
        response = h.call(h.control_a, "session.run", {"engine": "claude", "session": "s-one", "prompt": "p"})
        self.assertEqual(err.INTERNAL_ERROR, code_of(response))
        self.assertNotIn(secret_text, json.dumps(response))
        self.assertEqual("open", h.ok(h.control_a, "session.get", {"engine": "claude", "session": "s-one"})["state"])


class RunTests(unittest.TestCase):
    def test_run_returns_digest_only_records_that_validate(self) -> None:
        h = Harness()
        h.ok(h.control_a, "session.open", {"engine": "claude", "session": "s-one"})
        prompt = "please judge: unique-prompt-text-91"
        result = h.ok(h.control_a, "session.run", {"engine": "claude", "session": "s-one", "prompt": prompt})
        self.assertEqual([], list(contract_validator("RouteExecutionRequest").iter_errors(result["request"])))
        self.assertEqual([], list(contract_validator("RouteExecutionResult").iter_errors(result["result"])))
        self.assertEqual("ok", result["result"]["status"])
        self.assertEqual({"verdict": "pass"}, result["result"]["validated"])
        blob = json.dumps(result) + json.dumps(h.journal.poll("tenant-a", 0))
        self.assertNotIn(prompt, blob)
        self.assertNotIn(CANARY, blob)
        self.assertEqual("req-1", result["request"]["id"])
        self.assertEqual("tenant-a", result["request"]["session"]["tenant"])
        self.assertEqual(1, h.ok(h.control_a, "session.get", {"engine": "claude", "session": "s-one"})["runs"])

    def test_request_ids_are_sequential_per_tenant(self) -> None:
        h = Harness()
        for tenant_token, expected in ((h.control_a, ["req-1", "req-2"]), (h.control_b, ["req-1"])):
            h.ok(tenant_token, "session.open", {"engine": "claude", "session": "s-one"})
            ids = [
                h.ok(tenant_token, "session.run", {"engine": "claude", "session": "s-one", "prompt": "p"})["request"]["id"]
                for _ in expected
            ]
            self.assertEqual(expected, ids)

    def test_run_needs_an_open_session(self) -> None:
        h = Harness()
        params = {"engine": "claude", "session": "s-one", "prompt": "p"}
        self.assertEqual(err.UNKNOWN_SESSION, code_of(h.call(h.control_a, "session.run", params)))
        h.ok(h.control_a, "session.open", {"engine": "claude", "session": "s-one"})
        h.ok(h.control_a, "session.close", {"engine": "claude", "session": "s-one"})
        self.assertEqual(err.SESSION_CLOSED, code_of(h.call(h.control_a, "session.run", params)))
        self.assertEqual(err.SESSION_CLOSED, code_of(h.call(h.control_a, "session.open", {"engine": "claude", "session": "s-one"})))
        self.assertEqual(0, h.transport.calls)

    def test_open_is_idempotent(self) -> None:
        h = Harness()
        first = h.ok(h.control_a, "session.open", {"engine": "claude", "session": "s-one"})
        again = h.ok(h.control_a, "session.open", {"engine": "claude", "session": "s-one"})
        self.assertEqual(first, again)
        kinds = [e["kind"] for e in h.ok(h.control_a, "events.poll", {"after": 0})["events"]]
        self.assertEqual(["session.opened"], kinds)

    def test_session_limit(self) -> None:
        h = Harness()
        for index in range(64):
            h.ok(h.control_a, "session.open", {"engine": "claude", "session": f"s-{index}"})
        response = h.call(h.control_a, "session.open", {"engine": "claude", "session": "s-extra"})
        self.assertEqual(err.REFUSED, code_of(response))
        h.ok(h.control_b, "session.open", {"engine": "claude", "session": "s-extra"})

    def test_closed_sessions_do_not_consume_the_quota(self) -> None:
        h = Harness()
        for index in range(64):
            h.ok(h.control_a, "session.open", {"engine": "claude", "session": f"s-{index}"})
            h.ok(h.control_a, "session.close", {"engine": "claude", "session": f"s-{index}"})
        opened = h.ok(h.control_a, "session.open", {"engine": "claude", "session": "s-next"})
        self.assertEqual("open", opened["state"])
        self.assertEqual(
            err.SESSION_CLOSED,
            code_of(h.call(h.control_a, "session.open", {"engine": "claude", "session": "s-0"})),
        )

    def test_closing_an_older_session_keeps_that_closure(self) -> None:
        h = Harness()
        h.ok(h.control_a, "session.open", {"engine": "claude", "session": "early"})
        for index in range(64):
            h.ok(h.control_a, "session.open", {"engine": "claude", "session": f"s-{index}"})
            h.ok(h.control_a, "session.close", {"engine": "claude", "session": f"s-{index}"})
        closed = h.ok(h.control_a, "session.close", {"engine": "claude", "session": "early"})
        self.assertEqual("closed", closed["state"])
        again = h.ok(h.control_a, "session.close", {"engine": "claude", "session": "early"})
        self.assertEqual(closed, again)
        self.assertEqual("closed", h.ok(h.control_a, "session.get", {"engine": "claude", "session": "early"})["state"])
        self.assertEqual(
            err.UNKNOWN_SESSION,
            code_of(h.call(h.control_a, "session.get", {"engine": "claude", "session": "s-0"})),
        )

    def test_closed_session_history_is_bounded(self) -> None:
        h = Harness()
        for index in range(80):
            h.ok(h.control_a, "session.open", {"engine": "claude", "session": f"s-{index}"})
            h.ok(h.control_a, "session.close", {"engine": "claude", "session": f"s-{index}"})
        tenant = h.service._tenants["tenant-a"]
        closed = sum(1 for item in tenant.sessions.values() if item.closed)
        self.assertLessEqual(closed, 64)
        self.assertEqual(0, tenant.open_count)
        self.assertEqual("open", h.ok(h.control_a, "session.open", {"engine": "claude", "session": "s-0"})["state"])
        self.assertEqual(
            err.SESSION_CLOSED,
            code_of(h.call(h.control_a, "session.open", {"engine": "claude", "session": "s-79"})),
        )

    def test_close_is_idempotent(self) -> None:
        h = Harness()
        h.ok(h.control_a, "session.open", {"engine": "claude", "session": "s-one"})
        first = h.ok(h.control_a, "session.close", {"engine": "claude", "session": "s-one"})
        again = h.ok(h.control_a, "session.close", {"engine": "claude", "session": "s-one"})
        self.assertEqual(first, again)
        kinds = [e["kind"] for e in h.ok(h.control_a, "events.poll", {"after": 0})["events"]]
        self.assertEqual(["session.opened", "session.closed"], kinds)

    def test_lone_surrogate_prompt_is_invalid_and_leaves_the_session_open(self) -> None:
        h = Harness()
        h.ok(h.control_a, "session.open", {"engine": "claude", "session": "s-one"})
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.run",
                "params": {"engine": "claude", "session": "s-one", "prompt": "\ud800"},
            },
            ensure_ascii=True,
        )
        response = h.service.handle(body, f"Bearer {h.control_a}")
        self.assertEqual(err.INVALID_PARAMS, code_of(response))
        self.assertEqual("prompt_encoding", response["error"]["message"])
        self.assertEqual("open", h.ok(h.control_a, "session.get", {"engine": "claude", "session": "s-one"})["state"])
        self.assertEqual(0, h.transport.calls)
        h.ok(h.control_a, "session.close", {"engine": "claude", "session": "s-one"})

    def test_a_secret_in_the_prompt_is_refused_before_any_spawn(self) -> None:
        h = Harness()
        h.ok(h.control_a, "session.open", {"engine": "claude", "session": "s-one"})
        response = h.call(h.control_a, "session.run", {"engine": "claude", "session": "s-one", "prompt": f"x {CANARY} y"})
        self.assertEqual(err.REFUSED, code_of(response))
        self.assertEqual("secret_refused", response["error"]["data"]["code"])
        self.assertEqual(0, h.transport.calls)
        self.assertNotIn(CANARY, json.dumps(response) + json.dumps(h.journal.poll("tenant-a", 0)))
        self.assertEqual("open", h.ok(h.control_a, "session.get", {"engine": "claude", "session": "s-one"})["state"])

    def test_a_second_run_on_a_busy_session_is_rejected_not_queued(self) -> None:
        gate = threading.Event()
        h = Harness(transport=StubTransport(gate=gate))
        h.ok(h.control_a, "session.open", {"engine": "claude", "session": "s-one"})
        params = {"engine": "claude", "session": "s-one", "prompt": "p"}
        first: list[dict] = []
        worker = threading.Thread(target=lambda: first.append(h.call(h.control_a, "session.run", params)))
        worker.start()
        try:
            self.assertTrue(h.transport.entered.wait(10))
            self.assertEqual("busy", h.ok(h.control_a, "session.get", {"engine": "claude", "session": "s-one"})["state"])
            self.assertEqual(err.BUSY, code_of(h.call(h.control_a, "session.run", params)))
            self.assertEqual(err.BUSY, code_of(h.call(h.control_a, "session.close", {"engine": "claude", "session": "s-one"})))
            h.ok(h.control_b, "session.open", {"engine": "claude", "session": "s-one"})
        finally:
            gate.set()
            worker.join(10)
        self.assertEqual("ok", first[0]["result"]["result"]["status"])
        self.assertEqual(1, h.transport.calls)
        self.assertEqual("open", h.ok(h.control_a, "session.get", {"engine": "claude", "session": "s-one"})["state"])

    def test_adapter_failure_is_a_result_not_a_transport_error(self) -> None:
        h = Harness(transport=StubTransport(stdout="not json"))
        h.ok(h.control_a, "session.open", {"engine": "claude", "session": "s-one"})
        response = h.call(h.control_a, "session.run", {"engine": "claude", "session": "s-one", "prompt": "p"})
        self.assertNotIn("error", response)
        result = response["result"]["result"]
        self.assertNotEqual("ok", result["status"])
        self.assertEqual([], list(contract_validator("RouteExecutionResult").iter_errors(result)))


class JournalTests(unittest.TestCase):
    def test_sequence_is_gapless_and_filtered_by_tenant(self) -> None:
        journal = EventJournal()
        journal.append("tenant-a", "a1")
        journal.append("tenant-b", "b1")
        journal.append("tenant-a", "a2")
        polled = journal.poll("tenant-a", 0)
        self.assertEqual([1, 3], [e["seq"] for e in polled["events"]])
        self.assertNotIn("tenant", polled["events"][0])
        self.assertEqual(3, polled["next_after"])
        self.assertFalse(polled["reset"])
        quiet = journal.poll("tenant-b", 2)
        self.assertEqual([], quiet["events"])
        self.assertEqual(3, quiet["next_after"])

    def test_limit_pages_without_skipping(self) -> None:
        journal = EventJournal()
        for index in range(7):
            journal.append("tenant-a", f"k{index}")
        seen: list[int] = []
        after = 0
        for _ in range(10):
            page = journal.poll("tenant-a", after, limit=3)
            seen += [e["seq"] for e in page["events"]]
            if not page["events"]:
                break
            after = page["next_after"]
        self.assertEqual(list(range(1, 8)), seen)

    def test_stale_poll_returns_immediately_when_another_tenant_evicts(self) -> None:
        journal = EventJournal(capacity=1)
        journal.append("tenant-a", "a1")
        journal.append("tenant-b", "b1")
        started = threading.Event()
        out: list[dict] = []

        def waiter() -> None:
            started.set()
            out.append(journal.poll("tenant-a", 0, wait_s=30))

        thread = threading.Thread(target=waiter)
        thread.start()
        started.wait(5)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(out[0]["reset"])
        self.assertEqual([], out[0]["events"])

    def test_overflow_reports_a_reset_instead_of_a_silent_gap(self) -> None:
        journal = EventJournal(capacity=3)
        for index in range(5):
            journal.append("tenant-a", f"k{index}")
        stale = journal.poll("tenant-a", 0)
        self.assertTrue(stale["reset"])
        self.assertEqual([3, 4, 5], [e["seq"] for e in stale["events"]])
        current = journal.poll("tenant-a", stale["next_after"])
        self.assertFalse(current["reset"])
        self.assertEqual([], current["events"])
        self.assertFalse(journal.poll("tenant-a", 2)["reset"])
        self.assertTrue(journal.poll("tenant-a", 1)["reset"])

    def test_wait_returns_when_an_event_arrives_and_times_out_otherwise(self) -> None:
        journal = EventJournal()
        started = threading.Event()
        out: list[dict] = []

        def waiter() -> None:
            started.set()
            out.append(journal.poll("tenant-a", 0, wait_s=10))

        thread = threading.Thread(target=waiter)
        thread.start()
        started.wait(5)
        journal.append("tenant-b", "other")
        journal.append("tenant-a", "mine")
        thread.join(10)
        self.assertEqual(["mine"], [e["kind"] for e in out[0]["events"]])
        self.assertEqual([], journal.poll("tenant-a", 2, wait_s=0.05)["events"])

    def test_poll_via_service_is_bounded(self) -> None:
        h = Harness()
        for index in range(5):
            h.ok(h.control_a, "session.open", {"engine": "claude", "session": f"s-{index}"})
        page = h.ok(h.control_a, "events.poll", {"after": 0, "limit": 2})
        self.assertEqual(2, len(page["events"]))
        page = h.ok(h.control_a, "events.poll", {"after": 0, "limit": 10_000})
        self.assertEqual(5, len(page["events"]))


class StaticBoundaryTests(unittest.TestCase):
    ALLOWED_ROOTS = {
        "__future__", "collections", "dataclasses", "hashlib", "hmac", "json", "re",
        "secrets", "threading", "typing", "jsonschema", "scripts",
    }
    FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "open", "input"}

    def test_core_modules_import_no_process_or_network_api(self) -> None:
        for path in sorted(PACKAGE.glob("*.py")):
            if path.name in {"http_server.py", "client.py", "fake_engines.py", "record.py", "__main__.py", "cli.py"}:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom):
                    roots = {(node.module or "").split(".")[0]}
                else:
                    roots = set()
                self.assertLessEqual(roots, self.ALLOWED_ROOTS, f"{path.name} imports {roots}")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, self.FORBIDDEN_CALLS, path.name)

    def test_no_live_adapter_is_reachable_from_the_service(self) -> None:
        from scripts.v3.route_contract import live

        self.assertFalse(live.LIVE_ADAPTERS_ENABLED)
        h = Harness()
        for route in h.ok(h.control_a, "route.list")["routes"]:
            self.assertFalse(route["live_calls"])
            self.assertEqual("fake", route["adapter_kind"])


if __name__ == "__main__":
    unittest.main()
