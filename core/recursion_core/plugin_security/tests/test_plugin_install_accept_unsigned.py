"""Accepting a held release that no longer counts as signed grants nothing by default, and the question says
what the release is.

(1) After an unpin, a held release whose key was taken off the trusted list, or revoked in the state folder,
classifies unsigned. ``quarantine accept`` refuses it ``held_release_not_signed`` and writes nothing: the
words say what the release is now and why, that nothing was written, and the ways on.
(2) Every accept question names the release's trust and everything it would get: each permission, network
destination, credential reference and install script, not only what it asks beyond the running release.
(3) Only the person, for one named release: ``--accept-unsigned`` with the release's full artifact digest lets
the question be asked, saying the release is not signed; only the typed yes writes the grant. A wrong or short
digest refuses. ``list`` says in plain words that the grant came that way.
(4) A revoked key is refused even with ``--accept-unsigned``.
"""
from __future__ import annotations

import json
import os
import unittest

from recursion_core.plugin_security import CoreHealth, Grant, PluginLoader, ReservedEffects, TrustStore
from recursion_core.plugin_security.signing import key_id
from recursion_core.plugin_security.tests.support import Key, needs_crypto
from recursion_core.plugin_security.tests.test_plugin_cli_install import LINUX, LINUX_ONLY, _sha
from recursion_core.plugin_security.tests.test_plugin_install_pinned import PLUGIN, _tree
from recursion_core.plugin_security.tests.test_plugin_install_printed_places import _revoke
from recursion_core.plugin_security.tests.test_plugin_install_quarantine import NEXT_SOURCE, _QuarantineCase

ASKS = {"permissions": ("read", "panel", "credential", "process"), "nets": (), "creds": ("vault:notes",)}
NOT_SIGNED = "NOT signed by a key this install trusts"


class _UnsignedCase(_QuarantineCase):
    def held_then_unpinned(self, signer, how: str):
        """A pinned signed 1.0.0, its 1.1.0 asking for a credential reference and process held, the pin dropped
        with a typed yes, then the key taken off the live trusted list by hand or revoked in the state folder."""
        self.quarantined(signer, **ASKS)
        self.assertEqual("quarantined", self.install().get("result"))
        self.unpin(confirm=lambda _question: "yes")
        if how == "untrusted":
            data = self.live_json()
            data["trusted_keys"] = {}
            self.write_live(data)
        else:
            _revoke(self.state, signer)

    def doctor_codes(self) -> list:
        """What the loader's doctor finds for the live release under the live install.json."""
        data = self.live_json()
        grants = {item["plugin_id"]: Grant(item["plugin_id"], item["artifact_sha256"], item["signer_key_id"],
                                           tuple(item["permissions"])) for item in data["grants"]}
        folder = self.config / "plugins" / PLUGIN
        signature = folder / "manifest.sig"
        trust = TrustStore(dict(data["trusted_keys"]))
        try:
            loader = PluginLoader(trust, grants, ReservedEffects(), CoreHealth())
            findings = loader.doctor((folder / "manifest.json").read_bytes(),
                                     signature.read_text().strip() if signature.exists() else None,
                                     (folder / "plugin.py").read_bytes())
        finally:
            trust.close()
        return [item["code"] for item in findings]

    def refused_unsigned(self, **kwargs):
        """Accept with a yes refuses ``held_release_not_signed``, asks nothing and writes nothing."""
        before = _tree(self.config)
        live = self.live().read_bytes()
        asked = []
        try:
            result = self.call("quarantine_accept", PLUGIN,
                               confirm=lambda question: asked.append(question) or "yes", **kwargs)
        except Exception as exc:  # the refusal is checked below
            refusal = exc
        else:
            self.fail(f"accepted: {result.get('trust')}, grants {self.live_json()['grants']}, the loader's doctor "
                      f"finds {self.doctor_codes()}")
        self.assertEqual(("held_release_not_signed", []), (getattr(refusal, "code", repr(refusal)), asked))
        self.assertEqual(live, self.live().read_bytes())
        self.assertEqual(before, _tree(self.config))
        self.assertTrue(self.held_at().is_dir())
        return refusal


@unittest.skipUnless(LINUX, LINUX_ONLY)
class DefaultRefuseTests(_UnsignedCase):
    @needs_crypto
    def test_after_an_unpin_a_held_release_off_the_trusted_list_is_refused_and_nothing_is_written(self):
        signer = Key()
        self.held_then_unpinned(signer, "untrusted")
        detail = self.refused_unsigned().detail
        for words in (f"The held release 1.1.0 of the plugin {PLUGIN} is no longer signed by a key this install "
                      f"trusts: its key {key_id(signer.public)} is not on this install's trusted list.",
                      f"Nothing was written: the running release, its grant and the held copy at {self.held_at()} "
                      f"are as they were.", "To go on: put the key", "install a release signed by a key on this "
                      "install's trusted list", f"recursion plugin quarantine drop {PLUGIN}",
                      f"recursion plugin quarantine accept {PLUGIN} --accept-unsigned {_sha(NEXT_SOURCE)}"):
            self.assertIn(words, detail)
        status, said, out = self.main(["plugin", "quarantine", "accept", PLUGIN, "--config", str(self.config),
                                       "--state", str(self.state)], "yes\n")
        self.assertEqual((1, ""), (status, out))
        self.assertEqual(["refused: held_release_not_signed", detail], said.splitlines())

    @needs_crypto
    def test_after_an_unpin_a_held_release_whose_key_is_revoked_is_refused_even_with_the_digest(self):
        signer = Key()
        self.held_then_unpinned(signer, "revoked")
        detail = self.refused_unsigned().detail
        self.assertIn(f"its key {key_id(signer.public)} was revoked in the state folder {self.state}.", detail)
        self.assertNotIn("--accept-unsigned", detail)
        detail = self.refused_unsigned(accept_unsigned=_sha(NEXT_SOURCE)).detail
        self.assertIn("A revoked key is not accepted unsigned, even with --accept-unsigned.", detail)
        status, said, out = self.main(["plugin", "quarantine", "accept", PLUGIN, "--accept-unsigned",
                                       _sha(NEXT_SOURCE), "--config", str(self.config), "--state", str(self.state)],
                                      "yes\n")
        self.assertEqual((1, ""), (status, out))
        self.assertEqual("refused: held_release_not_signed", said.splitlines()[0])

    @needs_crypto
    def test_while_the_pin_stands_the_refusal_also_carries_the_new_code(self):
        signer = Key()
        self.quarantined(signer, **ASKS)
        self.install()
        data = self.live_json()
        data["trusted_keys"] = {}
        self.write_live(data)
        refusal = self.refused(lambda: self.call("quarantine_accept", PLUGIN, confirm=lambda _q: "yes",
                                                 accept_unsigned=_sha(NEXT_SOURCE)))
        self.assertEqual(["update_unsigned_over_signed", "held_release_not_signed"], refusal.codes)


