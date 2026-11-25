"""``plugin remove <id>`` completes for an installed plugin whose folder no longer matches what was installed.

(G1) An own file changed, missing or replaced, or entries the installer cannot prove: the whole folder is
moved aside into a kept place under ``plugin-generations`` in one rename, nothing is deleted, the command
names the plugin and the place in one line, the plugin is gone from the live tree and from install.json,
and ``list`` names the kept place afterwards. ``install`` and ``update`` keep refusing as before.
(G2) A plugin folder replaced by a link: the link itself is moved aside, never followed; what it points to
is left as it is.
(G3) A folder that cannot be moved aside (it does not allow writing): remove refuses with the way out, and
nothing is moved; following the way out, remove completes. A read-only folder inside it does not stop the
move, since the whole folder moves in one rename.
(G4) While a stopped publish is being finished, and for a changed folder of another plugin, remove refuses
exactly as before.
"""
from __future__ import annotations

import errno
import json
import os
import unittest

from recursion_core.plugin_security.tests.test_plugin_install_kept_named import ROOT, _KeptCase, _listed_kept
from recursion_core.plugin_security.tests.test_plugin_install_only_proven import _move_out, _whole_kept, entries


def _whole_unmovable(folder, plugin_id):
    return (f"The folder {folder} of the plugin {plugin_id} is not the copy this command installed and cannot be "
            f"moved aside ({os.strerror(errno.EACCES)}). Move it out of {folder.parent} yourself (a folder must "
            f"allow writing to be moved); then run the command again.")


class _RemoveCase(_KeptCase):
    def _changed(self, plugin_id="fixture.unsigned"):
        folder = self.live() / plugin_id
        (folder / "plugin.py").write_bytes(b"CHANGED = True\n")
        return folder

    def _install_and_update_refuse(self, folder, code="plugin_directory_untrusted", sentence=None):
        held, pointer = entries(folder), self.current()
        for command in ("install", "update"):
            status, said, out = self.main(self._argvs()[command])
            self.assertEqual((1, ""), (status, out), command)
            self.assertTrue(said.startswith(f"refused: {code}\n"), (command, said))
            if sentence is not None:
                self.assertEqual(f"refused: {code}\n{sentence}\n", said, command)
            self.assertEqual(held, entries(folder), command)
            self.assertEqual([], self._kept_places(), command)
            self.assertEqual(pointer, self.current(), command)

    def _removed_whole(self, folder, kind="folder", plugin_id="fixture.unsigned"):
        held = entries(folder)
        status, said, out = self.main(self.argv("remove", plugin_id=plugin_id))
        kept = self._kept_places()[0] / plugin_id
        self.assertEqual((0, f"{_whole_kept(folder, plugin_id, kept, kind)}\n"), (status, said))
        self.assertEqual([_whole_kept(folder, plugin_id, kept, kind)], json.loads(out)["kept"])
        self.assertEqual(held, entries(kept))
        self.assertFalse(os.path.lexists(folder))
        self.assertNotIn(plugin_id, [row["plugin_id"] for row in json.loads(out)["plugins"]])
        live = json.loads((self.live() / "install.json").read_text(encoding="utf-8"))
        self.assertNotIn(plugin_id, [grant["plugin_id"] for grant in live.get("grants", [])])
        status, said, out = self.main(self.argv("list"))
        self.assertEqual((0, f"{_listed_kept(plugin_id, kept)}\n"), (status, said))
        self.assertNotIn(plugin_id, [row["plugin_id"] for row in json.loads(out)["plugins"]])
        return kept


