"""A signed release of a pinned plugin that asks for more is held in quarantine, not installed.

(1) "Asks for more": the release's manifest requests a permission, a network destination, a credential
reference or an install script the running release did not request. Such a release, signed by a trusted
key, is not installed: its checked files are kept under ``plugin-quarantine/<id>/<version>/`` in the
config folder (owner-only), outside ``plugins/`` and never a generation. The running release stays live
with its grant. The install answers ``quarantined`` (exit 0) and prints what more it asks, one line each,
and the commands that go on from there.
(2) ``plugin quarantine list`` shows each held release and what more it asks.
(3) ``plugin quarantine accept <id>`` installs the held release only after a typed yes, with a new grant of
what it requests; ``order`` is never granted this way. Nothing is accepted by a flag or a default.
(4) ``plugin quarantine drop <id>`` moves the held files aside into a kept place and names it; nothing is
deleted.
(5) An unpinned plugin, and the refusal of an unsigned release of a pinned plugin, are as before.
(6) Every refusal of ``quarantine accept`` names the command that drops the held release, and a held release
that does not read is listed as ``damaged`` with that command; ``quarantine drop`` moves it aside.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import stat
import unittest

from recursion_core.plugin_security import canonical
from recursion_core.plugin_security.signing import key_id
from recursion_core.plugin_security.tests.support import Key, needs_crypto
from recursion_core.plugin_security.tests.test_plugin_cli_install import LINUX, LINUX_ONLY, _sha
from recursion_core.plugin_security.tests.test_plugin_install_pinned import PLUGIN, _PinCase, _tree

NEXT_SOURCE = b"VALUE = 7\n"
HOST = "api.example.com"


def _asking(developer, *, version="1.1.0", source=NEXT_SOURCE, permissions=("read", "panel", "network"),
            nets=(HOST,), creds=(), scripts=()):
    """A manifest of ``PLUGIN`` that requests ``permissions``, ``nets``, ``creds`` and ``scripts``."""
    return canonical({
        "schema": "csl-plugin-manifest/vNext",
        "plugin_id": PLUGIN,
        "version": version,
        "artifact_sha256": _sha(source),
        "dependency_lock_sha256": None,
        "install_scripts": list(scripts),
        "developer_key": developer,
        "permissions": list(permissions),
        "network_destinations": list(nets),
        "credential_refs": list(creds),
    })


class _QuarantineCase(_PinCase):
    def held_at(self, version="1.1.0") -> Path:
        return self.config / "plugin-quarantine" / PLUGIN / version

    def profile(self) -> dict:
        """Every file and folder of the profile outside the quarantine folder."""
        return {name: value for name, value in _tree(self.config).items()
                if not name.startswith("plugin-quarantine")}

    def generations(self) -> list:
        return sorted(os.listdir(self.config / "plugin-generations"))

    def call(self, name, *args, **kwargs):
        # Looked up by name, so that where a command does not exist the test fails on that, not on import.
        install = importlib.import_module("recursion_core.plugin_security.install")
        return getattr(install, name)(self.config, *args, state=self.state, **kwargs)

    def quarantined(self, signer, **asking):
        """Install ``signer``'s 1.0.0 with a grant, then offer its 1.1.0 that asks for more."""
        self.signed_install(signer)
        self.release("more", key=signer, signature="real", source=NEXT_SOURCE,
                     manifest=_asking(signer.public, **asking), version="1.1.0")


