"""The registry's commit is one it published, and each figure, name and field refuses with its own words.

Every registry is made up by the test in a local bare repository of its own; no test reaches a network
address. Each refusal leaves the profile as it was and no scratch folder.
"""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from unittest.mock import patch

from recursion_core.plugin_security.tests.test_plugin_cli_install import LINUX, LINUX_ONLY
from recursion_core.plugin_security.tests.test_plugin_install_files_beside_entry import PublicCase, _spec
from recursion_core.plugin_security.tests.test_plugin_install_from_registry import RegistryCase, _git

_PLUGIN = "fixture.unsigned"


@unittest.skipUnless(LINUX, LINUX_ONLY)
class PublishedCommitTests(RegistryCase):
    def test_a_commit_no_ref_of_the_registry_holds_is_refused(self):
        self.write_registry(_PLUGIN)
        bare, _commit = self.commit_registry()
        # A commit object planted in the registry and held by no branch or tag: served by its id alone.
        env = {**os.environ, "GIT_AUTHOR_NAME": "s", "GIT_AUTHOR_EMAIL": "s@example.invalid",
               "GIT_COMMITTER_NAME": "s", "GIT_COMMITTER_EMAIL": "s@example.invalid"}
        stray = subprocess.run(["git", "--git-dir", str(bare), "commit-tree", "HEAD^{tree}", "-m", "stray"],
                               check=True, capture_output=True, text=True, env=env, timeout=60).stdout.strip()
        self.assertEqual(_git(["--git-dir", str(bare), "for-each-ref", "--contains", stray]), "")
        self.assert_refused("registry_commit_not_published", _PLUGIN, "--registry", f"{bare}@{stray}",
                            words=("not on the default branch",))

    def test_a_commit_on_another_branch_only_is_refused(self):
        self.write_registry(_PLUGIN)
        _git(["init", "-q", "-b", "main", str(self.registry)])
        _git(["-C", str(self.registry), "add", "-A"])
        _git(["-C", str(self.registry), "-c", "user.name=r", "-c", "user.email=r@example.invalid", "commit", "-q",
              "-m", "one"])
        _git(["-C", str(self.registry), "checkout", "-q", "-b", "side"])
        (self.registry / "README.md").write_bytes(b"side\n")
        _git(["-C", str(self.registry), "add", "-A"])
        _git(["-C", str(self.registry), "-c", "user.name=r", "-c", "user.email=r@example.invalid", "commit", "-q",
              "-m", "two"])
        side = _git(["-C", str(self.registry), "rev-parse", "HEAD"]).strip()
        _git(["-C", str(self.registry), "checkout", "-q", "main"])
        bare = self.tmp / "two.git"
        _git(["clone", "--bare", "-q", str(self.registry), str(bare)])
        self.assert_refused("registry_commit_not_published", _PLUGIN, "--registry", f"{bare}@{side}")

    def test_an_https_registry_is_refused_before_git_starts(self):
        def no_git(*_args, **_kwargs):
            raise AssertionError("git was started")

        with patch("recursion_core.plugin_security.git_source.subprocess.Popen", no_git):
            self.assert_refused("registry_transport_unproven", _PLUGIN, "--registry",
                                "https://example.invalid/registry@" + "a" * 40, words=("git clone --mirror",))


@unittest.skipUnless(LINUX, LINUX_ONLY)
class TrueWordsTests(RegistryCase):
    def test_a_file_over_the_blob_figure_is_too_large_not_unreachable(self):
        self.write_registry(_PLUGIN)
        bare, commit = self.commit_registry()
        with patch("recursion_core.plugin_security.git_source._MAX_BLOB", 8):
            err = self.assert_refused("registry_too_large", _PLUGIN, "--registry", f"{bare}@{commit}")
        self.assertNotIn("Check the address", err)

    def test_a_listing_over_its_figure_is_too_large_not_unreachable(self):
        self.write_registry(_PLUGIN)
        bare, commit = self.commit_registry(extra={"registry/README.md": b"x\n", "docs/a.md": b"a\n"})
        with patch("recursion_core.plugin_security.git_source._MAX_LISTING", 120):
            self.assert_refused("registry_too_large", _PLUGIN, "--registry", f"{bare}@{commit}")

    def test_a_file_name_that_is_not_text_is_named_so(self):
        self.write_registry(_PLUGIN)
        with open(os.path.join(os.fsencode(self.registry), b"odd-\xff.md"), "wb") as handle:
            handle.write(b"x\n")
        bare, commit = self.commit_registry()
        self.assert_refused("registry_path_refused", _PLUGIN, "--registry", f"{bare}@{commit}",
                            words=("a file name that is not text",))

    def test_a_look_alike_name_in_the_registry_is_refused_and_named(self):
        self.write_registry(_PLUGIN)
        name = "registry/notesі.md"
        bare, commit = self.commit_registry(extra={name: b"x\n"})
        self.assert_refused("registry_path_refused", _PLUGIN, "--registry", f"{bare}@{commit}", words=(name,))

    def test_only_the_registry_areas_are_handed_to_the_install(self):
        from recursion_core.plugin_security import registry_source

        self.write_registry(_PLUGIN)
        bare, commit = self.commit_registry(extra={"ROOT.md": b"root\n", "tools/check.txt": b"tool\n"})
        seen = []
        real = registry_source.install_plugin

        def watching(config, root, *args, **kwargs):
            seen.append(sorted(os.listdir(root)))
            return real(config, root, *args, **kwargs)

        with patch.object(registry_source, "install_plugin", watching):
            status, _out, err = self.run_cli(_PLUGIN, "--registry", f"{bare}@{commit}")
        self.assertEqual(status, 0, err)
        self.assertEqual(seen, [["registry"]])


