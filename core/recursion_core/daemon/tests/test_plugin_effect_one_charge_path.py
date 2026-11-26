"""Every charge of a plugin effect goes through the core's one charge path, with every guard.

The owner or a paired device approving a plugin effect charges it through
``CoreService.authorize_effect``, the same path a plugin's own ``effect.authorize`` takes: core
health, the permission a key revocation removes, the run and its connection, the commit fence a
halt drains, a trip while in flight, and the queue record still approved inside that fence. A
refusal of any of them is raised as itself, with words that say what stopped the charge and that
nothing was charged; the ledger keeps no row and the approval stays approved, unconsumed.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
from pathlib import Path
import threading
import unittest

from recursion_core.daemon import plugins
from recursion_core.daemon.tests.test_plugin_effect_approval import (CONNECTION, LINUX, MODEL, PLUGIN_ID, PROVIDER,
                                                                     RUN_ID, SOURCE, _HostCase, _effect)
from recursion_core.plugin_security import Refused, canonical
from recursion_core.plugin_security.breaker import TYPED_LIMITS
from recursion_core.plugin_security.canonical import parse
from recursion_core.plugin_security.protocol import PROTOCOL_VERSION
from recursion_core.plugin_security.signing import key_id
from recursion_core.plugin_security.tests.support import Key, needs_crypto, profile, scope_of

try:
    from recursion_core.approvals import store as approval_rows
except ImportError:  # pragma: no cover - the queue is on this branch
    approval_rows = None

WAIT_S = 10.0  # a bound on every wait of these tests; nothing here sleeps for it


class _Pause:
    """Two events: the charge says it reached the point, the test lets it go on."""

    def __init__(self):
        self.reached, self.go, self.left = threading.Event(), threading.Event(), threading.Event()

    def hold(self):
        self.reached.set()
        self.go.wait(WAIT_S)


@contextlib.contextmanager
def _holding(guard, pause):
    try:
        with guard:
            pause.hold()
            yield
    finally:
        pause.left.set()  # the guard (the commit section, when there is one) has been left


class _OneChargePath:
    """The cases, on a composed host with a loaded plugin holding ``order`` and a bound run."""

    signed = True

    def needs_signed(self):
        if not self.signed:
            self.skipTest("a key revocation needs a signed plugin, and signing needs cryptography")

    def revoke(self, host):
        return self.host_op(host, "key.revoke", key_id=key_id(self.key.public))

    def host_op(self, host, op, **args):
        answer = parse(host.core.client_for("host")(canonical({"protocol": PROTOCOL_VERSION, "op": op,
                                                                "args": args})))
        self.assertTrue(answer["ok"], answer)
        return answer["result"]

    def pause_charge(self, host, *, before=None, inside=None):
        """A stand-in of the test's own around the ledger's charge: pause before it, or inside its guard."""
        real = host.authority.charge

        def charge(*args, **kwargs):
            if before is not None:
                before.hold()
            if inside is not None:
                kwargs["commit_guard"] = _holding(kwargs.get("commit_guard") or contextlib.nullcontext(), inside)
            return real(*args, **kwargs)

        host.authority.charge = charge

    def approve(self, host, approval_id, effect):
        return host.answer_plugin_effect(self.owner(), approval_id, effect, authority=self.envelope())

    def assert_stopped(self, host, approval_id, refused, code):
        self.assertIsInstance(refused, Refused)
        self.assertEqual(refused.code, code)
        self.assertIn("nothing was charged", refused.detail)
        self.assertEqual(self.charge_rows(), [])
        self.assertEqual(self.record(host, approval_id)["state"], "approved")

    def approve_refused(self, host, approval_id, effect):
        try:
            outcome = self.approve(host, approval_id, effect)
        except Refused as refused:
            return refused
        self.fail("charged after a guard should have stopped it: %r" % (outcome,))

    def test_a_poisoned_core_refuses_the_approved_charge(self):
        host = self.composed()
        effect = _effect()
        approval_id = host.request_plugin_effect(effect)
        host.health.poison("test_poison")
        refused = self.approve_refused(host, approval_id, effect)
        self.assert_stopped(host, approval_id, refused, "core_poisoned_restart_required")
        self.assertIn("restart the daemon", refused.detail)

    def test_a_revoked_key_refuses_the_approved_charge(self):
        self.needs_signed()
        host = self.composed()
        effect = _effect()
        approval_id = host.request_plugin_effect(effect)
        self.assertEqual(self.revoke(host)["order_off"], [PLUGIN_ID])
        refused = self.approve_refused(host, approval_id, effect)
        self.assert_stopped(host, approval_id, refused, "permission_required")
        self.assertIn("'order' permission", refused.detail)

    def test_a_faulted_run_refuses_with_the_breakers_own_code(self):
        host = self.composed()
        effect = _effect()
        approval_id = host.request_plugin_effect(effect)
        self.host_op(host, "run.fault", run_id=RUN_ID)
        refused = self.approve_refused(host, approval_id, effect)
        self.assert_stopped(host, approval_id, refused, "run_faulted")
        self.assertIn("halted", refused.detail)

    def test_a_revoke_acknowledged_while_the_charge_is_in_flight_refuses_it(self):
        self.needs_signed()
        host = self.composed()
        effect = _effect()
        approval_id = host.request_plugin_effect(effect)
        before = _Pause()
        self.pause_charge(host, before=before)
        outcome = []
        worker = threading.Thread(target=lambda: outcome.append(self._catch(host, approval_id, effect)))
        worker.start()
        self.assertTrue(before.reached.wait(WAIT_S))
        self.revoke(host)  # acknowledged: it has answered
        before.go.set()
        worker.join(WAIT_S)
        self.assertFalse(worker.is_alive())
        self.assert_stopped(host, approval_id, outcome[0], "permission_required")

    def test_a_revoke_answers_only_after_a_charge_inside_the_fence_has_left_it(self):
        self.needs_signed()
        host = self.composed()
        effect = _effect()
        approval_id = host.request_plugin_effect(effect)
        inside = _Pause()
        self.pause_charge(host, inside=inside)
        outcome, answered, left_when_answered = [], threading.Event(), []
        charger = threading.Thread(target=lambda: outcome.append(self._catch(host, approval_id, effect)))
        charger.start()
        self.assertTrue(inside.reached.wait(WAIT_S))

        def revoke():
            self.revoke(host)
            left_when_answered.append(inside.left.is_set())
            answered.set()

        revoker = threading.Thread(target=revoke)
        revoker.start()
        waited = answered.wait(0.5)
        inside.go.set()
        charger.join(WAIT_S)
        revoker.join(WAIT_S)
        self.assertFalse(charger.is_alive() or revoker.is_alive())
        self.assertFalse(waited, "the revoke answered while a charge was inside the commit section")
        # The charge read the old state inside the fence, so it committed; the revoke answered after it.
        self.assertEqual(outcome[0]["approval"]["state"], "consumed")
        self.assertEqual(self.charge_rows(), [(approval_id, effect["amount_minor"])])
        self.assertEqual(left_when_answered, [True])

    def test_a_trip_between_the_start_and_the_commit_refuses_connection_tripped(self):
        host = self.composed()
        effect = _effect()
        approval_id = host.request_plugin_effect(effect)
        before = _Pause()
        self.pause_charge(host, before=before)
        outcome = []
        worker = threading.Thread(target=lambda: outcome.append(self._catch(host, approval_id, effect)))
        worker.start()
        self.assertTrue(before.reached.wait(WAIT_S))
        self.assertTrue(self.host_op(host, "breaker.report", connection=CONNECTION,
                                     event_type=sorted(TYPED_LIMITS)[0])["tripped"])
        lease = self.host_op(host, "breaker.probe_begin", connection=CONNECTION)["lease"]
        self.assertTrue(self.host_op(host, "breaker.probe_reset", connection=CONNECTION, lease=lease,
                                     probe_ok=True)["reset"])
        before.go.set()
        worker.join(WAIT_S)
        self.assertFalse(worker.is_alive())
        self.assert_stopped(host, approval_id, outcome[0], "connection_tripped")

    def test_a_record_that_stops_being_approved_before_the_commit_is_not_charged(self):
        """No transition leaves ``approved`` but consumption at this head; a direct store write stands in."""
        host = self.composed()
        effect = _effect()
        approval_id = host.request_plugin_effect(effect)
        before = _Pause()
        self.pause_charge(host, before=before)
        outcome = []
        worker = threading.Thread(target=lambda: outcome.append(self._catch(host, approval_id, effect)))
        worker.start()
        self.assertTrue(before.reached.wait(WAIT_S))
        with host._approval_queue.store.transaction() as tx:  # noqa: SLF001 - the composed queue's own row
            held = approval_rows.load(tx, approval_id)
            approval_rows.save(tx, dict(held, state="vetoed"))
        before.go.set()
        worker.join(WAIT_S)
        self.assertFalse(worker.is_alive())
        refused = outcome[0]
        self.assertIsInstance(refused, Refused)
        self.assertEqual(refused.code, "approval_not_approved")
        self.assertIn("nothing was charged", refused.detail)
        self.assertEqual(self.charge_rows(), [])

    def test_no_core_refuses_with_a_typed_code_and_words(self):
        host = self.composed()
        effect = _effect()
        approval_id = host.request_plugin_effect(effect)
        host.core = None
        refused = self.approve_refused(host, approval_id, effect)
        self.assert_stopped(host, approval_id, refused, "core_unavailable")

    def _catch(self, host, approval_id, effect):
        try:
            return self.approve(host, approval_id, effect)
        except BaseException as exc:  # noqa: BLE001 - the worker's outcome is what the test reads
            return exc


@unittest.skipUnless(LINUX, "the owner-only socket peer and the token file are checked by uid on Linux")
@needs_crypto
class SignedOneChargePathTests(_OneChargePath, _HostCase):
    """A signed plugin and a signed authority profile: every store and check is the product's own."""

    def setUp(self):
        super().setUp()
        self.key = Key()
        self.installer = Key()

    def composed(self):
        host = self.host(anchor=self.installer.public, signed=True)
        self.assertEqual(host.records[0]["state"], "loaded")
        self.breaker.bind_run(RUN_ID, CONNECTION, PLUGIN_ID, PROVIDER, MODEL)
        return host

    def envelope(self):
        return profile(self.installer, plugin_id=PLUGIN_ID, source=SOURCE, provider=PROVIDER,
                       model_version=MODEL, not_before=0, expires_at=2_000_000)


