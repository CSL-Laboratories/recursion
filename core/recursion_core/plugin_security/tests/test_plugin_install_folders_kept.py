"""No command deletes a live folder it cannot prove the installer wrote, and every refusal prints the way out.

(R1) A live folder whose name is not a valid plugin id (``My.Copy``, ``x``, ``.hidden``, ``host``) refuses in
install, list and remove as a folder named with another valid id does: ``plugin_directory_untrusted``,
nothing written, and the command line prints the folder and the steps that end the refusal (rename it to
the id its manifest holds, when it holds one, or move it out of the plugins directory).
(R2) After a publish that stopped, the next command finishes it only when every live plugin folder is a
whole copy from the generation it publishes or from the generation it replaces; the publish's record names
both and is synced before the live tree changes. A folder or link placed by hand is left as it is and the
command refuses with the way out. (S1 of the read) The config directory is synced after
``plugin-generations`` is made in it.
"""
from __future__ import annotations

import os
import unittest

from recursion_core.plugin_security.tests import test_plugin_install_live_truth as live_truth
from recursion_core.plugin_security.tests import test_plugin_install_publish as publish
from recursion_core.plugin_security.tests.test_plugin_install_live_truth import MOVE_OUT, LiveCase

INVALID_NAMES = ("My.Copy", "x", ".hidden", "host")


def _move_out(path):
    return f"The folder {path} is not a plugin this command installed. {MOVE_OUT}"


class _Refusals(LiveCase):
    # The three commands and their checks, as slice 3's FolderNotWrittenTests runs them.
    _commands = live_truth.FolderNotWrittenTests._commands
    _refused_with = live_truth.FolderNotWrittenTests._refused_with
    generations = live_truth.FolderNotWrittenTests.generations

    def hand_folder(self, name, release=True):
        folder = self.live() / name
        folder.mkdir(mode=0o700)
        if release:
            source = self.roots["a"] / "registry" / "fixture.unsigned" / "1.0.0"
            for item in ("manifest.json", "plugin.py"):
                (folder / item).write_bytes((source / item).read_bytes())
                os.chmod(folder / item, 0o600)
        else:
            (folder / "notes.txt").write_bytes(b"mine\n")
        return folder


class InvalidFolderNameTests(_Refusals):
    """(R1) A live folder whose name is not a valid plugin id gets the way out in all three commands."""

    def test_an_installed_folder_renamed_to_a_name_that_is_not_an_id_prints_the_rename_steps(self):
        self.roots = self.registries()
        for name in INVALID_NAMES:
            with self.subTest(name=name):
                self.use(f"after-{name}")
                self.install_from(self.roots["a"])
                folder = self.live() / name
                os.rename(self.live() / "fixture.unsigned", folder)
                held = self.tree(folder)
                self._refused_with(live_truth._rename_steps(folder, "fixture.unsigned"), folder, held)
                os.rename(folder, self.live() / "fixture.unsigned")
                self.assertEqual(["fixture.unsigned"], self.listed_ids())

    def test_a_folder_placed_before_the_first_install_under_a_name_that_is_not_an_id_prints_the_rename_steps(self):
        self.roots = self.registries()
        for name in INVALID_NAMES:
            with self.subTest(name=name):
                self.use(f"before-{name}")
                self.write_live({})
                folder = self.hand_folder(name)
                held = self.tree(folder)
                self._refused_with(live_truth._rename_steps(folder, "fixture.unsigned"), folder, held)
                self.assertFalse(os.path.lexists(self.gens()))

    def test_a_folder_with_no_manifest_under_a_name_that_is_not_an_id_prints_the_move_out_steps(self):
        self.roots = self.registries()
        for first in (True, False):
            for name in INVALID_NAMES:
                with self.subTest(first=first, name=name):
                    self.use(f"plain-{int(first)}-{name}")
                    if first:
                        self.write_live({})
                    else:
                        self.install_from(self.roots["a"])
                    folder = self.hand_folder(name, release=False)
                    self._refused_with(_move_out(folder), folder, {"notes.txt": b"mine\n"})
                    os.rename(folder, self.tmp / f"kept-{int(first)}-{name}")
                    self.assertEqual([] if first else ["fixture.unsigned"], self.listed_ids())