@unittest.skipUnless(LINUX, LINUX_ONLY)
class ArgumentsFirstTests(RegistryCase):
    """A reserved or malformed id and a version that is not text cost no read of any registry."""

    def refused_unread(self, plugin_id, code, *, registry=None, version=None):
        from recursion_core.plugin_security import install, registry_source
        from recursion_core.plugin_security.install import InstallRefused

        reads = []

        def counting(*args, **kwargs):
            reads.append(args)
            raise AssertionError("a registry was read")

        before = self.listing()
        with patch.object(registry_source, "registry_checkout", counting), \
                patch.object(install, "check_registry", counting):
            with self.assertRaises(InstallRefused) as caught:
                registry_source.install_from_registry(self.config, plugin_id, registry=registry, version=version,
                                                      state=self.state)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(reads, [])
        self.assertEqual(self.listing(), before)
        return caught.exception

    def test_a_reserved_id_reads_no_registry(self):
        refusal = self.refused_unread("install.json", "plugin_id_reserved", registry=f"{self.tmp}/r.git@{'a' * 40}")
        self.assertIn("Pick another plugin id", refusal.detail)

    def test_a_malformed_id_reads_no_registry(self):
        for bad in ("Bad Id", "../x", "INSTALL.JSON"):
            self.refused_unread(bad, "plugin_not_in_registry", registry=f"{self.tmp}/r.git@{'a' * 40}")

    def test_a_version_that_is_not_text_reads_no_registry(self):
        self.refused_unread(_PLUGIN, "release_not_in_registry", registry=f"{self.tmp}/r.git@{'a' * 40}", version=3)

    def test_the_ids_refusal_wins_over_no_registry_named(self):
        self.refused_unread("install.json", "plugin_id_reserved")


@unittest.skipUnless(LINUX, LINUX_ONLY)
class FilesMapTests(PublicCase):
    def write_public_again(self, files):
        from recursion_core.plugin_security.registry import build_index, files_digest, index_bytes

        path = self.registry / "registry" / _PLUGIN / "entry.json"
        entry = json.loads(path.read_text())
        entry["releases"][0]["files"] = files
        entry["releases"][0]["release_sha256"] = files_digest(files)
        path.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n")
        (self.registry / "registry" / "index.json").write_bytes(index_bytes(build_index([entry])))

    def test_the_right_digest_with_a_wrong_size_is_refused(self):
        _entry, folder = self.write_public()
        files = {path.name: _spec(path.read_bytes()) for path in sorted(folder.iterdir())}
        files["plugin.py"]["size"] += 1
        self.write_public_again(files)
        self.assert_refused("release_file_digest_mismatch", _PLUGIN, "--registry", self.pinned(),
                            words=("plugin.py",))

    def test_a_file_map_with_another_field_is_refused_and_named(self):
        _entry, folder = self.write_public()
        files = {path.name: _spec(path.read_bytes()) for path in sorted(folder.iterdir())}
        files["plugin.py"]["mode"] = "0644"
        self.write_public_again(files)
        self.assert_refused("release_files", _PLUGIN, "--registry", self.pinned(), words=("plugin.py:",))

    def test_a_file_map_missing_a_field_is_refused(self):
        _entry, folder = self.write_public()
        files = {path.name: _spec(path.read_bytes()) for path in sorted(folder.iterdir())}
        del files["plugin.py"]["executable"]
        self.write_public_again(files)
        self.assert_refused("release_files", _PLUGIN, "--registry", self.pinned())


if __name__ == "__main__":
    unittest.main()
