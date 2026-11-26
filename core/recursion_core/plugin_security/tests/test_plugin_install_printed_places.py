"""Every command the plugin install prints for a person names the places the running command used.

(1) A printed command carries ``--config`` with the profile's absolute path unless the profile is the default
one, and ``--state`` with the state folder's absolute path unless the state folder is the default one, each by
the command line's one rule for the default place, quoted for the shell. Run as printed, it acts on the same
profile and checks the same revocation store as the command that printed it.
(2) One function builds every printed command. Every path that prints one is driven here for each of the four
pairs (profile default or not, state folder default or not), and every call of that function in the package is
reached: a new caller that no case drives fails this test.
(3) With no home and no XDG variable, a command given both places prints both and does not raise.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shlex
import sys
import unittest
from unittest import mock

from recursion_core.plugin_security import create_revocation_store
from recursion_core.plugin_security.manifest import TrustStore
from recursion_core.plugin_security.signing import key_id
from recursion_core.plugin_security.tests.support import Key, needs_crypto
from recursion_core.plugin_security.tests.test_plugin_cli_install import LINUX, LINUX_ONLY
from recursion_core.plugin_security.tests.test_plugin_install_pinned import PLUGIN, _tree
from recursion_core.plugin_security.tests.test_plugin_install_quarantine import NEXT_SOURCE, _QuarantineCase, _asking

# A printed command runs from ``recursion plugin`` to the first ``;``, ``(`` or ``)``, or to the end of the text.
_PRINTED = re.compile(r"recursion plugin [^;()]*")


def _printed(text: str) -> list:
    return [match.group(0).strip().rstrip(".") for match in _PRINTED.finditer(text)]


def _texts(value) -> list:
    """Every string inside a result: its values, notes, asks and next steps."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _texts(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _texts(item)]
    return []


