"""Can the control adapter's route file, run results and device door be made to read or write through a link?
(place 7 of 12)

The place, on master ba82fb92: ``daemon/control_adapter.py`` ``_open_regular`` :325-341, ``_clear_result_temp``
:344-360, ``_discard_opened_result_temp`` :363-379, ``load_routes`` :440-457 (lstat :451, the read :456),
``RouteStarter._record_result`` :759-783 (the open :770), ``door_directory`` :1144-1160 and ``DeviceDoor``
:1163-1220 (the token file's open :1187). It READS the route file and WRITES a run's result.json and the device
token file.

STAND-IN: ``_record_result`` is called on a plain stand-in for the starter that holds only the state folder (inside
the test's own temporary folder), a lock and the in-memory results; no daemon, socket or unit runs.

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

import json
import threading
from types import SimpleNamespace

from recursion_core.daemon import control_adapter as place

ROUTES = json.dumps({"routes": {}}).encode()


@unittest.skipUnless(POSIX and hasattr(os, "pidfd_open"), POSIX_ONLY)
class RoutesFileLinkTests(unittest.TestCase):
    def test_i_a_link_at_the_routes_file_is_refused(self):
        box = Box(self)
        box.file(box.outside / "routes.json", ROUTES)
        folder = box.folder(box.inside / "cfg")
        box.link(folder / "routes.json", box.outside / "routes.json")
        before = box.snapshot()
        with box.watch():
            routes, error = attempt(lambda: place.load_routes(folder / "routes.json"))
        box.assert_untouched(self, before, "load_routes")
        self.assertIsInstance(error, place.RoutesUnreadable)

    @unittest.expectedFailure
    def test_ii_a_link_above_the_routes_file_is_followed(self):
        """Expected failure: load_routes reads through a link at a middle part above the route file: routes.json in the outside folder is read and its routes returned."""
        box = Box(self)
        box.folder(box.outside / "cfg")
        box.file(box.outside / "cfg" / "routes.json", ROUTES)
        middle = box.link(box.inside / "mid", box.outside)
        before = box.snapshot()
        with box.watch():
            attempt(lambda: place.load_routes(middle / "cfg" / "routes.json"))
        box.assert_untouched(self, before, "load_routes")

    @unittest.expectedFailure
    def test_iii_the_routes_folder_replaced_after_the_lstat_is_followed(self):
        """Expected failure: load_routes reads through a link at its folder replaced after its lstat: routes.json in the outside folder is read and its routes returned."""
        box = Box(self)
        folder = box.folder(box.inside / "cfg")
        box.file(folder / "routes.json", ROUTES)
        target = box.folder(box.outside / "cfg")
        box.file(target / "routes.json", ROUTES)
        before = box.snapshot()
        with box.watch(), box.swap(os, "lstat", folder / "routes.json", folder, target):
            attempt(lambda: place.load_routes(folder / "routes.json"))
        box.assert_untouched(self, before, "load_routes")


@unittest.skipUnless(POSIX and hasattr(os, "pidfd_open"), POSIX_ONLY)
class RunResultLinkTests(unittest.TestCase):
    def record(self, state):
        starter = SimpleNamespace(_state=state, _lock=threading.Lock(), results={})
        attempt(lambda: place.RouteStarter._record_result(starter, "run-1", {"code": "ok"}))
        return starter

    def test_i_a_link_at_the_result_file_is_replaced_not_followed(self):
        box = Box(self)
        box.file(box.outside / "result.json", b"outside result\n")
        run = box.folder(box.folder(box.folder(box.inside / "state") / "runs") / "run-1")
        box.link(run / "result.json", box.outside / "result.json")
        before = box.snapshot()
        with box.watch():
            starter = self.record(box.inside / "state")
        box.assert_untouched(self, before, "_record_result")
        self.assertEqual({}, starter.results)  # written, not kept in memory
        self.assertTrue(stat.S_ISREG(os.lstat(run / "result.json").st_mode))

    @unittest.expectedFailure
    def test_ii_a_link_at_the_runs_folder_is_followed(self):
        """Expected failure: _record_result writes through a link at the runs folder (a middle part): result.json is made in the outside folder."""
        box = Box(self)
        box.folder(box.outside / "run-1")
        state = box.folder(box.inside / "state")
        box.link(state / "runs", box.outside)
        before = box.snapshot()
        with box.watch():
            self.record(state)
        box.assert_untouched(self, before, "_record_result")

    @unittest.expectedFailure
    def test_iii_the_run_folder_replaced_after_the_temp_clear_is_followed(self):
        """Expected failure: _record_result writes through a link at the run folder replaced after _clear_result_temp: result.json is made in the outside folder."""
        box = Box(self)
        state = box.folder(box.inside / "state")
        run = box.folder(box.folder(state / "runs") / "run-1")
        target = box.folder(box.outside / "run-1")
        before = box.snapshot()
        with box.watch(), box.swap(place, "_clear_result_temp", run / "result.json.tmp", run, target):
            self.record(state)
        box.assert_untouched(self, before, "_record_result")


@unittest.skipUnless(POSIX and hasattr(os, "pidfd_open"), POSIX_ONLY)
class DeviceDoorLinkTests(unittest.TestCase):
    def door(self, directory):
        door, error = attempt(lambda: place.DeviceDoor(directory))
        if door is not None:
            self.addCleanup(door.close)
        return error

    def test_i_a_link_at_the_door_folder_is_refused(self):
        box = Box(self)
        directory = box.link(box.inside / "door", box.folder(box.outside / "door"))
        before = box.snapshot()
        with box.watch():
            error = self.door(directory)
        box.assert_untouched(self, before, "DeviceDoor")
        self.assertIsInstance(error, PermissionError)

    @unittest.expectedFailure
    def test_ii_a_link_above_an_existing_door_folder_is_followed(self):
        """Expected failure: DeviceDoor writes through a link at a middle part above its existing folder: the device token file gui-device-token is written in the outside folder."""
        box = Box(self)
        box.folder(box.outside / "door")
        middle = box.link(box.inside / "mid", box.outside)
        before = box.snapshot()
        with box.watch():
            self.door(middle / "door")
        box.assert_untouched(self, before, "DeviceDoor")

    @unittest.expectedFailure
    def test_iii_the_door_folder_replaced_after_its_check_is_followed(self):
        """Expected failure: DeviceDoor writes through a link at its folder replaced after door_directory's lstat: the device token file gui-device-token is written in the outside folder."""
        box = Box(self)
        directory = box.folder(box.inside / "door")
        target = box.folder(box.outside / "door")
        before = box.snapshot()
        with box.watch(), box.swap(os, "lstat", directory, directory, target):
            self.door(directory)
        box.assert_untouched(self, before, "DeviceDoor")


if __name__ == "__main__":
    unittest.main()
