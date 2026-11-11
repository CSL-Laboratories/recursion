"""K3 authority profiles: a live profile mutated after its signature check cannot decide a charge.

``Authority.charge`` verifies the installer's signature over ``canonical(profile)``. The
plugin passes the profile dict in and keeps a reference to it, so on a free-threaded build
another plugin thread can change that dict after the bytes were signed-checked. Every
check, the return value and the ledger must follow the signed bytes only.

The hooked tests are deterministic: the test wraps the module's ``canonical`` or ``verify``
so that, once the real call has returned, a second real thread mutates the envelope's
profile dict and is joined before ``charge`` reads any field. The race test has no hook; it
only shows that no charge ever escapes while a thread keeps flipping fields, so it is
evidence of absence of escapes, not a reproduction on its own.

Runs only on a free-threaded interpreter with the GIL off (``-X gil=0``) and with
``cryptography`` installed; elsewhere it is skipped with that reason.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
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
needs_gil_off = unittest.skipUnless(_gil_off, "needs a free-threaded interpreter run with -X gil=0")


def _mutate_in_thread(live: dict, mutate) -> None:
    """Run ``mutate(live)`` in a second real thread and wait for it to finish."""
    errors: list = []

    def run() -> None:
        try:
            mutate(live)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    thread = threading.Thread(target=run, name="profile-mutator")
    thread.start()
    thread.join()
    if errors:
        raise errors[0]


@needs_crypto
@needs_gil_off
class AuthorityToctouTests(unittest.TestCase):
    """Signed: total 10_000, max effect 5_000, window [0, 2_000_000), account paper-acct, id prof-1."""

    def setUp(self) -> None:
        self.installer = Key()
        self.clock = Clock()
        self.mutations = 0
        self._fresh()

    def _fresh(self) -> None:
        """A new ledger and Authority, so each subtest starts from zero spend."""
        self.path = os.path.join(temp_dir(self), "ledger.db")
        create_ledger(self.path, self.installer.public)
        self.auth = Authority(self.installer.public, self.path, self.clock)
        self.addCleanup(self.auth.close)

    @contextlib.contextmanager
    def _hook(self, name: str, envelope: dict, mutate, *, after_true: bool = False):
        """Wrap ``authority.<name>`` so a mutator thread changes the live profile right after it."""
        real = getattr(authority_module, name)
        live = envelope["profile"]

        def hooked(*args):
            result = real(*args)
            if not after_true or result is True:
                _mutate_in_thread(live, mutate)
                self.mutations += 1
            return result

        setattr(authority_module, name, hooked)
        try:
            yield
        finally:
            setattr(authority_module, name, real)

    def _charge(self, envelope, amount, key, scope=None, **overrides):
        return self.auth.charge(envelope, **{**CALL, **overrides}, scope=scope or scope_of(),
                                amount_minor=amount, idempotency_key=key)

    def _refused(self, code: str, fn, *args, **kwargs) -> None:
        with self.assertRaises(Refused) as ctx:
            fn(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)

    def _ledger(self) -> tuple:
        db = sqlite3.connect(self.path)
        try:
            return (db.execute("SELECT profile_id, digest FROM profiles ORDER BY profile_id").fetchall(),
                    db.execute("SELECT idempotency_key, profile_id, amount_minor FROM charges"
                               " ORDER BY idempotency_key").fetchall())
        finally:
            db.close()

    def _cases(self):
        """(name, mutate, call) where the signed profile decides ``call`` and the mutation would flip it."""
        def raise_budgets(p):
            p["budgets"] = {"currency": "USD", "max_effect_minor": 10**9, "total_minor": 10**12}

        def widen_scope(p):
            p["scope"] = scope_of(account="other-acct")

        def swap_plugin(p):
            p["plugin_id"] = "evil.plugin"

        def add_action(p):
            p["actions"] = ["order.cancel", "order.submit"]

        def swap_model(p):
            p["model_version"] = "other-model"

        def swap_currency(p):
            p["budgets"] = dict(p["budgets"], currency="EUR")

        return [
            ("max_effect_raised", raise_budgets, lambda env, k: self._refused(
                "budget_max_effect_exceeded", self._charge, env, 6_000, k)),
            ("scope_changed", widen_scope, lambda env, k: self._refused(
                "profile_scope_mismatch", self._charge, env, 100, k, scope=scope_of(account="other-acct"))),
            ("plugin_changed", swap_plugin, lambda env, k: self._refused(
                "profile_plugin_mismatch", self._charge, env, 100, k, plugin_id="evil.plugin")),
            ("action_added", add_action, lambda env, k: self._refused(
                "profile_action_not_permitted", self._charge, env, 100, k, kind="order.cancel")),
            ("model_changed", swap_model, lambda env, k: self._refused(
                "profile_model_mismatch", self._charge, env, 100, k, model_version="other-model")),
            ("currency_changed", swap_currency, lambda env, k: self._refused(
                "profile_currency_mismatch", self._charge, env, 100, k, currency="EUR")),
        ]

    def _run_refusals(self, hook_name: str, after_true: bool, warm: bool = False) -> None:
        for name, mutate, call in self._cases():
            with self.subTest(name):
                self._fresh()
                env = profile(self.installer)
                charges = []
                if warm:  # an honest charge first, so the signature verdict is served from the cache
                    self._charge(env, 100, "k-warm")
                    charges = [("k-warm", "prof-1", 100)]
                    self.assertEqual(len(self.auth._verified), 1)
                before = self.mutations
                with self._hook(hook_name, env, mutate, after_true=after_true):
                    call(env, "k-" + name)
                self.assertEqual(self.mutations, before + 1, "the mutator thread did not run")
                self.assertEqual(self._ledger()[1], charges)

    def test_mutation_after_verify_cannot_widen_refusals(self) -> None:
        self._run_refusals("verify", after_true=True)

    def test_mutation_after_canonical_cannot_widen_refusals(self) -> None:
        # Hooked at canonical so it also fires when the signature verdict comes from a cache.
        self._run_refusals("canonical", after_true=False)

    def test_mutation_after_cached_verdict_cannot_widen_refusals(self) -> None:
        real_verify = authority_module.verify
        calls = []
        authority_module.verify = lambda *args: calls.append(args) or real_verify(*args)
        self.addCleanup(setattr, authority_module, "verify", real_verify)
        self._run_refusals("canonical", after_true=False, warm=True)
        self.assertEqual(len(calls), len(self._cases()))  # one per warm-up; every hooked charge was a hit

    def test_mutation_cannot_extend_expiry(self) -> None:
        for hook_name in ("verify", "canonical"):
            with self.subTest(hook_name):
                self._fresh()
                env = profile(self.installer)
                self.clock.now = 2_000_000  # signed expires_at
                with self._hook(hook_name, env, lambda p: p.update(expires_at=10**12),
                                after_true=hook_name == "verify"):
                    self._refused("profile_expired", self._charge, env, 100, "k-exp")
                self.assertEqual(env["profile"]["expires_at"], 10**12)  # the live dict really changed
                self.assertEqual(self._ledger(), ([], []))

    def test_mutation_cannot_raise_total_budget(self) -> None:
        env = profile(self.installer)
        signed_digest = hashlib.sha256(authority_module.canonical(env["profile"])).hexdigest()
        self._charge(env, 5_000, "k-a")
        self._charge(env, 5_000, "k-b")  # signed total 10_000 now spent
        signed_budgets = env["profile"]["budgets"]
        for hook_name in ("canonical", "verify"):
            with self.subTest(hook_name):
                env["profile"]["budgets"] = signed_budgets
                if hook_name == "verify":  # a new instance on the same ledger: cold cache, real verify
                    self.auth = Authority(self.installer.public, self.path, self.clock)
                    self.addCleanup(self.auth.close)
                before = self.mutations
                with self._hook(hook_name, env, lambda p: p.update(
                        budgets={"currency": "USD", "max_effect_minor": 5_000, "total_minor": 10**12}),
                        after_true=hook_name == "verify"):
                    self._refused("budget_total_exceeded", self._charge, env, 1_000, "k-c")
                self.assertEqual(self.mutations, before + 1)
        self.assertEqual(self._ledger(), ([("prof-1", signed_digest)],
                                          [("k-a", "prof-1", 5_000), ("k-b", "prof-1", 5_000)]))

    def test_ledger_and_result_use_signed_id_and_budget(self) -> None:
        for hook_name in ("verify", "canonical"):
            with self.subTest(hook_name):
                self._fresh()
                env = profile(self.installer)
                signed_digest = hashlib.sha256(authority_module.canonical(env["profile"])).hexdigest()

                def rebind(p):
                    p["profile_id"] = "prof-evil"
                    p["budgets"] = {"currency": "USD", "max_effect_minor": 5_000, "total_minor": 10**12}

                with self._hook(hook_name, env, rebind, after_true=hook_name == "verify"):
                    out = self._charge(env, 1_000, "k-1")
                self.assertEqual(env["profile"]["profile_id"], "prof-evil")  # the live dict really changed
                self.assertEqual(out, {"profile_id": "prof-1", "spent_minor": 1_000, "remaining_minor": 9_000})
                self.assertEqual(self._ledger(), ([("prof-1", signed_digest)], [("k-1", "prof-1", 1_000)]))

    def test_unhooked_race_never_lets_a_mutated_profile_charge(self) -> None:
        # No hook: a real thread keeps flipping max_effect_minor while the main thread charges
        # 6_000 against a signed max of 5_000. Each charge must refuse, either because the bytes
        # it canonicalised were not the signed ones or because the signed max decides.
        env = profile(self.installer)
        live = env["profile"]
        signed = dict(live["budgets"])
        evil = dict(signed, max_effect_minor=10**9, total_minor=10**12)
        stop = threading.Event()

        def flip() -> None:
            while not stop.is_set():
                live["budgets"] = evil
                live["budgets"] = signed

        thread = threading.Thread(target=flip, name="profile-flipper")
        thread.start()
        codes: dict = {}
        escaped = []
        try:
            for i in range(3_000):
                try:
                    escaped.append(self._charge(env, 6_000, "k-race-%d" % i))
                except Refused as exc:
                    codes[exc.code] = codes.get(exc.code, 0) + 1
        finally:
            stop.set()
            thread.join()
        self.assertEqual(escaped, [], "charges decided by unsigned values: %r" % codes)
        self.assertEqual(set(codes) - {"profile_signature_invalid", "budget_max_effect_exceeded"}, set())
        self.assertEqual(self._ledger()[1], [])


if __name__ == "__main__":
    unittest.main()
