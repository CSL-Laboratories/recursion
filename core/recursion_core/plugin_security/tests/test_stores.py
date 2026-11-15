"""``plugin_security.stores``: the checked open and the installer's creation of the three stores.

Each check in ``check_store`` and ``open_store`` has a test here that fails when that check alone is
removed. No signature is made or checked: the ledger only has to be bound to an anchor in the right
format, so a fixed public value stands in for one and the tests need no ``cryptography``.
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core.plugin_security import Authority, ConnectionBreaker, Refused, TrustStore, stores
from recursion_core.plugin_security import authority as authority_module
from recursion_core.plugin_security import breaker as breaker_module
from recursion_core.plugin_security import manifest as manifest_module

ANCHOR = "11" * 32  # a public value in the anchor's format; nothing signs with it or verifies against it

KINDS = (
    ("ledger", stores.LEDGER_MAGIC, lambda path: stores.create_ledger(path, ANCHOR)),
    ("breaker_store", stores.BREAKER_MAGIC, stores.create_breaker_store),
    ("revocation_store", stores.REVOCATION_MAGIC, stores.create_revocation_store),
)
SIDE_FILES = ("-wal", "-shm", "-journal")

_real_lstat = os.lstat


def _owned_by_someone_else(target: str, uid: int | None = None):
    """Patch ``os.lstat`` so that ``target`` alone reports another owner (``uid``, or ours plus one).
    Nothing is chowned."""
    def fake(path, *args, **kwargs):
        info = _real_lstat(path, *args, **kwargs)
        if os.fspath(path) == target:
            fields = list(info[:10])
            fields[4] = info.st_uid + 1 if uid is None else uid
            return os.stat_result(fields)
        return info
    return mock.patch("os.lstat", side_effect=fake)


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="stores-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = os.path.join(self._tmp.name, "plugin-security")
        os.mkdir(self.dir, 0o700)

    def path(self, name="store.sqlite3"):
        return os.path.join(self.dir, name)

    def assertRefused(self, code, call, *args):
        with self.assertRaises(Refused) as caught:
            call(*args)
        self.assertEqual(caught.exception.code, code, caught.exception.detail)
        return caught.exception


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
                     "requires Unix file modes and a non-root uid")
class OpenStoreTests(_StoreCase):
    def test_a_store_made_here_opens_with_the_shared_settings(self):
        real_connect = sqlite3.connect
        for prefix, magic, create in KINDS:
            with self.subTest(store=prefix):
                path = self.path(prefix + ".sqlite3")
                create(path)
                statements = []

                def traced(*args, **kwargs):
                    db = real_connect(*args, **kwargs)
                    db.set_trace_callback(statements.append)
                    return db
                with mock.patch("sqlite3.connect", side_effect=traced):
                    db = stores.open_store(path, magic)
                # FULL is this build's default too, so the statement itself is what is checked.
                self.assertIn("PRAGMA synchronous=FULL", statements)
                try:
                    self.assertIsNone(db.isolation_level)
                    self.assertEqual(db.execute("PRAGMA synchronous").fetchone()[0], 2)  # FULL
                    self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
                    self.assertEqual(db.execute("SELECT v FROM meta WHERE k='magic'").fetchone()[0], magic)
                    seen = []
                    worker = threading.Thread(target=lambda: seen.append(
                        db.execute("SELECT count(*) FROM meta").fetchone()[0]))
                    worker.start()
                    worker.join()
                    self.assertEqual(len(seen), 1)  # check_same_thread=False: another thread may use it
                finally:
                    db.close()
                stores.check_store(path, magic)

    def test_a_path_object_is_accepted(self):
        path = Path(self.path())
        stores.create_breaker_store(path)
        stores.open_store(path, stores.BREAKER_MAGIC).close()

    def test_a_missing_store_is_refused_and_not_created(self):
        for prefix, magic, _ in KINDS:
            with self.subTest(store=prefix):
                path = self.path(prefix + ".sqlite3")
                self.assertRefused(prefix + "_missing", stores.open_store, path, magic)
                self.assertRefused(prefix + "_missing", stores.check_store, path, magic)
                self.assertFalse(os.path.lexists(path))

    def test_a_missing_folder_is_refused(self):
        path = os.path.join(self.dir, "gone", "store.sqlite3")
        self.assertRefused("ledger_missing", stores.open_store, path, stores.LEDGER_MAGIC)
        self.assertFalse(os.path.lexists(os.path.dirname(path)))

    def test_a_store_removed_after_the_checks_is_refused_not_created(self):
        path = self.path()
        with mock.patch.object(stores, "check_store"):
            self.assertRefused("revocation_store_missing", stores.open_store, path, stores.REVOCATION_MAGIC)
        self.assertFalse(os.path.lexists(path))

    def test_a_link_to_a_good_store_is_refused(self):
        for prefix, magic, create in KINDS:
            with self.subTest(store=prefix):
                real = self.path(prefix + "-real.sqlite3")
                create(real)
                link = self.path(prefix + ".sqlite3")
                os.symlink(real, link)
                error = self.assertRefused(prefix + "_untrusted", stores.open_store, link, magic)
                self.assertIn("is a symlink", error.detail)
                dangling = self.path(prefix + "-dangling.sqlite3")
                os.symlink(self.path("nowhere"), dangling)
                self.assertRefused(prefix + "_untrusted", stores.open_store, dangling, magic)
                self.assertFalse(os.path.lexists(self.path("nowhere")))

    def test_something_other_than_a_regular_file_is_refused(self):
        os.mkdir(self.path("folder.sqlite3"), 0o700)
        self.assertRefused("ledger_untrusted", stores.open_store, self.path("folder.sqlite3"),
                           stores.LEDGER_MAGIC)
        os.mkfifo(self.path("fifo.sqlite3"), 0o600)
        self.assertRefused("ledger_untrusted", stores.open_store, self.path("fifo.sqlite3"),
                           stores.LEDGER_MAGIC)

    def test_group_or_world_write_on_the_store_is_refused_and_read_is_not(self):
        path = self.path()
        stores.create_breaker_store(path)
        for mode in (0o620, 0o602, 0o660, 0o666):
            with self.subTest(mode=oct(mode)):
                os.chmod(path, mode)
                self.assertRefused("breaker_store_untrusted", stores.open_store, path, stores.BREAKER_MAGIC)
        for mode in (0o640, 0o604, 0o644):
            with self.subTest(mode=oct(mode)):
                os.chmod(path, mode)
                stores.open_store(path, stores.BREAKER_MAGIC).close()

    def test_another_owner_is_refused(self):
        path = self.path()
        stores.create_ledger(path, ANCHOR)
        with _owned_by_someone_else(path):
            error = self.assertRefused("ledger_untrusted", stores.open_store, path, stores.LEDGER_MAGIC)
        self.assertIn("another user", error.detail)

    def test_side_files_beside_the_store_are_checked_when_present(self):
        path = self.path()
        stores.create_revocation_store(path)
        magic, code = stores.REVOCATION_MAGIC, "revocation_store_untrusted"
        for suffix in SIDE_FILES:
            side = path + suffix
            with self.subTest(side=suffix, case="link"):
                os.symlink(self.path("elsewhere" + suffix), side)
                self.assertRefused(code, stores.open_store, path, magic)
                os.unlink(side)
            with self.subTest(side=suffix, case="folder"):
                os.mkdir(side, 0o700)
                self.assertRefused(code, stores.open_store, path, magic)
                os.rmdir(side)
            with self.subTest(side=suffix, case="group write"):
                fd = os.open(side, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                os.chmod(side, 0o620)
                self.assertRefused(code, stores.open_store, path, magic)
                os.chmod(side, 0o600)
            with self.subTest(side=suffix, case="another owner"):
                with _owned_by_someone_else(side):
                    self.assertRefused(code, stores.open_store, path, magic)
            with self.subTest(side=suffix, case="an empty one of ours"):
                stores.open_store(path, magic).close()  # an owner-only regular side file is SQLite's own
            if os.path.lexists(side):
                os.unlink(side)

    def test_the_folder_must_be_ours_and_closed_to_writers(self):
        path = self.path()
        stores.create_ledger(path, ANCHOR)
        for mode in (0o770, 0o730, 0o703, 0o1777):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.dir, mode)
                self.assertRefused("ledger_untrusted", stores.open_store, path, stores.LEDGER_MAGIC)
        os.chmod(self.dir, 0o750)
        stores.open_store(path, stores.LEDGER_MAGIC).close()
        os.chmod(self.dir, 0o700)
        with _owned_by_someone_else(self.dir):
            self.assertRefused("ledger_untrusted", stores.open_store, path, stores.LEDGER_MAGIC)
        linked = os.path.join(self._tmp.name, "linked")
        os.symlink(self.dir, linked)
        error = self.assertRefused("ledger_untrusted", stores.open_store, os.path.join(linked, "store.sqlite3"),
                                   stores.LEDGER_MAGIC)
        self.assertIn("is a symlink", error.detail)
        not_a_folder = self.path("a-file")
        with open(not_a_folder, "wb"):
            pass
        os.chmod(not_a_folder, 0o600)
        error = self.assertRefused("ledger_untrusted", stores.open_store, os.path.join(not_a_folder, "store.sqlite3"),
                                   stores.LEDGER_MAGIC)
        self.assertIn("is not a folder", error.detail)

    def test_a_relative_path_is_refused(self):
        cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            stores.create_breaker_store(self.path())
            self.assertRefused("breaker_store_untrusted", stores.open_store, "store.sqlite3",
                               stores.BREAKER_MAGIC)
            self.assertRefused("breaker_store_untrusted", stores.open_store, b"/abs/bytes",
                               stores.BREAKER_MAGIC)
            self.assertRefused("breaker_store_untrusted", stores.open_store, 7, stores.BREAKER_MAGIC)
        finally:
            os.chdir(cwd)

    def test_a_path_with_a_nul_character_is_refused_untrusted(self):
        # os.lstat would raise ValueError, not a refusal (review 1, Sol R4).
        for prefix, magic, create in KINDS:
            with self.subTest(store=prefix):
                bad = self.path("bad") + "\0.sqlite3"
                error = self.assertRefused(prefix + "_untrusted", stores.check_store, bad, magic)
                self.assertIn("NUL", error.detail)
                self.assertRefused(prefix + "_untrusted", stores.open_store, bad, magic)
                self.assertRefused(prefix + "_untrusted", create, bad)
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_file_that_is_not_this_store_is_invalid(self):
        garbage = self.path("garbage.sqlite3")
        with open(garbage, "wb") as handle:
            handle.write(b"not a database at all, just some bytes" * 64)
        os.chmod(garbage, 0o600)
        self.assertRefused("ledger_invalid", stores.open_store, garbage, stores.LEDGER_MAGIC)
        bare = self.path("bare.sqlite3")
        db = sqlite3.connect(bare)
        db.execute("CREATE TABLE other (x)")
        db.commit()
        db.close()
        os.chmod(bare, 0o600)
        self.assertRefused("ledger_invalid", stores.open_store, bare, stores.LEDGER_MAGIC)
        no_magic = self.path("no-magic.sqlite3")
        db = sqlite3.connect(no_magic)
        db.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        db.commit()
        db.close()
        os.chmod(no_magic, 0o600)
        self.assertRefused("ledger_invalid", stores.open_store, no_magic, stores.LEDGER_MAGIC)
        breaker = self.path("breaker.sqlite3")
        stores.create_breaker_store(breaker)
        for prefix, magic, _ in KINDS:
            if magic != stores.BREAKER_MAGIC:
                with self.subTest(opened_as=prefix):
                    self.assertRefused(prefix + "_invalid", stores.open_store, breaker, magic)

    def test_an_unreadable_store_is_invalid(self):
        path = self.path()
        stores.create_revocation_store(path)
        os.chmod(path, 0)
        try:
            self.assertRefused("revocation_store_invalid", stores.open_store, path, stores.REVOCATION_MAGIC)
        finally:
            os.chmod(path, 0o600)

    def test_a_folder_that_cannot_be_searched_is_invalid_not_missing(self):
        path = self.path()
        stores.create_revocation_store(path)
        os.chmod(self.dir, 0o600)  # ours, closed to writers, but the store in it cannot be looked up
        try:
            self.assertRefused("revocation_store_invalid", stores.check_store, path, stores.REVOCATION_MAGIC)
        finally:
            os.chmod(self.dir, 0o700)

    def test_characters_a_uri_would_misread_stay_in_the_path(self):
        folder = os.path.join(self._tmp.name, "a b?c#d%41e")
        os.mkdir(folder, 0o700)
        path = os.path.join(folder, "store ?x=1#y.sqlite3")
        stores.create_ledger(path, ANCHOR)
        stores.open_store(path, stores.LEDGER_MAGIC).close()
        self.assertEqual(sorted(os.listdir(folder)), ["store ?x=1#y.sqlite3"])
        self.assertEqual(sorted(os.listdir(self._tmp.name)), ["a b?c#d%41e", "plugin-security"])

    def test_an_unknown_magic_is_a_programming_error(self):
        path = self.path()
        stores.create_ledger(path, ANCHOR)
        for call in (stores.open_store, stores.check_store):
            with self.subTest(call=call.__name__):
                for magic in ("csl-something-else/v0", None, [], {}):  # a list or a dict is not hashable
                    with self.assertRaises(ValueError):
                        call(path, magic)

    def test_a_refused_open_releases_its_connection(self):
        # A refused open hands back nothing, so it must let go of what it opened (review 1, R6: M05, M06).
        path = self.path()
        stores.create_breaker_store(path)
        for case, connection in (("sql error", mock.Mock(**{"execute.side_effect": sqlite3.OperationalError("x")})),
                                 ("another magic", mock.Mock(**{"execute.return_value.fetchone.return_value": ("x",)})),
                                 ("no magic row", mock.Mock(**{"execute.return_value.fetchone.return_value": None}))):
            with self.subTest(case=case):
                with mock.patch.object(stores.sqlite3, "connect", return_value=connection):
                    self.assertRefused("breaker_store_invalid", stores.open_store, path, stores.BREAKER_MAGIC)
                connection.close.assert_called_once_with()


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
                     "requires Unix file modes and a non-root uid")
class CreateStoreTests(_StoreCase):
    def test_each_new_store_opens_in_its_own_class(self):
        ledger, breaker, revoked = self.path("ledger.sqlite3"), self.path("breaker.sqlite3"), self.path("revoked.sqlite3")
        stores.create_ledger(ledger, ANCHOR)
        stores.create_breaker_store(breaker)
        stores.create_revocation_store(revoked)
        self.assertEqual(sorted(os.listdir(self.dir)), ["breaker.sqlite3", "ledger.sqlite3", "revoked.sqlite3"])
        Authority(ANCHOR, ledger, lambda: 0).close()
        ConnectionBreaker(breaker).close()
        trust = TrustStore({}, revocation_store=revoked)
        trust._db.close()

    def test_a_new_store_is_owner_only_under_any_umask_that_lets_the_owner_write(self):
        # The factories create the file 0600; a umask only takes bits away. One that takes the
        # owner's own write makes the factory fail, and nothing is left behind.
        old = os.umask(0o277)
        try:
            with self.assertRaises(sqlite3.Error):
                stores.create_breaker_store(self.path("readonly.sqlite3"))
        finally:
            os.umask(old)
        self.assertEqual(os.listdir(self.dir), [])
        for umask in (0o000, 0o022, 0o077):
            with self.subTest(umask=oct(umask)):
                path = self.path(f"store-{umask:o}.sqlite3")
                old = os.umask(umask)
                try:
                    stores.create_breaker_store(path)
                finally:
                    os.umask(old)
                self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
                stores.open_store(path, stores.BREAKER_MAGIC).close()

    def test_anything_already_at_the_path_is_refused_and_left_alone(self):
        for prefix, magic, create in KINDS:
            with self.subTest(store=prefix, case="a file"):
                path = self.path(prefix + ".sqlite3")
                with open(path, "wb") as handle:
                    handle.write(b"someone else's bytes")
                self.assertRefused(prefix + "_exists", create, path)
                with open(path, "rb") as handle:
                    self.assertEqual(handle.read(), b"someone else's bytes")
            with self.subTest(store=prefix, case="a dangling link"):
                link = self.path(prefix + "-link.sqlite3")
                target = self.path(prefix + "-target.sqlite3")
                os.symlink(target, link)
                self.assertRefused(prefix + "_exists", create, link)
                self.assertFalse(os.path.lexists(target))
            for suffix in SIDE_FILES:
                with self.subTest(store=prefix, case="a leftover " + suffix):
                    path = self.path(prefix + "-side.sqlite3")
                    with open(path + suffix, "wb") as handle:
                        handle.write(b"left over")
                    self.assertRefused(prefix + "_exists", create, path)
                    self.assertFalse(os.path.lexists(path))
                    os.unlink(path + suffix)

    def test_a_folder_that_is_missing_or_not_ours_is_refused(self):
        self.assertRefused("breaker_store_missing", stores.create_breaker_store,
                           os.path.join(self.dir, "gone", "store.sqlite3"))
        os.chmod(self.dir, 0o770)
        self.assertRefused("breaker_store_untrusted", stores.create_breaker_store, self.path())
        os.chmod(self.dir, 0o700)
        linked = os.path.join(self._tmp.name, "linked")
        os.symlink(self.dir, linked)
        self.assertRefused("breaker_store_untrusted", stores.create_breaker_store,
                           os.path.join(linked, "store.sqlite3"))
        self.assertRefused("breaker_store_untrusted", stores.create_breaker_store, "relative.sqlite3")
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_failure_part_way_leaves_nothing(self):
        # An anchor that is not a public key: the factory's own refusal, as a frozen code (review 1, R5).
        error = self.assertRefused("ledger_invalid", stores.create_ledger, self.path(), "not-a-key")
        self.assertEqual(error.detail, "malformed_public_key")
        self.assertEqual(os.listdir(self.dir), [])

        def half_made(path):
            with open(path, "wb") as handle:
                handle.write(b"half")
            with open(path + "-wal", "wb") as handle:
                handle.write(b"half")
            raise OSError("disk full")
        with mock.patch.object(breaker_module, "create_breaker_store", half_made):
            with self.assertRaises(OSError):
                stores.create_breaker_store(self.path())
        self.assertEqual(os.listdir(self.dir), [])

        real_factory = breaker_module.create_breaker_store
        for suffix in SIDE_FILES:  # each side file a factory leaves is refused and removed (review 1, Sol R5)
            with self.subTest(suffix=suffix):
                with mock.patch.object(breaker_module, "create_breaker_store",
                                       lambda path: (real_factory(path), open(path + suffix, "wb").close())):
                    error = self.assertRefused("breaker_store_invalid", stores.create_breaker_store, self.path())
                self.assertIn(suffix, error.detail)
                self.assertEqual(os.listdir(self.dir), [])

    def test_losing_the_race_for_the_path_is_refused(self):
        with mock.patch("os.link", side_effect=FileExistsError):
            self.assertRefused("ledger_exists", stores.create_ledger, self.path(), ANCHOR)
        self.assertEqual(os.listdir(self.dir), [])
        with mock.patch("os.link", side_effect=PermissionError):
            self.assertRefused("ledger_invalid", stores.create_ledger, self.path(), ANCHOR)
        self.assertEqual(os.listdir(self.dir), [])

    def test_the_folder_is_fsynced_after_the_link(self):
        order = []
        real_link, real_fsync = os.link, os.fsync
        folder = os.lstat(self.dir)

        def link(src, dst, **kwargs):
            order.append("link")
            return real_link(src, dst, **kwargs)

        def fsync(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (folder.st_dev, folder.st_ino):
                order.append("fsync folder")
            return real_fsync(fd)
        with mock.patch("os.link", side_effect=link), mock.patch("os.fsync", side_effect=fsync):
            stores.create_revocation_store(self.path())
        self.assertEqual(order, ["link", "fsync folder"])

    def test_a_new_store_is_checked_by_the_open_before_it_is_linked_into_place(self):
        # Checked under its temporary name, so a store that fails never reaches the path and a later
        # create can try again (review 1, DeepSeek F7).
        with mock.patch.object(stores, "open_store", side_effect=Refused("ledger_invalid")) as opened:
            self.assertRefused("ledger_invalid", stores.create_ledger, self.path(), ANCHOR)
        (checked, magic), _ = opened.call_args
        self.assertEqual((os.path.dirname(checked), magic), (self.dir, stores.LEDGER_MAGIC))
        self.assertTrue(os.path.basename(checked).startswith(".store.sqlite3.") and checked.endswith(".tmp"))
        self.assertEqual(os.listdir(self.dir), [])

        def wrong_magic(path):  # a factory whose store fails its own open: nothing at the path, and a retry works
            real_breaker(path)
            db = sqlite3.connect(path)
            db.execute("UPDATE meta SET v='csl-something-else/v0' WHERE k='magic'")
            db.commit()
            db.close()
        real_breaker = breaker_module.create_breaker_store
        with mock.patch.object(breaker_module, "create_breaker_store", wrong_magic):
            error = self.assertRefused("breaker_store_invalid", stores.create_breaker_store, self.path())
        self.assertEqual(error.detail, "another store's magic")
        self.assertEqual(os.listdir(self.dir), [])
        stores.create_breaker_store(self.path())
        stores.open_store(self.path(), stores.BREAKER_MAGIC).close()


@unittest.skipUnless(os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() != 0,
                     "requires Unix file modes and a non-root uid")
class AncestorTests(_StoreCase):
    """The folders above the store's own (review 1, R1): another user must not be able to change what
    the path names. Each case runs through both ``open_store`` and a create, two and one levels up."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="stores-up-")
        self.addCleanup(self._tmp.cleanup)
        self.base = os.path.realpath(self._tmp.name)  # the names the walk builds from "/"
        self.a = os.path.join(self.base, "a")
        self.b = os.path.join(self.a, "b")
        self.dir = os.path.join(self.b, "plugin-security")
        os.makedirs(self.dir, 0o700)
        self.store = os.path.join(self.dir, "store.sqlite3")
        stores.create_breaker_store(self.store)
        self.addCleanup(self._open_all)

    def _open_all(self):
        for folder in (self.a, self.b, self.dir):
            if os.path.isdir(folder) and not os.path.islink(folder):
                os.chmod(folder, 0o700)

    def refused_both_ways(self, store_path: str, code: str = "breaker_store_untrusted") -> Refused:
        with self.assertRaises(Refused) as opened:
            stores.open_store(store_path, stores.BREAKER_MAGIC)
        self.assertEqual(opened.exception.code, code, opened.exception.detail)
        new = os.path.join(os.path.dirname(store_path), "new.sqlite3")
        with self.assertRaises(Refused) as created:
            stores.create_breaker_store(new)
        self.assertEqual(created.exception.code, code, created.exception.detail)
        self.assertFalse(os.path.lexists(new))
        return opened.exception

    def accepted_both_ways(self, store_path: str) -> None:
        stores.open_store(store_path, stores.BREAKER_MAGIC).close()
        new = os.path.join(os.path.dirname(store_path), "new.sqlite3")
        stores.create_breaker_store(new)
        os.unlink(new)

    def test_a_folder_above_that_group_or_world_can_write_is_refused_unless_sticky(self):
        self.accepted_both_ways(self.store)
        for folder in (self.a, self.b):
            for mode in (0o777, 0o770, 0o722):
                with self.subTest(folder=os.path.basename(folder), mode=oct(mode)):
                    os.chmod(folder, mode)
                    error = self.refused_both_ways(self.store)
                    self.assertEqual(error.detail, f"{folder} is writable by group or world and not sticky")
            with self.subTest(folder=os.path.basename(folder), mode="0o1777"):
                os.chmod(folder, 0o1777)  # as /tmp: only an entry's owner can rename it
                self.accepted_both_ways(self.store)
            os.chmod(folder, 0o700)

    def test_a_folder_above_another_user_owns_is_refused_and_one_root_owns_is_not(self):
        for folder in (self.a, self.b):
            with self.subTest(folder=os.path.basename(folder)):
                with _owned_by_someone_else(folder):
                    error = self.refused_both_ways(self.store)
                self.assertEqual(error.detail, f"{folder} is owned by another user")
                with _owned_by_someone_else(folder, uid=0):
                    self.accepted_both_ways(self.store)

    def test_a_link_above_is_followed_and_its_target_is_held_to_the_same_rule(self):
        absolute = os.path.join(self.base, "abs-link")
        os.symlink(self.a, absolute)
        relative = os.path.join(self.base, "rel-link")
        os.symlink("a", relative)
        inner = os.path.join(self.a, "inner-link")
        os.symlink("b", inner)  # a link two levels up, inside a
        paths = [os.path.join(absolute, "b", "plugin-security", "store.sqlite3"),
                 os.path.join(relative, "b", "plugin-security", "store.sqlite3"),
                 os.path.join(inner, "plugin-security", "store.sqlite3")]
        for path in paths:
            with self.subTest(path=path, case="good target"):
                self.accepted_both_ways(path)
        os.chmod(self.a, 0o777)
        for path in paths[:2]:
            with self.subTest(path=path, case="target open to writers"):
                self.refused_both_ways(path)
        os.chmod(self.a, 0o700)
        os.chmod(self.b, 0o777)
        with self.subTest(case="target of the inner link open to writers"):
            self.refused_both_ways(paths[2])

    def test_a_link_above_another_user_owns_is_refused(self):
        link = os.path.join(self.base, "link")
        os.symlink(self.a, link)
        with _owned_by_someone_else(link):
            error = self.refused_both_ways(os.path.join(link, "b", "plugin-security", "store.sqlite3"))
        self.assertEqual(error.detail, f"{link} is a link another user owns")

    def test_a_link_loop_above_is_refused(self):
        os.symlink("loop-2", os.path.join(self.base, "loop-1"))
        os.symlink("loop-1", os.path.join(self.base, "loop-2"))
        error = self.refused_both_ways(os.path.join(self.base, "loop-1", "plugin-security", "store.sqlite3"))
        self.assertEqual(error.detail, "more than 40 links above the store's folder")

    def test_dot_dot_goes_up_from_where_the_resolution_is_not_from_the_text(self):
        self.accepted_both_ways(os.path.join(self.b, "..", "b", ".", "plugin-security", "store.sqlite3"))
        link = os.path.join(self.base, "to-b")
        os.symlink(self.b, link)
        # The kernel reads to-b/.. as a (b's parent), not as base: a lexical reading finds no base/b.
        through = os.path.join(link, "..", "b", "plugin-security", "store.sqlite3")
        self.accepted_both_ways(through)
        os.chmod(self.a, 0o777)
        self.refused_both_ways(through)

    def test_a_file_or_a_missing_name_above_is_refused(self):
        not_a_folder = os.path.join(self.a, "file")
        with open(not_a_folder, "wb") as handle:
            handle.write(b"x")
        error = self.refused_both_ways(os.path.join(not_a_folder, "plugin-security", "store.sqlite3"))
        self.assertEqual(error.detail, f"{not_a_folder} is not a folder")
        self.refused_both_ways(os.path.join(self.a, "gone", "plugin-security", "store.sqlite3"),
                               code="breaker_store_missing")

    def test_off_posix_only_the_link_and_type_checks_apply(self):
        # Windows has no os.geteuid and reports every file as group and world writable; the core's
        # stores still open there (child_env's simulated-Windows run of every core suite).
        link = os.path.join(self.dir, "link.sqlite3")
        os.symlink(self.store, link)
        os.chmod(self.store, 0o666)
        os.chmod(self.dir, 0o777)
        os.chmod(self.a, 0o777)
        self.assertRefused("breaker_store_untrusted", stores.check_store, self.store, stores.BREAKER_MAGIC)  # POSIX here
        with mock.patch.object(os, "name", "nt"), \
                mock.patch.object(os, "geteuid", side_effect=AssertionError("Windows has no os.geteuid")):
            stores.check_store(self.store, stores.BREAKER_MAGIC)  # (open_store's Path() would be a WindowsPath here)
            error = self.assertRefused("breaker_store_untrusted", stores.check_store, link, stores.BREAKER_MAGIC)
            self.assertIn("symlink", error.detail)
            self.assertRefused("breaker_store_untrusted", stores.check_store, self.dir, stores.BREAKER_MAGIC)
        os.chmod(self.store, 0o600)


