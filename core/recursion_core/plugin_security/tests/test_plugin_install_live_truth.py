"""The live ``plugins/`` tree is the truth, and no command ends in a traceback.

(P1) A trusted key, a grant or a reserved name taken out of or put into the live ``install.json`` by hand
stays as the user left it after ``list``, after an install of another plugin and after a remove of another
plugin, and the next generation carries it. A live plugin folder the installer did not write is never
deleted in silence: every command refuses ``plugin_directory_untrusted``, writes nothing, and the command
line prints the steps that end the refusal. (S1) A live folder whose name is not its manifest's plugin id
refuses the same way in all three commands, before the first install and after it. (P6 b) Every OSError of
install, list and remove ends as ``refused: filesystem_refused`` with the system's message and exit 1.
"""
from __future__ import annotations

import errno
import io
import json
import os
import unittest

from recursion_core.plugin_security.tests.support import PLUGIN_SOURCE
from recursion_core.plugin_security.tests.test_plugin_cli_install import LINUX, LINUX_ONLY, _sha
from recursion_core.plugin_security.tests.test_plugin_install_publish import PublishCase

KEY_FIRST = "22" * 32
KEY_SECOND = "33" * 32
MOVE_OUT = "Move it out of the plugins directory; then run the command again."


def _rename_steps(path, plugin_id):
    return (f"The folder {path} holds the plugin {plugin_id}. Rename the folder to {plugin_id}, "
            f"or move it out of the plugins directory; then run the command again.")


@unittest.skipUnless(LINUX, LINUX_ONLY)
class LiveCase(PublishCase):
    def live(self):
        return self.config / "plugins"

    def write_live(self, data):
        live = self.live()
        live.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.config, 0o700)
        os.chmod(live, 0o700)
        path = live / "install.json"
        path.write_bytes((json.dumps(data) + "\n").encode("utf-8"))
        os.chmod(path, 0o600)

    def read_live(self):
        return json.loads((self.live() / "install.json").read_text(encoding="utf-8"))

    def read_generation(self):
        number = self.current().strip()
        return json.loads((self.gens() / number / "install.json").read_text(encoding="utf-8"))

    @staticmethod
    def trust(data):
        reserved = data.get("reserved", {})
        return (data.get("trusted_keys", {}), data.get("grants", []), sorted(reserved.get("hosts", [])),
                sorted(reserved.get("credential_prefixes", [])))

    def argv(self, command, root=None, plugin_id="fixture.unsigned"):
        tail = ["--config", str(self.config), "--state", str(self.state)]
        if command == "install":
            return ["plugin", "install", "--registry", str(root), "--plugin", plugin_id, *tail]
        if command == "remove":
            return ["plugin", "remove", "--plugin", plugin_id, *tail]
        return ["plugin", "list", *tail]

    def main(self, argv):
        from recursion_core.plugin_security._entrypoints import main

        out, err = io.StringIO(), io.StringIO()
        status = main(argv, out, err)
        return status, err.getvalue(), out.getvalue()


