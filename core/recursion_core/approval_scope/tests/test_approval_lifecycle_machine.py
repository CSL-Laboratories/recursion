"""Terminal-commit faults and sibling grants (seed 3371112).

Parent ac976993 deletes a tombstone and the live request before ``hmac.new``
inside the bloom returns. A fault there leaves an approved id in none of the
live map, the tomb map, or the bloom, so the id can be minted again and a
sibling grant approves whatever effect now sits under it.
"""

from __future__ import annotations

import dataclasses
import unittest

from recursion_core.approval_scope import ApprovalAuthority, ApprovalRequest, CredentialRegistry, Refused
from recursion_core.approval_scope import scope as scope_mod
from recursion_core.approval_scope.tests.test_approval_scope import T0, Clock, cred, request

# Reviewer reproduction seed. Effects minted below mix it in so the run is fixed.
SEED = 3371112


class TombCommitTests(unittest.TestCase):
    def _fill_tombs(self, clock: Clock, auth: ApprovalAuthority, plugin, device) -> str:
        """Expire exactly ``_MAX_TOMBSTONES`` requests. The next retirement hashes."""
        n = 0
        cap = scope_mod._MAX_TOMBSTONES
        while len(auth._request_tombs) < cap:
            room = cap - len(auth._request_tombs)
            batch = min(room, scope_mod.MAX_OPEN_PER_OPENER)
            expiry = clock.now + 5
            opened = []
            for _ in range(batch):
                opened.append(auth.open(plugin, request(
                    expires_at=expiry, effect={"n": n, "seed": SEED})))
                n += 1
            for item in opened:
                auth.show(device, item.request_id)
                auth.grant(device, [item.request_id], ttl_seconds=30)
            clock.now = expiry
            self.assertEqual(auth.state(opened[-1].request_id), "expired")
        self.assertEqual(len(auth._request_tombs), cap)
        self.assertEqual(len(auth._grant_tombs), cap)
        self.assertEqual(len(auth._requests), 0)
        return next(iter(auth._request_tombs))

    def test_hmac_fault_during_tomb_commit_approves_a_retired_id(self) -> None:
        clock = Clock(T0)
        registry = CredentialRegistry()
        auth = ApprovalAuthority(clock, registry)
        plugin = cred(registry, "plugin", {"control"})
        device = cred(registry, "paired_device", {"approve"})
        oldest = self._fill_tombs(clock, auth, plugin, device)
        opened = auth.open(plugin, request(
            expires_at=clock.now + 60, effect={"n": "live", "seed": SEED}))
        auth.show(device, opened.request_id)
        g1 = auth.grant(device, [opened.request_id], ttl_seconds=30)
        g2 = auth.grant(device, [opened.request_id], ttl_seconds=30)
        rid = opened.request_id
        tombs_before = list(auth._request_tombs)
        request_words = list(auth._request_bloom._words)
        grant_words = list(auth._grant_bloom._words)
        real = scope_mod.hmac.new
        calls = {"n": 0}

        def armed(*_args, **_kwargs):
            calls["n"] += 1
            scope_mod.hmac.new = real
            raise RuntimeError("tomb-commit")

        scope_mod.hmac.new = armed
        try:
            with self.assertRaises(RuntimeError) as ctx:
                auth.approve(device, g1.scope_id, rid, opened.digest)
            self.assertEqual(str(ctx.exception), "tomb-commit")
        finally:
            scope_mod.hmac.new = real
        self.assertEqual(calls["n"], 1)
        # The fault is pre-commit: the live row, both tomb maps, and both blooms
        # are the ones approve started with. The id is not free to mint again.
        self.assertEqual(auth.state(rid), "pending")
        self.assertIn(rid, auth._requests)
        self.assertEqual(auth._requests[rid].state, "pending")
        self.assertEqual(list(auth._request_tombs), tombs_before)
        self.assertEqual(next(iter(auth._request_tombs)), oldest)
        self.assertNotIn(oldest, auth._request_bloom)
        self.assertNotIn(rid, auth._request_tombs)
        self.assertNotIn(rid, auth._request_bloom)
        self.assertEqual(list(auth._request_bloom._words), request_words)
        self.assertEqual(list(auth._grant_bloom._words), grant_words)
        self.assertEqual(auth._grants[g1.scope_id].used, set())
        self.assertEqual(auth._grants[g2.scope_id].used, set())
        draft = request(expires_at=clock.now + 60, effect={"n": "remint", "seed": SEED})
        real_hex = scope_mod.secrets.token_hex
        draws = {"n": 0}

        def fake(n, _real=real_hex):
            draws["n"] += 1
            if draws["n"] == 1:
                return rid
            return _real(n)

        scope_mod.secrets.token_hex = fake
        try:
            reopened = auth.open(plugin, draft)
        finally:
            scope_mod.secrets.token_hex = real_hex
        self.assertNotEqual(reopened.request_id, rid)
        self.assertNotEqual(reopened.digest, opened.digest)
        # A sibling grant still bound to the retired digest cannot approve the
        # reminted effect, and it cannot approve the original twice.
        with self.assertRaises(Refused) as ctx:
            auth.approve(device, g2.scope_id, reopened.request_id, opened.digest)
        self.assertEqual(ctx.exception.code, "scope_binding")
        out = auth.approve(device, g1.scope_id, rid, opened.digest)
        self.assertEqual(out["outcome"], "approved")
        self.assertEqual(out["digest"], opened.digest)
        self.assertEqual(auth.state(rid), "approved")
        with self.assertRaises(Refused) as ctx:
            auth.approve(device, g2.scope_id, rid, opened.digest)
        self.assertEqual(ctx.exception.code, "request_answered")

    def test_sibling_grant_cannot_approve_a_different_unshown_effect(self) -> None:
        clock = Clock(T0)
        registry = CredentialRegistry()
        auth = ApprovalAuthority(clock, registry)
        plugin = cred(registry, "plugin", {"control"})
        device = cred(registry, "paired_device", {"approve"})
        opened = auth.open(plugin, request(expires_at=clock.now + 60))
        auth.show(device, opened.request_id)
        g1 = auth.grant(device, [opened.request_id], ttl_seconds=30)
        g2 = auth.grant(device, [opened.request_id], ttl_seconds=30)
        entry = auth._requests[opened.request_id]
        self.assertIn(device.serial, entry.shown)
        other = ApprovalRequest.create(
            {"kind": "example.write", "target": "res-2", "amount": 9, "payload": {"k": "v"}},
            [], opened.request.expires_at)
        replacement = dataclasses.replace(other, request_id=opened.request_id)
        self.assertNotEqual(replacement.digest, opened.digest)
        # Shown for the original binding, now holding a different effect.
        entry.request = replacement
        with self.assertRaises(Refused) as ctx:
            auth.approve(device, g2.scope_id, opened.request_id, opened.digest)
        self.assertEqual(ctx.exception.code, "digest_mismatch")
        self.assertEqual(auth.state(opened.request_id), "pending")
        self.assertEqual(auth._grants[g1.scope_id].used, set())
        self.assertEqual(auth._grants[g2.scope_id].used, set())
        # Same effect the grant bound, but this approver has not been shown it.
        entry.request = opened.request
        entry.shown.clear()
        with self.assertRaises(Refused) as ctx:
            auth.approve(device, g2.scope_id, opened.request_id, opened.digest)
        self.assertEqual(ctx.exception.code, "prompt_not_shown")
        self.assertEqual(auth.state(opened.request_id), "pending")
        auth.show(device, opened.request_id)
        out = auth.approve(device, g2.scope_id, opened.request_id, opened.digest)
        self.assertEqual(out["outcome"], "approved")
        self.assertEqual(out["digest"], opened.digest)
        with self.assertRaises(Refused) as ctx:
            auth.approve(device, g1.scope_id, opened.request_id, opened.digest)
        self.assertEqual(ctx.exception.code, "request_answered")


if __name__ == "__main__":
    unittest.main()
