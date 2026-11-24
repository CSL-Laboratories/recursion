"""The held-folder module: an anchor that holds a folder and works beneath it without following a link.

Every case uses real folders, real links and real threads in one temporary folder it makes itself. The
swap case reuses the system facts' own swap (``test_system_facts``: ``renameat2`` with
``RENAME_EXCHANGE`` from a second thread). The POSIX cases skip on Windows with the reason; the refusal
case runs everywhere and, on a POSIX host, takes ``O_NOFOLLOW`` and ``O_DIRECTORY`` out of ``os`` for
its own duration, as a Windows interpreter has them out.
"""

from __future__ import annotations

import copy
import gc
import os
import pickle
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import recursion_core
from recursion_core.anchored import ABOVE, CAUSES, Anchor, AnchorRefused, _posix, open_anchor
from recursion_core.anchored.tests import test_system_facts as facts

POSIX = os.name == "posix"
NOT_POSIX = (f"a POSIX case; this host is {sys.platform} (os.name {os.name}), where open_anchor refuses "
             "platform_unsupported (the refusal case runs here)")
SWAP_ROUNDS = facts.L3_ROUNDS
THREAD_ROUNDS = 1_000


def _q(path) -> str:
    return shlex.quote(str(path))


def _link_way_out(path: Path) -> str:
    return (f"{_q(path)} is a link, and this command does not follow links. Move the link out of "
            f"{_q(path.parent)} and put the file or folder it stands for in its place; then run the command again.")


def _files(top: Path) -> int:
    return sum(len(files) for _, _, files in os.walk(top))  # os.walk follows no link


def _open_descriptors() -> int:
    return len(os.listdir("/proc/self/fd"))


_MODULE = "recursion_core.anchored.tests.test_anchor"  # by name: the gate runs this file as __main__


def _run_alone(case: str, tmpdir: str) -> subprocess.CompletedProcess:
    """One case of this module in a fresh interpreter, with ``TMPDIR`` set, as the gate's first run of a file."""
    env = dict(os.environ)
    core = os.path.dirname(os.path.dirname(os.path.abspath(recursion_core.__file__)))
    env["PYTHONPATH"] = os.pathsep.join(part for part in (core, env.get("PYTHONPATH", "")) if part)
    env["TMPDIR"] = tmpdir
    command = [sys.executable, "-X", "utf8", "-B", "-m", "unittest", f"{_MODULE}.{case}"]
    return subprocess.run(command, capture_output=True, text=True, timeout=150, env=env, cwd=tmpdir, check=False)


@unittest.skipUnless(POSIX, NOT_POSIX)
class SwapTests(unittest.TestCase):
    def test_a_swap_by_a_second_thread_lands_no_file_outside_through_ensure_dir_and_write_atomic(self) -> None:
        exchange = facts._exchanger()  # skips where the C library has no renameat2
        with tempfile.TemporaryDirectory() as tmp:
            base, outside = Path(tmp, "base"), Path(tmp, "outside")
            base.joinpath(*facts.PARTS).mkdir(parents=True)
            outside.joinpath(*facts.PARTS[3:]).mkdir(parents=True)  # a path through the link would resolve
            middle = base.joinpath(*facts.PARTS[:3])
            spare = middle.with_name(middle.name + ".swap")
            os.symlink(outside, spare)
            stop, swaps = threading.Event(), [0]

            def swapper() -> None:
                while not stop.is_set():
                    try:
                        exchange(middle, spare)
                        swaps[0] += 1
                    except OSError:
                        pass

            made = refused = 0
            causes: set = set()
            thread = threading.Thread(target=swapper, name="anchor-swapper")
            with open_anchor(base) as anchor:
                thread.start()
                try:
                    for index in range(SWAP_ROUNDS):
                        held = []
                        try:
                            current = anchor
                            for part in facts.PARTS:
                                current = current.ensure_dir(part)
                                held.append(current)
                            current.write_atomic(f"f{index}", b"x")
                            made += 1
                        except AnchorRefused as exc:
                            refused += 1
                            causes.add(exc.cause)
                        finally:
                            for folder in held:
                                folder.release()
                finally:
                    stop.set()
                    thread.join()
            if os.path.islink(middle):  # leave the real folder at its own name
                exchange(middle, spare)
            self.assertGreater(swaps[0], 0)
            self.assertEqual(SWAP_ROUNDS, made + refused)
            self.assertLessEqual(causes, {"link", "not_a_folder", "missing"})
            self.assertEqual(0, _files(outside))
            self.assertEqual(made, _files(base))


