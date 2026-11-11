"""K3 authority profiles: the bounded per-Authority cache of positive signature verdicts.

The cache holds only "``verify`` returned True for this (signature, canonical bytes)" for
one Authority's fixed anchor. Every other check stays per charge; these tests count the
real ``verify`` calls through a wrapper around the module's ``verify``.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import unittest

from recursion_core.plugin_security import Authority, Refused, create_ledger
from recursion_core.plugin_security import authority as authority_module
from recursion_core.plugin_security.tests.support import (
    PLUGIN_SOURCE, Clock, Key, needs_crypto, profile, scope_of, temp_dir,
)

DIGEST = hashlib.sha256(PLUGIN_SOURCE).hexdigest()
CALL = dict(plugin_id="demo.plugin", artifact_sha256=DIGEST, kind="order.submit",
            provider="fake-provider", model_version="fake-model-1", currency="USD")
_gil_off = hasattr(sys, "_is_gil_enabled") and not sys._is_gil_enabled()


@needs_crypto
class VerifyCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.installer = Key()
        self.clock = Clock()
        self.path = os.path.join(temp_dir(self), "ledger.db")
        create_ledger(self.path, self.installer.public)
        self.auth = self._open()
        self.verifies = 0
        real = authority_module.verify
        count_lock = threading.Lock()

        def counted(*args):
            with count_lock:
                self.verifies += 1
            return real(*args)

        authority_module.verify = counted
        self.addCleanup(setattr, authority_module, "verify", real)

    def _open(self) -> Authority:
        auth = Authority(self.installer.public, self.path, self.clock)
        self.addCleanup(auth.close)
        return auth

    def _charge(self, envelope, amount, key, auth=None, **overrides):
        return (auth or self.auth).charge(envelope, **{**CALL, **overrides}, scope=scope_of(),
                                          amount_minor=amount, idempotency_key=key)

    def _refused(self, code: str, fn, *args, **kwargs) -> None:
        with self.assertRaises(Refused) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)

    def test_hit_skips_verify_and_returns_same_result(self) -> None:
        env = profile(self.installer)
        first = self._charge(env, 1_000, "k1")
        self.assertEqual(self.verifies, 1)
        second = self._charge(env, 1_000, "k2")
        self.assertEqual(self.verifies, 1)  # served from the cache
        self.assertEqual(first, {"profile_id": "prof-1", "spent_minor": 1_000, "remaining_minor": 9_000})
        self.assertEqual(second, {"profile_id": "prof-1", "spent_minor": 2_000, "remaining_minor": 8_000})
        # A hit decides nothing else: replay, budget, window, scope and action still refuse per charge.
        self._refused("effect_replayed", self._charge, env, 1_000, "k2")
        self._refused("budget_max_effect_exceeded", self._charge, env, 6_000, "k3")
        self._refused("profile_plugin_mismatch", self._charge, env, 100, "k4", plugin_id="other.plugin")
        self._refused("profile_action_not_permitted", self._charge, env, 100, "k5", kind="order.cancel")
        self._charge(env, 5_000, "k6")
        self._refused("budget_total_exceeded", self._charge, env, 3_001, "k7")
        self.clock.now = 2_000_000
        self._refused("profile_expired", self._charge, env, 100, "k8")
        self.assertEqual(self.verifies, 1)

    def test_other_signature_or_byte_misses(self) -> None:
        env = profile(self.installer)
        self._charge(env, 100, "k1")
        self.assertEqual(self.verifies, 1)
        sig = env["signature"]
        bad_sig = dict(env, signature=sig[:-1] + ("0" if sig[-1] != "0" else "1"))
        self._refused("profile_signature_invalid", self._charge, bad_sig, 100, "k2")
        self.assertEqual(self.verifies, 2)
        other_sig = dict(env, signature=self.installer.sign(b"other bytes"))
        self._refused("profile_signature_invalid", self._charge, other_sig, 100, "k3")
        self.assertEqual(self.verifies, 3)
        # One byte changed in the profile (total 10_000 -> 10_001) under the cached signature.
        edited = dict(env, profile=dict(env["profile"], budgets=dict(env["profile"]["budgets"],
                                                                     total_minor=10_001)))
        self._refused("profile_signature_invalid", self._charge, edited, 100, "k4")
        self.assertEqual(self.verifies, 4)
        # The same edit genuinely signed is a different key: verified, then the ledger binding decides.
        resigned = profile(self.installer, total=10_001)
        self._refused("profile_id_rebound", self._charge, resigned, 100, "k5")
        self.assertEqual(self.verifies, 5)

    def test_failed_verify_is_not_cached(self) -> None:
        forged = profile(Key())
        for i in range(3):
            self._refused("profile_signature_invalid", self._charge, forged, 100, "k%d" % i)
        self.assertEqual(self.verifies, 3)
        self.assertEqual(len(self.auth._verified), 0)
        non_str = dict(forged, signature=["not", "hex"])
        self._refused("profile_signature_invalid", self._charge, non_str, 100, "k-list")
        self.assertEqual(len(self.auth._verified), 0)

    def test_bound_holds_and_evicts_least_recent(self) -> None:
        size = authority_module._VERIFY_CACHE_SIZE
        envs = [profile(self.installer, profile_id="prof-%d" % i) for i in range(size + 10)]
        for i, env in enumerate(envs):
            self._charge(env, 100, "k-%d" % i)
        self.assertEqual(self.verifies, size + 10)
        self.assertEqual(len(self.auth._verified), size)
        self._charge(envs[-1], 100, "k-last-again")  # most recent: still cached
        self.assertEqual(self.verifies, size + 10)
        self._charge(envs[0], 100, "k-first-again")  # evicted: verified again
        self.assertEqual(self.verifies, size + 11)
        self.assertEqual(len(self.auth._verified), size)

    def test_instances_do_not_share(self) -> None:
        env = profile(self.installer)
        other = self._open()  # same anchor, same ledger, separate instance
        self._charge(env, 100, "k1")
        self._charge(env, 100, "k2", auth=other)
        self.assertEqual(self.verifies, 2)
        self.assertIsNot(self.auth._verified, other._verified)
        # A different anchor's instance never sees this anchor's verdicts either.
        stranger = Key()
        path = os.path.join(temp_dir(self), "ledger2.db")
        create_ledger(path, stranger.public)
        foreign = Authority(stranger.public, path, self.clock)
        self.addCleanup(foreign.close)
        self._refused("profile_signature_invalid", self._charge, env, 100, "k3", auth=foreign)
        self.assertEqual(len(foreign._verified), 0)

    @unittest.skipUnless(_gil_off, "needs a free-threaded interpreter run with -X gil=0")
    def test_concurrent_charges_consistent(self) -> None:
        # 8 threads x 40 charges of 100 against a signed total of 10_000 (exactly 100 fit),
        # interleaved with forged and evicting envelopes so the cache is read, filled and
        # trimmed concurrently.
        env = profile(self.installer)
        forged = profile(Key())
        size = authority_module._VERIFY_CACHE_SIZE
        fillers = [profile(self.installer, profile_id="fill-%d" % i) for i in range(size + 64)]
        results: dict = {}
        errors: list = []
        barrier = threading.Barrier(8)
        lock = threading.Lock()

        def worker(t: int) -> None:
            try:
                barrier.wait()
                for i in range(40):
                    try:
                        self._charge(env, 100, "k-%d-%d" % (t, i))
                        code = "ok"
                    except Refused as exc:
                        code = exc.code
                    try:
                        self._charge(forged, 100, "f-%d-%d" % (t, i))
                        forged_code = "ok"
                    except Refused as exc:
                        forged_code = exc.code
                    filler = fillers[(t * 40 + i) % len(fillers)]
                    self._charge(filler, 1, "x-%d-%d" % (t, i))
                    with lock:
                        results[code] = results.get(code, 0) + 1
                        results["forged:" + forged_code] = results.get("forged:" + forged_code, 0) + 1
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        self.assertEqual(results, {"ok": 100, "budget_total_exceeded": 220,
                                   "forged:profile_signature_invalid": 320})
        self.assertLessEqual(len(self.auth._verified), size)
        out = self._charge(profile(self.installer, profile_id="prof-2"), 100, "k-after")
        self.assertEqual(out["remaining_minor"], 9_900)
        spent = self.auth._db.execute("SELECT SUM(amount_minor) FROM charges WHERE profile_id='prof-1'").fetchone()[0]
        self.assertEqual(spent, 10_000)


if __name__ == "__main__":
    unittest.main()
