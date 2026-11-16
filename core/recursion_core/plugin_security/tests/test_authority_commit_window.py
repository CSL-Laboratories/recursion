"""PS2 (P1, the Nov 15 sweeps' triage): the signed window is read again under the commit guard.

``Authority.charge`` read the clock once, before the ledger's lock, ``BEGIN IMMEDIATE`` and the
commit guard, so a charge that waited for any of them committed after the profile had expired.
No real key, no signature: these tests replace only the signature verdict (as
``test_profile_fields_without_crypto`` does), so they run without ``cryptography``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
import unittest

from recursion_core.plugin_security import Authority, Refused, create_ledger

ANCHOR = "11" * 32
SOURCE = b"VALUE = 42\n"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class MovesTheClock:
    """A commit guard whose entry stands for the wait for the fence: the clock moves meanwhile."""

    def __init__(self, clock, to):
        self.clock, self.to = clock, to

    def __enter__(self):
        self.clock.now = self.to

    def __exit__(self, *exc):
        return False


def _profile(**over):
    body = {
        "schema": "csl-authority-profile/vNext", "profile_id": "prof-1", "issuer": "installer",
        "plugin_id": "demo.plugin", "artifact_sha256": hashlib.sha256(SOURCE).hexdigest(),
        "scope": {"account": "paper-acct", "venue": "paper", "contract_family": "fake-futures",
                  "adapter": "fake-adapter", "environment": "paper"},
        "provider": "fake-provider", "model_version": "fake-model-1", "actions": ["order.submit"],
        "permission": "order", "not_before": 100, "expires_at": 200,
        "budgets": {"currency": "USD", "max_effect_minor": 5000, "total_minor": 10000},
    }
    body.update(over)
    return {"profile": body, "signature": "00" * 64}


class CommitWindowTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp(prefix="ps2-window-")
        self.addCleanup(shutil.rmtree, folder, True)
        self.path = os.path.join(folder, "ledger.sqlite3")
        create_ledger(self.path, ANCHOR)
        self.clock = Clock(199)
        self.auth = Authority(ANCHOR, self.path, self.clock)
        self.auth._verify = lambda signature, raw: True  # the signature verdict only (no key exists here)
        self.addCleanup(self.auth.close)

    def _charge(self, guard, key="idem-1"):
        p = _profile()["profile"]
        return self.auth.charge(
            _profile(), plugin_id=p["plugin_id"], artifact_sha256=p["artifact_sha256"], scope=p["scope"],
            kind="order.submit", provider=p["provider"], model_version=p["model_version"], currency="USD",
            amount_minor=1, idempotency_key=key, commit_guard=guard)

    def _ledger(self):
        db = sqlite3.connect(self.path)
        try:
            return (db.execute("SELECT COUNT(*) FROM charges").fetchone()[0],
                    db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0])
        finally:
            db.close()

    def _refused(self, guard, code):
        with self.assertRaises(Refused) as caught:
            self._charge(guard)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self._ledger(), (0, 0))  # nothing written, not even the profile binding

    def test_PS2_expiry_is_rechecked_after_the_commit_guard_is_entered(self):
        self._refused(MovesTheClock(self.clock, 200), "profile_expired")

    def test_a_clock_moved_before_not_before_under_the_guard_refuses(self):
        self._refused(MovesTheClock(self.clock, 99), "profile_not_yet_valid")

    def test_an_invalid_clock_under_the_guard_refuses(self):
        for bad in (199.0, None, "199"):
            with self.subTest(bad=bad):
                self._refused(MovesTheClock(self.clock, bad), "clock_invalid")

    def test_a_clock_still_inside_the_window_commits(self):
        result = self._charge(MovesTheClock(self.clock, 199))
        self.assertEqual(result["spent_minor"], 1)
        self.assertEqual(self._ledger(), (1, 1))

    def test_the_refused_charge_can_be_retried_and_is_refused_again(self):
        self._refused(MovesTheClock(self.clock, 200), "profile_expired")
        with self.assertRaises(Refused) as caught:
            self._charge(None)
        self.assertEqual(caught.exception.code, "profile_expired")  # the early read refuses it now


if __name__ == "__main__":
    unittest.main()