class StoppedPublishTests(_Refusals):
    """(R2) The next command after a stopped publish never deletes a folder it cannot prove the installer wrote."""

    def _stopped_after_the_live_tree_is_whole(self):
        # The reader's B3: the install of fixture.beta stops just before .publishing is cleared.
        from recursion_core.plugin_security import install as install_mod

        if not hasattr(self, "roots"):
            self.roots = self.registries()
        self.install_from(self.roots["a"])
        real = install_mod._clear_marker

        def stop(*_args, **_kwargs):
            raise OSError("stopped")

        install_mod._clear_marker = stop
        try:
            with self.assertRaises(OSError):
                self.install_from(self.roots["beta"], "fixture.beta")
        finally:
            install_mod._clear_marker = real
        self.assertEqual("2\n", self.current())
        self.assertTrue(os.path.lexists(self.gens() / ".publishing"))

    def _ends_after_moving_out(self, path):
        os.rename(path, self.tmp / "moved-out")
        self.assertEqual(["fixture.beta", "fixture.unsigned"], self.listed_ids())
        self.assertFalse(os.path.lexists(self.gens() / ".publishing"))
        self.assertEqual(self.tree(self.gens() / "2"), self.tree(self.live()))

    def test_a_folder_placed_after_a_stopped_publish_is_kept_and_every_command_prints_the_way_out(self):
        self._stopped_after_the_live_tree_is_whole()
        folder = self.hand_folder("hand.made", release=False)
        self._refused_with(_move_out(folder), folder, {"notes.txt": b"mine\n"})
        self.assertTrue(os.path.lexists(self.gens() / ".publishing"))
        self._ends_after_moving_out(folder)

    def test_a_folder_under_a_published_name_that_is_not_the_written_copy_is_kept(self):
        # Changed bytes of the same size, one file more or one file less: none is the copy the publish wrote.
        changes = {"changed": ("plugin.py", b"VALUE = 9\n"), "added": ("notes.txt", b"mine\n"),
                   "taken": ("plugin.py", None)}
        for kind, (name, data) in changes.items():
            with self.subTest(kind=kind):
                self.use(f"published-{kind}")
                self._stopped_after_the_live_tree_is_whole()
                folder = self.live() / "fixture.beta"
                if data is None:
                    os.unlink(folder / name)
                else:
                    (folder / name).write_bytes(data)
                held = self.tree(folder)
                self._refused_with(_move_out(folder), folder, held)
                os.rename(folder, self.tmp / f"mine-{kind}")
                self.assertEqual(["fixture.beta", "fixture.unsigned"], self.listed_ids())
                self.assertEqual(held, self.tree(self.tmp / f"mine-{kind}"))

    def test_a_link_placed_after_a_stopped_publish_is_kept(self):
        self._stopped_after_the_live_tree_is_whole()
        target = self.tmp / "elsewhere"
        target.mkdir()
        (target / "notes.txt").write_bytes(b"mine\n")
        link = self.live() / "linked"
        link.symlink_to(target, target_is_directory=True)
        self._refused_with(_move_out(link), target, {"notes.txt": b"mine\n"})
        self.assertTrue(link.is_symlink())
        self._ends_after_moving_out(link)

    def test_a_stop_at_each_step_of_a_remove_is_finished_only_after_a_hand_folder_is_moved_out(self):
        roots = self.registries()
        for at in range(400):
            self.use(f"remove-{at}")
            self.install_from(roots["a"])
            self.install_from(roots["beta"], "fixture.beta")
            came = self.stopped(at, self.live(), lambda: self.remove("fixture.beta"))
            with self.subTest(at=at):
                folder = self.live() / "hand.made"
                folder.mkdir(mode=0o700)
                (folder / "notes.txt").write_bytes(b"mine\n")
                for command in (self.listed, lambda: self.remove("fixture.unsigned")):
                    refusal = self.refused(command)
                    self.assertEqual(("plugin_directory_untrusted", _move_out(folder)),
                                     (refusal.code, refusal.detail))
                    self.assertEqual({"notes.txt": b"mine\n"}, self.tree(folder))
                os.rename(folder, self.tmp / f"moved-{at}")
                # The old folder fixture.beta, if the stop left it, is generation 2's copy: it goes, unasked.
                self.assertEqual(["fixture.unsigned"], self.listed_ids())
                self.assertEqual(self.tree(self.gens() / "3"), self.tree(self.live()))
            if not came:
                break
        else:
            self.fail("the remove did not finish within 400 steps")


class SyncTests(LiveCase):
    """(R2) The publish's record is synced before the live tree changes; the config directory is synced
    after plugin-generations is made in it."""

    _record = publish.PublishOrderTests._record

    def test_the_record_is_synced_before_the_first_change_of_the_live_tree(self):
        self.roots = self.registries()
        self.install_from(self.roots["a"])
        events = self._record(lambda: self.install_from(self.roots["beta"], "fixture.beta"))
        generations = os.path.realpath(self.gens())
        live = os.path.realpath(self.live())
        put = next(index for index, event in enumerate(events)
                   if event[0] == "rename" and event[2] == os.path.join(generations, ".publishing"))
        first_live = next(index for index, event in enumerate(events)
                          if event[0] == "rename" and os.path.dirname(event[2]) == live)
        self.assertLess(put, first_live)
        self.assertIn(("fsync", generations), events[put + 1:first_live])

    def test_the_config_directory_is_synced_after_plugin_generations_is_made(self):
        self.roots = self.registries()
        # A first install into an existing plugins/: no other step syncs the config directory.
        self.write_live({})
        events = self._record(lambda: self.install_from(self.roots["a"]))
        config = os.path.realpath(self.config)
        generations = os.path.realpath(self.gens())
        first = next(index for index, event in enumerate(events)
                     if event[0] == "rename" and os.path.dirname(event[2]) == generations)
        self.assertIn(("fsync", config), events[:first])


if __name__ == "__main__":
    unittest.main()
