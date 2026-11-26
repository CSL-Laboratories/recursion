"""The record of revocations under more than one writer, and writers killed part way (lane REMOTE).

These pin what ``FileRevocationStore`` takes from the functions it shares with the credential store
(``owner_file_lock``, ``replace_file``, ``sweep_temps``): two writers on one folder lose no record; a read never
makes a write fail; a writer killed before its rename, or in the middle of writing, leaves the record as it was
and stops no later writer; the file is written beside, synced, renamed, then its folder synced; a lock others may
write is refused. Each test makes its own temporary state folder. A writer process is this interpreter (with its
-X options) on a short program; each is bounded by ``CHILD_TIMEOUT_S`` and at most two run at once.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import recursion_core
from recursion_core.control import credentials
from recursion_core.remote.device_state import REVOKED_CONNECTION_HELD, DeviceBook
from recursion_core.remote.pairing import PairingGrant, PairingRefused
from recursion_core.remote.revocation_store import FileRevocationStore
from recursion_core.remote.tests import support

# How many devices each writer revokes (two connections each). At 150 each writer's run spans the other's
# whole run, so that with the lock taken out the two read-modify-writes interleave throughout (half the
# records were lost in every run measured so); a test value, not a setting.
WRITER_DEVICES = 150
# The longest a writer process may run, in seconds, before the test ends it and fails.
CHILD_TIMEOUT_S = 120.0
# The longest the test waits for a writer process to be ready, in seconds.
READY_TIMEOUT_S = 60.0

_PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(recursion_core.__file__)))

# A writer: one book on its own store of the folder; waits for the go file, then pairs, connects twice and
# revokes each of its devices; prints "done" when every revoke returned.
_WRITER = """
import os, sys, time
from recursion_core.remote.device_state import DeviceBook
from recursion_core.remote.pairing import PairingGrant
from recursion_core.remote.revocation_store import FileRevocationStore
state, name, count, go = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
book = DeviceBook(store=FileRevocationStore(state))
open(go + "." + name, "w").close()
while not os.path.exists(go):
    time.sleep(0.001)
for i in range(count):
    device = f"{name}{i}"
    book.add(PairingGrant("off1", device, "phone", ("read",), 0.0), expires_at=time.monotonic() + 3600)
    assert book.connect(device, f"{device}.0").ok and book.connect(device, f"{device}.1").ok
    book.revoke(device)
print("done")
"""

# A writer killed at a step of its write: the book pairs and revokes "kept" (acknowledged), then pairs
# "lost" and is killed during that revoke's write: just before the rename ("rename"), or after writing half
# the new bytes ("half").
_KILLED = """
import os, signal, sys, time
from recursion_core.control import credentials
from recursion_core.remote.device_state import DeviceBook
from recursion_core.remote.pairing import PairingGrant
from recursion_core.remote.revocation_store import FileRevocationStore
state, where = sys.argv[1], sys.argv[2]
book = DeviceBook(store=FileRevocationStore(state))
for device in ("kept", "lost"):
    book.add(PairingGrant("off1", device, "phone", ("read",), 0.0), expires_at=time.monotonic() + 3600)
    assert book.connect(device, device + ".c").ok
book.revoke("kept")
def die(*args, **kw):
    os.kill(os.getpid(), signal.SIGKILL)