@unittest.skipUnless(LINUX, LINUX_ONLY)
class PersonAcceptsOneReleaseTests(_UnsignedCase):
    @needs_crypto
    def test_the_digest_asks_the_true_question_and_only_yes_grants_it(self):
        signer = Key()
        self.held_then_unpinned(signer, "untrusted")
        digest = _sha(NEXT_SOURCE)
        before = _tree(self.config)
        for answer in (None, "no\n", "", "YES"):
            with self.subTest(answer=answer):
                self.not_changed(lambda: self.call("quarantine_accept", PLUGIN, accept_unsigned=digest,
                                                   confirm=None if answer is None else (lambda _q: answer)))
                self.assertEqual(before, _tree(self.config))
        asked = []
        result = self.call("quarantine_accept", PLUGIN, accept_unsigned=digest,
                           confirm=lambda question: asked.append(question) or "yes")
        self.assertEqual(1, len(asked))
        question = asked[0]
        self.assertTrue(question.startswith(
            f"Install the held release 1.1.0 of the plugin {PLUGIN}, {NOT_SIGNED}, and grant it: read, panel, "
            f"credential, process (order is never granted this way)?"), question)
        for words in ("the credential reference vault:notes", "no network destination", "no install script",
                      f"these permissions are granted by your word alone, for this release only (artifact {digest})"):
            self.assertIn(words, question)
        self.assertEqual(("unsigned", ["read", "panel", "credential", "process"]), (result["trust"], result["granted"]))
        grants = self.live_json()["grants"]
        self.assertEqual([(PLUGIN, digest, None, ["read", "panel", "credential", "process"])],
                         [(g["plugin_id"], g["artifact_sha256"], g["signer_key_id"], g["permissions"]) for g in grants])
        self.assertEqual([f"unsigned: its permissions read, panel, credential, process were granted by your own word "
                          f"for the release {digest}, not by a key this install trusts"],
                         [row.get("grant") for row in self.listed()["plugins"]])
        self.assertFalse(self.held_at().exists())

    @needs_crypto
    def test_the_command_line_takes_the_digest_and_asks_before_it_writes(self):
        signer = Key()
        self.held_then_unpinned(signer, "untrusted")
        before = _tree(self.config)
        argv = ["plugin", "quarantine", "accept", PLUGIN, "--accept-unsigned", _sha(NEXT_SOURCE), "--config",
                str(self.config), "--state", str(self.state)]
        status, said, out = self.main(argv, "")
        self.assertEqual((1, ""), (status, out))
        self.assertIn(NOT_SIGNED, said.splitlines()[0])
        self.assertEqual(before, _tree(self.config))
        status, said, out = self.main(argv, "yes\n")
        self.assertEqual(0, status, said)
        self.assertEqual("unsigned", json.loads(out)["trust"])

    @needs_crypto
    def test_a_wrong_or_short_digest_refuses_and_writes_nothing(self):
        signer = Key()
        self.held_then_unpinned(signer, "untrusted")
        digest = _sha(NEXT_SOURCE)
        for given in ("0" * 64, digest[:63], digest.upper(), digest + "0"):
            with self.subTest(given=given):
                detail = self.refused_unsigned(accept_unsigned=given).detail
                self.assertIn(f"The digest given with --accept-unsigned is not this release's artifact digest "
                              f"{digest}.", detail)


@unittest.skipUnless(LINUX, LINUX_ONLY)
class SignedQuestionTests(_UnsignedCase):
    @needs_crypto
    def test_a_still_signed_held_release_is_accepted_and_its_question_names_the_key_and_everything_it_gets(self):
        signer = Key()
        self.quarantined(signer, permissions=("read", "panel", "network", "credential"), creds=("vault:notes",))
        self.install()
        asked = []
        result = self.call("quarantine_accept", PLUGIN, confirm=lambda question: asked.append(question) or "yes")
        self.assertEqual(("signed", "quarantine accept"), (result["trust"], result["command"]))
        self.assertTrue(asked[0].startswith(
            f"Install the held release 1.1.0 of the plugin {PLUGIN}, signed by the key {key_id(signer.public)}, and "
            f"grant it: read, panel, network, credential (order is never granted this way)? With it the release "
            f"reaches the network destination api.example.com, holds the credential reference vault:notes and has "
            f"no install script."), asked[0])
        self.assertNotIn(NOT_SIGNED, asked[0])
        self.assertEqual([None], [row.get("grant") for row in self.listed()["plugins"]])


if __name__ == "__main__":
    unittest.main()
