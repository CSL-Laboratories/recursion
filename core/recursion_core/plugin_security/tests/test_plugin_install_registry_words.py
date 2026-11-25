"""A registry's name is always a name, a printed finding cannot forge a line, and the list refuses damage.

Every registry is made up by the test in its own temporary folder; no test reaches a network address.
"""
from __future__ import annotations

import json
import os
import shutil
import unittest

from recursion_core.plugin_security.install import InstallRefused
from recursion_core.plugin_security.registry_list import load
from recursion_core.plugin_security.tests.test_plugin_cli_install import LINUX, LINUX_ONLY
from recursion_core.plugin_security.tests.test_plugin_install_files_beside_entry import PublicCase, _spec

_PLUGIN = "fixture.unsigned"


@unittest.skipUnless(LINUX, LINUX_ONLY)
class NameIsAlwaysANameTests(PublicCase):
    def test_a_folder_of_the_registrys_name_in_the_working_folder_is_never_read(self):
        self.write_registry(_PLUGIN)
        work = self.tmp / "work"
        planted = work / "csl"
        shutil.copytree(self.registry, planted)
        here = os.getcwd()
        os.chdir(work)
        self.addCleanup(os.chdir, here)
        err = self.assert_refused("registry_list_not_wired", _PLUGIN, "--registry", "csl")
        self.assertIn("neither read nor changed", err)


@unittest.skipUnless(LINUX, LINUX_ONLY)
class PrintedFindingTests(PublicCase):
    def test_a_file_name_with_control_characters_is_printed_escaped(self):
        _entry, folder = self.write_public()
        files = {path.name: _spec(path.read_bytes()) for path in sorted(folder.iterdir())}
        files["a\x1b[31m\rforged\nrefused: x"] = _spec(b"x")
        from recursion_core.plugin_security.registry import build_index, files_digest, index_bytes

        path = self.registry / "registry" / _PLUGIN / "entry.json"
        entry = json.loads(path.read_text())
        entry["releases"][0]["files"] = files
        entry["releases"][0]["release_sha256"] = files_digest(files)
        path.write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n")
        (self.registry / "registry" / "index.json").write_bytes(index_bytes(build_index([entry])))
        err = self.assert_refused("release_file_outside", _PLUGIN, "--registry", self.pinned())
        self.assertNotIn("\x1b", err)
        self.assertNotIn("\r", err)
        self.assertNotIn("\nrefused: x", err)
        self.assertIn("\\x1b[31m\\x0dforged\\x0arefused: x", err)


class _Store:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw


class ListDamageTests(unittest.TestCase):
    def damaged(self, value, code="registry_list_damaged"):
        with self.assertRaises(InstallRefused) as caught:
            load(_Store(json.dumps(value).encode()), shipped=[])
        self.assertEqual(caught.exception.code, code)

    def entry(self, **over):
        item = {"name": "mine", "kind": "published", "address": "https://example.invalid/r", "commit": "1" * 40}
        item.update(over)
        return item

    def test_two_registries_with_one_name(self):
        self.damaged({"shape": 1, "registries": [self.entry(), self.entry()]})

    def test_a_kind_that_is_not_one_of_the_three(self):
        self.damaged({"shape": 1, "registries": [self.entry(kind="trusted")]})

    def test_a_pin_that_is_not_a_full_commit(self):
        self.damaged({"shape": 1, "registries": [], "pins": {"csl": "main"}})

    def test_a_stored_entry_with_a_field_more(self):
        self.damaged({"shape": 1, "registries": [self.entry(extra=True)]})

    def test_a_stored_entry_with_a_field_less(self):
        item = self.entry()
        del item["commit"]
        self.damaged({"shape": 1, "registries": [item]})


if __name__ == "__main__":
    unittest.main()
