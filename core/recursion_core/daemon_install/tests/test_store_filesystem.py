"""The install step refuses to make its stores on 9p, overlay or a network disk.

Every case reads a mount table written inside its own temporary folder (the installer's reader is pointed at
it); the machine's own table is never read, and no real mount is made.
"""

from __future__ import annotations

import importlib
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import recursion_core
from recursion_core.daemon_install import InstallRefused, Roots, install, owned_paths, upgrade
from recursion_core.daemon_install.tests import mount_stub
from recursion_core.daemon_install.tests.test_daemon_install import EXEC, POSIX_PATHS
from recursion_core.daemon_install.tests.mount_stub import setUpModule, tearDownModule  # noqa: F401
from recursion_core.plugin_security import stores
from recursion_core.plugin_security.signing import backend_available

LINUX = os.name == "posix" and POSIX_PATHS
INSTALL = importlib.import_module("recursion_core.daemon_install.install")
CLAUSE = "or run it with XDG_STATE_HOME set to an absolute folder on a local disk"
COMMAND = "Check a folder with: "
# The test modules that run install, upgrade or the setup command in this process: each takes setUpModule and
# tearDownModule from mount_stub, so none reads the machine's own mount table.
PINNED = (
    "recursion_core.daemon.tests.test_installed_profile_stores",
    "recursion_core.daemon_install.tests.test_anchor_key",
    "recursion_core.daemon_install.tests.test_chosen_state_home",
    "recursion_core.daemon_install.tests.test_daemon_install",
    "recursion_core.daemon_install.tests.test_ledger_step",
    "recursion_core.daemon_install.tests.test_store_filesystem",
    "recursion_core.daemon_install.tests.test_unit_surrogate",
    "recursion_core.setup.tests.test_setup_commands",
)
# The one that runs the setup command in a child process: it points the child's reader at a stand-in table.
CHILD_PINNED = ("recursion_core.setup.tests.test_packaging",)
RUNS_INSTALLER = re.compile(r"\b(?:install|upgrade|install_function)\(|\bsetup\.run\(")


def _message(folder, kind):
    return (f"Recursion does not create its stores in {folder}: it is on {kind}, where SQLite's write-ahead log "
            f"can damage a database. Put a local disk or volume at {folder} (in a container: mount a volume "
            f"there) and run recursiond install again, or run it with XDG_STATE_HOME set to an absolute folder "
            f"on a local disk. Check a folder with: findmnt -no FSTYPE -T {shlex.quote(folder)}")


def _escape(path):
    return path.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011").replace("\n", "\\012")


