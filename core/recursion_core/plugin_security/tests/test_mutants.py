"""B17 (CSL-787): authority-anchor mutants under the same-uid, in-process fallback.

Each mutant is a real plugin artifact loaded through ``PluginLoader`` *with no granted
permissions*, then the next order effect is attempted through the protocol. The test
pins the observed outcome of every mutant, both ways:

* ``blocked`` mutants must stay blocked;
* ``succeeds`` mutants are the known gap. In-process Python running as the daemon's uid
  can rewrite any core object or file, and these tests prove it rather than hide it.
  If a later change blocks one, the test fails so the table is updated on purpose.

Evidence class: locally observed on the interpreter running the suite. This is not the
C2-47 "m24" set itself (its exact list is in csl-research, not here); it is the same
attack family (anchor, verifier, ledger, approval state, forged and replayed input)
against this implementation. The separate-uid arm (F21) is not run here.
"""

from __future__ import annotations

import json
import os
import unittest

from recursion_core.plugin_security import ApprovalGate, Authority, Refused, canonical
from recursion_core.plugin_security.tests.support import (
    World, authenticate, effect, manifest_bytes, needs_crypto, profile,
)

# name -> (exact observed outcome, plugin source or None)
MUTANTS = {
    # Blocked: each needs a secret or state the plugin does not have.
    "forge_profile_with_own_key": ("profile_signature_invalid", None),
    "replay_charged_effect_after_restart": ("approval_required", None),
    "delete_ledger_then_restart": ("ledger_missing", None),
    # A plugin that names itself "host" to reach host-only ops (run.fault, breaker.report).
    "claim_host_identity": ("manifest_plugin_id_reserved", None),
    # Succeeds: same-uid in-process code rewrites the core. Not contained.
    "swap_anchor_attribute": ("succeeds", b"""
import gc
from recursion_core.plugin_security.authority import Authority
for obj in gc.get_objects():
    if isinstance(obj, Authority):
        obj._anchor = ATTACKER_PUBLIC
"""),
    "patch_verify_function": ("succeeds", b"""
import recursion_core.plugin_security.authority as authority_module
authority_module.verify = lambda *args: True
"""),
    "plant_verify_cache_entry": ("succeeds", b"""
import gc
from recursion_core.plugin_security.authority import Authority
for obj in gc.get_objects():
    if isinstance(obj, Authority):
        with obj._verified_lock:
            obj._verified[(FORGED_SIGNATURE, FORGED_PROFILE)] = None
"""),
    "rewrite_ledger_rows": ("succeeds", b"""
import gc
from recursion_core.plugin_security.authority import Authority
for obj in gc.get_objects():
    if isinstance(obj, Authority):
        try:
            obj._db.execute("DELETE FROM charges")
        except Exception:
            pass
"""),
    "self_approve_in_gate_memory": ("succeeds", b"""
import gc
from recursion_core.plugin_security.approval import ApprovalGate
for obj in gc.get_objects():
    if isinstance(obj, ApprovalGate):
        for item in obj._approvals.values():
            if item.state == "pending":
                item.state = "approved"
"""),
}


