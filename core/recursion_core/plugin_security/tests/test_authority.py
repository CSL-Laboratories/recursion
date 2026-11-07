"""K3 authority profiles: forgery, scope, expiry, split, replay, restart, concurrency, money."""

from __future__ import annotations

import multiprocessing
import os
import threading
import unittest

from recursion_core.plugin_security import Authority, Refused, canonical, create_ledger
from recursion_core.plugin_security.signing import key_id
from recursion_core.plugin_security.tests.support import (
    PLUGIN_SOURCE, Clock, Key, needs_crypto, profile, temp_dir,
)
import hashlib

DIGEST = hashlib.sha256(PLUGIN_SOURCE).hexdigest()
SCOPE = dict(plugin_id="demo.plugin", artifact_sha256=DIGEST, account="paper-acct", venue="paper",
             currency="USD")


def _charge(auth: Authority, envelope, amount, key, **overrides):
    return auth.charge(envelope, **{**SCOPE, **overrides}, amount_minor=amount, idempotency_key=key)


def _child_charges(path: str, anchor: str, envelope: dict, prefix: str, count: int, out) -> None:
    auth = Authority(anchor, path, lambda: 1_000_000)
    ok = 0
    for i in range(count):
        try:
            _charge(auth, envelope, 100, "%s-%d" % (prefix, i))
            ok += 1
        except Refused:
            pass
    auth.close()
    out.put(ok)


