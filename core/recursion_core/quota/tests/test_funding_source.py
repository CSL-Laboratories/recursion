"""A helper-sourced API key must not borrow a stored key's consent.

The witness is the integration entry for an ``api_key_helper`` connection:
the helper's key is not the stored key that shares the connection id, so
admission refuses ``consent_unsupported`` and does not ask KEYS.
"""

from __future__ import annotations

import unittest

from recursion_core.quota.tests.test_reservation import Case, KEY, RUN_A, key_route
from recursion_core.routes import contract


class FundingSourceWitness(Case):
    def test_external_helper_cannot_borrow_a_stored_keys_consent(self):
        made = key_route()
        made["connection"]["key_source"] = "api_key_helper"
        del made["connection"]["credential_env"]
        # Valid master contract, including the absence of credential_env.
        contract.validate_request({
            "contract": contract.CONTRACT_VERSION, "request_id": "r1",
            "tenant": "t1", "prompt": "hello", "deadline_s": 1,
            "route": made,
        })
        # Case has a stored key-A policy: opted in, cap 5, known spent 2.
        # The helper may return a different key; no identity binding is supplied.
        answer = self.res.admit(KEY, made, RUN_A)
        self.assertRefused(answer, "consent_unsupported")
        self.assertEqual(self.keys.calls, [])


if __name__ == "__main__":
    unittest.main()
