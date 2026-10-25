#!/usr/bin/env python3
"""AT1 contract tests: session owner, CredentialRef custody, surface rows.

No live provider is started. The canary below is a synthetic marker.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from dataclasses import asdict, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v3.route_adapter.credentials import (  # noqa: E402
    CredentialRef,
    InvocationHandle,
    LocalCredentialResolver,
)
from scripts.v3.route_adapter.descriptor import (  # noqa: E402
    CODEX_APP_SERVER_PROOF,
    CONTROL_MCP_CALLER_ONLY_ADMITTED,
    F5_COUNSEL_GATE,
    SUBSCRIPTION_FANOUT_IN_AT1,
    RouteSurfaceDescriptor,
    admit_experimental_driver,
    grok_rows,
    require_vendor_experimental_api,
)
from scripts.v3.route_adapter.errors import Busy, CustodyDenied, StaleGeneration  # noqa: E402
from scripts.v3.route_adapter.fake import FakeRouteDriver  # noqa: E402
from scripts.v3.route_adapter.session import (  # noqa: E402
    CONTENTION,
    DISTRIBUTED_FENCING,
    SessionKey,
    SessionOwner,
)

CANARY = "canary-secret-AT1-9f3a7c"
README = ROOT / "scripts" / "v3" / "route_adapter" / "README.md"


def _ref(tenant: str = "tenant-a", provider: str = "grok", name: str = "build-ref") -> CredentialRef:
    return CredentialRef(tenant_id=tenant, provider=provider, name=name)


def _blob(*parts: object) -> str:
    return json.dumps(parts, default=str)


class SessionOwnerTests(unittest.TestCase):
    def test_same_session_is_busy_and_generation_stays(self) -> None:
        owner = SessionOwner()
        key = SessionKey("tenant-a", "grok-build", "s1")
        first = owner.begin(key)
        self.assertEqual(first.generation, 1)
        self.assertEqual(first.op_id, "local-1")
        with self.assertRaises(Busy) as caught:
            owner.begin(key)
        self.assertEqual(caught.exception.code, "busy")
        self.assertEqual(caught.exception.key, key)
        self.assertNotIn(CANARY, str(caught.exception))
        self.assertTrue(owner.is_busy(key))
        self.assertEqual(owner.generation_of(key), 1)
        self.assertEqual(CONTENTION, "busy-reject")
        self.assertIs(DISTRIBUTED_FENCING, False)

    def test_independent_sessions_including_other_providers(self) -> None:
        owner = SessionOwner()
        build = owner.begin(SessionKey("tenant-a", "grok-build", "s1"))
        bot_route = owner.begin(SessionKey("tenant-a", "grok-api", "s1"))
        other_tenant = owner.begin(SessionKey("tenant-b", "grok-build", "s1"))
        self.assertEqual(build.generation, 1)
        self.assertEqual(bot_route.generation, 1)
        self.assertEqual(other_tenant.generation, 1)
        self.assertEqual(
            [build.op_id, bot_route.op_id, other_tenant.op_id],
            ["local-1", "local-2", "local-3"],
        )
        self.assertTrue(owner.is_busy(build.key))
        self.assertTrue(owner.is_busy(bot_route.key))
        self.assertTrue(owner.is_busy(other_tenant.key))

    def test_concurrent_independent_sessions(self) -> None:
        owner = SessionOwner()
        started = threading.Barrier(2)
        release = threading.Barrier(2)
        found: list[object] = []
        completed: list[bool] = []
        guard = threading.Lock()

        def work(session: str) -> None:
            started.wait()
            operation = owner.begin(SessionKey("tenant-a", "grok-build", session))
            with guard:
                found.append(operation)
            release.wait()
            ok = owner.complete(operation)
            with guard:
                completed.append(ok)

        threads = [
            threading.Thread(target=work, args=("left",)),
            threading.Thread(target=work, args=("right",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(found), 2)
        self.assertEqual(completed, [True, True])
        self.assertFalse(owner.is_busy(SessionKey("tenant-a", "grok-build", "left")))
        self.assertFalse(owner.is_busy(SessionKey("tenant-a", "grok-build", "right")))

    def test_concurrent_begin_on_one_session_rejects_one(self) -> None:
        owner = SessionOwner()
        key = SessionKey("tenant-a", "grok-build", "only")
        gate = threading.Barrier(2)
        outcomes: list[str] = []
        guard = threading.Lock()

        def work() -> None:
            gate.wait()
            try:
                owner.begin(key)
                label = "ok"
            except Busy:
                label = "busy"
            with guard:
                outcomes.append(label)

        threads = [threading.Thread(target=work), threading.Thread(target=work)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["busy", "ok"])
        self.assertEqual(owner.generation_of(key), 1)
        self.assertTrue(owner.is_busy(key))

    def test_cancel_releases_owner_and_stale_complete_is_discarded(self) -> None:
        owner = SessionOwner()
        key = SessionKey("tenant-a", "codex", "s1")
        first = owner.begin(key)
        self.assertTrue(owner.cancel(first))
        self.assertFalse(owner.is_busy(key))
        self.assertFalse(owner.complete(first))
        self.assertIn(first.op_id, owner.discarded())
        kinds = [event["kind"] for event in owner.observe(first)]
        self.assertEqual(kinds, ["started", "cancelled"])
        self.assertNotIn("completed", kinds)

        second = owner.begin(key)
        self.assertEqual(second.generation, 2)
        self.assertNotEqual(second.op_id, first.op_id)
        self.assertFalse(owner.complete(first))
        self.assertTrue(owner.is_busy(key))
        self.assertEqual(owner.generation_of(key), 2)
        with self.assertRaises(StaleGeneration):
            owner.send(first, "late")
        self.assertNotIn("late", _blob(owner.observe(second), owner.snapshot()))
        self.assertTrue(owner.complete(second))
        self.assertFalse(owner.is_busy(key))

    def test_local_generations_are_per_session(self) -> None:
        owner = SessionOwner()
        key = SessionKey("tenant-a", "grok-build", "s1")
        first = owner.begin(key)
        owner.complete(first)
        second = owner.begin(key)
        other = owner.begin(SessionKey("tenant-a", "grok-build", "s2"))
        self.assertEqual(second.generation, 2)
        self.assertEqual(other.generation, 1)
        self.assertTrue(str(second.op_id).startswith("local-"))
        self.assertNotEqual(second.op_id, other.op_id)


class CredentialCustodyTests(unittest.TestCase):
    def test_ref_has_no_secret_field_and_resolver_is_replaceable(self) -> None:
        self.assertEqual(
            [field.name for field in fields(CredentialRef)],
            ["tenant_id", "provider", "name"],
        )
        self.assertEqual(
            [field.name for field in fields(InvocationHandle)],
            ["ref", "accepted", "delivery"],
        )

        class RecordingResolver:
            def __init__(self) -> None:
                self.calls = 0

            def authorize(self, ref: CredentialRef, *, caller_tenant: str) -> InvocationHandle:
                self.calls += 1
                if caller_tenant != ref.tenant_id:
                    raise CustodyDenied("cross-tenant-denied")
                return InvocationHandle(ref=ref, accepted=True, delivery="runtime-visible-stub")

        resolver = RecordingResolver()
        driver = FakeRouteDriver(owner=SessionOwner(), resolver=resolver)
        operation = driver.start(
            SessionKey("tenant-a", "grok-build", "s1"),
            _ref(),
            caller_tenant="tenant-a",
        )
        self.assertEqual(resolver.calls, 1)
        self.assertEqual(operation.generation, 1)
        self.assertEqual(driver.observe(operation)[0]["credential"], _ref().public_dict())

    def test_canary_stays_out_of_logs_errors_and_snapshot(self) -> None:
        resolver = LocalCredentialResolver()
        ref = _ref()
        resolver.register(ref, CANARY)
        driver = FakeRouteDriver(owner=SessionOwner(), resolver=resolver)
        before = dict(os.environ)
        operation = driver.start(
            SessionKey("tenant-a", "grok-build", "s1"),
            ref,
            caller_tenant="tenant-a",
        )
        driver.send(operation, "hello")
        events = driver.observe(operation)
        driver.cancel(operation)
        snapshot = driver.snapshot()
        handle = resolver.authorize(ref, caller_tenant="tenant-a")
        public = _blob(
            events,
            snapshot,
            asdict(handle),
            asdict(ref),
            [asdict(item) for item in resolver.public_refs()],
            repr(resolver),
            repr(handle),
        )
        self.assertNotIn(CANARY, public)
        self.assertEqual(handle.delivery, "runtime-visible-stub")
        self.assertNotEqual(handle.delivery, "brokered")
        self.assertEqual(dict(os.environ), before)
        self.assertNotIn(CANARY, os.environ.values())

        with self.assertRaises(CustodyDenied) as denied:
            resolver.authorize(ref, caller_tenant="tenant-b")
        self.assertEqual(denied.exception.code, "cross-tenant-denied")
        self.assertNotIn(CANARY, str(denied.exception))
        self.assertFalse(driver.owner.is_busy(SessionKey("tenant-b", "grok-build", "s1")))

        other = LocalCredentialResolver()
        with self.assertRaises(CustodyDenied) as unknown:
            other.authorize(ref, caller_tenant="tenant-a")
        self.assertEqual(unknown.exception.code, "unknown-ref")
        self.assertNotIn(CANARY, str(unknown.exception))
        self.assertNotIn(CANARY, repr(other))
        self.assertEqual(dict(os.environ), before)

    def test_rejected_secret_field_is_not_persisted(self) -> None:
        owner = SessionOwner()
        leaked = {
            "tenant_id": "tenant-a",
            "provider": "grok",
            "name": "build-ref",
            "secret": CANARY,
        }
        with self.assertRaises(CustodyDenied) as caught:
            owner.begin(SessionKey("tenant-a", "grok-build", "s1"), credential=leaked)
        self.assertEqual(caught.exception.code, "credential-field-rejected")
        self.assertNotIn(CANARY, str(caught.exception))
        self.assertNotIn(CANARY, _blob(owner.snapshot()))
        self.assertEqual(owner.generation_of(SessionKey("tenant-a", "grok-build", "s1")), 0)

    def test_cross_tenant_start_does_not_acquire(self) -> None:
        resolver = LocalCredentialResolver()
        ref = _ref()
        resolver.register(ref, CANARY)
        driver = FakeRouteDriver(owner=SessionOwner(), resolver=resolver)
        key = SessionKey("tenant-a", "grok-build", "s1")
        with self.assertRaises(CustodyDenied):
            driver.start(key, ref, caller_tenant="tenant-b")
        self.assertFalse(driver.owner.is_busy(key))
        self.assertNotIn(CANARY, _blob(driver.snapshot()))


class FakeDriverTests(unittest.TestCase):
    def test_start_send_observe_cancel(self) -> None:
        resolver = LocalCredentialResolver()
        ref = _ref()
        resolver.register(ref, CANARY)
        driver = FakeRouteDriver(owner=SessionOwner(), resolver=resolver)
        key = SessionKey("tenant-a", "grok-build", "s1")
        operation = driver.start(key, ref, caller_tenant="tenant-a")
        driver.send(operation, "ping")
        self.assertEqual(
            [event["kind"] for event in driver.observe(operation)],
            ["started", "sent"],
        )
        with self.assertRaises(Busy):
            driver.start(key, ref, caller_tenant="tenant-a")
        self.assertTrue(driver.cancel(operation))
        self.assertEqual(
            [event["kind"] for event in driver.observe(operation)],
            ["started", "sent", "cancelled"],
        )
        self.assertFalse(driver.owner.is_busy(key))
        again = driver.start(key, ref, caller_tenant="tenant-a")
        self.assertEqual(again.generation, 2)
        self.assertNotIn(CANARY, _blob(driver.snapshot(), driver.observe(again)))


MESSAGE_CANARY = "canary-message-AT1-bb91e0"


class ReviewFixTests(unittest.TestCase):
    def test_default_drivers_share_a_process_owner(self) -> None:
        ref = _ref()
        resolver = LocalCredentialResolver()
        resolver.register(ref, CANARY)
        first = FakeRouteDriver(resolver=resolver)
        second = FakeRouteDriver(resolver=resolver)
        key = SessionKey("tenant-a", "grok-build", "process-default")
        operation = first.start(key, ref, caller_tenant="tenant-a")
        self.assertIs(first.owner, second.owner)
        with self.assertRaises(Busy):
            second.start(key, ref, caller_tenant="tenant-a")
        self.assertTrue(first.owner.is_busy(key))
        self.assertTrue(first.cancel(operation))
        self.assertFalse(first.owner.is_busy(key))

    def test_distinct_owners_do_not_cross_cancel(self) -> None:
        ref = _ref()
        left_resolver = LocalCredentialResolver()
        right_resolver = LocalCredentialResolver()
        left_resolver.register(ref, CANARY)
        right_resolver.register(ref, CANARY)
        left = FakeRouteDriver(owner=SessionOwner(), resolver=left_resolver)
        right = FakeRouteDriver(owner=SessionOwner(), resolver=right_resolver)
        key = SessionKey("tenant-a", "grok-build", "split-owners")
        left_op = left.start(key, ref, caller_tenant="tenant-a")
        right_op = right.start(key, ref, caller_tenant="tenant-a")
        self.assertEqual(left_op.op_id, "local-1")
        self.assertEqual(right_op.op_id, "local-1")
        self.assertNotEqual(left_op.owner_id, right_op.owner_id)
        self.assertFalse(right.cancel(left_op))
        self.assertFalse(left.cancel(right_op))
        self.assertTrue(left.owner.is_busy(key))
        self.assertTrue(right.owner.is_busy(key))
        self.assertEqual(left.observe(right_op), ())
        self.assertTrue(left.cancel(left_op))
        self.assertFalse(left.owner.is_busy(key))
        self.assertTrue(right.owner.is_busy(key))

    def test_session_tenant_must_match_caller_and_ref(self) -> None:
        class RecordingResolver:
            def __init__(self) -> None:
                self.calls = 0

            def authorize(self, ref: CredentialRef, *, caller_tenant: str) -> InvocationHandle:
                self.calls += 1
                return InvocationHandle(ref=ref, accepted=True, delivery="runtime-visible-stub")

        resolver = RecordingResolver()
        owner = SessionOwner()
        driver = FakeRouteDriver(owner=owner, resolver=resolver)
        ref = _ref()
        key = SessionKey("tenant-b", "grok-build", "crossed")
        with self.assertRaises(CustodyDenied) as caught:
            driver.start(key, ref, caller_tenant="tenant-a")
        self.assertEqual(caught.exception.code, "tenant-mismatch")
        self.assertNotIn(CANARY, str(caught.exception))
        self.assertEqual(resolver.calls, 0)
        self.assertFalse(owner.is_busy(key))
        self.assertEqual(owner.generation_of(key), 0)

    def test_message_body_is_absent_from_public_transcript(self) -> None:
        owner = SessionOwner()
        operation = owner.begin(SessionKey("tenant-a", "grok-build", "msg"))
        owner.send(operation, MESSAGE_CANARY)
        observed = owner.observe(operation)
        public = _blob(observed, owner.snapshot())
        self.assertNotIn(MESSAGE_CANARY, public)
        self.assertEqual([event["kind"] for event in observed], ["started", "sent"])
        for event in observed:
            self.assertNotIn("message", event)
            self.assertIn("op_id", event)
            self.assertIn("generation", event)


class DescriptorTests(unittest.TestCase):
    def test_build_bot_and_api_are_separate_rows(self) -> None:
        rows = grok_rows()
        self.assertEqual([row.row_id for row in rows], ["grok-build", "grok-bot", "grok-api"])
        self.assertEqual([row.product for row in rows], ["build", "bot", "api"])
        by_id = {row.row_id: row for row in rows}
        self.assertEqual(by_id["grok-bot"].inbound_session_control, "unsupported")
        self.assertEqual(by_id["grok-build"].inbound_session_control, "unestablished")
        self.assertEqual(by_id["grok-api"].inbound_session_control, "unestablished")
        for row in rows:
            self.assertFalse(row.experimental_driver_admitted)
            self.assertFalse(row.vendor_experimental_api)
            self.assertIsNone(row.vendor_experimental_method)
            self.assertEqual(row.control_mcp, "not-admitted")
            self.assertNotIn(CANARY, _blob(asdict(row)))
        self.assertIs(CONTROL_MCP_CALLER_ONLY_ADMITTED, False)
        with self.assertRaises(ValueError):
            RouteSurfaceDescriptor(
                row_id="grok-bot-mcp",
                product="bot",
                provider="grok",
                inbound_session_control="unsupported",
                experimental_driver_admitted=False,
                vendor_experimental_api=False,
                control_mcp="caller-only",
            )

    def test_experimental_flags_are_independent(self) -> None:
        original = grok_rows()[0]
        admitted = admit_experimental_driver(original)
        self.assertTrue(admitted.experimental_driver_admitted)
        self.assertFalse(admitted.vendor_experimental_api)
        self.assertFalse(original.experimental_driver_admitted)
        with self.assertRaises(ValueError) as caught:
            require_vendor_experimental_api(original, "  ")
        self.assertIn("required method", str(caught.exception))
        self.assertFalse(original.vendor_experimental_api)
        enabled = require_vendor_experimental_api(admitted, "turn/interrupt")
        self.assertTrue(enabled.experimental_driver_admitted)
        self.assertTrue(enabled.vendor_experimental_api)
        self.assertEqual(enabled.vendor_experimental_method, "turn/interrupt")
        self.assertFalse(admitted.vendor_experimental_api)

    def test_codex_proof_is_skipped_and_f5_stays_closed(self) -> None:
        self.assertEqual(CODEX_APP_SERVER_PROOF["status"], "skipped")
        self.assertIs(CODEX_APP_SERVER_PROOF["live_calls"], False)
        self.assertIn("Lucas", str(CODEX_APP_SERVER_PROOF["reason"]))
        self.assertIn("D-RP2-1", str(CODEX_APP_SERVER_PROOF["reason"]))
        self.assertEqual(F5_COUNSEL_GATE, "closed")
        self.assertIs(SUBSCRIPTION_FANOUT_IN_AT1, False)
        text = README.read_text(encoding="utf-8")
        for phrase in (
            "D-RP2-1",
            "skipped",
            "Lucas",
            "F-5",
            "closed",
            "subscription",
            "busy-reject",
            "CredentialRef",
            "unsupported",
            "experimental",
        ):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