@unittest.skipUnless(LINUX, LINUX_ONLY)
class HeldTests(_QuarantineCase):
    @needs_crypto
    def test_a_signed_release_adding_a_network_destination_is_held_and_the_running_release_stays_live(self):
        signer = Key()
        self.quarantined(signer)
        before = self.profile()
        result = self.install()
        self.assertEqual("quarantined", result.get("result"), result)
        self.assertEqual(before, self.profile())
        self.assertEqual([f"{PLUGIN}"], [row["plugin_id"] for row in self.listed()["plugins"]])
        self.assertEqual("1.0.0", self.listed()["plugins"][0]["version"])
        self.assertEqual([PLUGIN], [grant["plugin_id"] for grant in self.live_json()["grants"]])
        self.assertEqual(self.pinned_to(signer), self.live_json()["pins"])
        held = self.held_at()
        self.assertEqual(str(held), result["held_at"])
        self.assertEqual(NEXT_SOURCE, (held / "plugin.py").read_bytes())
        self.assertEqual(_asking(signer.public), (held / "manifest.json").read_bytes())
        self.assertTrue((held / "manifest.sig").is_file())
        for folder in (held.parent.parent, held.parent, held):
            self.assertEqual(0o700, stat.S_IMODE(os.lstat(folder).st_mode), folder)
        for name in os.listdir(held):
            self.assertEqual(0o600, stat.S_IMODE(os.lstat(held / name).st_mode), name)
        self.assertEqual([
            f"The release 1.1.0 of the plugin {PLUGIN} asks for the permission network, which the running "
            f"release 1.0.0 did not request.",
            f"The release 1.1.0 of the plugin {PLUGIN} asks for the network destination {HOST}, which the "
            f"running release 1.0.0 did not request.",
        ], result["asks"])

    @needs_crypto
    def test_the_command_line_answers_quarantined_with_exit_0_one_line_for_each_ask_and_the_commands(self):
        self.quarantined(Key())
        status, said, out = self.main(self.install_argv())
        self.assertEqual(0, status, said)
        result = json.loads(out)
        self.assertEqual(("install", "quarantined"), (result["command"], result["result"]))
        lines = said.splitlines()
        self.assertEqual(result["asks"], lines[:2])
        self.assertEqual(3, len(lines), said)
        for words in (f"The release 1.1.0 of the plugin {PLUGIN} was not installed", str(self.held_at()),
                      f"recursion plugin quarantine accept {PLUGIN}", f"recursion plugin quarantine drop {PLUGIN}",
                      "recursion plugin quarantine list"):
            self.assertIn(words, lines[2])
        self.assertEqual(lines[2], result["next"])

    @needs_crypto
    def test_a_credential_reference_and_an_install_script_are_each_one_line(self):
        self.quarantined(Key(), permissions=("read", "panel", "credential"), nets=(), creds=("vault:notes",),
                         scripts=("make build",))
        result = self.install()
        self.assertEqual("quarantined", result.get("result"), result)
        self.assertEqual(3, len(result["asks"]))
        for line, what in zip(result["asks"], ("the permission credential", "the credential reference vault:notes",
                                               "the install script make build")):
            self.assertIn(f"asks for {what}, which", line)

    @needs_crypto
    def test_a_release_by_another_trusted_key_that_asks_for_more_is_held_without_a_question(self):
        first, second = Key(), Key()
        self.signed_install(first, trusted={"first": first.public, "second": second.public})
        self.release("second", key=second, signature="real", source=NEXT_SOURCE,
                     manifest=_asking(second.public), version="1.1.0")
        before = self.profile()
        asked = []
        result = self.install(confirm=lambda question: asked.append(question) or "yes")
        self.assertEqual(("quarantined", []), (result.get("result"), asked))
        self.assertEqual(before, self.profile())

    @needs_crypto
    def test_the_same_release_again_is_left_as_held_and_a_newer_one_moves_the_older_aside(self):
        signer = Key()
        self.quarantined(signer)
        self.install()
        before = _tree(self.config)
        self.assertNotIn("kept", self.install())
        self.assertEqual(before, _tree(self.config))
        self.release("newer", key=signer, signature="real", source=NEXT_SOURCE,
                     manifest=_asking(signer.public, version="1.2.0"), version="1.2.0")
        result = self.install()
        self.assertEqual("quarantined", result.get("result"), result)
        self.assertEqual(["1.2.0"], os.listdir(self.held_at().parent))
        self.assertEqual(1, len(result["kept"]))
        self.assertTrue(result["kept"][0].startswith(f"The held release 1.1.0 of the plugin {PLUGIN} is kept at "))
        place = Path(result["kept"][0].split(" is kept at ", 1)[1].split(";", 1)[0])
        self.assertEqual(NEXT_SOURCE, (place / "plugin.py").read_bytes())

    @needs_crypto
    def test_quarantine_list_shows_the_held_release_and_what_it_asks(self):
        self.quarantined(Key())
        asks = self.install()["asks"]
        listed = self.call("quarantine_list")
        self.assertEqual([{"plugin_id": PLUGIN, "version": "1.1.0", "held_at": str(self.held_at()), "asks": asks}],
                         listed["held"])
        status, said, out = self.main(["plugin", "quarantine", "list", "--config", str(self.config)])
        self.assertEqual((0, ""), (status, said))
        self.assertEqual(listed["held"], json.loads(out)["held"])


