"""B17 (CSL-787) hardening on the same-uid fallback, plus bounded core state.

* A plugin cannot take the host's identity. Host-only ops (``run.bind``, ``run.fault``,
  ``breaker.report``) are authorised by the caller name the core binds into a client;
  before this change a manifest could name itself ``host`` and its client then passed
  the host check (Devin r4503755986). ``host`` is now a reserved plugin id, refused at
  manifest parse, so the loader never activates it and no plugin client carries it.
* The approval gate keeps bounded state: terminal approvals drop their effect bytes and
  keep only a bounded replay tombstone; expired pending approvals are swept (Devin
  r4503755867).
* The ledger indexes charges by profile, so the per-charge total is not a full scan
  (Devin r4503755920).

None of this contains hostile in-process code; see ``test_mutants.py`` for the four
same-uid attacks that still succeed and need a separate user or process (F21).
"""

from __future__ import annotations

import hashlib
import sqlite3
import unittest

from recursion_core.plugin_security import (
    HOST, ApprovalGate, CoreHealth, Grant, PluginLoader, Refused, ReservedEffects, TrustStore,
    parse_manifest,
)
from recursion_core.plugin_security.manifest import RESERVED_PLUGIN_IDS
from recursion_core.plugin_security.tests.support import (
    PLUGIN_SOURCE, Clock, FakeGilProbe, Key, World, authenticate, effect, manifest_bytes, needs_crypto,
    OPERATOR_TOKEN,
)


def _code(response: dict) -> str | None:
    return None if response["ok"] else response["refusal"]["code"]


@needs_crypto
class HostIdentityTests(unittest.TestCase):
    def test_host_is_a_reserved_plugin_id(self) -> None:
        self.assertIn(HOST, RESERVED_PLUGIN_IDS)
        dev = Key()
        with self.assertRaises(Refused) as ctx:
            parse_manifest(manifest_bytes(dev.public, plugin_id=HOST))
        self.assertEqual(ctx.exception.code, "manifest_plugin_id_reserved")

    def test_signed_and_granted_host_plugin_is_never_activated(self) -> None:
        # Even a trusted developer key plus an installer grant for "host" cannot load it.
        dev = Key()
        from recursion_core.plugin_security.signing import key_id
        raw = manifest_bytes(dev.public, plugin_id=HOST)
        grant = Grant(HOST, hashlib.sha256(PLUGIN_SOURCE).hexdigest(), key_id(dev.public),
                      ("read", "propose", "order"))
        loader = PluginLoader(TrustStore({"dev": dev.public}), {HOST: grant},
                              ReservedEffects(frozenset(), ()), CoreHealth(), FakeGilProbe())
        with self.assertRaises(Refused) as ctx:
            loader.load(raw, dev.sign(raw), PLUGIN_SOURCE)
        self.assertEqual(ctx.exception.code, "manifest_plugin_id_reserved")
        self.assertEqual(loader.permissions(HOST), ())
        self.assertIsNone(loader.artifact(HOST))

    def test_doctor_reports_the_reserved_id(self) -> None:
        dev = Key()
        w = World(self)
        raw = manifest_bytes(dev.public, plugin_id=HOST)
        findings = w.loader.doctor(raw, dev.sign(raw), PLUGIN_SOURCE)
        self.assertIn("manifest_plugin_id_reserved", [f["code"] for f in findings])