def _files(ledger):
    found = {}
    for suffix in ("", "-wal", "-shm"):
        try:
            with open(ledger + suffix, "rb") as handle:
                found[suffix] = handle.read()
        except FileNotFoundError:
            found[suffix] = None
    return found


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
        folder = shlex.split(detail.split(COMMAND, 1)[1])[-1]
        self.assertTrue(os.path.isabs(folder))
        self.assertTrue(os.path.isdir(folder), folder)

    def test_the_findmnt_command_gives_a_shell_exactly_the_folder(self):
        # Names a shell reads otherwise between double quotes, and one that plain single quotes would break.
        for name in ('my "state"', "my state", "st$(echo X)ate", "st`echo Y`ate", "st\\\\ate", "it's"):
            with self.subTest(name=name):
                home = os.path.join(self.case, name)
                os.mkdir(home, 0o700)
                self.mounts([("/", "ext4"), (home, "9p")])
                detail = self.refused(install, Roots(home=home), EXEC)
                self.assertEqual(shlex.split(detail.split(COMMAND, 1)[1]), ["findmnt", "-no", "FSTYPE", "-T", home])
                self.assertTrue(detail.startswith(f"Recursion does not create its stores in {home}: it is on 9p, "))
                self.assertEqual(detail, _message(home, "9p"))

    @unittest.skipUnless(shutil.which("sh"), "no POSIX shell on this machine")
    def test_the_command_pasted_into_a_shell_runs_nothing_from_the_folder_name(self):
        shell = shutil.which("sh")
        marker = os.path.join(self.case, "ran")
        for name in ("st$(touch ran)ate", "st`touch ran`ate"):  # two runs of one shell each, bounded in time
            with self.subTest(name=name):
                home = os.path.join(self.case, name)
                os.mkdir(home, 0o700)
                self.mounts([("/", "ext4"), (home, "9p")])
                line = self.refused(install, Roots(home=home), EXEC).split(COMMAND, 1)[1]
                start = "findmnt -no FSTYPE -T "
                self.assertTrue(line.startswith(start), line)
                printed = "printf '%s\\n' " + line[len(start):]  # printf in place of findmnt: the argument as read
                done = subprocess.run([shell, "-c", printed], cwd=self.case, capture_output=True, text=True,
                                      timeout=60, env={"PATH": os.environ.get("PATH", os.defpath)})
                self.assertEqual((done.returncode, done.stdout), (0, home + "\n"), done.stderr)
                self.assertFalse(os.path.lexists(marker))

    def test_of_two_mounts_on_the_same_folder_the_later_line_decides(self):
        self.mounts([("/", "ext4"), (self.home, "ext4"), (self.home, "9p")])  # 9p mounted over the ext4 one
        self.assertEqual(INSTALL.filesystem_type(self.home), "9p")
        self.assertEqual(self.refused(install, Roots(home=self.home), EXEC), _message(self.home, "9p"))
        self.mounts([("/", "ext4"), (self.home, "9p"), (self.home, "ext4")])  # ext4 mounted over the 9p one
        self.assertEqual(INSTALL.filesystem_type(self.home), "ext4")
        self.assertEqual(install(Roots(home=self.home), EXEC).filesystem, ())

    def test_a_mount_point_holding_a_space_is_read_from_the_tables_escaped_form(self):
        home = os.path.join(self.case, "my home")
        os.mkdir(home, 0o700)
        self.mounts([("/", "ext4"), (home, "9p")])
        with open(self.table, encoding="utf-8") as table:
            self.assertIn("my\\040home", table.read())  # the kernel writes a space in a mount point as \040
        self.assertEqual(INSTALL.filesystem_type(home), "9p")
        self.assertEqual(self.refused(install, Roots(home=home), EXEC), _message(home, "9p"))

    def test_a_mount_whose_name_only_begins_like_the_folder_does_not_hold_it(self):
        self.mounts([("/", "ext4"), (self.home[:-1], "9p")])  # .../hom is not a folder above .../home
        self.assertEqual(INSTALL.filesystem_type(self.home), "ext4")
        plan = install(Roots(home=self.home), EXEC)
        self.assertTrue(os.path.isfile(os.path.join(self.store_dir, "breaker.sqlite3")))
        self.assertEqual(plan.filesystem, ())

    @unittest.skipUnless(backend_available(), "the authority key needs the cryptography package")
    def test_a_refused_disk_opens_no_store_or_ledger_before_it_refuses(self):
        install(Roots(home=self.home), EXEC, make_anchor=True)  # a key and the ledger bound to it, on ext4
        ledger = owned_paths(Roots(home=self.home)).ledger
        self.assertTrue(os.path.isfile(ledger))
        self.mounts([("/", "ext4"), (self.home, "nfs4")])
        for call in (install, upgrade):
            with self.subTest(call=call.__name__):
                before = (self.home_tree(), _files(ledger))
                with mock.patch.object(stores, "open_store", wraps=stores.open_store) as opened, \
                        mock.patch("sqlite3.connect", wraps=sqlite3.connect) as connected:
                    self.refused(call, Roots(home=self.home), EXEC, make_anchor=True)
                self.assertEqual(opened.call_args_list, [])
                self.assertEqual(connected.call_args_list, [])
                self.assertEqual((self.home_tree(), _files(ledger)), before)  # the ledger, its -wal and -shm

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


class MountTableStandInTests(unittest.TestCase):
    """Every test module that runs the installer reads the stand-in table, also where / is overlay."""

    def test_each_module_that_runs_the_installer_puts_the_stand_in_over_an_overlay_root(self):
        with tempfile.TemporaryDirectory(prefix="overlay-table-") as folder:
            overlay = mount_stub.write_table(folder, "20 1 0:20 / / rw,relatime shared:1 - overlay overlay rw\n")
            with mock.patch.object(INSTALL, "_MOUNTINFO", overlay):
                self.assertEqual(INSTALL.filesystem_type(folder), "overlay")  # a container's root, as read
                for name in PINNED:
                    with self.subTest(module=name):
                        module = importlib.import_module(name)
                        self.assertIs(getattr(module, "setUpModule", None), mount_stub.setUpModule)
                        self.assertIs(getattr(module, "tearDownModule", None), mount_stub.tearDownModule)
                        module.setUpModule()
                        try:
                            self.assertEqual(INSTALL.filesystem_type(folder), "ext4")
                        finally:
                            module.tearDownModule()
                        self.assertEqual(INSTALL._MOUNTINFO, overlay)

    def test_no_test_module_runs_the_installer_without_the_stand_in(self):
        root = os.path.dirname(recursion_core.__file__)
        found = set()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if name != "__pycache__")
            if os.path.basename(dirpath) != "tests":
                continue
            for name in sorted(filenames):
                if not (name.startswith("test_") and name.endswith(".py")):
                    continue
                with open(os.path.join(dirpath, name), encoding="utf-8") as source:
                    text = source.read()
                if ("daemon_install" in text or "commands import setup" in text) and RUNS_INSTALLER.search(text):
                    rel = os.path.relpath(os.path.join(dirpath, name[:-3]), os.path.dirname(root))
                    found.add(rel.replace(os.sep, "."))
        self.assertEqual(found - set(PINNED) - set(CHILD_PINNED), set())
        self.assertEqual(set(PINNED) - found, set())
        for name in CHILD_PINNED:
            source = importlib.import_module(name).__loader__.get_source(name)
            self.assertIn("._MOUNTINFO = sys.argv[2]", source)


if __name__ == "__main__":
    unittest.main()