@needs_crypto
class SameUidMutantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World(self)
        self.w.load()
        self.w.bind()
        # Spend the whole budget legitimately first, so any later success is a cap bypass.
        self.env = profile(self.w.installer, total=2_000, max_effect=2_000)
        first = effect(amount=2_000)
        self.first_approval = self.w.approve(first)
        self.assertTrue(self.w.authorize(first, self.first_approval, self.env)["ok"])

    def tearDown(self) -> None:
        import recursion_core.plugin_security.authority as authority_module
        from recursion_core.plugin_security import signing
        authority_module.verify = signing.verify  # undo the module patch for later tests

    def _load_mutant(self, name: str, source: bytes) -> None:
        attacker = self.w.developer.__class__()
        source = source.replace(b"ATTACKER_PUBLIC", json.dumps(attacker.public).encode())
        if name == "plant_verify_cache_entry":
            self.forged = profile(attacker, profile_id="fresh", total=10**6, max_effect=10**6)
            source = source.replace(b"FORGED_SIGNATURE", repr(self.forged["signature"]).encode())
            source = source.replace(b"FORGED_PROFILE", repr(canonical(self.forged["profile"])).encode())
        raw = manifest_bytes(attacker.public, plugin_id="mutant." + name.replace("_", "-"), source=source,
                             permissions=())
        loaded = self.w.loader.load(raw, attacker.sign(raw), source)
        self.assertEqual(loaded.permissions, ())  # no grant at all
        self.attacker = attacker

    def _result_of(self, out: dict) -> str:
        return "succeeds" if out["ok"] else out["refusal"]["code"]

    def _attempt(self, name: str) -> str:
        """Try to spend past an exhausted budget, or act without an operator approval."""
        eff = effect(amount=1_500, payload={"mutant": name})
        if name == "forge_profile_with_own_key":
            forged = profile(self.w.developer.__class__(), profile_id="fresh", total=10**6, max_effect=10**6)
            return self._result_of(self.w.authorize(eff, self.w.approve(eff), forged))
        if name in ("swap_anchor_attribute", "patch_verify_function"):
            forged = profile(self.attacker, profile_id="fresh", total=10**6, max_effect=10**6)
            return self._result_of(self.w.authorize(eff, self.w.approve(eff), forged))
        if name == "plant_verify_cache_entry":
            return self._result_of(self.w.authorize(eff, self.w.approve(eff), self.forged))
        if name == "rewrite_ledger_rows":
            return self._result_of(self.w.authorize(eff, self.w.approve(eff), self.env))
        if name == "self_approve_in_gate_memory":
            note = effect(kind="note.post", amount=0, payload={"mutant": name})
            req = self.w.call(self.w.plugin, "effect.request", effect=note)["result"]
            self._load_mutant(name, MUTANTS[name][1])  # no operator decision is ever made
            return self._result_of(self.w.authorize(note, req["approval_id"], None))
        if name == "replay_charged_effect_after_restart":
            # A restart drops in-memory approvals and reopens the same ledger.
            self.w.authority.close()
            self.w.core._authority = Authority(self.w.installer.public, self.w.ledger, self.w.clock)
            self.addCleanup(self.w.core._authority.close)
            self.w.core._gate = ApprovalGate(lambda *a: None, authenticate, self.w.clock)
            return self._result_of(self.w.authorize(effect(amount=2_000), self.first_approval, self.env))
        if name == "claim_host_identity":
            attacker = self.w.developer.__class__()
            raw = manifest_bytes(attacker.public, plugin_id="host", source=b"x = 1\n", permissions=())
            try:
                self.w.loader.load(raw, attacker.sign(raw), b"x = 1\n")
            except Refused as exc:
                return exc.code
            # Loaded: its client carries the caller name "host" and passes the host check.
            spoofed = self.w.core.client_for("host")
            out = self.w.call(spoofed, "breaker.report", connection="conn-a", event_type="plan_limit")
            return self._result_of(out)
        if name == "delete_ledger_then_restart":
            self.w.authority.close()
            os.unlink(self.w.ledger)
            try:
                Authority(self.w.installer.public, self.w.ledger, self.w.clock)
            except Refused as exc:
                return exc.code
            return "succeeds"
        raise AssertionError(name)

    def test_mutant_table(self) -> None:
        observed = {}
        for name, (expected, source) in MUTANTS.items():
            with self.subTest(mutant=name):
                try:
                    self.setUp()
                    if source is not None and name != "self_approve_in_gate_memory":
                        self._load_mutant(name, source)
                    observed[name] = self._attempt(name)
                    self.assertEqual(observed[name], expected, name)
                finally:
                    # subTest suppresses an assertion only after its body exits. Restore
                    # the verifier and close this World's stores even when that assertion fails.
                    self.tearDown()
                    self.doCleanups()
        succeeded = sorted(n for n, o in observed.items() if o == "succeeds")
        # The known same-uid gap, pinned: five of nine mutants succeed in-process.
        self.assertEqual(succeeded, ["patch_verify_function", "plant_verify_cache_entry", "rewrite_ledger_rows",
                                     "self_approve_in_gate_memory", "swap_anchor_attribute"])


if __name__ == "__main__":
    unittest.main()
