"""A plugin installed signed is pinned to its key; an unsigned or failing release of it is refused.

(1) A signed install writes ``pins[<id>] = {"signer_key_id", "version"}`` into ``install.json`` through the
installer's own commit: one new generation, whose copy is the live file.
(2) A release of a pinned plugin that is unsigned, fails its check, or is signed by a key not on the trusted
list refuses ``update_unsigned_over_signed``, writes nothing (generations, ``current``, the live file and
the grant as they were) and names the plugin, the pinned key, what failed and the two ways forward.
(3) A release signed by another key that is on the trusted list moves the pin only after a typed yes (one
line of question); the grant is dropped as before. Without the yes nothing is written.
(4) ``plugin unpin <id>`` asks a typed yes, then drops the pin and every grant of that plugin and nothing
else; a plain install of an unsigned release then works.
(5) An unpinned plugin installs, updates and is removed as before: no ``pins`` key is ever written for it.
(6) A file written before pins existed: each installed plugin whose grant names a signing key is pinned to it
on the first command, and nothing else changes. A grant whose plugin is not installed pins nothing. A remove
keeps the pin.
"""
from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from recursion_core.plugin_security.signing import key_id
from recursion_core.plugin_security.tests.support import PLUGIN_SOURCE, Key, needs_crypto
from recursion_core.plugin_security.tests.test_plugin_cli_install import LINUX, LINUX_ONLY, ProfileCase, _manifest

FIXTURES = Path(__file__).resolve().parents[4] / "schemas" / "plugin" / "v1" / "fixtures"
PLUGIN = "fixture.unsigned"
OLD_KEYS = {"authority_anchor", "grants", "registry_labels", "reserved", "trusted_keys"}


def _tree(path: Path) -> dict:
    """Every file under ``path`` with its bytes, and every folder, by relative name."""
    found = {}
    for folder, names, files in os.walk(path):
        rel = os.path.relpath(folder, path)
        found[rel] = None
        for name in files:
            found[os.path.join(rel, name)] = Path(folder, name).read_bytes()
    return found


