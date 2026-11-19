"""A read or write the system refuses ends as the typed refusal ``filesystem_refused`` (slice 8b).

``install_plugin``, ``list_plugins`` and ``remove_plugin`` raise ``InstallRefused`` with the code
``filesystem_refused`` when the system refuses the disk (full, read-only, no permission). The refusal keeps the
system's own message in its text and its ``errno``, and the system's error as its cause; no caller gets a bare
``OSError``. The command line prints what it printed before (``refused: filesystem_refused`` and the system's
message, no traceback), now by catching the typed refusal.
"""
from __future__ import annotations

import errno
import os
import unittest

from recursion_core.plugin_security.tests import test_plugin_install_live_truth as live_truth
from recursion_core.plugin_security.tests.test_plugin_install_live_truth import LiveCase


class TypedFilesystemRefusalTests(LiveCase):
    _with = live_truth.FilesystemRefusedTests._with
    _under_config = live_truth.FilesystemRefusedTests._under_config

    def _refusal(self, name, fail, action):
        from recursion_core.plugin_security.install import InstallRefused

        def run():
            with self.assertRaises(InstallRefused) as caught:
                action()
            return caught.exception

        return self._with(name, fail, run)

    def _typed(self, number, refusal):
        from recursion_core.plugin_security.install import InstallRefused

        system = OSError(number, os.strerror(number))
        self.assertEqual(("filesystem_refused", ["filesystem_refused"], True),
                         (refusal.code, refusal.codes, refusal.show_detail))
        self.assertEqual(f"The system refused a file read or write: {system}", refusal.detail)
        self.assertEqual(number, refusal.errno)
        self.assertIsInstance(refusal.__cause__, OSError)
        self.assertNotIsInstance(refusal.__cause__, InstallRefused)

    def test_install_plugin_raises_the_typed_refusal_on_a_full_disk(self):
        self.roots = self.registries()

        def full(*_args):
            return True

        full.errno = errno.ENOSPC
        self._typed(errno.ENOSPC, self._refusal("write", full, lambda: self.install_from(self.roots["a"])))
        self.install_from(self.roots["a"])
        self.assertEqual(["fixture.unsigned"], self.listed_ids())

    def test_install_plugin_raises_the_typed_refusal_on_a_read_only_disk(self):
        self.roots = self.registries()
        refusal = self._refusal("mkdir", self._under_config(errno.EROFS), lambda: self.install_from(self.roots["a"]))
        self._typed(errno.EROFS, refusal)

    def test_list_plugins_raises_the_typed_refusal_without_permission(self):
        self.roots = self.registries()
        self.install_from(self.roots["a"])
        self._typed(errno.EACCES, self._refusal("listdir", self._under_config(errno.EACCES), self.listed))
        self.assertEqual(["fixture.unsigned"], self.listed_ids())

    def test_remove_plugin_raises_the_typed_refusal_without_permission(self):
        self.roots = self.registries()
        self.install_from(self.roots["a"])
        refusal = self._refusal("rename", self._under_config(errno.EACCES), lambda: self.remove("fixture.unsigned"))
        self._typed(errno.EACCES, refusal)
        self.remove("fixture.unsigned")
        self.assertEqual([], self.listed_ids())

    def test_remove_plugin_raises_the_typed_refusal_on_a_read_only_disk(self):
        self.roots = self.registries()
        refusal = self._refusal("mkdir", self._under_config(errno.EROFS), lambda: self.remove("fixture.unsigned"))
        self._typed(errno.EROFS, refusal)

    def test_the_command_line_prints_the_typed_refusal_as_before(self):
        self.roots = self.registries()
        self.install_from(self.roots["a"])
        seen = self._with("listdir", self._under_config(errno.EACCES), lambda: self.main(self.argv("list")))
        self.assertEqual((1, f"refused: filesystem_refused\nThe system refused a file read or write: "
                             f"[Errno {errno.EACCES}] {os.strerror(errno.EACCES)}\n", ""), seen)
        self.assertNotIn("Traceback", seen[1])


if __name__ == "__main__":
    unittest.main()