class FrozenInterfaceTests(unittest.TestCase):
    """The interface other lanes build against. A failure here is a change to a frozen interface."""

    def test_the_magics_are_the_ones_the_factories_write(self):
        self.assertEqual(stores.LEDGER_MAGIC, authority_module._LEDGER_MAGIC)
        self.assertEqual(stores.BREAKER_MAGIC, breaker_module._STORE_MAGIC)
        self.assertEqual(stores.REVOCATION_MAGIC, manifest_module._REVOCATION_MAGIC)
        self.assertEqual(stores.KINDS, {
            "csl-authority-ledger/v0": "ledger",
            "csl-breaker-store/v0": "breaker_store",
            "csl-revocation-store/v0": "revocation_store",
        })

    def test_the_frozen_names_and_signatures(self):
        self.assertEqual(set(stores.__all__), {
            "BREAKER_MAGIC", "KINDS", "LEDGER_MAGIC", "REVOCATION_MAGIC", "check_store",
            "create_breaker_store", "create_ledger", "create_revocation_store", "open_store"})
        signatures = {name: str(inspect.signature(getattr(stores, name))) for name in (
            "check_store", "open_store", "create_ledger", "create_breaker_store", "create_revocation_store")}
        self.assertEqual(signatures, {
            "check_store": "(path, magic: 'str') -> 'None'",
            "open_store": "(path, magic: 'str') -> 'sqlite3.Connection'",
            "create_ledger": "(path, anchor_public_key: 'str') -> 'None'",
            "create_breaker_store": "(path) -> 'None'",
            "create_revocation_store": "(path) -> 'None'",
        })

    def test_the_module_imports_none_of_the_store_classes(self):
        # authority.py, breaker.py and manifest.py can import this module without a cycle.
        import ast
        tree = ast.parse(Path(stores.__file__).read_text(encoding="utf-8"))
        top = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
        relative = {node.module for node in top if isinstance(node, ast.ImportFrom) and node.level}
        self.assertEqual(relative, {"errors"})


if __name__ == "__main__":
    unittest.main()