class ApprovalRetentionTests(unittest.TestCase):
    def _gate(self, clock: Clock, cap: int) -> ApprovalGate:
        return ApprovalGate(lambda *a: None, authenticate, clock, ttl_seconds=300, max_retained=cap)

    def _approve(self, gate: ApprovalGate, eff: dict) -> str:
        approval_id, digest = gate.request(eff)
        gate.decide(approval_id, digest, OPERATOR_TOKEN, True)
        return approval_id

    def test_used_approvals_are_bounded_and_hold_no_effect_bytes(self) -> None:
        clock = Clock()
        gate = self._gate(clock, cap=10)
        ids = []
        for i in range(50):
            eff = effect(kind="note.post", amount=0, payload={"n": i})
            approval_id = self._approve(gate, eff)
            gate.take(approval_id, eff)
            ids.append((approval_id, eff))
        self.assertLessEqual(len(gate._approvals), 10)
        self.assertTrue(all(item.raw is None for item in gate._approvals.values()))
        last_id, last_eff = ids[-1]
        with self.assertRaises(Refused) as ctx:
            gate.take(last_id, last_eff)
        self.assertEqual(ctx.exception.code, "approval_replayed")  # recent replay still named
        first_id, first_eff = ids[0]
        with self.assertRaises(Refused) as ctx:
            gate.take(first_id, first_eff)
        self.assertEqual(ctx.exception.code, "approval_required")  # evicted: still refused

    def test_expired_and_vetoed_approvals_are_swept(self) -> None:
        clock = Clock()
        gate = self._gate(clock, cap=5)
        for i in range(20):
            gate.request(effect(kind="note.post", amount=0, payload={"p": i}))
        vetoed_id, digest = gate.request(effect(kind="note.post", amount=0, payload={"v": 1}))
        gate.decide(vetoed_id, digest, OPERATOR_TOKEN, False)
        clock.now += 301
        live_id, _ = gate.request(effect(kind="note.post", amount=0, payload={"live": 1}))
        pending = [k for k, v in gate._approvals.items() if v.state == "pending"]
        self.assertEqual(pending, [live_id])
        self.assertLessEqual(len(gate._approvals), 6)  # one live + at most five tombstones

    def test_tombstone_keeps_expired_and_digest_refusals(self) -> None:
        clock = Clock()
        gate = self._gate(clock, cap=5)
        eff = effect(kind="note.post", amount=0, payload={"x": 1})
        approval_id = self._approve(gate, eff)
        clock.now += 301
        gate.request(effect(kind="note.post", amount=0, payload={"sweep": 1}))  # sweeps it
        self.assertIsNone(gate._approvals[approval_id].raw)
        with self.assertRaises(Refused) as ctx:
            gate.take(approval_id, eff)
        self.assertEqual(ctx.exception.code, "approval_expired")
        with self.assertRaises(Refused) as ctx:
            gate.take(approval_id, effect(kind="note.post", amount=0, payload={"x": 2}))
        self.assertEqual(ctx.exception.code, "approval_digest_mismatch")

    def test_sweep_touches_only_due_entries(self) -> None:
        # Devin r4507067541: a burst inside the TTL must not rescan every entry.
        class CountingDict(dict):
            scans = 0

            def items(self):
                CountingDict.scans += 1
                return super().items()

            def values(self):
                CountingDict.scans += 1
                return super().values()

        clock = Clock()
        gate = self._gate(clock, cap=10)
        gate._approvals = CountingDict()
        for i in range(200):
            gate.request(effect(kind="note.post", amount=0, payload={"b": i}))
        self.assertEqual(CountingDict.scans, 0)
        self.assertEqual(len(gate._approvals), 200)  # all live, none due
        clock.now += 301
        gate.request(effect(kind="note.post", amount=0, payload={"after": 1}))
        self.assertEqual(CountingDict.scans, 0)
        live = [k for k, v in dict.items(gate._approvals) if v.state == "pending"]
        self.assertEqual(len(live), 1)
        self.assertLessEqual(len(gate._approvals), 11)

    def test_terminal_approvals_do_not_grow_the_expiry_heap(self) -> None:
        # Devin r4507084791: take and decide retire an approval before its TTL,
        # but _retire capped only the tombstone dictionary. Until a later
        # request swept expired entries, _expiry kept one tuple for every
        # completed request in the TTL window, even after its tombstone was
        # evicted. With max_retained=10 and a fixed clock, request-and-veto
        # (or use) of 200 approvals left 10 tombstones and 200 heap tuples.
        clock = Clock()
        vetoed = self._gate(clock, cap=10)
        for i in range(200):
            approval_id, digest = vetoed.request(
                effect(kind="note.post", amount=0, payload={"v": i})
            )
            vetoed.decide(approval_id, digest, OPERATOR_TOKEN, False)
        self.assertLessEqual(len(vetoed._approvals), 10)
        self.assertLessEqual(len(vetoed._expiry), 10)

        used = self._gate(clock, cap=10)
        for i in range(200):
            eff = effect(kind="note.post", amount=0, payload={"u": i})
            approval_id = self._approve(used, eff)
            used.take(approval_id, eff)
        self.assertLessEqual(len(used._approvals), 10)
        self.assertLessEqual(len(used._expiry), 10)
        self.assertTrue(all(item.raw is None for item in used._approvals.values()))

    def test_expiry_heap_compaction_is_amortized(self) -> None:
        # Devin r4507255083: with many pending approvals and a small
        # max_retained, compacting when stale > max_retained rebuilt the
        # whole heap under the lock roughly every second veto (example:
        # max_retained=1, 10,000 pending, then 10,000 vetoes — about 50
        # million tuples scanned). Compact only when stale tuples exceed
        # max(max_retained, live) so rebuild work stays linear.
        class CountingGate(ApprovalGate):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.compactions = 0

            def _compact_expiry_if_needed(self) -> None:
                before = id(self._expiry)
                super()._compact_expiry_if_needed()
                if id(self._expiry) != before:
                    self.compactions += 1

        clock = Clock()
        gate = CountingGate(
            lambda *a: None, authenticate, clock, ttl_seconds=300, max_retained=1
        )
        n = 200
        pending = []
        for i in range(n):
            pending.append(gate.request(
                effect(kind="note.post", amount=0, payload={"a": i})
            ))
        self.assertEqual(gate.compactions, 0)
        for approval_id, digest in pending:
            gate.decide(approval_id, digest, OPERATOR_TOKEN, False)
        # The old threshold compacted about every second veto (~n/2).
        self.assertLess(gate.compactions, n // 4)
        self.assertLessEqual(len(gate._expiry), 1)
        self.assertLessEqual(len(gate._approvals), 1)

    def test_cap_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            self._gate(Clock(), cap=0)


@needs_crypto
class LedgerIndexTests(unittest.TestCase):
    def test_profile_total_uses_an_index(self) -> None:
        w = World(self)
        db = sqlite3.connect(w.ledger)
        self.addCleanup(db.close)
        plan = " ".join(str(row) for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT COALESCE(SUM(amount_minor), 0) FROM charges WHERE profile_id=?",
            ("p",)))
        self.assertIn("charges_by_profile", plan)

    def test_older_ledger_gains_the_index_on_open(self) -> None:
        from recursion_core.plugin_security import Authority
        w = World(self)
        w.authority.close()
        db = sqlite3.connect(w.ledger)
        db.execute("DROP INDEX charges_by_profile")
        db.close()
        Authority(w.installer.public, w.ledger, w.clock).close()
        db = sqlite3.connect(w.ledger)
        self.addCleanup(db.close)
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")]
        self.assertIn("charges_by_profile", names)


if __name__ == "__main__":
    unittest.main()
