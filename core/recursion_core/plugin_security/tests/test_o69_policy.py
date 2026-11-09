"""O69 (CSL-787): the Nov 11 same-uid fallback policy, quoted exactly in code and docs.

Lucas, Nov 8 (t1418u), O69 correction; not yet in PRODUCT.md 055cc7a5. The decision changes no runtime check;
these tests pin the recorded wording and check that nothing in the shipped code or docs
reads as a live-order grant. The code has one ``order`` permission and no paper/live
mode, so the only code-level check is that a profile cannot name any other permission.
"""

from __future__ import annotations

import hashlib
import os
import re
import unittest

from recursion_core import plugin_security
from recursion_core.plugin_security import Authority, Refused, create_ledger
from recursion_core.plugin_security.manifest import PERMISSIONS
from recursion_core.plugin_security.policy import SAME_UID_FALLBACK_POLICY, SAME_UID_FALLBACK_POLICY_SOURCE
from recursion_core.plugin_security.tests.support import PLUGIN_SOURCE, Clock, Key, needs_crypto, profile, temp_dir

O69_QUOTE = "it should have permission to run paper testing nov 11 regardless"
PACKAGE_DIR = os.path.dirname(plugin_security.__file__)


def _read(name: str) -> str:
    with open(os.path.join(PACKAGE_DIR, name), encoding="utf-8") as f:
        return f.read()


def _flat(text: str) -> str:
    return " ".join(text.split())


class PolicyTextTests(unittest.TestCase):
    def test_constant_is_the_exact_quote(self) -> None:
        self.assertEqual(SAME_UID_FALLBACK_POLICY, O69_QUOTE)
        self.assertEqual(SAME_UID_FALLBACK_POLICY_SOURCE, "Lucas, Nov 8 (t1418u), O69 correction; not yet in PRODUCT.md 055cc7a5")
        self.assertIs(plugin_security.SAME_UID_FALLBACK_POLICY, SAME_UID_FALLBACK_POLICY)

    def test_readme_quotes_the_constant_with_attribution(self) -> None:
        readme = _read("README.md")
        self.assertIn('"%s"' % SAME_UID_FALLBACK_POLICY, readme)
        self.assertIn(SAME_UID_FALLBACK_POLICY_SOURCE, _flat(readme))

    def test_docs_say_tamper_evidence_not_a_boundary(self) -> None:
        for name in ("README.md", "authority.py"):
            text = _flat(_read(name))
            self.assertIn("signed authority profile is tamper evidence, not a boundary", text, name)

    def test_paper_only_and_live_capital_separate(self) -> None:
        for name in ("README.md", "authority.py"):
            text = _flat(_read(name))
            self.assertIn("Paper only", text, name)
            self.assertIn("live capital stays separately authorised (PRODUCT.md 5.2)", text, name)
            self.assertIn("paper-order permission", text, name)

    def test_no_doc_line_grants_live_order_permission(self) -> None:
        grant = re.compile(r"\b(holds?|grants?|has|gets?)\b[^.]*\blive[- ]order permission", re.I)
        for name in ("README.md", "authority.py", "policy.py", "__init__.py", "loader.py"):
            for line in _read(name).splitlines():
                if grant.search(line) and not re.search(r"\bno\b", line, re.I):
                    self.fail("%s grants live-order permission: %r" % (name, line))

    def test_same_uid_still_not_a_boundary_and_f21_not_done(self) -> None:
        readme = _flat(_read("README.md"))
        self.assertIn("Same-uid is not a security boundary.", readme)
        self.assertIn("(F21), and F21 is not done.", readme)
        self.assertIn("four in-process attacks that succeed", readme)

    def test_fallback_rule_no_longer_undecided(self) -> None:
        readme = _flat(_read("README.md")).lower()
        for phrase in ("remains a founder step", "founder step", "undecided", "not yet decided"):
            self.assertNotIn(phrase, readme)
        self.assertIn("nov 11 same-uid fallback (o69, decided)", readme)


@needs_crypto
class NoLivePermissionInCodeTests(unittest.TestCase):
    """The code has no live-order permission for a profile or manifest to name."""

    def test_only_one_order_permission_exists(self) -> None:
        self.assertEqual([p for p in PERMISSIONS if "order" in p], ["order"])
        self.assertFalse(any("live" in p or "paper" in p for p in PERMISSIONS))

    def test_profile_naming_a_live_permission_is_refused(self) -> None:
        installer = Key()
        path = os.path.join(temp_dir(self), "ledger.db")
        create_ledger(path, installer.public)
        auth = Authority(installer.public, path, Clock())
        self.addCleanup(auth.close)
        digest = hashlib.sha256(PLUGIN_SOURCE).hexdigest()
        for i, permission in enumerate(("live_order", "live-order", "live", "order:live")):
            env = profile(installer, permission=permission)
            with self.assertRaises(Refused) as ctx:
                auth.charge(env, plugin_id="demo.plugin", artifact_sha256=digest, account="paper-acct",
                            venue="paper", currency="USD", amount_minor=1, idempotency_key="k-%d" % i)
            self.assertEqual(ctx.exception.code, "profile_issuer_or_permission", permission)


if __name__ == "__main__":
    unittest.main()
