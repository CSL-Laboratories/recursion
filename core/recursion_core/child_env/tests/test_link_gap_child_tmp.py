"""Can a child's temporary root and the home check be made to read or write through a link? (place 12 of 12)

The place, on master ba82fb92: ``child_env/policy.py`` ``_resolve`` :620-673 (lstat :653), ``_owned_chain``
:1227-1245, ``_private_dir`` :1267-1291, ``_make_route_tmp`` :1302-1317 and ``_route_tmp`` :1320-1334.
``_resolve``, ``_owned_chain`` and ``_private_dir`` only decide (lstat and readlink, no file opened);
``_make_route_tmp`` WRITES a new temporary root for engine children, and ``_route_tmp`` keeps or remakes it.

Each test makes a real link inside its own temporary folder, to an ``outside`` folder that is also inside it, and
checks that no byte was read from and no entry was made, changed or removed in ``outside``, and that a path the
place hands back stays out of ``outside``. A red test is a gap on master, marked as an expected failure with its
reason.
"""
import builtins
import contextlib
import hashlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# The same small helper stands in each of the twelve link-gap modules, so that no shared file is added.
POSIX = os.name != "nt" and all(hasattr(os, name) for name in ("symlink", "O_NOFOLLOW", "geteuid", "getuid"))
POSIX_ONLY = "links, uids and O_NOFOLLOW are POSIX: on Windows this place refuses before it uses a path"


def _within(path, root):
    path, root = os.fspath(path), os.fspath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def attempt(call):
    """Run the place. Any exception is its refusal; the test then reads only what reached ``outside``."""
    try:
        return call(), None
    except Exception as exc:  # noqa: BLE001 - a later refusal of any type turns a red test green
        return None, exc


class Box:
    """One temporary folder per test: ``inside`` is where the place is pointed, ``outside`` is the only link
    target, and HOME, XDG_CONFIG_HOME, XDG_STATE_HOME, XDG_RUNTIME_DIR and TMPDIR are folders inside it too."""

    def __init__(self, test):
        holder = tempfile.TemporaryDirectory()
        test.addCleanup(holder.cleanup)
        self.root = Path(os.path.realpath(holder.name))
        os.chmod(self.root, 0o700)
        self.inside, self.outside = self.root / "inside", self.root / "outside"
        env = {}
        for key, name in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"),
                          ("XDG_RUNTIME_DIR", "runtime"), ("TMPDIR", "tmp")):
            env[key] = str(self.folder(self.root / name))
        self.folder(self.inside)
        self.folder(self.outside)
        for patcher in (mock.patch.dict(os.environ, env), mock.patch.object(tempfile, "tempdir", env["TMPDIR"])):
            patcher.start()
            test.addCleanup(patcher.stop)
        self.opened = []

    def _mine(self, path):
        if not _within(path, self.root):
            raise AssertionError(f"{path} is not inside the test's own folder")
        return Path(path)

    def folder(self, path, mode=0o700):
        path = self._mine(path)
        os.mkdir(path)
        os.chmod(path, mode)
        return path

    def file(self, path, data=b"outside bytes 7f3a\n", mode=0o600):
        path = self._mine(path)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(path, mode)
        return path

    def link(self, at, to):
        self._mine(to)
        os.symlink(to, self._mine(at))
        return Path(at)

    def replace_with_link(self, folder, to):
        folder = self._mine(folder)
        os.rename(folder, folder.with_name(folder.name + ".moved"))
        self.link(folder, to)

    def snapshot(self):
        """Every entry of ``outside``: its type, size, time and, for a file, the hash of its bytes."""
        seen = {".": os.lstat(self.outside).st_mtime_ns}
        for top, dirs, files in os.walk(self.outside):
            for name in dirs + files:
                path = os.path.join(top, name)
                info = os.lstat(path)
                body = None
                if stat.S_ISREG(info.st_mode):
                    with open(path, "rb") as handle:
                        body = hashlib.sha256(handle.read()).hexdigest()
                seen[os.path.relpath(path, self.outside)] = (stat.S_IFMT(info.st_mode), info.st_size,
                                                              info.st_mtime_ns, body)
        return seen

    @contextlib.contextmanager
    def watch(self):
        """Record each path the place opened or listed with success; after the call, keep those that lead
        into ``outside``. An open that a link refused read nothing and is not recorded."""
        calls = []
        os_open, listdir, scandir, plain_open = os.open, os.listdir, os.scandir, builtins.open

        def where(path, dir_fd=None):
            if isinstance(path, int):
                return None
            path = os.fsdecode(os.fspath(path))
            if dir_fd is not None and not os.path.isabs(path):
                path = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), path)
            return path

        def opening(path, flags, mode=0o777, *, dir_fd=None):
            fd = os_open(path, flags, mode, dir_fd=dir_fd)
            calls.append(where(path, dir_fd))
            return fd

        def listing(path="."):
            names = listdir(path)
            calls.append(where(path))
            return names

        def scanning(path="."):
            entries = scandir(path)
            calls.append(where(path))
            return entries

        def reading(file, *args, **kwargs):
            handle = plain_open(file, *args, **kwargs)
            calls.append(where(file))
            return handle

        with contextlib.ExitStack() as stack:
            for name, owner, stand_in in (("open", os, opening), ("listdir", os, listing), ("scandir", os, scanning),
                                          ("open", builtins, reading), ("open", io, reading)):
                stack.enter_context(mock.patch.object(owner, name, stand_in))
            try:
                yield
            finally:
                stack.close()
                out = os.path.realpath(self.outside)
                self.opened = sorted({path for path in calls if path and _within(os.path.realpath(path), out)})

    @contextlib.contextmanager
    def swap(self, owner, name, path, folder, to, *, before=False):
        """``owner.name`` runs as usual; at its first call on ``path`` (after that call returns, or just before it
        with ``before``) ``folder`` is replaced by a link to ``to``. The same point on every run: no thread, no timer."""
        real, fired, path = getattr(owner, name), [], os.fspath(path)

        def wrapped(*args, **kwargs):
            hit = not fired and args and not isinstance(args[0], int) and os.fspath(args[0]) == path
            if hit and before:
                fired.append(name)
                self.replace_with_link(folder, to)
            result = real(*args, **kwargs)
            if hit and not before:
                fired.append(name)
                self.replace_with_link(folder, to)
            return result

        with mock.patch.object(owner, name, wrapped):
            yield fired

    def assert_untouched(self, test, before, place):
        after = self.snapshot()
        made = sorted(set(after) - set(before))
        gone = sorted(set(before) - set(after))
        changed = sorted(name for name in set(before) & set(after) if before[name] != after[name])
        test.assertEqual(([], [], []), (made, changed, gone),
                         f"{place} wrote through the link: made {made}, changed {changed}, removed {gone}")
        test.assertEqual([], self.opened, f"{place} read through the link: opened {self.opened}")

