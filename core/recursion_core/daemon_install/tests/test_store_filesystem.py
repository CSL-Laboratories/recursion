"""The install step refuses to make its stores on 9p, overlay or a network disk.

Every case reads a mount table written inside its own temporary folder (the installer's reader is pointed at
it); the machine's own table is never read, and no real mount is made.
"""

from __future__ import annotations

import ast
import importlib
import os
import random
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
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


def _bash_reads(argument):
    """What bash reads ``argument`` as (``printf`` in place of findmnt), or None where bash is not there."""

    bash = shutil.which("bash")
    if not bash:
        return None
    done = subprocess.run([bash, "-c", "printf '%s\\n' " + argument], capture_output=True, text=True, timeout=60,
                          env={"PATH": os.environ.get("PATH", os.defpath)})
    return done.stdout[:-1] if done.returncode == 0 else None


# The rounds of the swap run: a test's own count, read from the environment so a long run is taken in pieces.
SWAP_ROUNDS = int(os.environ.get("RECURSION_GROUND_SWAP_ROUNDS", "20"))


@unittest.skipUnless(LINUX, "the installer is Linux-only")
class GroundAsTheKernelResolvesItTests(unittest.TestCase):
    """The disk read is the disk the stores land on: the path walked as the kernel walks it, from opened folders,
    and the stores made held to the folder opened when its disk was read."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="store-ground-kernel-")
        self.addCleanup(tmp.cleanup)
        self.case = os.path.realpath(tmp.name)
        os.chmod(self.case, 0o700)
        self.home = os.path.join(self.case, "home")
        os.mkdir(self.home, 0o700)
        self.table = os.path.join(self.case, "mountinfo")
        self.mounts([("/", "ext4")])
        patcher = mock.patch.object(INSTALL, "_MOUNTINFO", self.table, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def mounts(self, rows):
        lines = [f"{20 + n} 1 0:{20 + n} / {_escape(point)} rw,relatime shared:{n} - {kind} src{n} rw"
                 for n, (point, kind) in enumerate(rows)]
        with open(self.table, "w", encoding="utf-8") as out:
            out.write("\n".join(lines) + "\n")

    def refused(self, kind, call, *args, **kwargs):
        with self.assertRaises(InstallRefused) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.kind, kind, caught.exception.detail)
        return caught.exception.detail

    def folder(self, *names):
        path = os.path.join(self.case, *names)
        os.makedirs(path, 0o700)
        return path

    def test_dotdot_after_a_link_is_taken_from_where_the_link_leads(self):
        self.folder("disk", "ext4dir")
        nfs = self.folder("disk", "nfs", "plugin-security")
        os.symlink(os.path.join(self.case, "disk", "ext4dir"), os.path.join(self.case, "link"))
        self.mounts([("/", "ext4"), (os.path.dirname(nfs), "nfs4")])
        written = os.path.join(self.case, "link", "..", "nfs", "plugin-security")
        self.assertEqual(os.path.realpath(written), nfs)  # where the kernel opens it
        self.assertEqual(INSTALL.filesystem_type(written), "nfs4")
        self.assertEqual(self.refused("store_filesystem_unsupported", INSTALL.check_store_ground, (written,)),
                         _message(nfs, "nfs4"))

    def test_a_dangling_link_is_read_where_its_target_would_be_made_and_the_words_say_so(self):
        s3 = self.folder("s3")
        os.symlink("gone/state", os.path.join(s3, "state"))
        self.mounts([("/", "ext4"), (s3, "nfs4")])
        detail = self.refused("store_filesystem_unsupported", INSTALL.check_store_ground, (os.path.join(s3, "state"),))
        self.assertTrue(detail.startswith(f"Recursion does not create its stores in {s3}: it is on nfs4, "), detail)
        self.assertIn(f"{s3}/state is a link to gone/state, which is not there: what is made through it would be "
                      f"made in {s3}, so that folder's disk was read.", detail)
        folder = shlex.split(detail.split(COMMAND, 1)[1])[-1]
        self.assertEqual(folder, s3)  # the findmnt step names the folder the type was read for, which is there

    def test_mount_points_are_normalised_once_before_they_are_ranked(self):
        m = self.folder("m")
        for written in (m + "/", m + "//", self.case + "//m", self.case + "/./m", "//" + m.lstrip("/")):
            with self.subTest(point=written):
                self.mounts([("/", "ext4"), (written, "nfs4")])  # a trailing slash, doubled slashes, a dot
                self.assertEqual(INSTALL.filesystem_type(m), "nfs4")
                self.refused("store_filesystem_unsupported", INSTALL.check_store_ground, (m,))
        self.mounts([("//", "ext4"), ("/", "overlay")])  # only slashes is /, ranked as /: the later line decides
        self.assertEqual(INSTALL.filesystem_type(m), "overlay")
        with open(self.table, "w", encoding="utf-8") as out:
            out.write("41 36 0:32 /  rw - nfs4 srv:/y rw\n")  # an empty point holds no folder
        self.assertIsNone(INSTALL.filesystem_type(m))
        with open(self.table, "w", encoding="utf-8") as out:
            out.write("20 1 0:20 / / rw - ext4 src0 rw\n41 36 0:32 /  rw - nfs4 srv:/y rw\n")
        self.assertEqual(INSTALL.filesystem_type(m), "ext4")

    def test_every_spelling_of_one_place_gives_one_answer(self):
        m = self.folder("m")
        self.folder("x")
        os.symlink(self.case, os.path.join(self.home, "case"))
        self.mounts([("/", "ext4"), (m, "nfs4")])
        spellings = (m, m + "/", m + "//", m + "/.", self.case + "//m", self.case + "/./m", m + "/../m",
                     self.case + "/x/../m", os.path.join(self.home, "case", "m"), os.path.join(self.home, "case", "..",
                     os.path.basename(self.case), "m"), m + "/missing/..")
        for spelling in spellings:
            with self.subTest(spelling=spelling):
                ground = INSTALL.store_ground(spelling)
                self.assertEqual((ground.where, ground.below, ground.kind), (m, (), "nfs4"))
                self.assertEqual(self.refused("store_filesystem_unsupported", INSTALL.check_store_ground, (spelling,)),
                                 _message(m, "nfs4"))
        self.assertEqual(INSTALL.store_ground(m + "//new/./made").below, ("new", "made"))

    def test_the_words_name_a_folder_with_a_control_character_in_one_escaped_line(self):
        home = self.folder("st\nate")
        self.mounts([("/", "ext4"), (home, "9p")])
        detail = self.refused("store_filesystem_unsupported", install, Roots(home=home), EXEC)
        self.assertNotIn("\n", detail)
        shown = "$'" + home.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"
        self.assertEqual(detail.count(shown), 3)  # the sentence twice and the findmnt step: one form
        self.assertTrue(detail.startswith(f"Recursion does not create its stores in {shown}: it is on 9p, "))
        read = _bash_reads(detail.split(COMMAND, 1)[1][len("findmnt -no FSTYPE -T "):])
        if read is not None:
            self.assertEqual(read, home)

    def test_a_loop_of_links_is_refused_with_words_a_person_can_act_on(self):
        os.symlink("b", os.path.join(self.case, "a"))
        os.symlink("a", os.path.join(self.case, "b"))
        written = os.path.join(self.case, "a", "state")
        detail = self.refused("store_ground_undecided", INSTALL.check_store_ground, (written,))
        self.assertTrue(detail.startswith(f"Recursion cannot tell which disk {written} is on, so it creates no store "
                                          f"there: more than 40 links on the way. "), detail)
        self.assertIn("XDG_STATE_HOME set to an absolute folder on a local disk", detail)

    def _swap(self, home, other):
        aside = home + ".judged"
        os.rename(home, aside)
        os.symlink(other, home)
        return aside

    def _other_home(self):
        nfs = self.folder("nfs")
        other = self.folder("nfs", "home")
        for names in ((".local", "state", "recursion", "plugin-security"), (".config", "systemd", "user")):
            self.folder("nfs", "home", *names)
        self.mounts([("/", "ext4"), (nfs, "nfs4")])
        return nfs, other

    def test_a_folder_swapped_after_its_disk_is_read_is_refused_before_anything_is_made(self):
        nfs, other = self._other_home()
        before = _tree(nfs)
        real = INSTALL._found

        def found_then_swapped(*args, **kwargs):
            answer = real(*args, **kwargs)
            self._swap(self.home, other)
            return answer

        with mock.patch.object(INSTALL, "_found", found_then_swapped):
            detail = self.refused("store_ground_changed", install, Roots(home=self.home), EXEC)
        self.assertEqual(detail, f"{self.home} was moved or replaced after its disk was read and before the stores "
                                 "were made, so Recursion made nothing. Leave the folders on the way to it as they "
                                 "are while the command runs, and run it again.")
        self.assertEqual(_tree(nfs), before)  # nothing on the refused disk

    def test_a_folder_swapped_while_a_store_is_made_is_refused_naming_the_store(self):
        nfs, other = self._other_home()
        real = stores.create_breaker_store

        def swapped_then_made(path):
            self._swap(self.home, other)
            return real(path)

        with mock.patch.object(stores, "create_breaker_store", swapped_then_made):
            detail = self.refused("store_ground_changed", install, Roots(home=self.home), EXEC)
        made = os.path.join(self.home, ".local", "state", "recursion", "plugin-security", "breaker.sqlite3")
        self.assertFalse(os.path.lexists(os.path.join(other, ".local", "state", "recursion", "plugin-security",
                                                      "breaker.sqlite3")))  # nothing made on the refused disk
        self.assertTrue(detail.startswith(f"{made} does not lead to the store in {self.home}, the folder whose disk "
                                          "was read (ext4): "), detail)
        self.assertIn("so Recursion removes what this run made", detail)
        self.assertEqual(_tree(self.home + ".judged"), {})  # and nothing of it left where it was made

    def test_a_folder_swapped_at_any_moment_never_leaves_a_store_on_another_disk_unsaid(self):
        """SWAP_ROUNDS installs; in each, a thread swaps the home for a link into a refused disk once, at a moment
        drawn between the start and the end of one install's own measured time, so the swap falls before, between
        and after the disk is read and the stores are made."""

        def round_folders(name):
            home = self.folder(name, "home")
            nfs = self.folder(name, "nfs")
            other = self.folder(name, "nfs", "home")
            for names in ((".local", "state", "recursion", "plugin-security"), (".config", "systemd", "user")):
                os.makedirs(os.path.join(other, *names), 0o700)
            return home, nfs, other

        home, nfs, other = round_folders("calibration")
        self.mounts([("/", "ext4"), (nfs, "nfs4")])
        started = time.perf_counter()
        install(Roots(home=home), EXEC)
        took = time.perf_counter() - started  # the window the swap moments are drawn from
        tally, unsaid, other_refusal = {}, [], 0
        for n in range(SWAP_ROUNDS):
            home, nfs, other = round_folders(f"r{n}")
            self.mounts([("/", "ext4"), (nfs, "nfs4")])
            stop = threading.Event()
            delay = random.Random(n).uniform(0, took)

            def swapper():
                if not stop.wait(delay):
                    self._swap(home, other)

            thread = threading.Thread(target=swapper)
            thread.start()
            try:
                install(Roots(home=home), EXEC)
                outcome = "made"
            except InstallRefused as refusal:
                outcome = refusal.kind
            except Exception as exc:  # an error of the swap itself
                outcome = type(exc).__name__
            finally:
                stop.set()
                thread.join()
            swapped = os.path.islink(home)
            tally[(outcome, swapped)] = tally.get((outcome, swapped), 0) + 1
            on_nfs = [path for path in _tree(nfs) if path.endswith(".sqlite3")]
            if on_nfs and outcome == "made":
                unsaid.append((n, on_nfs))
            elif on_nfs and outcome != "store_ground_changed":
                other_refusal += 1
        print(f"\nswap rounds {SWAP_ROUNDS}, window {took:.4f}s: "
              + ", ".join(f"{k}{' swapped' if s else ''} {v}" for (k, s), v in sorted(tally.items()))
              + f"; unsaid {len(unsaid)}; a store on the refused stand-in under another refusal: {other_refusal}")
        self.assertEqual(unsaid, [])
        self.assertEqual(other_refusal, 0)

    def _holding_stores(self):
        """The refused place already holds stores, made while its disk was not yet listed refused (no unit)."""

        nfs = self.folder("nfs")
        other = self.folder("nfs", "home")
        self.folder("nfs", "home", ".config", "systemd", "user")
        install(Roots(home=other), EXEC)
        os.unlink(owned_paths(Roots(home=other)).unit)
        self.mounts([("/", "ext4"), (nfs, "nfs4")])
        return nfs, other

    def _swapped_at(self, moment):
        """The fixed order: the home swapped for a link into a refused place that holds stores, just after the
        folders read are held to the folders opened (``_still_on_ground``) or just before the stores are proven
        (``_made_on_ground``). The install is refused, leaves the refused place as it was, and leaves nothing of its
        own in the folder it judged."""

        nfs, other = self._holding_stores()
        before = _tree(nfs)
        real = getattr(INSTALL, moment)

        def swapped(*args, **kwargs):
            if moment == "_still_on_ground":
                answer = real(*args, **kwargs)
                self._swap(self.home, other)
                return answer
            self._swap(self.home, other)
            return real(*args, **kwargs)

        with mock.patch.object(INSTALL, moment, swapped):
            detail = self.refused("store_ground_changed", install, Roots(home=self.home), EXEC)
        self.assertEqual(_tree(nfs), before)  # nothing made on the refused disk, the stores there untouched
        self.assertEqual(_tree(self.home + ".judged"), {})  # nothing of this run left in the folder judged
        self.assertEqual(len(detail.splitlines()), 1, detail)

    def test_a_swap_into_a_place_that_holds_stores_after_the_check_is_refused_and_leaves_nothing(self):
        self._swapped_at("_still_on_ground")

    def test_a_swap_into_a_place_that_holds_stores_before_the_proof_is_refused_and_leaves_nothing(self):
        self._swapped_at("_made_on_ground")

    def _failing_unit(self, also=None):
        unit = owned_paths(Roots(home=self.home)).unit
        real = INSTALL.write_exclusive

        def failing(path, *args, **kwargs):
            if path == unit:
                if also is not None:
                    also()
                raise OSError(28, "No space left on device")
            return real(path, *args, **kwargs)

        return mock.patch.object(INSTALL, "write_exclusive", failing)

    def test_a_failure_after_the_stores_are_made_leaves_nothing_this_run_made_in_the_state_folder(self):
        with self._failing_unit():
            with self.assertRaises(OSError):
                install(Roots(home=self.home), EXEC)
        self.assertFalse(os.path.lexists(os.path.join(self.home, ".local")), _tree(self.home))

    @unittest.skipUnless(backend_available(), "needs the cryptography package")
    def test_a_failure_after_the_ledger_is_made_removes_the_key_made_with_it(self):
        told = []
        paths = owned_paths(Roots(home=self.home))
        with self._failing_unit():
            with self.assertRaises(OSError):
                install(Roots(home=self.home), EXEC, make_anchor=True, on_anchor_made=told.append)
        self.assertFalse(os.path.lexists(paths.ledger))
        self.assertFalse(os.path.lexists(paths.anchor_key))
        self.assertEqual(told, [])  # no key is said to be kept: none is
        install(Roots(home=self.home), EXEC, make_anchor=True, on_anchor_made=told.append)  # a fresh start again
        self.assertEqual(told, [paths.anchor_key])

    def test_a_store_folder_another_caller_makes_first_is_used(self):
        real = INSTALL._mkdir

        def the_other_caller_first(name, mode=0o777, **kwargs):
            if "dir_fd" in kwargs:  # a folder made through the folder judged
                real(name, 0o700, **kwargs)
            return real(name, mode, **kwargs)

        with mock.patch.object(INSTALL, "_mkdir", the_other_caller_first):
            plan = install(Roots(home=self.home), EXEC)
        self.assertTrue(plan.changed)
        self.assertTrue(os.path.isfile(owned_paths(Roots(home=self.home)).breaker_store))

    def test_what_a_failure_could_not_remove_is_named_in_one_line(self):
        paths = owned_paths(Roots(home=self.home))

        def another_file():
            open(os.path.join(paths.store_dir, "kept.txt"), "w").close()  # put there by another, so kept

        with self._failing_unit(another_file):
            with self.assertRaises(OSError) as caught:
                install(Roots(home=self.home), EXEC)
        note = " ".join(getattr(caught.exception, "__notes__", ()))
        self.assertTrue(note.startswith(f"This run could not remove what it made at {paths.store_dir}, "), note)
        self.assertIn("then run the command again.", note)
        self.assertEqual(len(note.splitlines()), 1)
        self.assertFalse(os.path.lexists(paths.breaker_store))
        self.assertTrue(os.path.isfile(os.path.join(paths.store_dir, "kept.txt")))

    def test_a_store_put_at_its_name_meanwhile_is_not_taken_for_the_one_made(self):
        paths = owned_paths(Roots(home=self.home))
        real = stores.create_breaker_store

        def another_first(path):
            real(path)
            with open(paths.breaker_store, "wb") as other:  # another's file at the store's name, after the look
                other.write(b"another's")

        with mock.patch.object(stores, "create_breaker_store", another_first):
            self.refused("breaker_store_exists", install, Roots(home=self.home), EXEC)
        with open(paths.breaker_store, "rb") as kept:
            self.assertEqual(kept.read(), b"another's")  # left as it is, never taken for the store made
        self.assertFalse(os.path.lexists(paths.revocation_store))


    def test_a_folder_named_with_any_character_that_is_not_printable_gives_one_line_of_words(self):
        disk = self.folder("nfs")
        self.mounts([("/", "ext4"), (disk, "nfs4")])
        for name, shown in (("n\x85x", "n\\u0085x"), ("u\u2028x", "u\\u2028x"), ("p\u2029x", "p\\u2029x"),
                            ("c\x9b31mx", "c\\u009b31mx"), ("z\u200bx", "z\\u200bx")):
            with self.subTest(name=ascii(name)):
                folder = self.folder("nfs", name)
                detail = self.refused("store_filesystem_unsupported", INSTALL.check_store_ground, (folder,))
                self.assertEqual(len(detail.splitlines()), 1, ascii(detail))
                self.assertNotIn(name[1], detail)
                self.assertIn("$'" + os.path.join(disk, shown) + "'", detail)

    def test_a_mount_point_holding_a_carriage_return_is_read_as_the_kernel_writes_it(self):
        point = self.folder("m\rp")
        self.mounts([("/", "ext4"), (point, "nfs4")])  # the kernel escapes only space, tab, newline and backslash
        self.assertEqual(INSTALL.filesystem_type(self.folder("m\rp", "plugin-security")), "nfs4")
        self.assertEqual(INSTALL.filesystem_type(point), "nfs4")

    def _second_inside_first(self, make_anchor=False):
        """The fixed order of two installs of one profile at once: the first has made the breaker store and is about
        to make the revocation store when the second runs whole. Returns the first's plan and the second's answer."""

        second = []
        real = stores.create_revocation_store

        def the_second_meanwhile(path):
            if not second:
                second.append(None)
                try:
                    second[0] = install(Roots(home=self.home), EXEC, make_anchor=make_anchor)
                except InstallRefused as refusal:
                    second[0] = refusal
            return real(path)

        with mock.patch.object(stores, "create_revocation_store", the_second_meanwhile):
            plan = install(Roots(home=self.home), EXEC, make_anchor=make_anchor)
        return plan, second[0]

    def _one_at_a_time(self, make_anchor):
        paths = owned_paths(Roots(home=self.home))
        plan, second = self._second_inside_first(make_anchor)
        self.assertIsInstance(second, InstallRefused)
        self.assertEqual(second.kind, "install_running", second.detail)
        self.assertEqual(second.detail, f"another install of this profile is running (recursiond install or upgrade "
                                        f"for {self.home}), so this one made nothing. Let it end, then run the command "
                                        "again.")
        self.assertTrue(plan.changed)
        kept = [paths.breaker_store, paths.revocation_store, paths.unit]
        if make_anchor:
            kept += [paths.ledger, paths.anchor_key]
        for path in kept:
            self.assertTrue(os.path.isfile(path), path)  # nothing the first made and reported was removed
        before = _tree(self.home)
        self.assertFalse(install(Roots(home=self.home), EXEC, make_anchor=make_anchor).changed)  # all found again
        self.assertEqual(_tree(self.home), before)

    def test_a_second_install_of_one_profile_while_the_first_runs_is_refused_and_removes_nothing(self):
        self._one_at_a_time(False)

    @unittest.skipUnless(backend_available(), "needs the cryptography package")
    def test_a_second_install_of_one_profile_with_the_key_is_refused_and_removes_nothing(self):
        self._one_at_a_time(True)

    def test_a_second_upgrade_of_one_profile_while_the_first_runs_is_refused(self):
        install(Roots(home=self.home), EXEC)
        os.unlink(owned_paths(Roots(home=self.home)).revocation_store)
        second = []
        real = stores.create_revocation_store

        def the_second_meanwhile(path):
            if not second:
                second.append(None)
                try:
                    second[0] = upgrade(Roots(home=self.home), EXEC)
                except InstallRefused as refusal:
                    second[0] = refusal
            return real(path)

        with mock.patch.object(stores, "create_revocation_store", the_second_meanwhile):
            upgrade(Roots(home=self.home), EXEC)
        self.assertEqual(getattr(second[0], "kind", second[0]), "install_running")

    def test_the_second_waits_for_the_first_as_long_as_the_setting_says(self):
        with mock.patch.object(INSTALL, "INSTALL_LOCK_WAIT", 0.3):
            started = time.monotonic()
            plan, second = self._second_inside_first()
            waited = time.monotonic() - started
        self.assertEqual(getattr(second, "kind", second), "install_running")
        self.assertGreaterEqual(waited, 0.3)  # it waited the setting's time for the first, then was refused
        self.assertTrue(plan.changed)
        paths = owned_paths(Roots(home=self.home))
        first_in, let_go, answers = threading.Event(), threading.Event(), {}
        real = stores.create_breaker_store
        os.unlink(paths.unit)
        os.unlink(paths.breaker_store)

        def held_first(path):
            first_in.set()
            let_go.wait(60)
            return real(path)

        def run(name):
            try:
                answers[name] = install(Roots(home=self.home), EXEC).changed
            except BaseException as exc:  # noqa: BLE001 -- the answer is read below
                answers[name] = exc

        with mock.patch.object(stores, "create_breaker_store", held_first), \
                mock.patch.object(INSTALL, "INSTALL_LOCK_WAIT", None):
            first = threading.Thread(target=run, args=("first",))
            first.start()
            self.assertTrue(first_in.wait(60))
            second_run = threading.Thread(target=run, args=("second",))
            second_run.start()
            second_run.join(0.3)
            self.assertTrue(second_run.is_alive())  # waiting for the first, with no time limit
            let_go.set()
            first.join(60)
            second_run.join(60)
        self.assertEqual(answers, {"first": True, "second": False})  # the second found what the first made

    def test_a_failed_run_removes_a_store_only_while_its_name_holds_the_very_file_it_made(self):
        """Another program puts its own file at the store's name at the moment the failed run looks at what it made:
        that file is kept, never taken for the run's own."""

        paths = owned_paths(Roots(home=self.home))
        failing, placed = [], []
        real_stat = os.stat

        def another_at_the_name(path, *args, **kwargs):
            answer = real_stat(path, *args, **kwargs)
            if (failing and not placed and kwargs.get("dir_fd") is not None
                    and "breaker.sqlite3" in os.fspath(path)):
                placed.append(path)
                others = os.path.join(paths.store_dir, "others.part")
                with open(others, "wb") as out:
                    out.write(b"another's")
                os.rename(others, paths.breaker_store)
            return answer

        with self._failing_unit(lambda: failing.append(1)), mock.patch("os.stat", another_at_the_name):
            with self.assertRaises(OSError):
                install(Roots(home=self.home), EXEC)
        self.assertTrue(placed)
        with open(paths.breaker_store, "rb") as kept:
            self.assertEqual(kept.read(), b"another's")
        self.assertFalse(os.path.lexists(paths.revocation_store))


    def _key_case(self, name, holds_key):
        """A home and a refused place that holds stores (and, with ``holds_key``, a key and its ledger)."""

        base = self.folder(name)
        home, nfs = os.path.join(base, "home"), os.path.join(base, "nfs")
        other = os.path.join(nfs, "home")
        for each in (home, other):
            os.makedirs(os.path.join(each, ".config", "systemd", "user"), 0o700)
        self.mounts([("/", "ext4")])
        install(Roots(home=other), EXEC, make_anchor=holds_key)
        os.unlink(owned_paths(Roots(home=other)).unit)
        self.mounts([("/", "ext4"), (nfs, "nfs4")])
        return home, nfs, other

    @unittest.skipUnless(backend_available(), "needs the cryptography package")
    def test_a_run_refused_after_a_swap_of_the_home_removes_the_key_it_made(self):
        for holds_key in (False, True):
            for moment in ("ensure_ledger", "_made_on_ground"):
                with self.subTest(holds_key=holds_key, moment=moment):
                    home, nfs, other = self._key_case(f"key-{moment}-{holds_key}", holds_key)
                    before = _tree(nfs)
                    real = getattr(INSTALL, moment)

                    def swapped(*args, _real=real, _home=home, _other=other, **kwargs):
                        self._swap(_home, _other)
                        return _real(*args, **kwargs)

                    with mock.patch.object(INSTALL, moment, swapped):
                        detail = self.refused("store_ground_changed", install, Roots(home=home), EXEC, make_anchor=True)
                    authority = os.path.join(home + ".judged", ".config", "recursion", "authority")
                    self.assertEqual(os.listdir(authority) if os.path.isdir(authority) else [], [])  # no key, no part
                    self.assertEqual(_tree(nfs), before)  # the refused place's own key and stores untouched
                    self.assertEqual(len(detail.splitlines()), 1, detail)

    @unittest.skipUnless(backend_available(), "needs the cryptography package")
    def test_a_persons_key_is_never_removed_by_a_refused_run(self):
        paths = owned_paths(Roots(home=self.home))
        install(Roots(home=self.home), EXEC, make_anchor=True)
        os.unlink(paths.unit)
        with open(paths.anchor_key, "rb") as kept:
            key = kept.read()
        bound = INSTALL._ledger_anchor(paths.ledger)
        nfs, other = self._holding_stores()

        def back():
            if os.path.islink(self.home):
                os.unlink(self.home)
                os.rename(self.home + ".judged", self.home)
            if os.path.lexists(paths.unit):
                os.unlink(paths.unit)

        for moment in ("_anchor_and_ledger", "_made_on_ground", "unit", "second"):
            with self.subTest(moment=moment):
                if moment == "unit":
                    with self._failing_unit():
                        with self.assertRaises(OSError):
                            install(Roots(home=self.home), EXEC, make_anchor=True)
                elif moment == "second":
                    os.unlink(paths.revocation_store)  # the first makes it, and the second runs meanwhile
                    plan, second = self._second_inside_first(make_anchor=True)
                    self.assertEqual(getattr(second, "kind", second), "install_running")
                else:
                    real = getattr(INSTALL, moment)

                    def swapped(*args, _real=real, **kwargs):
                        self._swap(self.home, other)
                        return _real(*args, **kwargs)

                    with mock.patch.object(INSTALL, moment, swapped):
                        self.refused("store_ground_changed", install, Roots(home=self.home), EXEC, make_anchor=True)
                back()
                with open(paths.anchor_key, "rb") as kept:
                    self.assertEqual(kept.read(), key)  # the person's key, byte for byte
                self.assertEqual(INSTALL._ledger_anchor(paths.ledger), bound)

    @unittest.skipUnless(backend_available(), "needs the cryptography package")
    def test_the_words_name_the_key_a_refused_run_made_and_could_not_remove(self):
        paths = owned_paths(Roots(home=self.home))
        real_rename = os.rename

        def kept_key(src, dst, *args, **kwargs):
            if os.fspath(src) == os.path.basename(paths.anchor_key):
                raise PermissionError(1, "Operation not permitted", src)
            return real_rename(src, dst, *args, **kwargs)

        with self._failing_unit(), mock.patch("os.rename", kept_key):
            with self.assertRaises(OSError) as caught:
                install(Roots(home=self.home), EXEC, make_anchor=True)
        note = " ".join(getattr(caught.exception, "__notes__", ()))
        self.assertIn(f"This run could not remove what it made at {paths.anchor_key}", note)
        self.assertEqual(len(note.splitlines()), 1)
        self.assertTrue(os.path.isfile(paths.anchor_key))
        os.unlink(paths.anchor_key)
        moved = os.path.join(self.case, "moved.key")
        with self._failing_unit(lambda: os.rename(paths.anchor_key, moved)):  # another program moves it away
            with self.assertRaises(OSError) as caught:
                install(Roots(home=self.home), EXEC, make_anchor=True)
        note = " ".join(getattr(caught.exception, "__notes__", ()))
        self.assertIn(f"The authority key this run made at {paths.anchor_key} was moved or replaced while the "
                      "command ran, so this run could not remove it", note)
        self.assertEqual(len(note.splitlines()), 1)
        self.assertTrue(os.path.isfile(moved))
        self.assertFalse(os.path.lexists(paths.anchor_key))


