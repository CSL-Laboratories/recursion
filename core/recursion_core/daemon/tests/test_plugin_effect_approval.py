"""The plugin host's approver: the OS user on the owner-only socket, or the device token.

On the tip this branch was cut from, the composed gate's authenticator names no principal,
so a plugin effect cannot be approved by anybody. After the composition, the owner's
approval answers through ``approval.answer`` and ``PluginEffectSink.carry`` charges the
effect once. A caller who is not that owner or the device is ``scope_refused``. A second
carry is refused and does not charge again. A charge that the ledger already has for that
approval id (``effect_replayed``) returns the receipt from the first charge.

The in-memory gate stays unable to approve: a decision there would mark an effect approved
with nothing charged.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest

from recursion_core.daemon import breaker as breaker_module
from recursion_core.daemon import plugins
from recursion_core.daemon.tests import plugin_fixture as fx
from recursion_core.plugin_security import Refused, create_ledger
from recursion_core.plugin_security.signing import backend_available
from recursion_core.plugin_security.tests.support import FakeGilProbe, Key, needs_crypto, profile

try:
    from recursion_core.approvals.model import ApprovalRefused
    from recursion_core.approvals import store as approval_rows
except ImportError:  # pragma: no cover - the queue is on this branch
    ApprovalRefused = None
    approval_rows = None

LINUX = os.name == "posix" and os.uname().sysname == "Linux"
CLOCK = lambda: 1_000_000  # noqa: E731 - one frozen instant for the gate, the queue and the ledger
PLUGIN_ID = "fixture.operator"
SOURCE = b"VALUE = 1\n"
RUN_ID = "run-1"
CONNECTION = "conn-a"
PROVIDER = "fake-provider"
MODEL = "fake-model-1"


def _effect(*, amount=1_000, payload=None):
    return {
        "schema": "csl-effect/vNext", "kind": "order.submit", "plugin_id": PLUGIN_ID, "run_id": RUN_ID,
        "connection": CONNECTION,
        "scope": {"account": "paper-acct", "venue": "paper", "contract_family": "fake-futures",
                  "adapter": "fake-adapter", "environment": "paper"},
        "currency": "USD", "amount_minor": amount,
        "payload": {"note": "one"} if payload is None else payload,
    }


class _HostCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rc-approver-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.chmod(self.tmp, 0o700)
        self.config = self.tmp / "config" / "recursion"
        self.state = self.tmp / "state"
        self.plugins_dir = self.config / "plugins"
        self.ledger = self.state / "plugin-security" / "ledger.sqlite3"
        self.token_path = self.tmp / "device-token"
        self.token = "device-token-for-this-daemon"
        self.token_path.write_bytes(self.token.encode() + b"\n")
        os.chmod(self.token_path, 0o600)

    def host(self, *, anchor=None, signed=False, **options):
        source = SOURCE
        grants = []
        trusted = {}
        if signed:
            grants.append(fx.grant_for(source, key=self.key, permissions=("order",)))
            trusted = {"dev": self.key.public}
        extra = {} if anchor is None else {"authority_anchor": anchor}
        fx.write_install(self.config, trusted=trusted, grants=grants, **extra)
        child = self.plugins_dir / "fixture"
        if signed:
            fx.write_plugin(child, source, key=self.key, permissions=("order",))
        else:
            child.mkdir(mode=0o700, exist_ok=True)
            os.chmod(child, 0o700)
        stored, _created = breaker_module.open_breaker_store(self.state, clock=lambda: 0.0)
        self.addCleanup(stored.close)
        if anchor is not None:
            create_ledger(str(self.ledger), anchor)
        options.setdefault("gil_probe", FakeGilProbe())
        options.setdefault("clock", CLOCK)
        # The tip's host takes no device path. Passing one there would be a constructor error, not the refusal.
        if "device_token_path" in inspect.signature(plugins.PluginHost).parameters:
            options.setdefault("device_token_path", self.token_path)
        options.setdefault("verification_available", (lambda: True) if not signed else None)
        made = plugins.PluginHost(self.config, self.state, stored, **options)
        self.addCleanup(made.close)
        self.breaker = stored
        return made

    def loaded_host(self, *, anchor):
        """A host whose plugin is loaded holding ``order`` (unsigned, acknowledged) and whose run is bound.

        A charge passes the core's checks only for such a plugin, so a test that replaces the ledger's
        ``charge`` reaches its replacement only on this host.
        """
        fx.write_install(self.config, trusted={}, authority_anchor=anchor,
                         grants=[fx.grant_for(SOURCE, permissions=("order",), unsigned_order_acknowledged=True)])
        fx.write_plugin(self.plugins_dir / "fixture", SOURCE, permissions=("order",))
        stored, _created = breaker_module.open_breaker_store(self.state, clock=lambda: 0.0)
        self.addCleanup(stored.close)
        create_ledger(str(self.ledger), anchor)
        made = plugins.PluginHost(self.config, self.state, stored, gil_probe=FakeGilProbe(), clock=CLOCK,
                                  device_token_path=self.token_path, verification_available=lambda: True)
        self.addCleanup(made.close)
        self.breaker = stored
        self.assertEqual(made.records[0]["state"], "loaded", made.records[0])
        stored.bind_run(RUN_ID, CONNECTION, PLUGIN_ID, PROVIDER, MODEL)
        return made

    def owner(self):
        return plugins.OwnerSocketPeer(os.geteuid(), os.getpid())

    def record(self, host, approval_id):
        with host._approval_queue.store.transaction() as tx:  # noqa: SLF001 - the composed queue's own row
            return approval_rows.load(tx, approval_id)

    def charge_rows(self):
        if not self.ledger.exists():
            return []
        db = sqlite3.connect(f"file:{self.ledger}?mode=ro", uri=True)
        try:
            return db.execute("SELECT idempotency_key, amount_minor FROM charges").fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            db.close()


@unittest.skipUnless(LINUX, "the owner-only socket peer and the token file are checked by uid on Linux")
class PluginEffectApprovalTests(_HostCase):
    def test_the_owner_carries_a_plugin_effect_once_and_a_stranger_cannot(self):
        """The gap on this tip: nobody can approve. The contract after: the owner carries once."""
        host = self.host()
        effect = _effect()
        if getattr(host, "answer_plugin_effect", None) is None:
            approval_id, digest = host.gate.request(effect)
            with self.assertRaises(Refused) as refused:
                host.gate.decide(approval_id, digest, {"kind": "owner", "id": "local"}, True)
            self.fail("plugin effect refused: %s" % refused.exception.code)

        stranger = plugins.OwnerSocketPeer(os.geteuid() + 1, os.getpid())
        approval_id = host.request_plugin_effect(effect)
        self.assertEqual(self.record(host, approval_id)["state"], "pending")
        refused = host.answer_plugin_effect(stranger, approval_id, effect, authority=None)
        self.assertEqual(refused, {"code": "scope_refused", "detail": "approve"})
        self.assertEqual(self.record(host, approval_id)["state"], "pending")
        self.assertEqual(self.charge_rows(), [])

        plugin_shaped = {"kind": "plugin", "id": PLUGIN_ID}
        again = host.answer_plugin_effect(plugin_shaped, approval_id, effect, authority=None)
        self.assertEqual(again, {"code": "scope_refused", "detail": "approve"})
        self.assertEqual(self.record(host, approval_id)["state"], "pending")

    def test_a_plugin_credential_still_cannot_decide_on_the_in_memory_gate(self):
        host = self.host()
        effect = _effect()
        approval_id, digest = host.gate.request(effect)
        credentials = [{"kind": "owner", "id": "local", "uid": os.geteuid()}, "operator_ui"]
        if hasattr(plugins, "OwnerSocketPeer"):
            credentials.append(self.owner())
        for credential in credentials:
            with self.assertRaises(Refused) as refused:
                host.gate.decide(approval_id, digest, credential, True)
            self.assertEqual(refused.exception.code, "approval_requires_operator")

    def test_a_wrong_token_and_an_unreadable_peer_are_scope_refused(self):
        host = self.host()
        effect = _effect()
        approval_id = host.request_plugin_effect(effect)
        wrong = host.answer_plugin_effect(plugins.DevicePresentation("not-the-token"), approval_id, effect,
                                          authority=None)
        self.assertEqual(wrong, {"code": "scope_refused", "detail": "approve"})
        unread = host.answer_plugin_effect(plugins.OwnerSocketPeer(os.geteuid(), 2 ** 30), approval_id, effect,
                                           authority=None)
        self.assertEqual(unread, {"code": "scope_refused", "detail": "approve"})
        self.assertEqual(self.record(host, approval_id)["state"], "pending")
        self.assertEqual(self.charge_rows(), [])

    def test_a_denial_and_an_interruption_leave_the_effect_uncharged(self):
        anchor = "ab" * 32
        host = self.loaded_host(anchor=anchor)
        effect = _effect()
        peer = self.owner()
        denied_id = host.request_plugin_effect(effect)
        denied = host.answer_plugin_effect(peer, denied_id, effect, authority=None, answer="deny")
        self.assertEqual(denied["code"], "ok")
        self.assertEqual(denied["approval"]["state"], "denied")
        self.assertEqual(self.charge_rows(), [])

        approved_id = host.request_plugin_effect(_effect(payload={"note": "two"}))

        def interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt("synthetic interruption during the charge")

        host.authority.charge = interrupt
        with self.assertRaises(KeyboardInterrupt):
            host.answer_plugin_effect(peer, approved_id, _effect(payload={"note": "two"}),
                                      authority={"profile": {}, "signature": "00"}, answer="approve")
        self.assertEqual(self.record(host, approved_id)["state"], "approved")
        self.assertEqual(self.charge_rows(), [])

    def test_a_refused_charge_leaves_the_approval_approved(self):
        anchor = "cd" * 32
        host = self.loaded_host(anchor=anchor)
        effect = _effect()
        approval_id = host.request_plugin_effect(effect)

        def refuse(*_args, **_kwargs):
            raise Refused("budget_total_exceeded")

        host.authority.charge = refuse
        with self.assertRaises(Refused) as refused:
            host.answer_plugin_effect(self.owner(), approval_id, effect, authority={"profile": {}, "signature": "11"})
        self.assertEqual(refused.exception.code, "budget_total_exceeded")
        self.assertEqual(self.record(host, approval_id)["state"], "approved")
        self.assertEqual(self.charge_rows(), [])


@unittest.skipUnless(LINUX, "the owner-only socket peer and the token file are checked by uid on Linux")
@needs_crypto
class ChargedCarryTests(_HostCase):
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

    def test_the_owner_and_the_device_each_carry_once_and_a_replay_returns_the_receipt(self):
        if not backend_available():  # needs_crypto already skipped; kept so the reason stays next to the charge
            self.skipTest("cryptography not installed for this interpreter")
        host = self.composed()
        effect = _effect()
        approval_id = host.request_plugin_effect(effect)
        calls = []
        real = host.authority.charge

        def counting(*args, **kwargs):
            calls.append(kwargs.get("idempotency_key"))
            return real(*args, **kwargs)

        host.authority.charge = counting
        carried = host.answer_plugin_effect(self.owner(), approval_id, effect, authority=self.envelope())
        self.assertEqual(carried["approval"]["state"], "consumed")
        self.assertEqual(carried["approval"]["run_id"], RUN_ID)
        self.assertEqual(carried["approval"]["answered_by"], {"kind": "owner", "id": "local"})
        self.assertEqual(calls, [approval_id])
        self.assertEqual(self.charge_rows(), [(approval_id, effect["amount_minor"])])
        receipt = carried["charge"]

        with self.assertRaises(ApprovalRefused) as refused:
            host.effect_sink.carry(approval_id, effect)
        self.assertEqual(refused.exception.code, "already_consumed")
        self.assertEqual(calls, [approval_id])
        self.assertEqual(self.charge_rows(), [(approval_id, effect["amount_minor"])])

        # A later call has no memory of the receipt. It must call the ledger, hear effect_replayed
        # for this same approval id, and return the receipt the first charge produced.
        host._approver._receipts._memory.clear()  # noqa: SLF001 - the durable copy is what a new process reads
        host._approver._envelopes.clear()
        replayed = host._approver.charge(effect, approval_id)
        self.assertEqual(replayed, receipt)
        self.assertEqual(calls, [approval_id, approval_id])
        self.assertEqual(self.charge_rows(), [(approval_id, effect["amount_minor"])])

        other = _effect(payload={"note": "device"})
        # A second effect needs its own run binding only if the run id changes. Same run, new digest.
        device_id = host.request_plugin_effect(other)
        device = host.answer_plugin_effect(plugins.DevicePresentation(self.token), device_id, other,
                                           authority=self.envelope())
        self.assertEqual(device["approval"]["state"], "consumed")
        self.assertEqual(device["approval"]["answered_by"], {"kind": "device", "id": "page"})
        self.assertEqual(calls, [approval_id, approval_id, device_id])

        with self.assertRaises(ApprovalRefused) as refused:
            host.effect_sink.carry(device_id, other)
        self.assertEqual(refused.exception.code, "already_consumed")
        self.assertEqual(calls, [approval_id, approval_id, device_id])

    def test_two_callers_charge_once(self):
        host = self.composed()
        effect = _effect()
        approval_id = host.request_plugin_effect(effect)
        calls = []
        real = host.authority.charge

        def counting(*args, **kwargs):
            calls.append(kwargs["idempotency_key"])
            return real(*args, **kwargs)

        host.authority.charge = counting
        outcomes = []

        def once():
            try:
                outcomes.append(host.answer_plugin_effect(self.owner(), approval_id, effect,
                                                          authority=self.envelope()))
            except BaseException as exc:  # noqa: BLE001 - the second caller is recorded, not lost
                outcomes.append(exc)

        first, second = threading.Thread(target=once), threading.Thread(target=once)
        first.start()
        second.start()
        first.join(5)
        second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(len(outcomes), 2)
        consumed = [item for item in outcomes if isinstance(item, dict) and item.get("approval", {}).get("state") == "consumed"]
        refused = [item for item in outcomes if isinstance(item, ApprovalRefused)]
        self.assertEqual(len(consumed), 1)
        self.assertEqual(len(refused), 1)
        self.assertIn(refused[0].code, ("already_consumed", "already_resolved"))
        self.assertEqual(calls, [approval_id])
        self.assertEqual(self.record(host, approval_id)["state"], "consumed")


if __name__ == "__main__":
    unittest.main()
