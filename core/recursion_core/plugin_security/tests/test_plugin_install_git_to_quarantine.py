"""A release fetched from a local bare git repository that asks for more is held with exactly the fetched bytes.

A signed, pinned plugin runs at 1.0.0. Its 1.1.0 asks for more and is not vendored: the registry holds no
``plugin.py`` for it, and its entry names a full commit of a local bare git repository made by the test. The
install answers ``quarantined``: the held ``plugin.py`` is the fetched bytes, digest for digest, the profile
outside the quarantine is as before, and no fetch scratch folder is left. ``quarantine accept`` with a typed yes
installs exactly those bytes and removes the held copy. A commit whose file differs by one byte refuses
``source_digest_mismatch`` and leaves the whole profile byte for byte as before, with no quarantine folder. No
fetched byte is run or imported on the way. No network address is passed to git.
"""
from __future__ import annotations

import hashlib
import json
import sys
import unittest

from recursion_core.plugin_security import install as inst
from recursion_core.plugin_security.registry import build_index, index_bytes
from recursion_core.plugin_security.tests.support import Key, needs_crypto
from recursion_core.plugin_security.tests.test_plugin_cli_git_source import GitCase, _git
from recursion_core.plugin_security.tests.test_plugin_cli_install import LINUX, LINUX_ONLY
from recursion_core.plugin_security.tests.test_plugin_install_pinned import PLUGIN, _tree
from recursion_core.plugin_security.tests.test_plugin_install_quarantine import NEXT_SOURCE, _QuarantineCase, _asking


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(LINUX, LINUX_ONLY)
class GitToQuarantineTests(_QuarantineCase, GitCase):
    def unvendor(self, commit):
        """Take 1.1.0's ``plugin.py`` out of the registry; its entry names ``commit`` of a local bare repository."""
        entry_path = self.registry / "registry" / PLUGIN / "entry.json"
        entry = json.loads(entry_path.read_text())
        entry["releases"][0]["source"] = {"repository": "https://example.invalid/fixtures/" + PLUGIN,
                                          "commit": commit, "path": "plugin.py"}
        entry_path.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n")
        (self.registry / "registry" / PLUGIN / "1.1.0" / "plugin.py").unlink()
        (self.registry / "registry" / "index.json").write_bytes(index_bytes(build_index([entry])))

    def git_install(self, bare):
        return inst.install_plugin(self.config, self.registry, PLUGIN, state=self.state, git_repository=str(bare))

    def offered_from_git(self, source):
        """1.0.0 installed and pinned; 1.1.0 asks for more and lives only in a bare repository holding ``source``."""
        signer = Key()
        self.signed_install(signer)
        self.release("more", key=signer, signature="real", source=source,
                     manifest=_asking(signer.public, source=source), version="1.1.0")
        bare, commit = self.make_bare({"plugin.py": source})
        self.unvendor(commit)
        self.assertEqual([], self.codes_of(self.registry))
        return bare, commit

    def blob(self, bare, commit) -> bytes:
        return _git(["--git-dir", str(bare), "cat-file", "blob", f"{commit}:plugin.py"]).encode()

    @needs_crypto
    def test_a_the_install_holds_exactly_the_fetched_bytes(self):
        bare, commit = self.offered_from_git(NEXT_SOURCE)
        release = self.registry / "registry" / PLUGIN / "1.1.0"
        before = self.profile()
        result = self.git_install(bare)
        held = self.held_at()
        self.assertEqual("quarantined", result.get("result"), result)
        self.assertEqual(before, self.profile())
        self.assertEqual(NEXT_SOURCE, (held / "plugin.py").read_bytes())
        # Digest for digest: the commit's blob, the manifest's artifact digest and the held copy are one.
        manifest = json.loads((release / "manifest.json").read_bytes())
        self.assertEqual(_sha(self.blob(bare, commit)), _sha((held / "plugin.py").read_bytes()))
        self.assertEqual(manifest["artifact_sha256"], _sha((held / "plugin.py").read_bytes()))
        self.assertEqual((release / "manifest.json").read_bytes(), (held / "manifest.json").read_bytes())
        self.assertEqual((release / "manifest.sig").read_bytes(), (held / "manifest.sig").read_bytes())
        self.assertFalse((release / "plugin.py").exists())
        self.assert_no_scratch()

    @needs_crypto
    def test_b_accept_installs_exactly_those_bytes_and_removes_the_held_copy(self):
        bare, commit = self.offered_from_git(NEXT_SOURCE)
        self.assertEqual("quarantined", self.git_install(bare).get("result"))
        held = self.held_at()
        inst.quarantine_accept(self.config, PLUGIN, state=self.state, confirm=lambda question: "yes")
        live = (self.config / "plugins" / PLUGIN / "plugin.py").read_bytes()
        self.assertEqual(NEXT_SOURCE, live)
        self.assertEqual(_sha(self.blob(bare, commit)), _sha(live))
        self.assertFalse(held.exists())
        self.assert_no_scratch()

    @needs_crypto
    def test_c_a_commit_one_byte_off_refuses_and_leaves_nothing_anywhere(self):
        signer = Key()
        self.signed_install(signer)
        self.release("more", key=signer, signature="real", source=NEXT_SOURCE,
                     manifest=_asking(signer.public), version="1.1.0")
        bare, commit = self.make_bare({"plugin.py": b"VALUE = 8\n"})
        self.unvendor(commit)
        before = _tree(self.config)
        code = self.refused(lambda: self.git_install(bare)).code
        self.assertEqual("source_digest_mismatch", code)
        self.assertEqual(before, _tree(self.config))
        self.assertFalse((self.config / "plugin-quarantine").exists())
        self.assert_no_scratch()

    @needs_crypto
    def test_d_no_fetched_byte_is_run_or_imported_on_the_way(self):
        marker = self.tmp / "the-fetched-code-ran"
        source = (f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n").encode()
        bare, commit = self.offered_from_git(source)
        modules = set(sys.modules)
        self.assertEqual("quarantined", self.git_install(bare).get("result"))
        self.assertEqual(source, (self.held_at() / "plugin.py").read_bytes())
        inst.quarantine_accept(self.config, PLUGIN, state=self.state, confirm=lambda question: "yes")
        self.assertEqual(source, (self.config / "plugins" / PLUGIN / "plugin.py").read_bytes())
        self.assertFalse(marker.exists())
        roots = (str(self.config), str(bare))
        loaded = [name for name in set(sys.modules) - modules
                  if str(getattr(sys.modules[name], "__file__", None) or "").startswith(roots)]
        self.assertEqual([], loaded)
        # The control: the same bytes, when run, do leave the marker.
        exec(compile(source, str(self.tmp / "control.py"), "exec"), {})
        self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