@unittest.skipUnless(LINUX, "the owner-only socket peer and the token file are checked by uid on Linux")
class StandInSignatureOneChargePathTests(_OneChargePath, _HostCase):
    """For an interpreter without cryptography: an unsigned plugin granted ``order`` with the
    acknowledgement, and the ledger's signature check alone replaced by a stand-in. Every guard,
    the fence, the queue and the ledger's own transaction are the product's."""

    signed = False

    def composed(self):
        host = self.loaded_host(anchor="ef" * 32)
        self.assertIn("order", host.loader.permissions(PLUGIN_ID))
        host.authority._verify = lambda _signature, _raw: True  # the signature check alone
        return host

    def envelope(self):
        body = {"schema": "csl-authority-profile/vNext", "profile_id": "prof-1", "issuer": "installer",
                "plugin_id": PLUGIN_ID, "artifact_sha256": hashlib.sha256(SOURCE).hexdigest(), "scope": scope_of(),
                "provider": PROVIDER, "model_version": MODEL, "actions": ["order.submit"], "permission": "order",
                "not_before": 0, "expires_at": 2_000_000,
                "budgets": {"currency": "USD", "max_effect_minor": 5_000, "total_minor": 10_000}}
        return {"profile": body, "signature": "00" * 64}


class OneCallOfTheLedgerChargeTests(unittest.TestCase):
    def test_exactly_one_call_of_authority_charge_inside_the_one_function(self):
        package = Path(plugins.__file__).resolve().parent.parent
        calls = []
        for path in sorted(package.rglob("*.py")):
            rel = path.relative_to(package).as_posix()
            if "tests" in path.relative_to(package).parts or rel == "plugin_security/authority.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            stack = []

            def walk(node):
                named = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                if named:
                    stack.append(node.name)
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "charge"):
                    calls.append((rel, stack[-1] if stack else None))
                for child in ast.iter_child_nodes(node):
                    walk(child)
                if named:
                    stack.pop()

            walk(tree)
        self.assertEqual(calls, [("plugin_security/protocol.py", "authorize_effect")])


if __name__ == "__main__":
    unittest.main()