real_fdopen = os.fdopen
class Half:
    def __init__(self, handle):
        self.handle = handle
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        self.handle.close()
    def write(self, data):
        self.handle.write(data[: len(data) // 2])
        self.handle.flush()
        die()
    def flush(self):
        pass
    def fileno(self):
        return self.handle.fileno()
if where == "rename":
    credentials.os.replace = die
else:
    credentials.os.fdopen = lambda fd, mode="r", *a, **k: Half(real_fdopen(fd, mode)) if "w" in mode else real_fdopen(fd, mode, *a, **k)
book.revoke("lost")
print("not killed")
"""


def _interpreter() -> list[str]:
    """This interpreter with its -X options (the free-threaded build's gil=0 among them)."""
    args = [sys.executable]
    for key, value in sys._xoptions.items():
        args += ["-X", key if value is True else f"{key}={value}"]
    return args


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = _PACKAGE_ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def grant(device_id: str) -> PairingGrant:
    return PairingGrant("off1", device_id, "phone", ("read",), 0.0)


class Folder(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = os.path.join(self._tmp.name, "state")
        self.clock = support.FakeClock()
        self.wall = support.FakeClock()
        self.wall.now = 1_000_000.0
        self.store = FileRevocationStore(self.state)
        self.folder = os.path.dirname(self.store.path)

    def book(self) -> DeviceBook:
        return DeviceBook(clock=self.clock, wall=self.wall, store=FileRevocationStore(self.state))

    def temps(self) -> list[str]:
        return [n for n in os.listdir(self.folder) if n.endswith(".tmp")]

    def spawn(self, program: str, *args: str) -> subprocess.Popen:
        child = subprocess.Popen(_interpreter() + ["-c", program, *args], env=_env(), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)

        def end() -> None:
            if child.poll() is None:
                child.kill()
            child.communicate()
        self.addCleanup(end)
        return child

    def finish(self, child: subprocess.Popen) -> tuple[int, str, str]:
        try:
            out, err = child.communicate(timeout=CHILD_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            child.kill()
            out, err = child.communicate()
            self.fail(f"a writer ran past {CHILD_TIMEOUT_S} s")
        return child.returncode, out, err[-2000:]

    def writers(self, names: tuple[str, ...]) -> list[subprocess.Popen]:
        """Writer processes, each ready on its own book, then let go together."""
        go = os.path.join(self._tmp.name, "go")
        children = [self.spawn(_WRITER, self.state, name, str(WRITER_DEVICES), go) for name in names]
        deadline = time.monotonic() + READY_TIMEOUT_S
        while not all(os.path.exists(f"{go}.{name}") for name in names):
            for child in children:
                if child.poll() is not None:
                    self.fail(f"a writer ended before it was ready: {self.finish(child)}")
            if time.monotonic() > deadline:
                self.fail("the writers were not ready in time")
            time.sleep(0.01)
        open(go, "w").close()
        return children

    def assert_every_record(self, names: tuple[str, ...]) -> None:
        held = FileRevocationStore(self.state).load()
        want = {f"{name}{i}" for name in names for i in range(WRITER_DEVICES)}
        self.assertEqual(len(set(held.devices) & want), len(want), "revoked devices lost")
        conns = {f"{d}.{k}" for d in want for k in (0, 1)}
        self.assertEqual(len(set(held.connections) & conns), len(conns), "revoked connections lost")


class TwoWritersTests(Folder):
    def test_two_writer_processes_on_one_folder_lose_no_record(self) -> None:
        names = ("p", "q")
        for child in self.writers(names):
            code, out, err = self.finish(child)
            self.assertEqual((code, out.strip()), (0, "done"), err)
        self.assert_every_record(names)
        after = self.book()
        after.add(grant("other"), expires_at=self.clock.now + 3600)
        for conn in ("p0.0", f"q{WRITER_DEVICES - 1}.1"):
            self.assertEqual(after.connect("other", conn).detail, REVOKED_CONNECTION_HELD)

    def test_two_writer_threads_on_two_stores_of_one_folder_lose_no_record(self) -> None:
        names = ("s", "t")
        books = [DeviceBook(store=FileRevocationStore(self.state)) for _ in names]
        start = threading.Barrier(len(names))
        failures: list = []

        def run(book: DeviceBook, name: str) -> None:
            try:
                start.wait()
                for i in range(WRITER_DEVICES):
                    device = f"{name}{i}"
                    book.add(grant(device), expires_at=time.monotonic() + 3600)
                    book.connect(device, f"{device}.0")
                    book.connect(device, f"{device}.1")
                    book.revoke(device)
            except BaseException as exc:   # the test reads it below
                failures.append(repr(exc))

        threads = [threading.Thread(target=run, args=(b, n)) for b, n in zip(books, names)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(CHILD_TIMEOUT_S)
        self.assertEqual(failures, [])
        self.assert_every_record(names)

    def test_reads_while_a_writer_writes_never_make_a_write_fail(self) -> None:
        """A read takes the lock too: its sweep of a killed writer's temporary files must never meet a live
        writer's file."""
        names = ("r",)
        (child,) = self.writers(names)
        loads, refused = 0, []
        while child.poll() is None:
            try:
                FileRevocationStore(self.state).load()
                loads += 1
            except Exception as exc:   # the test reads it below
                refused.append(repr(exc))
        code, out, err = self.finish(child)
        self.assertEqual((code, out.strip()), (0, "done"), err)
        self.assertEqual(refused, [])
        self.assertGreater(loads, 0)
        self.assert_every_record(names)


class KilledWriterTests(Folder):
    def killed(self, where: str) -> None:
        code, out, err = self.finish(self.spawn(_KILLED, self.state, where))
        self.assertEqual(code, -9, (out, err))

    def assert_kept_held(self, book: DeviceBook) -> None:
        with self.assertRaises(PairingRefused) as caught:
            book.add(grant("kept"), expires_at=self.clock.now + 3600)
        self.assertEqual(caught.exception.code, "device_exists")
        book.add(grant("other"), expires_at=self.clock.now + 3600)
        self.assertEqual(book.connect("other", "kept.c").detail, REVOKED_CONNECTION_HELD)

    def test_a_writer_killed_before_its_rename_stops_no_later_writer_and_its_file_is_swept(self) -> None:
        running = self.book()                     # a writer already started, which sweeps no more
        self.killed("rename")
        self.assertEqual(len(self.temps()), 1)    # the killed writer's file, left beside the record
        running.add(grant("x"), expires_at=self.clock.now + 3600)
        running.connect("x", "x.c")
        self.assertEqual(running.revoke("x"), ("x.c",))
        after = self.book()
        self.assertEqual(self.temps(), [])
        self.assert_kept_held(after)
        with self.assertRaises(PairingRefused):
            after.add(grant("x"), expires_at=self.clock.now + 3600)

    def test_a_writer_killed_in_the_middle_of_its_write_leaves_the_record_as_it_was(self) -> None:
        self.killed("half")
        after = self.book()
        self.assertTrue(after.status()["revocations_readable"], after.status()["revocations_unreadable"])
        self.assert_kept_held(after)


class WriteStepsTests(Folder):
    def test_the_record_is_written_beside_synced_renamed_then_its_folder_synced(self) -> None:
        book = self.book()
        for device in ("a", "b"):
            book.add(grant(device), expires_at=self.clock.now + 3600)
            book.connect(device, f"{device}.c")
        book.revoke("a")                          # the first revocation writes the mark; this test reads the next
        steps: list = []
        real_open, real_fsync, real_replace = os.open, os.fsync, os.replace

        def opened(path, flags, *args, **kw):
            steps.append(("open", os.fspath(path)))
            return real_open(path, flags, *args, **kw)

        def synced(fd):
            steps.append(("fsync",))
            return real_fsync(fd)

        def replaced(src, dst):
            steps.append(("replace", os.fspath(src), os.fspath(dst)))
            return real_replace(src, dst)

        with mock.patch.object(credentials.os, "open", opened), mock.patch.object(credentials.os, "fsync", synced), \
                mock.patch.object(credentials.os, "replace", replaced):
            book.revoke("b")
        renames = [i for i, s in enumerate(steps) if s[0] == "replace" and s[2] == self.store.path]
        self.assertEqual(len(renames), 1, steps)
        at = renames[0]
        temp = steps[at][1]
        self.assertNotEqual(temp, self.store.path)
        self.assertEqual(os.path.dirname(temp), self.folder)
        self.assertTrue(temp.endswith(".tmp"))
        made = steps.index(("open", temp))
        self.assertIn(("fsync",), steps[made + 1:at], "the new file is synced before the rename")
        self.assertEqual(steps[at + 1:at + 3], [("open", self.folder), ("fsync",)], "then its folder is synced")
        self.assertEqual(self.temps(), [])

    def test_a_lock_others_may_write_is_refused(self) -> None:
        self.book()
        lock = os.path.join(self.folder, ".revocations.json.lock")
        os.chmod(lock, 0o606)
        after = self.book()
        decision = after.connect("b", "c")
        self.assertEqual(decision.code, "revocations_unreadable")
        self.assertIn("lock is not an owner-only file", decision.detail)


if __name__ == "__main__":
    unittest.main()