from recursion_core.child_env import policy as place


@unittest.skipUnless(POSIX, POSIX_ONLY)
class ChildTempRootLinkTests(unittest.TestCase):
    def fresh(self, tmpdir):
        """No temporary root cached for this process, and the process temp folder set to ``tmpdir``."""
        for patcher in (mock.patch.dict(place._TMP, clear=True), mock.patch.object(tempfile, "tempdir", tmpdir)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_stays_inside(self, box, path):
        self.assertFalse(path is not None and _within(os.path.realpath(path), box.outside),
                         f"the place handed back {path}, which is in the outside folder")

    @unittest.expectedFailure
    def test_i_a_link_at_the_temp_folder_is_resolved_and_used(self):
        """Expected failure: _make_route_tmp writes through a link at the process temp folder (the last part): its realpath is taken and a recursion-child-tmp folder is made in the outside folder."""
        box = Box(self)
        self.fresh(os.fspath(box.link(box.inside / "tmp", box.outside)))
        before = box.snapshot()
        with box.watch():
            path, error = attempt(place._make_route_tmp)
        box.assert_untouched(self, before, "_make_route_tmp")
        self.assert_stays_inside(box, path)

    @unittest.expectedFailure
    def test_ii_a_link_above_the_temp_folder_is_resolved_and_used(self):
        """Expected failure: _make_route_tmp writes through a link at a middle part above the process temp folder: a recursion-child-tmp folder is made in the outside folder."""
        box = Box(self)
        box.folder(box.outside / "tmp")
        middle = box.link(box.inside / "mid", box.outside)
        self.fresh(os.fspath(middle / "tmp"))
        before = box.snapshot()
        with box.watch():
            path, error = attempt(place._make_route_tmp)
        box.assert_untouched(self, before, "_make_route_tmp")
        self.assert_stays_inside(box, path)

    def test_iii_the_cached_root_replaced_after_its_lstat_is_remade(self):
        box = Box(self)
        self.fresh(os.fspath(box.folder(box.inside / "tmp")))
        cached = box.folder(box.inside / "root")
        place._TMP[os.getpid()] = os.fspath(cached)
        target = box.folder(box.outside / "root")
        before = box.snapshot()
        with box.watch(), box.swap(os, "lstat", os.fspath(cached), cached, target) as fired:
            path, error = attempt(place._route_tmp)
        self.assertTrue(fired)
        box.assert_untouched(self, before, "_route_tmp")
        self.assert_stays_inside(box, path)
        self.assertIsNone(error)
        self.assertNotEqual(os.fspath(cached), path)


@unittest.skipUnless(POSIX, POSIX_ONLY)
class PrivateDirLinkTests(unittest.TestCase):
    def test_i_a_link_at_the_home_is_refused(self):
        box = Box(self)
        home = box.link(box.inside / "home", box.folder(box.outside / "home"))
        before = box.snapshot()
        with box.watch():
            verdict, error = attempt(lambda: place._private_dir(os.fspath(home)))
        box.assert_untouched(self, before, "_private_dir")
        self.assertEqual((False, None), (verdict, error))


if __name__ == "__main__":
    unittest.main()
