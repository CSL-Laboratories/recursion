"""Exercise Authority's real field checks; simulate only the signature verdict.

No keys, credentials, auth files, ledger, or cryptography backend are needed.
These are validation tests, not evidence that a signature verifies.
"""
import hashlib
import unittest
from unittest import mock
from recursion_core.plugin_security.authority import Authority, PROFILE_SCHEMA
from recursion_core.plugin_security import Refused
from recursion_core.plugin_security.canonical import canonical

class ProfileFieldValidationWithoutCrypto(unittest.TestCase):
    def setUp(self):
        self.authority = Authority.__new__(Authority)
        self.verify = mock.Mock(return_value=True)
        self.authority._verify = self.verify

    def envelope(self, **changes):
        body = {
            "schema": PROFILE_SCHEMA, "profile_id": "test-profile", "issuer": "installer",
            "plugin_id": "test.plugin", "artifact_sha256": "0" * 64,
            "scope": {"account": "paper", "venue": "paper", "contract_family": "test",
                      "adapter": "test", "environment": "paper"},
            "provider": "vendor/model@revision", "model_version": "opus[1m]",
            "actions": ["order.submit"], "permission": "order",
            "not_before": 0, "expires_at": 100,
            "budgets": {"currency": "USD", "max_effect_minor": 1, "total_minor": 2},
        }
        body.update(changes)
        return {"profile": body, "signature": "simulated-verdict-only"}

    def test_model_rule_is_enforced_at_the_profile_boundary(self):
        for model in ("opus[1m]", "".join(map(chr, range(0x21, 0x7f))), "m" * 128):
            with self.subTest(model=model):
                envelope = self.envelope(model_version=model)
                parsed, digest = self.authority._check_profile(envelope)
                self.assertEqual(parsed["model_version"], model)
                self.assertIsNot(parsed, envelope["profile"])
                raw = canonical(envelope["profile"])
                self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
                self.verify.assert_called_with(envelope["signature"], raw)
        for model in ("", "bad model", "m" * 129, "opus[1m]\n", "a\x00b", "a\x7fb", None, 1, [], {}):
            with self.subTest(model=model):
                with self.assertRaises(Refused) as caught:
                    self.authority._check_profile(self.envelope(model_version=model))
                self.assertEqual(caught.exception.code, "profile_model")

    def test_provider_keeps_the_id_rule_at_the_profile_boundary(self):
        for provider in ("vendor/model@revision", "a:b-c_d.1"):
            self.assertEqual(self.authority._check_profile(self.envelope(provider=provider))[0]["provider"], provider)
        for provider in ("vendor[1]", "a+b", "bad provider", "", "p" * 129, None, 1):
            with self.subTest(provider=provider):
                with self.assertRaises(Refused) as caught:
                    self.authority._check_profile(self.envelope(provider=provider))
                self.assertEqual(caught.exception.code, "profile_model")

    def test_non_ascii_and_a_negative_signature_verdict_still_refuse(self):
        with self.assertRaises(Refused) as caught:
            self.authority._check_profile(self.envelope(model_version="mod\u00e8le"))
        self.assertEqual(caught.exception.code, "profile_noncanonical")
        self.verify.assert_not_called()
        self.verify.return_value = False
        with self.assertRaises(Refused) as caught:
            self.authority._check_profile(self.envelope())
        self.assertEqual(caught.exception.code, "profile_signature_invalid")
