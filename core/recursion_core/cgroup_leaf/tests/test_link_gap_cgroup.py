"""Can the cgroup leaf's kill, remove and adopt be made to write through a link? (place 11 of 12)

The place, on master ba82fb92: ``cgroup_leaf/__init__.py`` ``Cgroup2`` :91-133 (realpath of the mount :95,
cgroup.procs :101, cgroup.kill :104, remove :128-133) and ``DelegatedRoot`` :136-191 (the root check :147-157,
launch's leaf check :172, adopt's leaf check :189). It WRITES: cgroup.kill, cgroup.procs, and it removes
sub-cgroup folders.

STAND-IN: the cgroup mount is a plain folder inside the test's own temporary folder (``Cgroup2(mount=...)``), its
cgroup.procs and cgroup.kill are plain files; no real cgroup is read or written, no process is launched or killed.

Each test makes a real link inside its own temporary folder, to an ``outside`` folder that is also inside it, and
checks that no byte was read from and no entry was made, changed or removed in ``outside``. A red test is a gap on
master, marked as an expected failure with its reason.
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

from recursion_core.cgroup_leaf import Cgroup2, DelegatedRoot

LINUX = POSIX and all(hasattr(os, name) for name in ("pidfd_open", "pipe2", "O_CLOEXEC"))


def stand_in_cgroup(box, path):
    """A plain folder with plain cgroup.procs and cgroup.kill files, standing in for a cgroup."""
    box.folder(path, 0o755)
    box.file(path / "cgroup.procs", b"", 0o644)
    box.file(path / "cgroup.kill", b"0", 0o644)
    return path


@unittest.skipUnless(LINUX, POSIX_ONLY)
class CgroupLinkTests(unittest.TestCase):
    def mount(self, box):
        mount = box.folder(box.inside / "mount", 0o755)
        return Cgroup2(mount=mount), stand_in_cgroup(box, mount / "delegated")

    def test_i_a_link_at_cgroup_kill_is_refused(self):
        box = Box(self)
        box.file(box.outside / "cgroup.kill", b"0")
        fs, root = self.mount(box)
        leaf = box.folder(root / "run-1", 0o755)
        box.link(leaf / "cgroup.kill", box.outside / "cgroup.kill")
        before = box.snapshot()
        with box.watch():
            result, error = attempt(lambda: fs.kill(leaf))
        box.assert_untouched(self, before, "Cgroup2.kill")
        self.assertIsInstance(error, OSError)

    def test_i_a_link_at_the_delegated_root_is_refused(self):
        box = Box(self)
        fs, root = self.mount(box)
        stand_in_cgroup(box, box.outside / "delegated")
        linked = box.link(root.parent / "linked", box.outside / "delegated")
        before = box.snapshot()
        with box.watch():
            result, error = attempt(lambda: DelegatedRoot(linked, fs))
        box.assert_untouched(self, before, "DelegatedRoot")
        self.assertIsInstance(error, PermissionError)

    @unittest.expectedFailure
    def test_i_a_link_at_the_leaf_is_walked_by_remove(self):
        """Expected failure: Cgroup2.remove writes through a link at the leaf (the last part): os.walk enters the linked folder and its sub-folder in the outside folder is removed."""
        box = Box(self)
        box.folder(box.outside / "run-1")
        box.folder(box.outside / "run-1" / "child")
        fs, root = self.mount(box)
        leaf = box.link(root / "run-1", box.outside / "run-1")
        before = box.snapshot()
        with box.watch():
            attempt(lambda: fs.remove(leaf))
        box.assert_untouched(self, before, "Cgroup2.remove")

    @unittest.expectedFailure
    def test_ii_a_link_at_the_leaf_is_followed_by_kill(self):
        """Expected failure: Cgroup2.kill writes through a link at the leaf (a middle part of cgroup.kill's path): the byte 1 is written into cgroup.kill in the outside folder."""
        box = Box(self)
        stand_in_cgroup(box, box.outside / "run-1")
        fs, root = self.mount(box)
        leaf = box.link(root / "run-1", box.outside / "run-1")
        before = box.snapshot()
        with box.watch():
            attempt(lambda: fs.kill(leaf))
        box.assert_untouched(self, before, "Cgroup2.kill")

    @unittest.expectedFailure
    def test_iii_the_leaf_replaced_during_adopt_is_killed_through_the_link(self):
        """Expected failure: DelegatedRoot.adopt passes a leaf replaced by a link after its first lstat, and Cgroup2.kill then writes the byte 1 into cgroup.kill in the outside folder."""
        box = Box(self)
        fs, root = self.mount(box)
        leaf = stand_in_cgroup(box, root / "run-1")
        target = stand_in_cgroup(box, box.outside / "run-1")
        delegated = DelegatedRoot(root, fs)
        before = box.snapshot()
        with box.watch():
            with box.swap(os, "lstat", leaf, leaf, target):
                run, error = attempt(lambda: delegated.adopt("run-1"))
            if error is None:
                attempt(lambda: run.fs.kill(run.leaf))  # the adopted run's own fs and leaf
        box.assert_untouched(self, before, "DelegatedRoot.adopt and Cgroup2.kill")


if __name__ == "__main__":
    unittest.main()