class RemoveCompletesTests(_RemoveCase):
    """(G1, G2) The whole folder is kept aside and remove completes; install and update still refuse."""

    def test_remove_of_a_plugin_with_an_own_file_changed_completes(self):
        self.install_from(self.roots["a"])
        folder = self._changed()
        self._install_and_update_refuse(folder, sentence=_move_out(folder))
        kept = self._removed_whole(folder)
        self.assertEqual(("file", b"CHANGED = True\n"), entries(kept)["./plugin.py"])
        self.assertEqual("2\n", self.current())
        self.assertEqual([], self.listed_ids())

    def test_remove_of_a_plugin_with_an_own_file_missing_completes(self):
        self.install_from(self.roots["a"])
        folder = self.live() / "fixture.unsigned"
        os.unlink(folder / "plugin.py")
        self._install_and_update_refuse(folder, code="plugin_file_untrusted")
        kept = self._removed_whole(folder)
        self.assertNotIn("./plugin.py", entries(kept))
        self.assertIn("./manifest.json", entries(kept))

    def test_remove_with_a_hand_placed_entry_beside_a_missing_own_file_keeps_both(self):
        self.install_from(self.roots["a"])
        folder = self._with_extras({"settings.json": b"{}\n"})
        os.unlink(folder / "plugin.py")
        self._install_and_update_refuse(folder, code="plugin_file_untrusted")
        kept = self._removed_whole(folder)
        self.assertEqual(("file", b"{}\n"), entries(kept)["./settings.json"])

    def test_remove_of_a_plugin_whose_folder_was_replaced_by_a_link_moves_the_link_itself(self):
        self.install_from(self.roots["a"])
        folder = self.live() / "fixture.unsigned"
        target = self.link_in_place(folder)
        (target / "plugin.py").write_bytes(b"CHANGED = True\n")
        elsewhere = entries(target)
        self._install_and_update_refuse(folder, sentence=_move_out(folder))
        kept = self._removed_whole(folder, kind="link")
        self.assertEqual(("link", str(target)), entries(kept)["."])
        # Never followed: what the link points to is left as it was.
        self.assertEqual(elsewhere, entries(target))

    def test_remove_of_a_plugin_whose_folder_was_replaced_by_a_file_keeps_the_file(self):
        self.install_from(self.roots["a"])
        folder = self.live() / "fixture.unsigned"
        os.rename(folder, self.tmp / "folder-away")
        folder.write_bytes(b"mine\n")
        os.chmod(folder, 0o600)
        kept = self._removed_whole(folder, kind="file")
        self.assertEqual(("file", b"mine\n"), entries(kept)["."])

    def test_remove_of_an_unchanged_plugin_still_keeps_nothing(self):
        self.install_from(self.roots["a"])
        status, said, _out = self.main(self.argv("remove"))
        self.assertEqual((0, ""), (status, said))
        self.assertEqual([], self._kept_places())

    def test_a_library_caller_gets_the_kept_line(self):
        self.install_from(self.roots["a"])
        folder = self._changed()
        result = self.remove("fixture.unsigned")
        kept = self._kept_places()[0] / "fixture.unsigned"
        self.assertEqual([_whole_kept(folder, "fixture.unsigned", kept)], result["kept"])
        self.assertEqual([], [row["plugin_id"] for row in result["plugins"]])


@unittest.skipIf(ROOT, "root moves a folder it cannot write to")
class UnmovableFolderTests(_RemoveCase):
    """(G3) A folder that does not allow writing refuses with the way out; nothing is half moved."""

    def test_a_changed_folder_that_cannot_be_moved_refuses_with_the_way_out(self):
        self.install_from(self.roots["a"])
        folder = self._changed()
        os.chmod(folder, 0o500)
        self.addCleanup(lambda: os.path.isdir(folder) and os.chmod(folder, 0o700))
        held, pointer, listing = entries(self.live()), self.current(), self.generations()
        self.assertEqual((1, f"refused: plugin_directory_untrusted\n{_whole_unmovable(folder, 'fixture.unsigned')}\n",
                          ""), self.main(self.argv("remove")))
        self.assertEqual(held, entries(self.live()))
        self.assertEqual([], self._kept_places())
        self.assertEqual((pointer, listing), (self.current(), self.generations()))
        # The way out: the folder allows writing again; remove completes.
        os.chmod(folder, 0o700)
        self._removed_whole(folder)

    def test_a_read_only_folder_inside_a_changed_plugin_folder_does_not_stop_the_move(self):
        self.install_from(self.roots["a"])
        folder = self._changed()
        locked = folder / "ro"
        locked.mkdir(mode=0o700)
        (locked / "inside.txt").write_bytes(b"mine\n")
        os.chmod(locked, 0o500)
        kept_locked = self.gens() / ".kept-2" / "fixture.unsigned" / "ro"
        for path in (locked, kept_locked):
            self.addCleanup(lambda path=path: os.path.isdir(path) and os.chmod(path, 0o700))
        kept = self._removed_whole(folder)
        self.assertEqual(kept_locked, kept / "ro")
        self.assertEqual(("file", b"mine\n"), entries(kept)["./ro/inside.txt"])


class RemoveStillRefusesTests(_RemoveCase):
    """(G4) A stopped publish, and a changed folder of another plugin, refuse exactly as before."""

    def test_while_a_stopped_publish_is_finished_remove_of_a_changed_plugin_refuses(self):
        self.stopped_with_live_whole()
        folder = self._changed()
        held, record = entries(folder), self.gens() / ".publishing"
        self.assertEqual((1, f"refused: plugin_directory_untrusted\n{_move_out(folder)}\n", ""),
                         self.main(self.argv("remove")))
        self.assertEqual(held, entries(folder))
        self.assertTrue(os.path.lexists(record))
        self.assertEqual([], self._kept_places())

    def test_a_changed_folder_of_another_plugin_still_refuses_the_remove(self):
        self.install_from(self.roots["a"])
        self.install_from(self.roots["beta"], "fixture.beta")
        beta = self._changed("fixture.beta")
        held = entries(self.live())
        self.assertEqual((1, f"refused: plugin_directory_untrusted\n{_move_out(beta)}\n", ""),
                         self.main(self.argv("remove")))
        self.assertEqual(held, entries(self.live()))
        self.assertEqual([], self._kept_places())
        self.assertEqual("2\n", self.current())


if __name__ == "__main__":
    unittest.main()
