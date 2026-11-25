"""A release whose files stand beside its entry: the install proves each file and the whole release.

The public form of a release names every file of its folder with its digest and size (``files``) and
the digest of that map (``release_sha256``); its ``source`` is null, so no plugin repository is read.
Every registry here is made up by the test and committed into a local bare repository of its own; no
test reaches a network address. Each refusal names the file and leaves the profile as it was.
"""
from __future__ import annotations

import hashlib
import json
import os
import unittest
from unittest.mock import patch

from recursion_core.plugin_security.tests.test_plugin_cli_install import LINUX, LINUX_ONLY
from recursion_core.plugin_security.tests.test_plugin_install_from_registry import RegistryCase

_PLUGIN = "fixture.unsigned"


def _spec(data: bytes) -> dict:
    return {"executable": False, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def _digest(files: dict) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii")).hexdigest()


class PublicCase(RegistryCase):
    def write_public(self, *, files=None, release_sha256=None, source=None):
        """Write the release in the public form: files named beside the entry, source null."""
        from recursion_core.plugin_security.registry import build_index, index_bytes

        entry = self.write_registry(_PLUGIN)
        folder = self.registry / "registry" / _PLUGIN / "1.0.0"
        if files is None:
            files = {path.name: _spec(path.read_bytes()) for path in sorted(folder.iterdir())}
        release = entry["releases"][0]
        release["source"] = source
        release["files"] = files
        release["release_sha256"] = release_sha256 or _digest(files)
        (self.registry / "registry" / _PLUGIN / "entry.json").write_text(
            json.dumps(entry, indent=2, sort_keys=True) + "\n")
        (self.registry / "registry" / "index.json").write_bytes(index_bytes(build_index([entry])))
        return entry, folder

    def pinned(self):
        bare, commit = self.commit_registry()
        return f"{bare}@{commit}"


@unittest.skipUnless(LINUX, LINUX_ONLY)
class FilesBesideTheEntryTests(PublicCase):
    def test_the_install_reads_the_files_beside_the_entry_and_no_plugin_repository(self):
        self.write_public()
        registry = self.pinned()

        def no_plugin_repository(*_args, **_kwargs):
            raise AssertionError("a plugin repository was read")

        with patch("recursion_core.plugin_security.install.fetch_plugin_source", no_plugin_repository):
            status, out, err = self.run_cli(_PLUGIN, "--registry", registry)
        self.assertEqual(status, 0, err)
        self.assertEqual(json.loads(out)["plugin_id"], _PLUGIN)
        self.assertTrue((self.config / "plugins" / _PLUGIN / "plugin.py").is_file())

    def test_a_file_that_differs_is_refused_and_named(self):
        _entry, folder = self.write_public()
        (folder / "plugin.py").write_bytes(b"VALUE = 'changed after the entry was written'\n")
        self.assert_refused("release_file_digest_mismatch", _PLUGIN, "--registry", self.pinned(),
                            words=(f"registry/{_PLUGIN}/1.0.0/plugin.py",))

    def test_a_named_file_that_is_missing_is_refused_and_named(self):
        _entry, folder = self.write_public()
        files = {path.name: _spec(path.read_bytes()) for path in sorted(folder.iterdir())}
        files["manifest.sig"] = _spec(b"0" * 128 + b"\n")
        self.write_public_again(files)
        self.assert_refused("release_file_missing", _PLUGIN, "--registry", self.pinned(),
                            words=("manifest.sig",))

    def test_an_extra_file_is_refused_and_named(self):
        _entry, folder = self.write_public()
        (folder / "manifest.sig").write_bytes(b"0" * 128 + b"\n")
        self.assert_refused("release_file_extra", _PLUGIN, "--registry", self.pinned(),
                            words=(f"registry/{_PLUGIN}/1.0.0/manifest.sig",))

    def test_a_link_in_the_release_folder_is_refused_and_named(self):
        _entry, folder = self.write_public()
        os.symlink("plugin.py", folder / "manifest.sig")
        self.assert_refused("registry_link_refused", _PLUGIN, "--registry", self.pinned(),
                            words=(f"registry/{_PLUGIN}/1.0.0/manifest.sig",))

    def test_a_named_file_outside_the_entry_folder_is_refused_and_named(self):
        _entry, folder = self.write_public()
        files = {path.name: _spec(path.read_bytes()) for path in sorted(folder.iterdir())}
        files["../../other/plugin.py"] = _spec(b"x")
        self.write_public_again(files)
        self.assert_refused("release_file_outside", _PLUGIN, "--registry", self.pinned(),
                            words=("../../other/plugin.py",))

    def test_a_release_digest_that_is_not_the_files_map_is_refused(self):
        self.write_public(release_sha256="0" * 64)
        self.assert_refused("release_digest_mismatch", _PLUGIN, "--registry", self.pinned())

    def test_a_source_beside_the_files_is_refused(self):
        self.write_public(source={"repository": "https://example.invalid/fixtures/x",
                                  "commit": "a0c3713d92acd5bc3cd3e3b6469b51e250b94715", "path": "plugin.py"})
        self.assert_refused("release_source", _PLUGIN, "--registry", self.pinned())

    def write_public_again(self, files):
        from recursion_core.plugin_security.registry import build_index, index_bytes

        path = self.registry / "registry" / _PLUGIN / "entry.json"
        entry = json.loads(path.read_text())
        entry["releases"][0]["files"] = files
        entry["releases"][0]["release_sha256"] = _digest(files)
        path.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n")
        (self.registry / "registry" / "index.json").write_bytes(index_bytes(build_index([entry])))


if __name__ == "__main__":
    unittest.main()
