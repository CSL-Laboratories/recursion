"""The install step refuses to make its stores on 9p, overlay or a network disk.

Every case reads a mount table written inside its own temporary folder (the installer's reader is pointed at
it); the machine's own table is never read, and no real mount is made.
"""

from __future__ import annotations

import importlib
import os
import stat
import tempfile
import unittest
from unittest import mock

from recursion_core.daemon_install import InstallRefused, Roots, install, upgrade
from recursion_core.daemon_install.tests.test_daemon_install import EXEC, POSIX_PATHS

LINUX = os.name == "posix" and POSIX_PATHS
INSTALL = importlib.import_module("recursion_core.daemon_install.install")
CLAUSE = "or run it with XDG_STATE_HOME set to an absolute folder on a local disk"


def _message(folder, kind):
    return (f"Recursion does not create its stores in {folder}: it is on {kind}, where SQLite's write-ahead log "
            f"can damage a database. Put a local disk or volume at {folder} (in a container: mount a volume "
            f"there) and run recursiond install again, or run it with XDG_STATE_HOME set to an absolute folder "
            f"on a local disk. Check a folder with: findmnt -no FSTYPE -T \"{folder}\"")


def _escape(path):
    return path.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011").replace("\n", "\\012")


def _tree(root):
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            info = os.lstat(path)
            found[path] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_ino, info.st_size)
    return found


@unittest.skipUnless(LINUX, "the installer is Linux-only")
class StoreFilesystemTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="store-ground-")
        self.addCleanup(tmp.cleanup)
        self.case = os.path.realpath(tmp.name)
        os.chmod(self.case, 0o700)
        self.home = os.path.join(self.case, "home")
        os.mkdir(self.home, 0o700)
        self.table_dir = os.path.join(self.case, "table")
        os.mkdir(self.table_dir, 0o700)
        self.table = os.path.join(self.table_dir, "mountinfo")
        self.store_dir = os.path.join(self.home, ".local", "state", "recursion", "plugin-security")
        self.unit = os.path.join(self.home, ".config", "systemd", "user", "recursion-daemon.service")
        self.mounts([("/", "ext4")])
        patcher = mock.patch.object(INSTALL, "_MOUNTINFO", self.table, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def mounts(self, rows):
        lines = [f"{20 + n} 1 0:{20 + n} / {_escape(point)} rw,relatime shared:{n} - {kind} src{n} rw"
                 for n, (point, kind) in enumerate(rows)]
        with open(self.table, "w", encoding="utf-8") as out:
            out.write("\n".join(lines) + "\n")

    def home_tree(self):
        return _tree(self.home)

    def refused(self, call, *args, **kwargs):
        with self.assertRaises(InstallRefused) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.kind, "store_filesystem_unsupported", caught.exception.detail)
        return caught.exception.detail

    def test_install_refuses_9p_overlay_and_nfs4_with_the_exact_message_and_makes_nothing(self):
        for kind in ("9p", "overlay", "nfs4"):
            with self.subTest(kind=kind):
                self.mounts([("/", "ext4"), (self.home, kind)])
                before = self.home_tree()
                detail = self.refused(install, Roots(home=self.home), EXEC)
                self.assertEqual(detail, _message(self.home, kind))
                self.assertEqual(self.home_tree(), before)  # no folder, store, custody root or unit
                self.assertFalse(os.path.lexists(self.unit))

    def test_the_message_names_xdg_state_home_as_the_second_way(self):
        self.mounts([("/", "ext4"), (self.home, "9p")])
        detail = self.refused(install, Roots(home=self.home), EXEC)
        self.assertIn(CLAUSE, detail)
        self.assertIn("XDG_STATE_HOME", detail)

    def test_a_folder_chosen_with_xdg_state_home_on_a_refused_ground_is_refused_the_same_way(self):
        share = os.path.join(self.case, "share")
        os.mkdir(share, 0o700)
        self.mounts([("/", "ext4"), (share, "9p")])  # the home is on ext4; the chosen place is not
        before = _tree(self.case)
        detail = self.refused(install, Roots(home=self.home, state_home=os.path.join(share, "state")), EXEC)
        self.assertEqual(detail, _message(share, "9p"))
        self.assertEqual(_tree(self.case), before)

    def test_the_longest_mount_point_decides(self):
        self.mounts([("/", "overlay"), (self.case, "ext4")])  # an ext4 mount under an overlay root
        plan = install(Roots(home=self.home), EXEC)
        self.assertTrue(os.path.isfile(os.path.join(self.store_dir, "breaker.sqlite3")))
        self.assertEqual(plan.filesystem, ())

    def test_the_folder_in_the_message_is_there_so_findmnt_runs_as_printed(self):
        self.mounts([("/", "ext4"), (self.home, "overlay")])
        detail = self.refused(install, Roots(home=self.home), EXEC)
        folder = detail.rsplit('-T "', 1)[1].rstrip('"')
        self.assertTrue(os.path.isabs(folder))
        self.assertTrue(os.path.isdir(folder), folder)

    def test_an_unreadable_table_refuses_nothing_and_the_plan_says_the_type_was_not_read(self):
        os.remove(self.table)
        plan = install(Roots(home=self.home), EXEC)
        self.assertTrue(os.path.isfile(os.path.join(self.store_dir, "revoked.sqlite3")))
        self.assertEqual(plan.filesystem, (f"the file-system type of {self.home} was not read",))

    def test_upgrade_refuses_the_same_way_and_changes_nothing(self):
        install(Roots(home=self.home), EXEC)
        self.mounts([("/", "ext4"), (self.home, "nfs")])
        before = self.home_tree()
        detail = self.refused(upgrade, Roots(home=self.home), EXEC)
        self.assertEqual(detail, _message(self.store_dir, "nfs"))
        self.assertEqual(self.home_tree(), before)

    def test_the_decision_takes_accept_risk_as_an_argument_and_refuses_by_default(self):
        check = getattr(INSTALL, "check_store_ground")
        self.mounts([("/", "ext4"), (self.home, "cifs")])
        with self.assertRaises(InstallRefused) as caught:
            check((self.home,))
        self.assertEqual(caught.exception.kind, "store_filesystem_unsupported")
        lines = check((self.home,), "accept_risk")
        self.assertEqual(len(lines), 1)
        self.assertIn(self.home, lines[0])
        self.assertIn("cifs", lines[0])
        self.assertIn("the risk was accepted", lines[0])
        with self.assertRaises(InstallRefused) as caught:
            check((self.home,), "yes")
        self.assertEqual(caught.exception.kind, "filesystem_unsafe")

    def test_the_refused_kinds_are_the_doctor_rows_kinds_in_order(self):
        self.assertEqual(getattr(INSTALL, "REFUSED_FILESYSTEMS"),
                         ("9p", "v9fs", "drvfs", "overlay", "nfs", "nfs4", "cifs", "smb3"))


if __name__ == "__main__":
    unittest.main()