@unittest.skipUnless(LINUX, LINUX_ONLY)
class AcceptTests(_QuarantineCase):
    @needs_crypto
    def test_accept_without_the_typed_yes_writes_nothing(self):
        self.quarantined(Key(), permissions=("read", "panel", "network", "order"))
        self.install()
        before = _tree(self.config)
        asked = []

        def answer(question):
            asked.append(question)
            return "no\n"

        for confirm in (None, answer, lambda _question: "YES", lambda _question: None, lambda _question: ""):
            with self.subTest(confirm=confirm):
                said = self.not_changed(lambda: self.call("quarantine_accept", PLUGIN, confirm=confirm))
                self.assertIn("the answer was not yes", said.detail)
                self.assertEqual(before, _tree(self.config))
        self.assertEqual(1, len(asked))
        self.assertNotIn("\n", asked[0])
        for words in (PLUGIN, "1.1.0", "read, panel, network", "order is never granted this way", "yes"):
            self.assertIn(words, asked[0])
        status, said, out = self.main(["plugin", "quarantine", "accept", PLUGIN, "--config", str(self.config)], "")
        self.assertEqual((1, ""), (status, out))
        self.assertEqual(asked[0], said.splitlines()[0])
        self.assertTrue(said.splitlines()[1].startswith("not changed: "), said)
        self.assertEqual(before, _tree(self.config))

    @needs_crypto
    def test_accept_with_the_typed_yes_installs_it_with_a_new_grant_and_never_order(self):
        from recursion_core.daemon import plugins

        signer = Key()
        self.quarantined(signer, permissions=("read", "panel", "network", "order"))
        self.install()
        status, said, out = self.main(["plugin", "quarantine", "accept", PLUGIN, "--config", str(self.config),
                                       "--state", str(self.state)], "yes\n")
        self.assertEqual(0, status, said)
        result = json.loads(out)
        self.assertEqual(("quarantine accept", "1.1.0", 2, "not granted"),
                         (result["command"], result["version"], result["generation"], result["order"]))
        self.assertEqual(["2"], [name for name in self.generations() if name.isdigit()][-1:])
        self.assertEqual(NEXT_SOURCE, (self.config / "plugins" / PLUGIN / "plugin.py").read_bytes())
        grants = self.live_json()["grants"]
        self.assertEqual(1, len(grants))
        self.assertEqual((PLUGIN, _sha(NEXT_SOURCE), key_id(signer.public), ["read", "panel", "network"]),
                         (grants[0]["plugin_id"], grants[0]["artifact_sha256"], grants[0]["signer_key_id"],
                          grants[0]["permissions"]))
        self.assertEqual(self.pinned_to(signer, "1.1.0"), self.live_json()["pins"])
        self.assertEqual({PLUGIN}, set(plugins.read_install(self.config / "plugins")["grants"]))
        self.assertFalse(self.held_at().exists())
        self.assertEqual([], self.call("quarantine_list")["held"])

    @needs_crypto
    def test_accept_checks_the_held_release_again_and_refuses_one_no_longer_signed_by_a_trusted_key(self):
        signer = Key()
        self.quarantined(signer)
        self.install()
        data = self.live_json()
        data["trusted_keys"] = {}
        self.write_live(data)
        before = _tree(self.config)
        asked = []
        refusal = self.refused(lambda: self.call("quarantine_accept", PLUGIN,
                                                 confirm=lambda question: asked.append(question) or "yes"))
        self.assertEqual(("update_unsigned_over_signed", []), (refusal.code, asked))
        self.assertEqual(before, _tree(self.config))

    def test_accept_and_drop_with_nothing_held_write_nothing_and_name_the_list_command(self):
        self.release("unsigned")
        self.install()
        before = _tree(self.config)
        for name in ("quarantine_accept", "quarantine_drop"):
            with self.subTest(name=name):
                said = self.not_changed(lambda: self.call(name, PLUGIN, **(
                    {"confirm": lambda _q: "yes"} if name == "quarantine_accept" else {})))
                for words in (f"no release of the plugin {PLUGIN} is held in quarantine", "nothing was written",
                              "recursion plugin quarantine list"):
                    self.assertIn(words, said.detail)
                self.assertEqual(before, _tree(self.config))