class HandEditStaysTests(LiveCase):
    """(P1, D1) The live install.json is the truth; every command carries its trust fields unchanged."""

    EDITED = ({"second": KEY_SECOND}, [], ["one.example", "two.example"], [])

    def _hand_edited(self):
        self.roots = self.registries()
        grant = {"plugin_id": "fixture.unsigned", "artifact_sha256": _sha(PLUGIN_SOURCE), "signer_key_id": None,
                 "permissions": ["read", "panel"]}
        self.write_live({"trusted_keys": {"first": KEY_FIRST}, "grants": [grant],
                         "reserved": {"hosts": ["one.example"], "credential_prefixes": []}})
        self.install_from(self.roots["a"])
        installed = self.read_live()
        self.assertEqual({"first": KEY_FIRST}, installed["trusted_keys"])
        self.assertEqual(["fixture.unsigned"], [item["plugin_id"] for item in installed["grants"]])

        # By hand: the key and the grant out, a second key and a second reserved host in.
        def change(data):
            data["trusted_keys"] = {"second": KEY_SECOND}
            data["grants"] = []
            data["reserved"]["hosts"].append("two.example")

        edited = self.read_live()
        change(edited)
        self.write_live(edited)

    def test_list_keeps_a_hand_edit_of_the_live_install_file(self):
        self._hand_edited()
        self.assertEqual(["fixture.unsigned"], self.listed_ids())
        self.assertEqual(self.EDITED, self.trust(self.read_live()))
        from recursion_core.daemon import plugins as daemon_plugins
        self.assertEqual({}, daemon_plugins.read_install(self.live())["grants"])

    def test_an_install_of_another_plugin_carries_the_hand_edit_into_the_next_generation(self):
        self._hand_edited()
        result = self.install_from(self.roots["beta"], "fixture.beta")
        self.assertEqual(2, result["generation"])
        self.assertEqual(self.EDITED, self.trust(self.read_generation()))
        self.assertEqual(self.EDITED, self.trust(self.read_live()))
        self.assertEqual(["fixture.beta", "fixture.unsigned"], self.listed_ids())

    def test_a_remove_of_another_plugin_carries_the_hand_edit_into_the_next_generation(self):
        self._hand_edited()
        self.install_from(self.roots["beta"], "fixture.beta")
        result = self.remove("fixture.beta")
        self.assertEqual(3, result["generation"])
        self.assertEqual(self.EDITED, self.trust(self.read_generation()))
        self.assertEqual(self.EDITED, self.trust(self.read_live()))
        self.assertEqual(["fixture.unsigned"], self.listed_ids())

    def test_an_install_stopped_before_its_pointer_does_not_put_back_a_hand_edit(self):
        self._hand_edited()
        from recursion_core.plugin_security import install as install_mod

        real = install_mod._write_pointer

        def full_disk(*_args, **_kwargs):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

        install_mod._write_pointer = full_disk
        try:
            with self.assertRaises(OSError):
                self.install_from(self.roots["beta"], "fixture.beta")
        finally:
            install_mod._write_pointer = real
        self.assertEqual("1\n", self.current())
        self.assertEqual(["fixture.unsigned"], self.listed_ids())
        self.assertEqual(self.EDITED, self.trust(self.read_live()))

    def test_a_plugin_folder_taken_out_by_hand_stays_out(self):
        self.roots = self.registries()
        self.install_from(self.roots["a"])
        self.install_from(self.roots["beta"], "fixture.beta")
        away = self.tmp / "taken-out"
        os.rename(self.live() / "fixture.beta", away)
        self.assertEqual(["fixture.unsigned"], self.listed_ids())
        self.install_from(self.roots["new"])
        self.assertFalse(os.path.lexists(self.live() / "fixture.beta"))
        self.assertEqual(["fixture.unsigned"], self.listed_ids())


class FolderNotWrittenTests(LiveCase):
    """(P1, D4) A live folder the installer did not write refuses typed, names itself, and is left in place."""

    def _commands(self):
        return {
            "list": (self.listed, self.argv("list")),
            "install": (lambda: self.install_from(self.roots["beta"], "fixture.beta"),
                        self.argv("install", self.roots["beta"], "fixture.beta")),
            "remove": (lambda: self.remove("fixture.unsigned"), self.argv("remove")),
        }

    def _refused_with(self, sentence, folder, held):
        pointer, listing = self.current(), self.generations()
        for name, (command, argv) in self._commands().items():
            with self.subTest(command=name):
                refusal = self.refused(command)
                self.assertEqual(("plugin_directory_untrusted", sentence), (refusal.code, refusal.detail))
                self.assertEqual((1, f"refused: plugin_directory_untrusted\n{sentence}\n", ""), self.main(argv))
                self.assertEqual(held, self.tree(folder))
                self.assertEqual(pointer, self.current())
                self.assertEqual(listing, self.generations() if listing is not None else None)

    def generations(self):
        return sorted(os.listdir(self.gens())) if os.path.lexists(self.gens()) else None

    def test_a_folder_placed_by_hand_is_not_deleted_and_every_command_prints_the_way_out(self):
        self.roots = self.registries()
        self.install_from(self.roots["a"])
        folder = self.live() / "hand.made"
        folder.mkdir(mode=0o700)
        (folder / "notes.txt").write_bytes(b"mine\n")
        sentence = f"The folder {folder} is not a plugin this command installed. {MOVE_OUT}"
        self._refused_with(sentence, folder, {"notes.txt": b"mine\n"})
        os.rename(folder, self.tmp / "hand.made")
        self.assertEqual(["fixture.unsigned"], self.listed_ids())

    def test_a_changed_copy_of_an_installed_plugin_is_not_replaced_in_silence(self):
        self.roots = self.registries()
        self.install_from(self.roots["a"])
        folder = self.live() / "fixture.unsigned"
        (folder / "plugin.py").write_bytes(b"CHANGED = True\n")
        held = self.tree(folder)
        sentence = f"The folder {folder} is not a plugin this command installed. {MOVE_OUT}"
        self._refused_with(sentence, folder, held)

    def test_a_folder_whose_name_is_not_its_plugin_id_refuses_before_the_first_install(self):
        self.roots = self.registries()
        self.write_live({})
        folder = self.live() / "my.copy"
        folder.mkdir(mode=0o700)
        release = self.roots["a"] / "registry" / "fixture.unsigned" / "1.0.0"
        for name in ("manifest.json", "plugin.py"):
            (folder / name).write_bytes((release / name).read_bytes())
            os.chmod(folder / name, 0o600)
        held = self.tree(folder)
        self._refused_with(_rename_steps(folder, "fixture.unsigned"), folder, held)
        self.assertFalse(os.path.lexists(self.gens()))
        # The printed steps end the refusal.
        os.rename(folder, self.live() / "fixture.unsigned")
        self.assertEqual(["fixture.unsigned"], self.listed_ids())

    def test_a_folder_whose_name_is_not_its_plugin_id_refuses_after_an_install(self):
        self.roots = self.registries()
        self.install_from(self.roots["a"])
        folder = self.live() / "my.copy"
        os.rename(self.live() / "fixture.unsigned", folder)
        held = self.tree(folder)
        self._refused_with(_rename_steps(folder, "fixture.unsigned"), folder, held)
        os.rename(folder, self.live() / "fixture.unsigned")
        self.assertEqual(["fixture.unsigned"], self.listed_ids())