def _call_sites() -> set:
    """Each call of ``_command`` in the modules of the plugin_security package, as (file, line), read with ast.

    This is how the calls are counted: the source of every module is parsed and each call whose callee is named
    ``_command`` is one site. The test then records, through a wrapper of ``install._command``, the file and line
    each printed command was built from, and requires the two sets to be equal."""
    from recursion_core.plugin_security import install

    sites = set()
    for path in sorted(Path(install.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name == "_command":
                    sites.add((str(path.resolve()), node.lineno))
    return sites


def _revoke(state: Path, signer) -> None:
    """Revoke ``signer``'s key in the revocation store of the state folder ``state``, made by the product's code."""
    folder = state / "plugin-security"
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    path = str(folder / "revoked.sqlite3")
    create_revocation_store(path)
    trust = TrustStore({}, revocation_store=path)
    try:
        trust.revoke(key_id(signer.public))
    finally:
        trust.close()


class _PlacesCase(_QuarantineCase):
    def assert_places(self, command: str, config_default: bool, state_default: bool) -> None:
        words = shlex.split(command)
        self.assertEqual(["recursion", "plugin"], words[:2], command)
        for flag, place, default in (("--config", self.config, config_default),
                                     ("--state", self.state, state_default)):
            if default:
                self.assertNotIn(flag, words, command)
            else:
                self.assertEqual(1, words.count(flag), command)
                self.assertEqual(str(Path(place).absolute()), words[words.index(flag) + 1], command)


@unittest.skipUnless(LINUX, LINUX_ONLY)
class PrintedAcceptRunsInTheSamePlacesTests(_PlacesCase):
    @needs_crypto
    def test_the_printed_accept_line_run_as_printed_checks_the_revocation_store_of_the_own_state_folder(self):
        signer = Key()
        self.quarantined(signer)
        status, said, out = self.main(self.install_argv())
        self.assertEqual(0, status, said)
        result = json.loads(out)
        self.assertEqual("quarantined", result.get("result"), result)
        accept = [command for command in _printed(result["next"]) if " accept " in command]
        self.assertEqual(1, len(accept), result["next"])
        _revoke(self.state, signer)
        before = _tree(self.config)
        fresh = self.tmp / "fresh-home"
        fresh.mkdir(mode=0o700)
        with mock.patch.dict(os.environ, {"HOME": str(fresh), "XDG_STATE_HOME": str(fresh / "state"),
                                          "XDG_CONFIG_HOME": str(fresh / "config")}):
            status, said, out = self.main(shlex.split(accept[0])[1:], "yes\n")
        self.assertEqual((1, ""), (status, out), said)
        self.assertIn("revoked", said)
        self.assertEqual(before, _tree(self.config))
        self.assertTrue(self.held_at().is_dir())


@unittest.skipUnless(LINUX, LINUX_ONLY)
class EveryPrintedCommandTests(_PlacesCase):
    def use_places(self, number: int, config_default: bool, state_default: bool) -> None:
        """A profile and a state folder of their own, each the default place or not (with a space, so the
        printed path is quoted), and the XDG variables that make the default places."""
        self.tmp = self.root / f"pair-{number}"
        self.tmp.mkdir(mode=0o700)
        xdg_config, xdg_state = self.tmp / "xdg-config", self.tmp / "xdg-state"
        os.environ["XDG_CONFIG_HOME"], os.environ["XDG_STATE_HOME"] = str(xdg_config), str(xdg_state)
        self.config = xdg_config / "recursion" if config_default else self.tmp / "con fig" / "recursion"
        self.state = xdg_state / "recursion" if state_default else self.tmp / "st ate"

    def setUp(self):
        super().setUp()
        self.root = self.tmp
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)

    def drive_every_path(self) -> list:
        """Every path that prints a command; returns every text a person reads from them."""
        texts = []
        signer = Key()
        self.signed_install(signer)
        for name in ("quarantine_accept", "quarantine_drop"):
            for plugin_id in (PLUGIN, "Not An Id"):
                kwargs = {"confirm": lambda _question: "yes"} if name == "quarantine_accept" else {}
                texts.append(self.not_changed(lambda: self.call(name, plugin_id, **kwargs)).detail)
        self.release("unsigned", source=NEXT_SOURCE, manifest=_asking(signer.public), version="1.1.0")
        refusal = self.refused(self.install)
        self.assertEqual("update_unsigned_over_signed", refusal.code)
        texts.append(refusal.detail)
        self.release("more", key=signer, signature="real", source=NEXT_SOURCE,
                     manifest=_asking(signer.public), version="1.1.0")
        result = self.install()
        self.assertEqual("quarantined", result.get("result"), result)
        texts.extend(_texts(result))
        held = self.held_at()
        extra = held / "notes.txt"
        extra.write_bytes(b"left by hand\n")
        texts.append(self.refused(lambda: self.call("quarantine_accept", PLUGIN, confirm=lambda _q: "yes")).detail)
        texts.extend(_texts(self.call("quarantine_list")))
        extra.unlink()
        partial = held.parent / ".partial-1.1.0"
        partial.mkdir(mode=0o700)
        texts.append(self.refused(lambda: self.call("quarantine_accept", PLUGIN, confirm=lambda _q: "yes")).detail)
        partial.rmdir()
        plugin = held / "plugin.py"
        good = plugin.read_bytes()
        plugin.write_bytes(bytes([good[0] ^ 1]) + good[1:])
        texts.append(self.refused(lambda: self.call("quarantine_accept", PLUGIN, confirm=lambda _q: "yes")).detail)
        texts.extend(_texts(self.call("quarantine_list")))
        plugin.write_bytes(good)
        # A held release that no longer counts as signed, after an unpin: its refusal names the ways on.
        self.unpin(confirm=lambda _question: "yes")
        data = self.live_json()
        data["trusted_keys"] = {}
        self.write_live(data)
        texts.append(self.refused(lambda: self.call("quarantine_accept", PLUGIN, confirm=lambda _q: "yes")).detail)
        return texts

    @needs_crypto
    def test_every_printed_command_names_each_place_exactly_when_it_is_not_the_default(self):
        from recursion_core.plugin_security import install

        sites = _call_sites()
        self.assertTrue(sites)
        real = install._command
        number = 0
        for config_default in (True, False):
            for state_default in (True, False):
                number += 1
                with self.subTest(config_default=config_default, state_default=state_default):
                    self.use_places(number, config_default, state_default)
                    reached = set()

                    def recorded(*args, **kwargs):
                        caller = sys._getframe(1)
                        reached.add((str(Path(caller.f_code.co_filename).resolve()), caller.f_lineno))
                        return real(*args, **kwargs)

                    with mock.patch.object(install, "_command", recorded):
                        texts = self.drive_every_path()
                    commands = [command for text in texts for command in _printed(text)]
                    self.assertGreaterEqual(len(commands), len(sites), commands)
                    for command in commands:
                        self.assert_places(command, config_default, state_default)
                    self.assertEqual(sites, reached, "a call of _command that no case drives, or a case that "
                                                     "reaches none")


@unittest.skipUnless(LINUX, LINUX_ONLY)
class NoHomeTests(_PlacesCase):
    @needs_crypto
    def test_with_no_home_and_no_xdg_variable_the_hold_names_both_places_and_does_not_raise(self):
        self.quarantined(Key())
        environ = {name: value for name, value in os.environ.items()
                   if name not in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME")}
        with mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(Path, "home", side_effect=RuntimeError("Could not determine home directory.")):
            status, said, out = self.main(self.install_argv())
        self.assertEqual(0, status, said)
        result = json.loads(out)
        self.assertEqual("quarantined", result.get("result"), result)
        commands = _printed(result["next"])
        self.assertEqual(3, len(commands), result["next"])
        for command in commands:
            self.assert_places(command, False, False)


if __name__ == "__main__":
    unittest.main()