@unittest.skipUnless(LINUX, LINUX_ONLY)
class DropTests(_QuarantineCase):
    @needs_crypto
    def test_drop_moves_the_held_files_into_a_kept_place_and_names_it_and_deletes_nothing(self):
        self.quarantined(Key())
        self.install()
        files = {name: (self.held_at() / name).read_bytes() for name in os.listdir(self.held_at())}
        before = self.profile()
        status, said, out = self.main(["plugin", "quarantine", "drop", PLUGIN, "--config", str(self.config)])
        self.assertEqual(0, status, said)
        result = json.loads(out)
        self.assertEqual("quarantine drop", result["command"])
        self.assertEqual(result["kept"], said.splitlines())
        line = result["kept"][0]
        self.assertTrue(line.startswith(f"The held release 1.1.0 of the plugin {PLUGIN} is kept at "), line)
        self.assertIn("nothing was deleted", line)
        place = Path(line.split(" is kept at ", 1)[1].split(";", 1)[0])
        self.assertEqual(files, {name: (place / name).read_bytes() for name in os.listdir(place)})
        self.assertFalse(self.held_at().exists())
        self.assertEqual([], self.call("quarantine_list")["held"])
        after = self.profile()
        added = {name for name in after if name not in before}
        self.assertTrue(added and all(name.startswith(os.path.join("plugin-generations", ".kept-")) for name in added),
                        added)
        self.assertEqual(before, {name: value for name, value in after.items() if name not in added})
        self.assertTrue(any(str(place.parent) in kept for kept in self.listed().get("kept", ())))


@unittest.skipUnless(LINUX, LINUX_ONLY)
class AsBeforeTests(_QuarantineCase):
    """Green before and after: an unpinned plugin, and slice 9's refusal, are not held."""

    def test_an_unpinned_plugin_whose_update_asks_for_more_installs_as_before(self):
        self.release("first")
        self.install()
        self.release("more", source=NEXT_SOURCE, manifest=_asking("11" * 32), version="1.1.0")
        result = self.install()
        self.assertEqual(("unsigned", 2), (result["trust"], result["generation"]))
        self.assertNotIn("result", result)
        self.assertFalse((self.config / "plugin-quarantine").exists())

    @needs_crypto
    def test_an_unsigned_release_of_a_pinned_plugin_that_asks_for_more_is_still_refused(self):
        signer = Key()
        self.signed_install(signer)
        self.release("more", source=NEXT_SOURCE, manifest=_asking(signer.public), version="1.1.0")
        before = _tree(self.config)
        self.assertEqual("update_unsigned_over_signed", self.refused(self.install).code)
        self.assertEqual(before, _tree(self.config))

    @needs_crypto
    def test_a_release_by_the_pinned_key_that_asks_for_nothing_more_installs_as_before(self):
        signer = Key()
        self.signed_install(signer)
        self.release("same", key=signer, signature="real", source=NEXT_SOURCE,
                     manifest=_asking(signer.public, permissions=("read", "panel"), nets=()), version="1.1.0")
        result = self.install()
        self.assertEqual(("signed", 2), (result["trust"], result["generation"]))
        self.assertNotIn("result", result)
        self.assertFalse((self.config / "plugin-quarantine").exists())