class FilesystemRefusedTests(LiveCase):
    """(P6 b) A read or write the system refuses ends as one typed line, never a traceback."""

    def _with(self, name, fail, action):
        real = getattr(os, name)

        def call(*args, **kwargs):
            if fail(*args):
                raise OSError(fail.errno, os.strerror(fail.errno))
            return real(*args, **kwargs)

        setattr(os, name, call)
        try:
            return action()
        finally:
            setattr(os, name, real)

    def _under_config(self, number):
        config = os.fspath(self.config)

        def fail(*args):
            return any(isinstance(arg, (str, os.PathLike)) and os.fspath(arg).startswith(config) for arg in args[:2])

        fail.errno = number
        return fail

    def _expect(self, number):
        return (1, f"refused: filesystem_refused\nThe system refused a file read or write: [Errno {number}] "
                   f"{os.strerror(number)}\n", "")

    def test_a_full_disk_during_install_is_refused_typed_and_the_next_install_works(self):
        self.roots = self.registries()

        def full(*_args):
            return True

        full.errno = errno.ENOSPC
        seen = self._with("write", full, lambda: self.main(self.argv("install", self.roots["a"])))
        self.assertEqual(self._expect(errno.ENOSPC), seen)
        self.assertEqual(0, self.main(self.argv("install", self.roots["a"]))[0])
        self.assertEqual(["fixture.unsigned"], self.listed_ids())

    def test_a_read_only_disk_is_refused_typed_for_install_list_and_remove(self):
        self.roots = self.registries()
        for command in ("install", "list", "remove"):
            with self.subTest(command=command):
                seen = self._with("mkdir", self._under_config(errno.EROFS),
                                  lambda: self.main(self.argv(command, self.roots["a"])))
                self.assertEqual(self._expect(errno.EROFS), seen)

    def test_no_permission_is_refused_typed_for_list_and_remove(self):
        self.roots = self.registries()
        self.install_from(self.roots["a"])
        for command, name in (("list", "listdir"), ("remove", "rename")):
            with self.subTest(command=command):
                seen = self._with(name, self._under_config(errno.EACCES), lambda: self.main(self.argv(command)))
                self.assertEqual(self._expect(errno.EACCES), seen)
        self.assertEqual(["fixture.unsigned"], self.listed_ids())

    def test_a_config_folder_under_a_regular_file_is_refused_typed(self):
        self.roots = self.registries()
        parent = self.tmp / "a-file"
        parent.write_bytes(b"not a folder\n")
        self.config = parent / "recursion"
        for command in ("install", "list", "remove"):
            with self.subTest(command=command):
                status, err, out = self.main(self.argv(command, self.roots["a"]))
                self.assertEqual((1, "", True), (status, out, err.startswith(
                    "refused: filesystem_refused\nThe system refused a file read or write: ")))
                self.assertNotIn("Traceback", err)
                self.assertEqual(b"not a folder\n", parent.read_bytes())

    def test_a_config_path_that_is_a_regular_file_is_refused_with_an_installer_code(self):
        self.roots = self.registries()
        self.config.parent.mkdir(parents=True)
        self.config.write_bytes(b"not a folder\n")
        for command in ("install", "list", "remove"):
            with self.subTest(command=command):
                self.assertEqual((1, "refused: plugin_directory_untrusted\n", ""),
                                 self.main(self.argv(command, self.roots["a"])))


if __name__ == "__main__":
    unittest.main()
