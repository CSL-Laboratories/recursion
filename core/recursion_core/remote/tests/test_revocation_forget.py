"""The command that forgets every revocation (lane REMOTE): it says only what it did, and leaves a line that it ran.

Each test makes its own temporary state folder. The locks a running daemon would hold are held here by the test's
own descriptors and books (a flock is held per open file, so the same process stands in for another one).
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import stat
import tempfile
import time
import unittest

from recursion_core.remote import revocation_store
from recursion_core.remote.device_state import DeviceBook
from recursion_core.remote.pairing import PairingGrant
from recursion_core.remote.revocation_store import FileRevocationStore
from recursion_core.remote.tests import support


def grant(device_id: str) -> PairingGrant:
    return PairingGrant("off1", device_id, "phone", ("read",), 0.0)


class Folder(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = os.path.join(self._tmp.name, "state")
        self.clock = support.FakeClock()
        self.path = FileRevocationStore(self.state).path
        self.folder = os.path.dirname(self.path)
        self.lines = os.path.join(self.folder, revocation_store.FORGOTTEN_FILE)

    def revoked_two(self) -> None:
        book = DeviceBook(clock=self.clock, store=FileRevocationStore(self.state))
        for device_id, conn_id in (("dev_a", "a1"), ("dev_b", "b1")):
            book.add(grant(device_id), expires_at=self.clock.now + 3600)
            book.connect(device_id, conn_id)
            book.revoke(device_id)

    def command(self, state: str | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = revocation_store.main(["--state-dir", state or self.state, revocation_store.COMMAND_FORGET])
        return code, out.getvalue(), err.getvalue()

    def hold(self, name: str) -> int:
        fd = os.open(os.path.join(self.folder, name), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd


class MistypedFolderTests(Folder):
    def test_a_state_folder_that_does_not_exist_is_refused_and_nothing_is_made(self) -> None:
        missing = os.path.join(self._tmp.name, "stat")
        code, out, err = self.command(missing)
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        for words in (f"no state folder at {missing}", "nothing was forgotten"):
            self.assertIn(words, err)
        self.assertFalse(os.path.lexists(missing))

    def test_a_state_folder_without_the_relay_folder_is_refused_and_nothing_is_made(self) -> None:
        bare = os.path.join(self._tmp.name, "bare")
        os.mkdir(bare, 0o700)
        code, out, err = self.command(bare)
        self.assertNotEqual(code, 0)
        self.assertIn("nothing was forgotten", err)
        self.assertEqual(os.listdir(bare), [])


class RunningDaemonTests(Folder):
    def test_a_live_book_on_the_record_refuses_the_command(self) -> None:
        self.revoked_two()
        live = DeviceBook(clock=self.clock, store=FileRevocationStore(self.state))
        code, out, err = self.command()
        self.assertNotEqual(code, 0)
        for words in ("nothing was forgotten", "is held by the daemon, another run of this command, or another program", "stop it first"):
            self.assertIn(words, err)
        self.assertEqual(FileRevocationStore(self.state).load().devices.keys(), {"dev_a", "dev_b"})
        del live

    def test_the_record_lock_held_refuses_the_command_at_once(self) -> None:
        self.revoked_two()
        self.hold(".revocations.json.lock")
        started = time.monotonic()
        code, out, err = self.command()
        self.assertLess(time.monotonic() - started, revocation_store.REVOCATIONS_LOCK_TIMEOUT_S / 2)
        self.assertNotEqual(code, 0)
        for words in ("nothing was forgotten", "is held by the daemon, another run of this command, or another program", "stop it first"):
            self.assertIn(words, err)
        self.assertFalse(os.path.lexists(self.lines))

    def test_a_book_gone_lets_the_command_run(self) -> None:
        self.revoked_two()
        live = DeviceBook(clock=self.clock, store=FileRevocationStore(self.state))
        del live
        self.assertEqual(self.command()[0], 0)


class LineTests(Folder):
    def test_the_command_appends_one_line_with_the_time_the_user_and_the_count(self) -> None:
        self.revoked_two()
        before = time.time()
        code, out, err = self.command()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("2 revocations were forgotten", out)
        self.assertIn(self.lines, out)
        self.assertEqual(stat.S_IMODE(os.lstat(self.lines).st_mode), 0o600)
        rows = [json.loads(line) for line in open(self.lines, encoding="utf-8").read().splitlines()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["revocations"], row["connections"], row["uid"]), (2, 2, os.geteuid()))
        self.assertIsInstance(row["user"], str)
        self.assertTrue(row["user"])
        self.assertGreaterEqual(row["at"], before)
        self.assertEqual(self.command()[0], 0)
        rows = [json.loads(line) for line in open(self.lines, encoding="utf-8").read().splitlines()]
        self.assertEqual([r["revocations"] for r in rows], [2, 0])

    def test_a_record_that_cannot_be_read_is_forgotten_and_the_line_says_how_many_is_unknown(self) -> None:
        self.revoked_two()
        with open(self.path, "wb") as handle:
            handle.write(b"{not json")
        code, out, err = self.command()
        self.assertEqual(code, 0)
        row = json.loads(open(self.lines, encoding="utf-8").read().splitlines()[0])
        self.assertIsNone(row["revocations"])
        self.assertIn("not JSON", row["unreadable"])
        self.assertIn("could not be read", out)


if __name__ == "__main__":
    unittest.main()