@needs_crypto
class AuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.installer = Key()
        self.clock = Clock()
        self.path = os.path.join(temp_dir(self), "ledger.db")
        create_ledger(self.path, self.installer.public)
        self.auth = self._open()

    def _open(self) -> Authority:
        auth = Authority(self.installer.public, self.path, self.clock)
        self.addCleanup(auth.close)
        return auth

    def _refused(self, code: str, fn, *args, **kwargs) -> None:
        with self.assertRaises(Refused) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)

    def test_valid_charge(self) -> None:
        out = _charge(self.auth, profile(self.installer), 1_000, "k1")
        self.assertEqual(out, {"profile_id": "prof-1", "spent_minor": 1_000, "remaining_minor": 9_000})

    def test_forged_profiles_refused(self) -> None:
        plugin_key = Key()
        genuine = profile(self.installer)
        forged = profile(plugin_key)  # a plugin signing its own profile
        widened = dict(genuine, profile=dict(genuine["profile"], budgets=dict(
            genuine["profile"]["budgets"], total_minor=10**9)))
        for name, env in {"plugin_signed": forged, "widened_after_signing": widened,
                          "no_signature": dict(genuine, signature=""),
                          "signature_of_other_bytes": dict(genuine, signature=self.installer.sign(b"x"))}.items():
            with self.subTest(name):
                self._refused("profile_signature_invalid", _charge, self.auth, env, 100, "k-" + name)
        self._refused("profile_envelope", _charge, self.auth, {"profile": genuine["profile"]}, 100, "k-x")
        self._refused("profile_noncanonical", _charge, self.auth,
                      dict(genuine, profile=dict(genuine["profile"], not_before=0.5)), 100, "k-y")

    def test_anchor_rules(self) -> None:
        self._refused("anchor_is_csl_key", Authority, self.installer.public, self.path, self.clock,
                      frozenset({key_id(self.installer.public)}))
        self._refused("ledger_anchor_mismatch", Authority, Key().public, self.path, self.clock)
        self._refused("ledger_missing", Authority, self.installer.public, self.path + ".gone", self.clock)
        self.assertRaises(FileExistsError, create_ledger, self.path, self.installer.public)

    def test_signed_but_invalid_profiles_refused(self) -> None:
        cases = {
            "profile_issuer_or_permission": dict(issuer="csl"),
            "profile_scope": dict(scope={"account": "a"}),
            "profile_budgets": dict(budgets={"currency": "USD", "max_effect_minor": 9, "total_minor": 5}),
            "profile_window": dict(not_before=5, expires_at=5),
        }
        for code, change in cases.items():
            with self.subTest(code=code):
                self._refused(code, _charge, self.auth, profile(self.installer, **change), 1, "k-" + code)

    def test_scope_expiry_and_binding(self) -> None:
        env = profile(self.installer, not_before=999_000, expires_at=1_000_100)
        self._refused("profile_scope_mismatch", _charge, self.auth, env, 1, "a", account="other")
        self._refused("profile_scope_mismatch", _charge, self.auth, env, 1, "b", venue="live")
        self._refused("profile_plugin_mismatch", _charge, self.auth, env, 1, "c", plugin_id="other.plugin")
        self._refused("profile_plugin_mismatch", _charge, self.auth, env, 1, "d", artifact_sha256="0" * 64)
        self._refused("profile_currency_mismatch", _charge, self.auth, env, 1, "e", currency="EUR")
        self.clock.now = 998_999
        self._refused("profile_not_yet_valid", _charge, self.auth, env, 1, "f")
        self.clock.now = 1_000_100
        self._refused("profile_expired", _charge, self.auth, env, 1, "g")

    def test_split_effects_cannot_dodge_total(self) -> None:
        env = profile(self.installer, total=1_000, max_effect=400)
        for i in range(2):
            _charge(self.auth, env, 400, "s%d" % i)
        self._refused("budget_total_exceeded", _charge, self.auth, env, 201, "s2")
        _charge(self.auth, env, 200, "s3")
        self._refused("budget_total_exceeded", _charge, self.auth, env, 1, "s4")
        self._refused("budget_max_effect_exceeded", _charge, self.auth, env, 401, "s5")

    def test_replay_and_rebinding(self) -> None:
        env = profile(self.installer)
        _charge(self.auth, env, 100, "once")
        self._refused("effect_replayed", _charge, self.auth, env, 100, "once")
        # Same id re-signed with a larger budget cannot reset or raise the cap.
        resigned = profile(self.installer, total=10**6, max_effect=10**6)
        self._refused("profile_id_rebound", _charge, self.auth, resigned, 100, "other")

    def test_restart_keeps_spend(self) -> None:
        env = profile(self.installer, total=1_000, max_effect=1_000)
        _charge(self.auth, env, 900, "r1")
        self.auth.close()
        restarted = self._open()
        self._refused("budget_total_exceeded", _charge, restarted, env, 101, "r2")
        self._refused("effect_replayed", _charge, restarted, env, 900, "r1")
        _charge(restarted, env, 100, "r3")

    def test_malformed_money_refused(self) -> None:
        env = profile(self.installer)
        for amount in (0, -5, 1.0, 1e9, float("nan"), float("inf"), True, "100", None, 2**60, [1]):
            with self.subTest(amount=repr(amount)), self.assertRaises(Refused) as ctx:
                _charge(self.auth, env, amount, "m")
            self.assertIn(ctx.exception.code, ("amount_invalid", "budget_max_effect_exceeded"))
        self._refused("idempotency_key_invalid", _charge, self.auth, env, 1, "")

    def test_thread_concurrency_never_overspends(self) -> None:
        env = profile(self.installer, total=1_000, max_effect=100)
        ok = []
        def worker(n: int) -> None:
            for i in range(10):
                try:
                    _charge(self.auth, env, 100, "t%d-%d" % (n, i))
                    ok.append(1)
                except Refused:
                    pass
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(ok), 10)

    def test_process_concurrency_never_overspends(self) -> None:
        env = profile(self.installer, total=1_000, max_effect=100)
        ctx = multiprocessing.get_context("spawn")
        out = ctx.Queue()
        procs = [ctx.Process(target=_child_charges, args=(self.path, self.installer.public, env,
                                                          "p%d" % n, 6, out)) for n in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        self.assertEqual(sum(out.get(timeout=10) for _ in procs), 10)


if __name__ == "__main__":
    unittest.main()