class _PinCase(ProfileCase):
    def release(self, name, **kwargs):
        """Write a registry root of its own for one release, and use it for the next install."""
        self.registry = self.tmp / f"registry-{name}"
        return self.write_registry(**kwargs)

    def live(self) -> Path:
        return self.config / "plugins" / "install.json"

    def live_json(self) -> dict:
        return json.loads(self.live().read_text(encoding="utf-8"))

    def write_live(self, data: dict) -> None:
        self.live().write_bytes((json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        os.chmod(self.live(), 0o600)

    def held(self) -> dict:
        """The whole profile: generations, current, the live tree with install.json and every plugin file."""
        return _tree(self.config)

    def refused(self, call):
        from recursion_core.plugin_security.install import InstallRefused

        with self.assertRaises(InstallRefused) as caught:
            call()
        return caught.exception

    def not_changed(self, call):
        # Looked up by name, so that where it does not exist the call itself runs and the test says what it did.
        install = importlib.import_module("recursion_core.plugin_security.install")
        with self.assertRaises(getattr(install, "NothingChanged", type("Absent", (Exception,), {}))) as caught:
            call()
        return caught.exception

    def main(self, argv, answer=""):
        """The command line, with ``answer`` as what the user types."""
        from recursion_core.plugin_security._entrypoints import main

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(answer)):
            status = main(argv, out, err)
        return status, err.getvalue(), out.getvalue()

    def install_argv(self):
        return ["plugin", "install", "--registry", str(self.registry), "--plugin", PLUGIN,
                "--config", str(self.config), "--state", str(self.state)]

    def unpin(self, plugin_id=PLUGIN, confirm=None):
        from recursion_core.plugin_security.install import unpin_plugin

        return unpin_plugin(self.config, plugin_id, state=self.state, confirm=confirm)

    def signed_install(self, signer, *, trusted=None, version="1.0.0"):
        """Trust ``signer`` (and ``trusted``), install a release it signed, and give that release a grant."""
        from recursion_core.daemon.tests import plugin_fixture as fx

        fx.write_install(self.config, trusted=trusted or {"developer": signer.public})
        self.release("signed", key=signer, signature="real", version=version)
        result = self.install()
        self.assertEqual(("signed", "signature_valid_trusted_key"), (result["trust"], result["signature"]))
        data = self.live_json()
        data["grants"] = [fx.grant_for(PLUGIN_SOURCE, key=signer, plugin_id=PLUGIN)]
        self.write_live(data)
        return result

    def pinned_to(self, signer, version="1.0.0"):
        return {PLUGIN: {"signer_key_id": key_id(signer.public), "version": version}}


@unittest.skipUnless(LINUX, LINUX_ONLY)
class PinWrittenTests(_PinCase):
    @needs_crypto
    def test_a_signed_install_writes_its_pin_in_the_one_new_generation(self):
        from recursion_core.daemon import plugins
        from recursion_core.daemon.tests import plugin_fixture as fx

        signer = Key()
        fx.write_install(self.config, trusted={"developer": signer.public})
        self.release("signed", key=signer, signature="real")
        result = self.install()
        self.assertEqual(1, result["generation"])
        self.assertEqual(self.pinned_to(signer), self.live_json().get("pins"))
        generation = self.config / "plugin-generations" / "1" / "install.json"
        self.assertEqual(generation.read_bytes(), self.live().read_bytes())
        self.assertEqual(["1"], sorted(name for name in os.listdir(self.config / "plugin-generations")
                                       if name.isdigit()))
        # The daemon still reads the file; the pin grants nothing.
        self.assertEqual({}, plugins.read_install(self.config / "plugins")["grants"])

    def test_the_daemon_and_the_installer_read_the_good_pin_fixture_and_the_installer_refuses_the_bad_one(self):
        from recursion_core.daemon import plugins

        good = json.loads((FIXTURES / "good" / "install.v1" / "h06-a-pin-for-a-signed-plugin.json")
                          .read_text(encoding="utf-8"))
        bad = json.loads((FIXTURES / "bad" / "install.v1" / "b11-a-pin-with-a-member-nobody-defined.json")
                         .read_text(encoding="utf-8"))
        from recursion_core.daemon.tests import plugin_fixture as fx
        fx.write_install(self.config)
        self.write_live(good)
        self.assertEqual({"notes.search"}, set(plugins.read_install(self.config / "plugins")["grants"]))
        self.assertEqual([], self.listed()["plugins"])
        self.write_live(bad)
        self.assertEqual("install_unreadable", self.refused(self.listed).code)


@unittest.skipUnless(LINUX, LINUX_ONLY)
class RefusedTests(_PinCase):
    def assert_refused_unchanged(self, signer, what):
        signer_key = key_id(signer.public)
        before = self.held()
        refusal = self.refused(self.install)
        self.assertEqual(("update_unsigned_over_signed", ["update_unsigned_over_signed"]),
                         (refusal.code, refusal.codes))
        self.assertTrue(refusal.show_detail)
        for words in (f"The plugin {PLUGIN} is pinned to the key {signer_key}", what, "Nothing was written",
                      f"Install a release signed by {signer_key}", f"recursion plugin unpin {PLUGIN}"):
            self.assertIn(words, refusal.detail)
        self.assertEqual(before, self.held())
        status, said, out = self.main(self.install_argv())
        self.assertEqual((1, f"refused: update_unsigned_over_signed\n{refusal.detail}\n", ""), (status, said, out))
        self.assertEqual(before, self.held())

    @needs_crypto
    def test_an_unsigned_release_of_a_pinned_plugin_is_refused_and_nothing_is_written(self):
        signer = Key()
        self.signed_install(signer)
        self.release("unsigned", source=b"VALUE = 7\n", version="1.1.0")
        self.assert_refused_unchanged(signer, "release 1.1.0 is unsigned")

    @needs_crypto
    def test_a_release_signed_by_a_key_not_on_the_trusted_list_is_refused(self):
        signer, other = Key(), Key()
        self.signed_install(signer)
        self.release("untrusted", key=other, signature="real", source=b"VALUE = 7\n", version="1.1.0")
        self.assert_refused_unchanged(signer, f"is signed by the key {key_id(other.public)}, which is not on this "
                                      f"install's trusted list")

    @needs_crypto
    def test_a_tampered_tree_without_its_signature_is_refused(self):
        signer = Key()
        self.signed_install(signer)
        # The pinned key's own manifest, with other code and the signature left out.
        self.release("tampered", key=signer, source=b"TAMPERED = True\n", version="1.0.0")
        self.assert_refused_unchanged(signer, "release 1.0.0 is unsigned")

    @needs_crypto
    def test_a_tampered_tree_that_keeps_its_signature_is_refused_by_the_registry_as_before(self):
        signer = Key()
        self.signed_install(signer)
        self.release("tampered", key=signer, signature="real", source=b"TAMPERED = True\n", version="1.1.0")
        # The signature file no longer verifies the manifest: another key signed these bytes.
        changed = self.registry / "registry" / PLUGIN / "1.1.0" / "manifest.json"
        before = self.held()
        (changed.parent / "manifest.sig").write_text(Key().sign(changed.read_bytes()) + "\n")
        self.assertEqual("signature_invalid", self.refused(self.install).code)
        self.assertEqual(before, self.held())

    @needs_crypto
    def test_a_remove_keeps_the_pin_so_an_unsigned_install_after_it_is_refused(self):
        self.signed_install(Key())
        self.remove()
        self.assertIn(PLUGIN, self.live_json().get("pins", {}))
        self.release("unsigned", source=b"VALUE = 7\n", version="1.1.0")
        self.assertEqual("update_unsigned_over_signed", self.refused(self.install).code)

    @needs_crypto
    def test_a_release_signed_by_the_pinned_key_installs_and_the_pin_takes_its_version(self):
        signer = Key()
        self.signed_install(signer)
        self.release("next", key=signer, signature="real", source=b"VALUE = 7\n", version="1.1.0")
        self.assertEqual(2, self.install()["generation"])
        self.assertEqual(self.pinned_to(signer, "1.1.0"), self.live_json().get("pins"))


@unittest.skipUnless(LINUX, LINUX_ONLY)
class ChangedTrustedKeyTests(_PinCase):
    def setUp(self):
        super().setUp()
        self.first, self.second = Key(), Key()

    def prepared(self):
        self.signed_install(self.first, trusted={"first": self.first.public, "second": self.second.public})
        self.release("second", key=self.second, signature="real", source=b"VALUE = 7\n", version="1.1.0")

    @needs_crypto
    def test_without_the_typed_yes_nothing_is_written(self):
        self.prepared()
        before = self.held()
        asked = []

        def answer(question):
            asked.append(question)
            return "no\n"

        for confirm in (None, answer, lambda _question: "YES", lambda _question: None):
            with self.subTest(confirm=confirm):
                said = self.not_changed(lambda: self.install(**({} if confirm is None else {"confirm": confirm})))
                self.assertIn(f"stays pinned to the key {key_id(self.first.public)}", said.detail)
                self.assertEqual(before, self.held())
        self.assertEqual(1, len(asked))
        self.assertNotIn("\n", asked[0])
        for words in (PLUGIN, key_id(self.first.public), key_id(self.second.public), "trusted list", "yes"):
            self.assertIn(words, asked[0])
        status, said, out = self.main(self.install_argv(), "no\n")
        self.assertEqual((1, ""), (status, out))
        self.assertEqual(asked[0] + "\n", said.splitlines(keepends=True)[0])
        self.assertTrue(said.splitlines()[1].startswith("not changed: "))
        self.assertEqual(before, self.held())

    @needs_crypto
    def test_with_the_typed_yes_the_pin_moves_and_the_grant_is_dropped(self):
        self.prepared()
        status, said, out = self.main(self.install_argv(), "yes\n")
        self.assertEqual(0, status, said)
        self.assertEqual(2, json.loads(out)["generation"])
        data = self.live_json()
        self.assertEqual({PLUGIN: {"signer_key_id": key_id(self.second.public), "version": "1.1.0"}}, data.get("pins"))
        self.assertEqual([], data["grants"])


@unittest.skipUnless(LINUX, LINUX_ONLY)
class UnpinTests(_PinCase):
    @needs_crypto
    def test_unpin_without_the_typed_yes_writes_nothing(self):
        self.signed_install(Key())
        before = self.held()
        for confirm in (None, lambda _question: "nope\n", lambda _question: ""):
            with self.subTest(confirm=confirm):
                said = self.not_changed(lambda: self.unpin(confirm=confirm))
                self.assertIn("the answer was not yes", said.detail)
                self.assertEqual(before, self.held())
        status, said, out = self.main(["plugin", "unpin", PLUGIN, "--config", str(self.config)], "")
        self.assertEqual((1, ""), (status, out))
        self.assertTrue(said.startswith(f"Unpin the plugin {PLUGIN} from the key "), said)
        self.assertEqual(before, self.held())

    @needs_crypto
    def test_unpin_drops_the_pin_and_the_grant_only_and_a_plain_install_then_works(self):
        signer = Key()
        self.signed_install(signer)
        data = self.live_json()
        files = {name: value for name, value in self.held().items() if name.startswith(os.path.join("plugins", PLUGIN))}
        status, said, out = self.main(["plugin", "unpin", PLUGIN, "--config", str(self.config)], "yes\n")
        self.assertEqual(0, status, said)
        self.assertEqual(("unpin", 2), (json.loads(out)["command"], json.loads(out)["generation"]))
        after = self.live_json()
        self.assertEqual({}, after["pins"])
        self.assertEqual([], after["grants"])
        self.assertEqual({key: value for key, value in data.items() if key not in ("pins", "grants")},
                         {key: value for key, value in after.items() if key not in ("pins", "grants")})
        self.assertEqual(files, {name: value for name, value in self.held().items()
                                 if name.startswith(os.path.join("plugins", PLUGIN))})
        self.release("unsigned", source=b"VALUE = 7\n", version="1.1.0")
        result = self.install()
        self.assertEqual(("unsigned", 3), (result["trust"], result["generation"]))
        self.assertEqual({}, self.live_json()["pins"])

    def test_unpin_of_a_plugin_that_is_not_pinned_writes_nothing(self):
        self.release("unsigned")
        self.install()
        before = self.held()
        self.assertIn("is not pinned", self.not_changed(lambda: self.unpin(confirm=lambda _q: "yes")).detail)
        self.assertEqual(before, self.held())

    def test_unpin_refuses_platform_unsupported_before_any_file(self):
        from recursion_core.plugin_security import install

        with mock.patch.object(install, "platform_gap", return_value="O_NOFOLLOW"):
            refusal = self.refused(lambda: self.unpin(confirm=lambda _q: "yes"))
        self.assertEqual("platform_unsupported", refusal.code)
        self.assertFalse(self.config.exists())


@unittest.skipUnless(LINUX, LINUX_ONLY)
class UnpinnedGuardTests(_PinCase):
    """Green before and after: an unpinned plugin is installed, updated and removed as before."""

    def test_an_unpinned_plugin_installs_updates_and_is_removed_as_before(self):
        self.release("first")
        first = self.install()
        self.assertEqual(("unsigned", 1, "not granted"), (first["trust"], first["generation"], first["order"]))
        self.assertEqual(OLD_KEYS, set(self.live_json()))
        self.release("more", source=b"VALUE = 7\n", version="1.1.0", permissions=("read", "propose", "panel"))
        second = self.install()
        self.assertEqual(("unsigned", 2), (second["trust"], second["generation"]))
        self.assertEqual(OLD_KEYS, set(self.live_json()))
        self.assertEqual(set(first), set(second))
        self.remove()
        self.assertEqual(OLD_KEYS, set(self.live_json()))
        self.assertEqual([], self.listed()["plugins"])


@unittest.skipUnless(LINUX, LINUX_ONLY)
class UpgradeTests(_PinCase):
    def h01_with_its_plugin(self, installed=True):
        """The good fixture h01, and, when ``installed``, the plugin its grant names in the live tree."""
        from recursion_core.daemon.tests import plugin_fixture as fx

        data = json.loads((FIXTURES / "good" / "install.v1" / "h01-one-trusted-key-one-grant.json")
                          .read_text(encoding="utf-8"))
        fx.write_install(self.config)
        self.write_live(data)
        if installed:
            # The grant's digest is the SHA-256 of no bytes: an empty plugin.py.
            folder = self.config / "plugins" / "notes.search"
            folder.mkdir(mode=0o700)
            os.chmod(folder, 0o700)
            for name, raw in (("manifest.json", _manifest(b"", "notes.search", developer_key="11" * 32,
                                                          version="1.2.0")), ("plugin.py", b"")):
                (folder / name).write_bytes(raw)
                os.chmod(folder / name, 0o600)
        return data

    def test_a_signer_bound_grant_whose_plugin_is_not_installed_pins_nothing(self):
        self.h01_with_its_plugin(installed=False)
        self.release("unsigned")
        self.install()
        self.assertNotIn("pins", self.live_json())

    def test_a_good_fixture_with_a_signer_bound_grant_gets_its_pin_and_nothing_else(self):
        from recursion_core.daemon import plugins

        data = self.h01_with_its_plugin()
        before = plugins.read_install(self.config / "plugins")
        self.release("unsigned")
        self.install()
        written = self.live_json()
        grant = data["grants"][0]
        self.assertEqual({grant["plugin_id"]: {"signer_key_id": grant["signer_key_id"], "version": "1.2.0"}},
                         written.get("pins"))
        after = plugins.read_install(self.config / "plugins")
        for key in ("trusted_keys", "grants", "reserved", "authority_anchor"):
            self.assertEqual(before[key], after[key], key)
        manifest = (self.config / "plugins" / PLUGIN / "manifest.json").read_bytes()
        self.assertEqual({hashlib.sha256(manifest).hexdigest()}, set(written["registry_labels"]))
        self.assertEqual(["fixture.unsigned", "notes.search"], [row["plugin_id"] for row in self.listed()["plugins"]])

    def test_a_good_fixture_without_a_signer_bound_grant_gets_no_pin(self):
        from recursion_core.daemon.tests import plugin_fixture as fx

        data = json.loads((FIXTURES / "good" / "install.v1" / "h03-manual-grant-to-an-unsigned-plugin.json")
                          .read_text(encoding="utf-8"))
        fx.write_install(self.config)
        self.write_live(data)
        self.release("unsigned")
        self.install()
        self.assertNotIn("pins", self.live_json())

    @needs_crypto
    def test_a_file_from_before_pins_pins_the_signed_plugin_on_the_first_command(self):
        signer = Key()
        self.signed_install(signer)
        data = self.live_json()
        data.pop("pins", None)
        self.write_live(data)
        self.release("unsigned", source=b"VALUE = 7\n", version="1.1.0")
        self.assertEqual("update_unsigned_over_signed", self.refused(self.install).code)
        self.release("next", key=signer, signature="real", source=b"VALUE = 8\n", version="1.2.0")
        self.install()
        self.assertEqual(self.pinned_to(signer, "1.2.0"), self.live_json().get("pins"))


if __name__ == "__main__":
    unittest.main()
