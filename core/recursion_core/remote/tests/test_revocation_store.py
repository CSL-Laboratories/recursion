"""The record of revocations across a restart (lane REMOTE): a revoked device's connection ids stay refused.

Each test makes its own temporary state folder; a "restart" is a new ``DeviceBook`` on a new store of the same
folder, with nothing of the first book kept. Clocks are moved by hand (``support.FakeClock``).
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from recursion_core.control import credentials
from recursion_core.remote import revocation_store
from recursion_core.remote.device_state import REVOKED_CONNECTION_HELD, DeviceBook, RevocationNotWritten
from recursion_core.remote.pairing import PairingGrant, PairingRefused
from recursion_core.remote.revocation_store import (FileRevocationStore, Revocations, RevocationStoreError,
                                                    StoredConnection)
from recursion_core.remote.tests import support


def grant(device_id: str, scopes=("read", "run.stop")) -> PairingGrant:
    return PairingGrant("off1", device_id, "phone", tuple(scopes), 0.0)


class MemoryStore:
    """A stand-in store: what ``put`` and ``forget`` were given, and failures on demand."""

    def __init__(self) -> None:
        self.devices: dict = {}
        self.connections: dict = {}
        self.boot = None
        self.fail_load: str | None = None
        self.fail_put: str | None = None
        self.calls: list = []

    def load(self) -> Revocations:
        self.calls.append("load")
        if self.fail_load is not None:
            raise RevocationStoreError(self.fail_load)
        return Revocations(dict(self.devices), dict(self.connections), self.boot)

    def put(self, change: Revocations) -> None:
        self.calls.append("put")
        if self.fail_put is not None:
            raise RevocationStoreError(self.fail_put)
        self.devices.update(change.devices)
        self.connections.update(change.connections)
        if change.boot is not None:
            self.boot = change.boot

    def forget(self, conn_ids) -> None:
        self.calls.append("forget")
        if self.fail_put is not None:
            raise RevocationStoreError(self.fail_put)
        for conn_id in conn_ids:
            self.connections.pop(conn_id, None)


class Folder(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = os.path.join(self._tmp.name, "state")
        self.clock = support.FakeClock()
        self.wall = support.FakeClock()
        self.wall.now = 1_000_000.0

    def book(self, **kw) -> DeviceBook:
        """A new book on a new store of the same folder: what the daemon has after a restart."""
        kw.setdefault("store", FileRevocationStore(self.state))
        return DeviceBook(clock=self.clock, wall=self.wall, **kw)

    def paired(self, book: DeviceBook, device_id: str) -> None:
        book.add(grant(device_id), expires_at=self.clock.now + 3600)


class RestartTests(Folder):
    def test_a_revoked_connection_id_is_refused_for_another_device_after_a_restart(self) -> None:
        first = self.book()
        self.paired(first, "dev_a")
        for conn in ("c", 7, ("fd", 7)):
            self.assertTrue(first.connect("dev_a", conn).ok)
        self.assertEqual(first.revoke("dev_a"), ("c", 7, ("fd", 7)))
        after = self.book()
        self.paired(after, "dev_b")
        for conn in ("c", 7, ("fd", 7)):
            with self.subTest(conn=conn):
                decision = after.connect("dev_b", conn)
                self.assertEqual((decision.ok, decision.code, decision.detail),
                                 (False, "connection_exists", REVOKED_CONNECTION_HELD))
                self.assertEqual(after.check_connection(conn, "read").code, "device_revoked")
        self.assertTrue(after.connect("dev_b", "fresh").ok)

    def test_a_revoked_device_id_is_never_paired_again_after_a_restart(self) -> None:
        first = self.book()
        self.paired(first, "dev_a")
        first.revoke("dev_a")
        after = self.book()
        with self.assertRaises(PairingRefused) as caught:
            self.paired(after, "dev_a")
        self.assertEqual(caught.exception.code, "device_exists")
        self.assertEqual(after.connect("dev_a", "c").code, "device_revoked")
        self.assertEqual(after.admit("dev_a", "read").code, "device_revoked")

    def test_revoke_returns_only_when_the_record_is_on_disk(self) -> None:
        """A crash right after revoke returns is stood in for by reading the file with a new store while the
        first book is left as it is: nothing of it is flushed or closed."""
        first = self.book()
        self.paired(first, "dev_a")
        first.connect("dev_a", "c")
        first.revoke("dev_a")
        held = FileRevocationStore(self.state).load()
        self.assertIn("dev_a", held.devices)
        self.assertEqual(held.connections["c"], StoredConnection("dev_a", None))

    def test_a_write_killed_after_the_rename_still_holds_after_the_restart(self) -> None:
        """The write is killed right after the rename step: the rename stands, then the write raises as a killed
        process would never return. revoke says the record may not be durable; the restart still refuses."""
        first = self.book()
        self.paired(first, "dev_a")
        first.connect("dev_a", "c")
        real = os.replace

        def killed(src, dst):
            real(src, dst)
            raise OSError("killed after the rename")

        with mock.patch.object(credentials.os, "replace", killed):
            with self.assertRaises(RevocationNotWritten) as caught:
                first.revoke("dev_a")
        self.assertEqual(caught.exception.code, "revocation_unwritten")
        self.assertEqual(caught.exception.connections, ("c",))
        after = self.book()
        self.paired(after, "dev_b")
        self.assertEqual(after.connect("dev_b", "c").detail, REVOKED_CONNECTION_HELD)

    def test_a_directory_that_cannot_be_synced_is_said(self) -> None:
        first = self.book()
        self.paired(first, "dev_a")
        first.connect("dev_a", "c")
        real = os.fsync
        calls = []

        def second_fails(fd):
            calls.append(fd)
            if len(calls) == 2:   # the first sync is the new file's, the second its directory's
                raise OSError("no sync")
            real(fd)

        with mock.patch.object(credentials.os, "fsync", second_fails):
            with self.assertRaises(RevocationNotWritten) as caught:
                first.revoke("dev_a")
        self.assertIn("could not be synced", caught.exception.detail)

    def test_the_release_is_written_at_the_disconnect(self) -> None:
        first = self.book()
        self.paired(first, "dev_a")
        first.connect("dev_a", "c")
        first.revoke("dev_a")
        self.wall.now += 5
        first.disconnect("c")
        held = FileRevocationStore(self.state).load()
        self.assertEqual(held.connections["c"], StoredConnection("dev_a", self.wall.now))

    def test_an_id_the_file_cannot_keep_is_refused_at_connect(self) -> None:
        first = self.book()
        self.paired(first, "dev_a")
        for conn in (1.5, True, frozenset({1}), ("a", 2.0)):
            with self.subTest(conn=conn):
                self.assertEqual(first.connect("dev_a", conn).code, "connection_invalid")
        self.assertTrue(DeviceBook(clock=self.clock).connect("dev_x", 1.5).code == "device_unknown")


class KeepAcrossRestartTests(Folder):
    def revoked_and_released(self, keep) -> None:
        first = self.book(revoked_connection_keep_s=keep)
        self.paired(first, "dev_a")
        first.connect("dev_a", "c")
        first.revoke("dev_a")
        first.disconnect("c")

    def b_on_c(self, keep):
        after = self.book(revoked_connection_keep_s=keep)
        self.paired(after, f"dev_b{self.wall.now}")
        return after.connect(f"dev_b{self.wall.now}", "c")

    def test_the_keep_time_runs_on_the_wall_clock_across_a_restart(self) -> None:
        self.revoked_and_released(30)
        self.wall.now += 29.5
        self.assertEqual(self.b_on_c(30).detail, REVOKED_CONNECTION_HELD)
        self.wall.now += 0.5
        self.assertTrue(self.b_on_c(30).ok)

    def test_never_forget_holds_across_a_restart(self) -> None:
        self.revoked_and_released(None)
        self.wall.now += 10 * 365 * 86400.0
        self.assertEqual(self.b_on_c(None).detail, REVOKED_CONNECTION_HELD)

    def test_a_connection_open_when_the_daemon_stopped_is_released_at_the_restart(self) -> None:
        first = self.book(revoked_connection_keep_s=30)
        self.paired(first, "dev_a")
        first.connect("dev_a", "c")
        first.revoke("dev_a")          # no disconnect: the daemon stops with the connection open
        self.wall.now += 1000          # the restart, long after the revoke
        restarted = self.book(revoked_connection_keep_s=30)
        self.assertEqual(FileRevocationStore(self.state).load().connections["c"],
                         StoredConnection("dev_a", self.wall.now))
        self.paired(restarted, "dev_b")
        self.assertEqual(restarted.connect("dev_b", "c").detail, REVOKED_CONNECTION_HELD)
        self.wall.now += 29.5          # a second restart, before the keep time from the first one ends
        self.assertEqual(self.b_on_c(30).detail, REVOKED_CONNECTION_HELD)
        self.wall.now += 0.5
        self.assertTrue(self.b_on_c(30).ok)

    def test_a_wall_clock_that_moved_back_keeps_the_record_longer(self) -> None:
        self.revoked_and_released(30)
        self.wall.now -= 3600
        after = self.book(revoked_connection_keep_s=30)
        self.paired(after, "dev_b")
        self.assertEqual(after.connect("dev_b", "c").detail, REVOKED_CONNECTION_HELD)
        self.clock.now += 29.5
        self.assertEqual(after.connect("dev_b", "c").detail, REVOKED_CONNECTION_HELD)
        self.clock.now += 0.5
        self.assertTrue(after.connect("dev_b", "c").ok)


class FailClosedTests(Folder):
    def test_a_file_that_cannot_be_read_refuses_every_connection_until_it_can(self) -> None:
        first = self.book()
        self.paired(first, "dev_a")
        first.connect("dev_a", "c")
        first.revoke("dev_a")
        path = FileRevocationStore(self.state).path
        good = open(path, "rb").read()
        with open(path, "wb") as handle:
            handle.write(b"{not json")
        after = self.book()
        with self.assertRaises(PairingRefused) as caught:
            self.paired(after, "dev_b")
        self.assertEqual(caught.exception.code, "revocations_unreadable")
        for words in ("could not be read", "no phone is let in until it can be", "revoked phone", path,
                      "not JSON", "restored"):
            self.assertIn(words, caught.exception.detail)
        with open(path, "wb") as handle:
            handle.write(good)
        self.paired(after, "dev_b")
        self.assertEqual(after.connect("dev_b", "c").detail, REVOKED_CONNECTION_HELD)

    def test_a_file_others_may_read_is_refused(self) -> None:
        first = self.book()
        self.paired(first, "dev_a")
        first.revoke("dev_a")
        os.chmod(FileRevocationStore(self.state).path, 0o644)
        after = self.book()
        decision = after.connect("dev_b", "c")
        self.assertEqual(decision.code, "revocations_unreadable")
        self.assertIn("owner-only", decision.detail)

    def test_a_store_that_cannot_be_read_refuses_connect_and_add(self) -> None:
        store = MemoryStore()
        store.fail_load = "the disk is gone"
        book = DeviceBook(clock=self.clock, wall=self.wall, store=store)
        self.assertEqual(book.connect("dev_b", "c").code, "revocations_unreadable")
        with self.assertRaises(PairingRefused):
            self.paired(book, "dev_b")
        store.fail_load = None
        self.paired(book, "dev_b")
        self.assertTrue(book.connect("dev_b", "c").ok)

    def test_a_revoke_whose_write_fails_still_revokes_and_says_so(self) -> None:
        store = MemoryStore()
        book = DeviceBook(clock=self.clock, wall=self.wall, store=store)
        self.paired(book, "dev_a")
        self.paired(book, "dev_b")
        book.connect("dev_a", "c")
        store.fail_put = "no space left"
        with self.assertRaises(RevocationNotWritten) as caught:
            book.revoke("dev_a")
        self.assertIsInstance(caught.exception, PairingRefused)
        self.assertEqual(caught.exception.connections, ("c",))
        for words in ("revoked until the daemon stops", "could not be written", "no space left", "Revoke it again"):
            self.assertIn(words, caught.exception.detail)
        self.assertEqual(book.check_connection("c", "read").code, "device_revoked")
        self.assertEqual(book.connect("dev_b", "c").detail, REVOKED_CONNECTION_HELD)
        self.assertEqual(store.devices, {})
        store.fail_put = None
        self.assertEqual(book.revoke("dev_a"), ())
        self.assertIn("dev_a", store.devices)
        self.assertEqual(store.connections["c"].device_id, "dev_a")

    def test_the_store_lock_timeout_is_a_setting(self) -> None:
        for bad in (0, -1, float("inf"), True, "10"):
            with self.subTest(bad=bad):
                with self.assertRaises(RevocationStoreError) as caught:
                    FileRevocationStore(self.state, lock_timeout_s=bad)
                self.assertIn("setting_invalid", str(caught.exception))
        self.assertEqual(revocation_store.REVOCATIONS_LOCK_TIMEOUT_S, 10.0)

    def test_the_file_is_owner_only_and_all_or_nothing(self) -> None:
        first = self.book()
        self.paired(first, "dev_a")
        first.revoke("dev_a")
        path = FileRevocationStore(self.state).path
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        doc = json.loads(open(path, encoding="utf-8").read())
        self.assertEqual(sorted(doc), ["boot", "connections", "devices", "version"])
        self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if n.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