@unittest.skipUnless(POSIX, NOT_POSIX)
class AboveTheAnchorTests(unittest.TestCase):
    def test_a_home_reached_through_a_link_the_user_owns_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp, "real")
            real.joinpath("home").mkdir(parents=True, mode=0o700)
            os.symlink(real, Path(tmp, "homelink"))
            with open_anchor(Path(tmp, "homelink", "home")) as home, home.ensure_dir(".local") as local:
                local.write_atomic("record.json", b'{"ok": true}')
                self.assertEqual(b'{"ok": true}', local.read("record.json", 64))
            self.assertEqual(b'{"ok": true}', real.joinpath("home", ".local", "record.json").read_bytes())

    def test_a_folder_above_that_group_or_world_can_write_refuses_unless_it_is_sticky(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shared = Path(os.path.realpath(tmp), "shared")  # the module names the link-free path it resolved
            shared.joinpath("home").mkdir(parents=True, mode=0o700)
            os.chmod(shared, 0o777)
            with self.assertRaises(AnchorRefused) as caught:
                open_anchor(shared / "home")
            self.assertEqual(("writable", str(shared)), (caught.exception.cause, caught.exception.path))
            self.assertEqual(f"{_q(shared)} can be written by group or world. Take those write bits off with "
                             f"chmod go-w {_q(shared)}; then run the command again.", caught.exception.way_out)
            os.chmod(shared, 0o1777)
            open_anchor(shared / "home").release()
            os.chmod(shared, 0o700)

    def test_the_writable_case_holds_when_the_temporary_folder_lies_behind_a_link(self) -> None:
        # As on macOS, where the temporary folder is under /var, a link to private/var.
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "real").mkdir(mode=0o700)
            os.symlink(Path(tmp, "real"), Path(tmp, "link"))
            case = ("AboveTheAnchorTests."
                    "test_a_folder_above_that_group_or_world_can_write_refuses_unless_it_is_sticky")
            done = _run_alone(case, str(Path(tmp, "link")))
            self.assertEqual(0, done.returncode, done.stderr[-3000:])
            self.assertEqual([], os.listdir(Path(tmp, "real")))

    def test_the_folders_above_are_held_to_the_owner_rule_and_the_owner_cause_is_reached(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root owns everything this case would show as another user's")
        real_euid = os.geteuid
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "home").mkdir(mode=0o700)
            os.geteuid = lambda: real_euid() + 1  # a stand-in: this user is someone else for the call
            try:
                with self.assertRaises(AnchorRefused) as caught:
                    open_anchor(Path(tmp, "home"))
            finally:
                os.geteuid = real_euid
            self.assertEqual("owner", caught.exception.cause)
            self.assertEqual(real_euid(), os.lstat(caught.exception.path).st_uid)
            self.assertIn("is owned by another user", caught.exception.way_out)

    def test_the_anchor_itself_that_group_or_world_can_write_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp, "home")
            home.mkdir(mode=0o700)
            for mode in (0o770, 0o702):
                os.chmod(home, mode)
                with self.subTest(mode=oct(mode)), self.assertRaises(AnchorRefused) as caught:
                    open_anchor(home)
                self.assertEqual(("writable", str(home)), (caught.exception.cause, caught.exception.path))
            os.chmod(home, 0o700)
            open_anchor(home).release()

    def test_more_than_forty_links_above_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.symlink("l2", Path(tmp, "l1"))
            os.symlink("l1", Path(tmp, "l2"))
            with self.assertRaises(AnchorRefused) as caught:
                open_anchor(Path(tmp, "l1", "home"))
            self.assertEqual("too_many_links", caught.exception.cause)

    def test_the_anchor_itself_is_a_folder_and_not_a_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "real").mkdir()
            os.symlink(Path(tmp, "real"), Path(tmp, "link"))
            Path(tmp, "file").write_bytes(b"x")
            for name, cause in (("link", "link"), ("file", "not_a_folder"), ("absent", "missing")):
                with self.subTest(name=name), self.assertRaises(AnchorRefused) as caught:
                    open_anchor(Path(tmp, name))
                self.assertEqual(cause, caught.exception.cause)
            with self.assertRaises(ValueError):
                open_anchor("relative/path")
            with self.assertRaises(ValueError):
                open_anchor(Path(tmp), above="none")

    def test_a_folder_swapped_in_between_the_check_and_the_open_refuses_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp, "home")
            home.mkdir(mode=0o700)
            real_open, swapped = os.open, []

            def open_after_a_swap(path, *args, **kwargs):
                if isinstance(path, str) and path.endswith(os.sep + "home") and not swapped:  # a real rename
                    os.rename(home, Path(tmp, "home.before"))
                    os.mkdir(home, 0o700)
                    swapped.append(True)
                return real_open(path, *args, **kwargs)

            os.open = open_after_a_swap
            os.supports_dir_fd.add(open_after_a_swap)  # the wrapper stands where os.open stood
            try:
                with self.assertRaises(AnchorRefused) as caught:
                    open_anchor(home)
            finally:
                os.open = real_open
                os.supports_dir_fd.discard(open_after_a_swap)
            self.assertEqual([True], swapped)
            self.assertEqual(("changed", str(home)), (caught.exception.cause, caught.exception.path))

    def test_operator_root_follows_the_typed_link_once_and_nothing_below_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp, "registry")
            real.joinpath("entries").mkdir(parents=True)
            Path(tmp, "outside").mkdir()
            os.symlink(Path(tmp, "outside"), real / "linked")
            os.symlink(real, Path(tmp, "typed"))
            with open_anchor(Path(tmp, "typed"), above="operator_root") as root:
                self.assertEqual(os.path.realpath(real), root.path)
                root.child("entries").release()
                with self.assertRaises(AnchorRefused) as caught:
                    root.child("linked")
                self.assertEqual("link", caught.exception.cause)
            with self.assertRaises(AnchorRefused) as caught:
                open_anchor(Path(tmp, "absent"), above="operator_root")
            self.assertEqual("missing", caught.exception.cause)