@unittest.skipUnless(LINUX, "the installer is Linux-only")
class TheReadmeSaysWhatTheInstallerDoesTests(unittest.TestCase):
    """The README's sentences on the key, its ledger and the stores after a failed step are true of the code."""

    def test_the_readme_says_what_a_failed_step_does_with_the_key_its_ledger_and_the_stores(self):
        with open(os.path.join(os.path.dirname(INSTALL.__file__), "README.md"), encoding="utf-8") as handle:
            text = " ".join(handle.read().split())
        for old in ("before the later steps, so a later step that fails does not hide it",
                    "a ledger already bound to the key just made keeps the key with it",
                    "with the stores already made"):
            self.assertNotIn(old, text)
        for new in ("The line is printed only at the end of a step that ended made",
                    "a key made now together with the ledger made for it",
                    "only while its name holds the very file made",
                    "A key of the person, found with its ledger, is never removed",
                    "another install of this profile is running",
                    "and the step then removes what it made (stores found are left as they are)"):
            self.assertIn(new, text)


class OneReadingOfTheGroundTests(unittest.TestCase):
    """Every caller reads the disk through ``store_ground``; none reads the mount table or judges a path by text."""

    TEXT_RESOLVERS = {"abspath", "realpath", "normpath", "lexists", "exists", "isdir"}
    READERS = ("store_ground", "_walk", "_kernel_name", "_mount_type", "_point", "filesystem_type", "_judge",
               "check_store_ground", "_still_on_ground", "_made_on_ground")

    def _install_tree(self):
        with open(INSTALL.__file__, encoding="utf-8") as source:
            return ast.parse(source.read())

    def _functions(self, tree):
        return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}

    def _names_in(self, node):
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}

    def test_only_store_ground_reads_the_mount_table(self):
        functions = self._functions(self._install_tree())
        for name, node in functions.items():
            names = self._names_in(node)
            with self.subTest(function=name):
                if "_MOUNTINFO" in names:
                    self.assertEqual(name, "_mount_type")
                if "_mount_type" in names or "_walk" in names or "_kernel_name" in names:
                    self.assertEqual(name, "store_ground")

    def test_no_reader_of_the_ground_resolves_a_path_by_text(self):
        functions = self._functions(self._install_tree())
        for name in self.READERS:
            with self.subTest(function=name):
                calls = {node.func.attr for node in ast.walk(functions[name]) if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Attribute)}
                self.assertEqual(calls & self.TEXT_RESOLVERS - ({"normpath"} if name in ("_walk", "_point") else set()),
                                 set())

    def test_install_and_upgrade_hold_the_stores_to_the_folder_read(self):
        functions = self._functions(self._install_tree())
        for name in ("install", "upgrade"):
            with self.subTest(function=name):
                calls = sorted((node.lineno, node.func.id) for node in ast.walk(functions[name])
                               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
                                   "_judge", "_still_on_ground", "ensure_stores", "_made_on_ground",
                                   "check_store_ground"))
                order = [called for _, called in calls]
                self.assertEqual(order[:4], ["_judge", "_still_on_ground", "ensure_stores", "_made_on_ground"])
                self.assertNotIn("check_store_ground", order)

    def test_no_other_module_reads_the_mount_table(self):
        root = os.path.dirname(recursion_core.__file__)
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if name not in ("__pycache__", "tests"))
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                if not name.endswith(".py") or os.path.samefile(path, INSTALL.__file__):
                    continue
                with open(path, encoding="utf-8") as source:
                    text = source.read()
                if "mountinfo" in text or "filesystem_type(" in text or "_mount_type(" in text:
                    found.append(os.path.relpath(path, root))
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