@unittest.skipUnless(LINUX, LINUX_ONLY)
class WayOutTests(_QuarantineCase):
    """Each refusal of a held release names the command that drops it, with the plugin's real id."""

    drop_words = f"recursion plugin quarantine drop {PLUGIN}"

    def refused_accept(self, code: str) -> str:
        """Accept with a yes refuses ``code``, asks nothing and writes nothing, also on the command line (exit 1,
        the code, then the detail). Returns the detail."""
        before = _tree(self.config)
        asked = []
        refusal = self.refused(lambda: self.call("quarantine_accept", PLUGIN,
                                                 confirm=lambda question: asked.append(question) or "yes"))
        self.assertEqual((code, []), (refusal.code, asked))
        self.assertEqual(before, _tree(self.config))
        status, said, out = self.main(["plugin", "quarantine", "accept", PLUGIN, "--config", str(self.config),
                                       "--state", str(self.state)], "yes\n")
        self.assertEqual((1, ""), (status, out))
        self.assertEqual([f"refused: {code}", refusal.detail], said.splitlines())
        self.assertEqual(before, _tree(self.config))
        return refusal.detail

    @needs_crypto
    def test_an_extra_file_in_the_held_folder_refuses_with_the_drop_command(self):
        self.quarantined(Key())
        self.install()
        (self.held_at() / "notes.txt").write_bytes(b"left by hand\n")
        detail = self.refused_accept("plugin_directory_untrusted")
        self.assertIn(f"The folder {self.held_at()} is not a release this command held. Run: ", detail)
        self.assertIn(self.drop_words, detail)

    @needs_crypto
    def test_a_partial_folder_left_beside_the_held_release_refuses_with_the_drop_command(self):
        self.quarantined(Key())
        self.install()
        (self.held_at().parent / ".partial-1.1.0").mkdir(mode=0o700)
        detail = self.refused_accept("plugin_directory_untrusted")
        self.assertIn("holds more than one release, or an entry this command did not write. Run: ", detail)
        self.assertIn(self.drop_words, detail)

    @needs_crypto
    def test_accept_of_a_held_release_that_does_not_read_names_the_drop_command(self):
        self.quarantined(Key())
        self.install()
        plugin = self.held_at() / "plugin.py"
        good = plugin.read_bytes()
        plugin.write_bytes(bytes([good[0] ^ 1]) + good[1:])
        detail = self.refused_accept("generation_unreadable")
        self.assertTrue(detail.startswith(f"The release held at {self.held_at()} does not read as the release "
                                          f"1.1.0 of the plugin {PLUGIN}"), detail)
        self.assertIn(f"cannot be accepted. Run: {self.drop_words}", detail)
        plugin.write_bytes(good)
        (self.held_at() / "registry-label").write_bytes(b"\xff\n")
        self.assertIn(f"cannot be accepted. Run: {self.drop_words}", self.refused_accept("generation_unreadable"))

    @needs_crypto
    def test_a_held_release_that_does_not_read_is_listed_as_damaged_and_drop_moves_it_aside(self):
        self.quarantined(Key())
        self.install()
        plugin = self.held_at() / "plugin.py"
        good = plugin.read_bytes()
        plugin.write_bytes(bytes([good[0] ^ 1]) + good[1:])
        listed = self.call("quarantine_list")["held"]
        self.assertEqual([{"plugin_id", "version", "held_at", "asks", "damaged", "next"}],
                         [set(row) for row in listed], listed)
        self.assertEqual((PLUGIN, "1.1.0", str(self.held_at()), [], True),
                         tuple(listed[0][key] for key in ("plugin_id", "version", "held_at", "asks", "damaged")))
        self.assertIn(f"{self.held_at()} does not read", listed[0]["next"])
        self.assertIn(f"Run: {self.drop_words}", listed[0]["next"])
        status, said, out = self.main(["plugin", "quarantine", "list", "--config", str(self.config), "--state",
                                       str(self.state)])
        self.assertEqual((0, ""), (status, said))
        self.assertEqual(listed, json.loads(out)["held"])
        result = self.call("quarantine_drop", PLUGIN)
        self.assertTrue(result["kept"][0].startswith(f"The held release 1.1.0 of the plugin {PLUGIN} is kept at "),
                        result)
        place = Path(result["kept"][0].split(" is kept at ", 1)[1].split(";", 1)[0])
        self.assertEqual(bytes([good[0] ^ 1]) + good[1:], (place / "plugin.py").read_bytes())
        self.assertFalse(self.held_at().exists())
        self.assertEqual([], self.call("quarantine_list")["held"])


if __name__ == "__main__":
    unittest.main()
