"""The held-folder module: an anchor that holds a folder and works beneath it without following a link.

Every case uses real folders, real links and real threads in one temporary folder it makes itself. The
swap case reuses the system facts' own swap (``test_system_facts``: ``renameat2`` with
``RENAME_EXCHANGE`` from a second thread). The POSIX cases skip on Windows with the reason; the refusal
case runs everywhere and, on a POSIX host, takes ``O_NOFOLLOW`` and ``O_DIRECTORY`` out of ``os`` for
its own duration, as a Windows interpreter has them out.
"""

from __future__ import annotations

import os
import shlex
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from recursion_core.anchored import ABOVE, CAUSES, Anchor, AnchorRefused, open_anchor
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
            shared = Path(tmp, "shared")
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
                open_anchor(os.path.abspath(tempfile.gettempdir()))
        finally:
            os.supports_dir_fd.discard(os.open)
            os.open, os.lstat = real_open, real_lstat
            for name, value in removed.items():
                setattr(os, name, value)
        self.assertEqual("platform_unsupported", caught.exception.cause)
        self.assertEqual([], calls)
        self.assertIn("Run the command on Linux or macOS.", caught.exception.way_out)


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


if __name__ == "__main__":
    unittest.main()
