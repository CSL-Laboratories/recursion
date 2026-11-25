"""The list of registries: its shape, the shipped entries kept apart, and which registry a plugin comes from.

The list module reads and writes only a store handed to it. Here the store is a stand-in over one file
in the test's own temporary folder; no test reads a person's settings, and no test reaches a network
address.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from recursion_core.plugin_security.install import InstallRefused
from recursion_core.plugin_security.registry_list import Registry, choose, load, save, shipped_registries

_C1 = "1" * 40
_C2 = "2" * 40
_C3 = "3" * 40


class _Store:
    """A stand-in store: one file in the test's temporary folder."""

    def __init__(self, path: Path):
        self.path = path

    def read(self):
        return self.path.read_bytes() if self.path.exists() else None

    def write(self, data: bytes):
        self.path.write_bytes(data)


def _shipped(*items):
    return shipped_registries(json.dumps({"shape": 1, "registries": [
        {"name": n, "kind": "official", "address": a, "commit": c} for n, a, c in items]}).encode())


class ListCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rc-registry-list-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = _Store(self.tmp / "list.json")

    def code(self, call):
        with self.assertRaises(InstallRefused) as caught:
            call()
        return caught.exception.code, caught.exception.detail


class ShapeTests(ListCase):
    def test_the_release_ships_its_registries_as_data_and_ships_none_yet(self):
        self.assertEqual(shipped_registries(), [])

    def test_a_newer_shape_is_refused_with_the_shape_this_reader_reads(self):
        self.store.write(json.dumps({"shape": 2, "registries": []}).encode())
        code, detail = self.code(lambda: load(self.store, shipped=[]))
        self.assertEqual(code, "registry_list_shape_newer")
        self.assertIn("shape 2", detail)
        self.assertIn("reads shape 1", detail)

    def test_an_empty_list_says_how_to_name_a_registry(self):
        value = load(self.store, shipped=[])
        code, detail = self.code(lambda: choose(value, "fixture.one", lambda item: True))
        self.assertEqual(code, "registry_not_named")
        self.assertIn("--registry", detail)


class ChoiceTests(ListCase):
    def listed(self, *kinds):
        value = load(self.store, shipped=[])
        for position, kind in enumerate(kinds):
            commit = None if kind == "local" else _C1
            address = str(self.tmp / f"r{position}") if kind == "local" else f"https://example.invalid/r{position}"
            value.add(Registry(f"r{position}", kind, address, commit))
        return value

    def test_an_id_in_exactly_one_registry_is_installed_from_it(self):
        value = self.listed("official", "published")
        self.assertEqual(choose(value, "fixture.one", lambda item: item.name == "r1").name, "r1")

    def test_an_id_in_two_registries_is_refused_naming_both(self):
        value = self.listed("official", "published")
        code, detail = self.code(lambda: choose(value, "fixture.one", lambda item: True))
        self.assertEqual(code, "plugin_in_several_registries")
        self.assertIn("r0", detail)
        self.assertIn("r1", detail)
        self.assertIn("--registry <name>", detail)

    def test_the_order_of_the_list_never_decides(self):
        first = self.listed("official", "published")
        second = load(_Store(self.tmp / "other.json"), shipped=[])
        for item in reversed(first.own):
            second.add(item)
        self.assertEqual(self.code(lambda: choose(first, "x.y", lambda item: True)),
                         self.code(lambda: choose(second, "x.y", lambda item: True)))

    def test_the_kind_is_a_label_and_never_a_gate(self):
        # An official and a local registry both holding the id: neither wins by its kind.
        value = self.listed("official", "local")
        code, _detail = self.code(lambda: choose(value, "fixture.one", lambda item: True))
        self.assertEqual(code, "plugin_in_several_registries")

    def test_an_id_in_no_listed_registry(self):
        value = self.listed("official")
        code, detail = self.code(lambda: choose(value, "fixture.one", lambda item: False))
        self.assertEqual(code, "plugin_not_in_any_registry")
        self.assertIn("r0", detail)

    def test_a_removed_registry_is_off_the_list(self):
        value = self.listed("official", "published")
        value.remove("r0")
        self.assertEqual([item.name for item in value.registries()], ["r1"])


class ShippedApartTests(ListCase):
    def test_a_later_release_with_another_shipped_value_keeps_the_persons_entries(self):
        first = _shipped(("csl", "https://example.invalid/first", _C1))
        value = load(self.store, shipped=first)
        value.add(Registry("mine", "published", "https://example.invalid/mine", _C2))
        value.add(Registry("folder", "local", str(self.tmp / "folder"), None))
        value.pin("csl", _C3)
        save(self.store, value)
        # A later release ships the registry under another name at another address.
        later = _shipped(("csl2", "https://example.invalid/second", _C2))
        again = load(self.store, shipped=later)
        self.assertEqual([item.name for item in again.own], ["mine", "folder"])
        self.assertEqual(again.own, value.own)
        self.assertIn("This release no longer ships the registry csl; your own registries are unchanged.",
                      again.changed)
        self.assertIn("This release ships a new registry csl2 at https://example.invalid/second@" + _C2 + ".",
                      again.changed)
        self.assertIn("Your pin of the registry csl is kept, but this release no longer ships it.", again.changed)
        save(self.store, again)
        stored = json.loads(self.store.read())
        self.assertEqual([item["name"] for item in stored["registries"]], ["mine", "folder"])
        self.assertEqual([item["name"] for item in stored["shipped_seen"]], ["csl2"])

    def test_a_pin_of_a_shipped_registry_is_the_persons_and_applies(self):
        value = load(self.store, shipped=_shipped(("csl", "https://example.invalid/first", _C1)))
        value.pin("csl", _C2)
        save(self.store, value)
        again = load(self.store, shipped=_shipped(("csl", "https://example.invalid/first", _C1)))
        self.assertEqual(again.named("csl").commit, _C2)
        self.assertEqual(again.changed, [])

    def test_a_shipped_registry_cannot_be_removed(self):
        value = load(self.store, shipped=_shipped(("csl", "https://example.invalid/first", _C1)))
        code, _detail = self.code(lambda: value.remove("csl"))
        self.assertEqual(code, "registry_shipped")


class NotWiredTests(unittest.TestCase):
    def run_cli(self, *argv):
        from recursion_core.plugin_security._entrypoints import main

        with tempfile.TemporaryDirectory() as folder:
            out, err = io.StringIO(), io.StringIO()
            status = main(["plugin", *argv, "--config", folder + "/c", "--state", folder + "/s"] if argv[0] == "install"
                          else ["plugin", *argv], out=out, err=err, inp=io.StringIO(""))
            self.assertFalse(Path(folder, "c").exists())
            return status, err.getvalue()

    def test_a_registry_named_by_its_name_says_the_list_is_not_read_yet(self):
        status, err = self.run_cli("install", "fixture.one", "--registry", "csl")
        self.assertEqual(status, 1)
        self.assertIn("refused: registry_list_not_wired\n", err)
        self.assertIn("--registry <address>@<full commit>", err)

    def test_the_commands_that_would_rewrite_the_list_say_so(self):
        for action in ("add", "remove", "pin", "list"):
            status, err = self.run_cli("registry", action, "csl")
            self.assertEqual(status, 1, action)
            self.assertIn("refused: registry_list_not_wired\n", err)
            self.assertIn("neither read nor changed", err)


if __name__ == "__main__":
    unittest.main()