@unittest.skipUnless(POSIX, NOT_POSIX)
class BelowTheAnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.home = self.tmp / "my home"  # a space, so the printed path is quoted
        self.outside = self.tmp / "outside"
        self.home.mkdir(mode=0o700)
        self.outside.mkdir()

    def test_a_link_below_the_anchor_refuses_with_its_way_out_and_nothing_is_written_outside(self) -> None:
        os.symlink(self.outside, self.home / ".local")
        (self.outside / "secret").write_bytes(b"outside")
        os.symlink(self.outside / "secret", self.home / "secret")
        with open_anchor(self.home) as home:
            for call in (lambda: home.child(".local"), lambda: home.ensure_dir(".local")):
                with self.assertRaises(AnchorRefused) as caught:
                    call().release()
                self.assertEqual(("link", str(self.home / ".local")), (caught.exception.cause, caught.exception.path))
                self.assertEqual(_link_way_out(self.home / ".local"), caught.exception.way_out)
            with self.assertRaises(AnchorRefused) as caught:
                home.read("secret", 64)
            self.assertEqual("link", caught.exception.cause)
            self.assertEqual(_link_way_out(self.home / "secret"), caught.exception.way_out)
            for call in (lambda: home.remove("secret"), lambda: home.rename("secret", home, "moved")):
                with self.assertRaises(AnchorRefused) as caught:
                    call()
                self.assertEqual("link", caught.exception.cause)
            home.write_atomic("secret", b"inside")  # replaces the link itself, never what it points at
        self.assertEqual(b"outside", (self.outside / "secret").read_bytes())
        self.assertEqual(["secret"], os.listdir(self.outside))
        self.assertFalse((self.home / "secret").is_symlink())
        self.assertEqual(b"inside", (self.home / "secret").read_bytes())

    def test_child_refuses_a_name_that_is_not_one_part(self) -> None:
        with open_anchor(self.home) as home:
            for name in ("", ".", "..", "a/b", "/", "a\0b", None):
                with self.subTest(name=name), self.assertRaises(AnchorRefused) as caught:
                    home.child(name)
                self.assertEqual(("bad_name", str(self.home)), (caught.exception.cause, caught.exception.path))

    def test_write_atomic_makes_an_owner_only_file_through_the_held_folder_and_leaves_no_temporary(self) -> None:
        old = os.umask(0)
        try:
            with open_anchor(self.home) as home, home.ensure_dir("state") as state:
                state.write_atomic("a.json", b"one")
                state.write_atomic("a.json", b"two")
                state.write_atomic("b.json", b"", mode=0o640)
        finally:
            os.umask(old)
        folder = self.home / "state"
        self.assertEqual(0o700, stat.S_IMODE(folder.stat().st_mode))
        self.assertEqual(["a.json", "b.json"], sorted(os.listdir(folder)))
        self.assertEqual(b"two", (folder / "a.json").read_bytes())
        self.assertEqual(0o600, stat.S_IMODE((folder / "a.json").stat().st_mode))
        self.assertEqual(0o640, stat.S_IMODE((folder / "b.json").stat().st_mode))

    def test_read_refuses_anything_but_a_regular_file_the_user_owns_that_others_cannot_write(self) -> None:
        (self.home / "folder").mkdir()
        os.mkfifo(self.home / "fifo")
        (self.home / "open").write_bytes(b"x")
        os.chmod(self.home / "open", 0o620)
        (self.home / "big").write_bytes(b"0123456789")
        with open_anchor(self.home) as home:
            for name, cause in (("folder", "not_regular"), ("fifo", "not_regular"), ("open", "writable"),
                                ("absent", "missing")):
                with self.subTest(name=name), self.assertRaises(AnchorRefused) as caught:
                    home.read(name, 64)
                self.assertEqual((cause, str(self.home / name)), (caught.exception.cause, caught.exception.path))
            with self.assertRaises(AnchorRefused) as caught:
                home.read("big", 9)
            self.assertEqual("too_large", caught.exception.cause)
            self.assertEqual(b"0123456789", home.read("big", 10))

    def test_entries_kind_rename_and_remove_work_through_the_held_folders(self) -> None:
        (self.home / "file").write_bytes(b"x")
        (self.home / "dir").mkdir()
        os.symlink(self.outside, self.home / "link")
        with open_anchor(self.home) as home, home.child("dir") as below:
            self.assertEqual([("dir", "dir"), ("file", "file"), ("link", "link")], home.entries())
            self.assertEqual(("file", "link", None), (home.kind("file"), home.kind("link"), home.kind("absent")))
            home.rename("file", below, "moved")
            self.assertEqual([("moved", "file")], below.entries())
            below.remove("moved")
            home.remove("dir")
            with self.assertRaises(AnchorRefused) as caught:
                home.remove("absent")
            self.assertEqual("missing", caught.exception.cause)
            with self.assertRaises(AnchorRefused) as caught:
                home.child("link")
            self.assertEqual("link", caught.exception.cause)
        self.assertEqual(["link"], os.listdir(self.home))
        self.assertTrue(self.outside.is_dir())

    def test_a_write_that_fails_leaves_no_temporary_file(self) -> None:
        (self.home / "dir").mkdir()
        with open_anchor(self.home) as home:
            with self.assertRaises(AnchorRefused) as caught:
                home.write_atomic("dir", b"onto a folder")
            self.assertEqual("not_regular", caught.exception.cause)
        self.assertEqual(["dir"], os.listdir(self.home))
        self.assertEqual([], os.listdir(self.home / "dir"))

    def test_ensure_dir_makes_the_folder_in_the_held_folder_not_by_its_path(self) -> None:
        with open_anchor(self.home) as home:
            moved = self.tmp / "home.moved"
            os.rename(self.home, moved)  # the path now leads elsewhere; the held folder is the same
            os.symlink(self.outside, self.home)
            with home.ensure_dir("made") as made:
                made.write_atomic("record", b"in")
        self.assertEqual([], os.listdir(self.outside))
        self.assertEqual(b"in", (moved / "made" / "record").read_bytes())

    def test_write_atomic_and_rename_sync_the_file_and_the_folders(self) -> None:
        real_fsync, synced = os.fsync, []

        def recording(fd):
            info = os.fstat(fd)
            synced.append((info.st_dev, info.st_ino))
            return real_fsync(fd)

        os.fsync = recording
        try:
            with open_anchor(self.home) as home, home.ensure_dir("a") as a, home.ensure_dir("b") as b:
                a.write_atomic("f", b"x")
                written = list(synced)
                del synced[:]
                a.rename("f", b, "g")
        finally:
            os.fsync = real_fsync

        def ident(path: Path) -> tuple:
            info = os.stat(path)
            return info.st_dev, info.st_ino

        file, folder_a, folder_b = ident(self.home / "b" / "g"), ident(self.home / "a"), ident(self.home / "b")
        self.assertEqual([file, folder_a], written)
        self.assertEqual([folder_b, folder_a], synced)

    def test_read_refuses_a_file_of_another_user(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root owns everything this case would show as another user's")
        (self.home / "mine").write_bytes(b"x")
        real_euid = os.geteuid
        with open_anchor(self.home) as home:
            os.geteuid = lambda: real_euid() + 1  # a stand-in: the file now belongs to someone else
            try:
                with self.assertRaises(AnchorRefused) as caught:
                    home.read("mine", 64)
            finally:
                os.geteuid = real_euid
        self.assertEqual(("owner", str(self.home / "mine")), (caught.exception.cause, caught.exception.path))

    def test_write_atomic_and_ensure_dir_refuse_a_mode_outside_0o777_before_anything_is_made(self) -> None:
        bad = (0o4700, 0o2700, 0o1700, 0o10000, -1, "0o600", 384.0, None)
        with open_anchor(self.home) as home:
            for index, mode in enumerate(bad):
                box = self.home / f"box{index}"
                with self.subTest(mode=mode), home.ensure_dir(box.name) as inside:
                    with self.assertRaises(ValueError) as caught:
                        inside.write_atomic("f", b"x", mode=mode)
                    self.assertIn("0o777", str(caught.exception))
                    with self.assertRaises(ValueError):
                        inside.ensure_dir("d", mode=mode).release()
                    self.assertEqual([], os.listdir(box))

    def test_a_group_or_world_writable_mode_given_on_purpose_is_applied_as_given(self) -> None:
        old = os.umask(0o077)
        try:
            with open_anchor(self.home) as home:
                home.write_atomic("shared", b"x", mode=0o666)
                home.write_atomic("group", b"x", mode=0o664)
                home.write_atomic("plain", b"x", mode=0o644)
        finally:
            os.umask(old)
        self.assertEqual((0o666, 0o664, 0o644), tuple(stat.S_IMODE((self.home / name).stat().st_mode)
                                                      for name in ("shared", "group", "plain")))

    def test_release_closes_the_descriptor_once_and_a_released_anchor_refuses_use(self) -> None:
        anchor = open_anchor(self.home)
        with anchor.ensure_dir("a") as below:
            self.assertIsInstance(below, Anchor)
        anchor.release()
        anchor.release()
        with self.assertRaises(ValueError):
            anchor.child("a")


class CauseSetTests(unittest.TestCase):
    def test_the_causes_are_one_finite_set_in_their_first_order(self) -> None:
        self.assertEqual(("link", "not_a_folder", "not_regular", "owner", "writable", "too_many_links", "changed",
                          "bad_name", "missing", "too_large", "platform_unsupported"), CAUSES[:11])
        self.assertEqual(("owner_trusted", "operator_root"), ABOVE)
        with self.assertRaises(ValueError):
            AnchorRefused("something_else", "/", "")


class WindowsRefusalTests(unittest.TestCase):
    def test_without_o_nofollow_and_o_directory_open_anchor_refuses_platform_unsupported_before_any_open(self) -> None:
        target = os.path.abspath(tempfile.gettempdir())  # its first call opens a probe file: not counted
        removed = {name: getattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY") if hasattr(os, name)}
        real_open, real_lstat, calls = os.open, os.lstat, []

        def counted(real):
            def call(*args, **kwargs):
                calls.append(real.__name__)
                return real(*args, **kwargs)
            return call

        for name in removed:
            delattr(os, name)
        os.open, os.lstat = counted(real_open), counted(real_lstat)
        os.supports_dir_fd.add(os.open)  # so the only thing missing is the two flags
        try:
            with self.assertRaises(AnchorRefused) as caught:
                open_anchor(target)
        finally:
            os.supports_dir_fd.discard(os.open)
            os.open, os.lstat = real_open, real_lstat
            for name, value in removed.items():
                setattr(os, name, value)
        self.assertEqual("platform_unsupported", caught.exception.cause)
        self.assertEqual([], calls)
        self.assertIn("Run the command on Linux or macOS.", caught.exception.way_out)

    def test_the_refusal_case_holds_alone_in_a_fresh_interpreter(self) -> None:
        # As on Windows, where every case before it skips: nothing has asked for the temporary folder yet.
        with tempfile.TemporaryDirectory() as tmp:
            case = ("WindowsRefusalTests."
                    "test_without_o_nofollow_and_o_directory_open_anchor_refuses_platform_unsupported_before_any_open")
            done = _run_alone(case, tmp)
            self.assertEqual(0, done.returncode, done.stderr[-3000:])


@unittest.skipUnless(POSIX and os.path.isdir("/proc/self/fd"), "counts descriptors in /proc/self/fd (Linux)")
class ThreadTests(unittest.TestCase):
    def test_two_threads_each_with_its_own_anchor_on_one_tree_leave_no_error_and_no_stray_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "tree", "shared").mkdir(parents=True, mode=0o700)
            errors: list = []
            before = _open_descriptors()

            def work(n: int) -> None:
                try:
                    for index in range(THREAD_ROUNDS):
                        data = f"{n}:{index}".encode()
                        with open_anchor(Path(tmp, "tree")) as tree, tree.ensure_dir("shared") as shared:
                            with shared.ensure_dir(f"own{n}") as own:
                                own.write_atomic("record", data)
                                if own.read("record", 64) != data:
                                    errors.append((n, index, "read back differs"))
                                own.rename("record", shared, f"moved{n}")
                            shared.entries()
                            shared.remove(f"moved{n}")
                except Exception as exc:  # noqa: BLE001  every error is the finding
                    errors.append((n, repr(exc)))

            threads = [threading.Thread(target=work, args=(n,), name=f"anchor-{n}") for n in (1, 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            after = _open_descriptors()
            self.assertEqual([], errors)
            self.assertEqual(before, after)
            self.assertEqual(["own1", "own2"], sorted(os.listdir(Path(tmp, "tree", "shared"))))


@unittest.skipUnless(POSIX, NOT_POSIX)
class SharedAnchorTests(unittest.TestCase):
    def test_one_anchor_used_by_four_threads_lists_the_whole_folder_every_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp, "full")
            folder.mkdir(mode=0o700)
            for index in range(40):
                folder.joinpath(f"e{index:02d}").write_bytes(b"")
            want = [(f"e{index:02d}", "file") for index in range(40)]
            wrong: list = []
            start = threading.Barrier(4)

            def lister(anchor: Anchor) -> None:
                start.wait()
                for _ in range(THREAD_ROUNDS):
                    got = anchor.entries()
                    if got != want:
                        wrong.append(len(got))

            with open_anchor(folder) as anchor:
                threads = [threading.Thread(target=lister, args=(anchor,), name=f"lister-{n}") for n in range(4)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                self.assertEqual(want, anchor.entries())
            self.assertEqual([], wrong[:20], f"{len(wrong)} of {4 * THREAD_ROUNDS} listings were not the folder")


@unittest.skipUnless(POSIX and os.path.isdir("/proc/self/fd"), "counts descriptors in /proc/self/fd (Linux)")
class ChainedFormTests(unittest.TestCase):
    def test_the_chained_form_leaves_no_descriptor_open_on_success_or_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp, "home")
            home.joinpath("a", "b").mkdir(parents=True, mode=0o700)
            os.symlink(Path(tmp), home / "a" / "link")
            with open_anchor(home) as anchor:
                before = _open_descriptors()
                for _ in range(100):
                    anchor.child("a").child("b").release()
                    try:
                        anchor.child("a").child("link")
                    except AnchorRefused:
                        pass
                gc.collect()
                self.assertEqual(before, _open_descriptors())


class _CountingOs:
    """A stand-in for the module's ``os``: each close is counted by its number, then done for real."""

    def __init__(self) -> None:
        self.closed: list = []

    def __getattr__(self, name: str):
        return getattr(os, name)

    def close(self, fd: int) -> None:
        self.closed.append(fd)
        os.close(fd)


class _Widened(Anchor):
    """A test-only stand-in: the held descriptor is read slowly, so a second release would land between
    a release's read of it and its clear (the window a free race has, made wide enough to hit)."""

    @property
    def _fd(self):
        value = self.__dict__.get("_held")
        time.sleep(0.0002)
        return value

    @_fd.setter
    def _fd(self, value) -> None:
        self.__dict__["_held"] = value


@unittest.skipUnless(POSIX, NOT_POSIX)
class ReleaseTests(unittest.TestCase):
    ROUNDS = 40

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name, "held")
        self.folder.mkdir(mode=0o700)

    def test_two_threads_releasing_one_anchor_at_the_same_moment_shut_its_descriptor_once(self) -> None:
        counting, flags, errors, counts, hung = _CountingOs(), _posix._walk_flags(), [], [], 0

        def releaser(anchor: Anchor, start: threading.Barrier) -> None:
            try:
                start.wait()
                anchor.release()
            except Exception as exc:  # noqa: BLE001  every error is the finding
                errors.append(repr(exc))

        with mock.patch.object(_posix, "os", counting):
            for _ in range(self.ROUNDS):
                fd = os.open(self.folder, flags[0])
                anchor = _Widened(fd, str(self.folder), flags, None)
                start = threading.Barrier(2, timeout=10)
                del counting.closed[:]
                threads = [threading.Thread(target=releaser, args=(anchor, start), name=f"releaser-{n}", daemon=True)
                           for n in (1, 2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                if any(thread.is_alive() for thread in threads):
                    hung += 1
                    break
                counts.append(counting.closed.count(fd))
        self.assertEqual(0, hung)
        self.assertEqual([], errors[:5], f"{len(errors)} errors in {self.ROUNDS} rounds")
        self.assertEqual({1}, set(counts), f"{sum(n > 1 for n in counts)} of {len(counts)} rounds shut it twice")

    def test_a_second_release_in_one_thread_is_harmless_and_shuts_nothing_more(self) -> None:
        counting = _CountingOs()
        with mock.patch.object(_posix, "os", counting):
            anchor = open_anchor(self.folder)
            fd = anchor.fd
            anchor.release()
            anchor.release()
        self.assertEqual([fd], counting.closed)


@unittest.skipUnless(POSIX, NOT_POSIX)
class CopyTests(unittest.TestCase):
    def test_an_anchor_refuses_copy_deepcopy_and_pickle_and_keeps_its_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp, "held")
            folder.mkdir(mode=0o700)
            folder.joinpath("entry").write_bytes(b"")
            made, refused = [], []
            with open_anchor(folder) as anchor:
                for how, call in (("copy", copy.copy), ("deepcopy", copy.deepcopy),
                                  ("pickle", lambda a: pickle.loads(pickle.dumps(a)))):
                    try:
                        other = call(anchor)
                    except TypeError as exc:
                        refused.append((how, str(exc)))
                    else:
                        other.__dict__["_fd"] = None  # a copy shares the number: let it go without shutting it
                        made.append(how)
                self.assertEqual([("entry", "file")], anchor.entries())
            self.assertEqual([], made)
            self.assertEqual(["copy", "deepcopy", "pickle"], [how for how, _ in refused])
            for _, text in refused:
                self.assertIn("cannot be copied or pickled", text)


if __name__ == "__main__":
    unittest.main()
