"""PS4 (P2, the Nov 15 sweeps' triage): a malformed ``actions`` member is refused ``profile_actions``.

``_check_profile`` sorted a set of the signed actions before it checked their types, so an
unhashable member (``[{}]``, ``[[]]``) or one that does not order with a string
(``["order.submit", 1]``) raised ``TypeError`` (``core_internal_error`` at the wire) instead of
the typed refusal. No real key: only the signature verdict is replaced, as
``test_profile_fields_without_crypto`` does.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest

from recursion_core.plugin_security import Authority, Refused, create_ledger

ANCHOR = "11" * 32
SOURCE = b"VALUE = 42\n"


def _profile(actions):
    return {"profile": {
        "schema": "csl-authority-profile/vNext", "profile_id": "prof-1", "issuer": "installer",
        "plugin_id": "demo.plugin", "artifact_sha256": hashlib.sha256(SOURCE).hexdigest(),
        "scope": {"account": "paper-acct", "venue": "paper", "contract_family": "fake-futures",
                  "adapter": "fake-adapter", "environment": "paper"},
        "provider": "fake-provider", "model_version": "fake-model-1", "actions": actions,
        "permission": "order", "not_before": 0, "expires_at": 200,
        "budgets": {"currency": "USD", "max_effect_minor": 5000, "total_minor": 10000},
    }, "signature": "00" * 64}


class ActionMemberTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.mkdtemp(prefix="ps4-actions-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "ledger.sqlite3")
        create_ledger(path, ANCHOR)
        self.auth = Authority(ANCHOR, path, lambda: 100)
        self.auth._verify = lambda signature, raw: True  # the signature verdict only (no key exists here)
        self.addCleanup(self.auth.close)

    def _charge(self, actions):
        p = _profile(actions)["profile"]
        return self.auth.charge(
            _profile(actions), plugin_id=p["plugin_id"], artifact_sha256=p["artifact_sha256"],
            scope=p["scope"], kind="order.submit", provider=p["provider"],
            model_version=p["model_version"], currency="USD", amount_minor=1, idempotency_key="idem-1")

    def test_PS4_malformed_action_members_are_profile_actions(self):
        for actions in ([{}], [[]], ["order.submit", 1], [1, "order.submit"], [None], ["order.submit", {}],
                        [["order.submit"]]):
            with self.subTest(actions=actions):
                with self.assertRaises(Refused) as caught:
                    self._charge(actions)
                self.assertEqual(caught.exception.code, "profile_actions")

    def test_the_other_action_rules_still_hold(self):
        for actions in ([], ["order.submit", "order.submit"], ["order.submit", "order.cancel"]):
            with self.subTest(actions=actions):
                with self.assertRaises(Refused) as caught:
                    self._charge(actions)
                self.assertEqual(caught.exception.code, "profile_actions")
        self.assertEqual(self._charge(["order.submit"])["spent_minor"], 1)


if __name__ == "__main__":
    unittest.main()
