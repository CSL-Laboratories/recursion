"""The refusal of a config folder other users may write names its way out (slice 8a).

When ``$XDG_CONFIG_HOME/recursion`` is there and its group or other users may write to it, every plugin
command refuses ``plugin_directory_untrusted`` and leaves the folder's mode and contents as they are: nothing
narrows it. The command line prints the folder's path and the one command that makes it owner-only
(``chmod 700`` and that path), in the same form as the other refusals that say how to end them. The test
helper ``plugin_fixture.write_install`` makes its folders owner-only whatever the process mask is, so no test
depends on the mask.
"""
from __future__ import annotations

import os
import shlex
import stat
import unittest

from recursion_core.plugin_security.tests.test_plugin_install_live_truth import LiveCase


def _way_out(folder):
    return (f"The folder {folder} can be written by other users, and no plugin is installed or read from such a "
            f"folder. Make it owner-only with: chmod 700 {shlex.quote(str(folder))}; then run the command again.")


class ConfigFolderWayOutTests(LiveCase):
    def test_a_folder_a_desktop_session_made_under_mask_002_refuses_every_command_with_the_way_out(self):
        self.roots = self.registries()
        previous = os.umask(0o002)
        try:
            # As a desktop user's session makes it: the mode comes from the process mask.
            self.config.mkdir(parents=True)
            (self.config / "notes.txt").write_bytes(b"mine\n")
            made = stat.S_IMODE(os.lstat(self.config).st_mode)
            self.assertEqual(0o775, made)
            held = self.tree(self.config)
            for command in ("install", "list", "remove"):
                with self.subTest(command=command):
                    status, said, out = self.main(self.argv(command, self.roots["a"]))
                    self.assertEqual((1, f"refused: plugin_directory_untrusted\n{_way_out(self.config)}\n", ""),
                                     (status, said, out))
                    self.assertIn(str(self.config), said)
                    self.assertIn(f"chmod 700 {shlex.quote(str(self.config))}", said)
                    self.assertEqual(made, stat.S_IMODE(os.lstat(self.config).st_mode))
                    self.assertEqual(held, self.tree(self.config))
                    self.assertEqual(["notes.txt"], os.listdir(self.config))
            self.assertEqual("plugin_directory_untrusted", self.refused(self.listed).code)
            # The way out ends the refusal.
            os.chmod(self.config, 0o700)
            self.assertEqual(0, self.main(self.argv("install", self.roots["a"]))[0])
            self.assertEqual(["fixture.unsigned"], self.listed_ids())
        finally:
            self.assertEqual(0o002, os.umask(previous))

    def test_the_fixture_helper_makes_its_folders_owner_only_under_any_mask(self):
        from recursion_core.daemon.tests import plugin_fixture as fx

        for mask in (0o002, 0o000, 0o022):
            with self.subTest(mask=oct(mask)):
                previous = os.umask(mask)
                try:
                    config = self.tmp / f"mask-{mask:03o}" / "xdg" / "recursion"
                    plugins = fx.write_install(config)
                finally:
                    os.umask(previous)
                for folder in (plugins, config, config.parent, config.parent.parent):
                    self.assertEqual(0o700, stat.S_IMODE(os.lstat(folder).st_mode), folder)


if __name__ == "__main__":
    unittest.main()
