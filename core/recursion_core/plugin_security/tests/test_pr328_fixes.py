"""Fixes on the PR 328 branch: trip generation, plugin path trust, long store paths, encoding.

Each test names the behaviour that was wrong on head 9f501110 before the fix.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
import unittest

from recursion_core.plugin_security import (
    ConnectionBreaker, Grant, PluginLoader, Refused, TrustStore, canonical, create_breaker_store,
    create_ledger, create_revocation_store, parse, parse_manifest,
)
from recursion_core.plugin_security.tests.support import (
    PLUGIN_SOURCE, FakeGilProbe, Key, manifest_bytes, needs_crypto, temp_dir,
)


def _deep_path(root: str, total: int) -> str:
    """A path of exactly ``total`` bytes whose every component fits in NAME_MAX."""
    current = root
    while len(current.encode()) + 41 < total:
        current = os.path.join(current, "d" * 40)
        os.mkdir(current)
    name_len = total - len(current.encode()) - 1
    if not 1 <= name_len <= 200:
        raise AssertionError("cannot place a %s-byte path under %s" % (total, root))
    return os.path.join(current, "f" * name_len)


class _SchemaRace:
    """A connection that pauses an unlocked schema read until the other opener has read too.

    The migration under test reads the schema inside its write transaction, so this
    pause does not run. An unlocked read is the race the second opener used to lose.
    """

    def __init__(self, db, name, barrier, alters, unlocked):
        self._db = db
        self._name = name
        self._barrier = barrier
        self._alters = alters
        self._unlocked = unlocked

    def execute(self, sql, parameters=()):
        if sql.lstrip().upper().startswith("ALTER TABLE"):
            self._alters.append(self._name)
        cursor = self._db.execute(sql, parameters)
        if "table_info(tripped)" in sql and not self._db.in_transaction:
            rows = cursor.fetchall()
            self._unlocked.append(self._name)
            self._barrier.wait(3)
            return rows
        return cursor

    def close(self) -> None:
        db = self._db
        self._db = None
        if db is not None:
            db.close()


class _HideGeneration:
    """Report ``generation`` as missing so the migration retries an ``ALTER`` that already landed."""

    def __init__(self, db):
        self._db = db

    def execute(self, sql, parameters=()):
        cursor = self._db.execute(sql, parameters)
        if "table_info(tripped)" in sql:
            return [row for row in cursor.fetchall() if row[1] != "generation"]
        return cursor

    def close(self) -> None:
        db = self._db
        self._db = None
        if db is not None:
            db.close()


class TripGenerationTests(unittest.TestCase):
    """CSL-804: a probe that ended before a later trip must not clear that trip."""

    def setUp(self) -> None:
        self.path = os.path.join(temp_dir(self), "breaker.db")
        create_breaker_store(self.path)

    def _open(self) -> ConnectionBreaker:
        breaker = ConnectionBreaker(self.path)
        self.addCleanup(breaker.close)
        return breaker

    def test_stale_probe_on_another_breaker_does_not_clear_a_newer_trip(self) -> None:
        first = self._open()
        self.assertTrue(first.report("conn-a", "rate_limit"))
        lease = first.begin_probe("conn-a")  # probe is in flight, then it ends
        second = self._open()  # another breaker on the same store
        self.assertTrue(second.report("conn-a", "plan_limit"))  # a newer trip
        self.assertFalse(first.reset_after_probe("conn-a", lease, True))
        self.assertTrue(first.tripped("conn-a"))
        self.assertTrue(second.tripped("conn-a"))
        # The newer trip still needs its own probe.
        fresh = second.begin_probe("conn-a")
        self.assertTrue(second.reset_after_probe("conn-a", fresh, True))
        self.assertFalse(first.tripped("conn-a"))

    def test_a_store_from_before_the_generation_column_still_keeps_a_newer_trip(self) -> None:
        path = os.path.join(temp_dir(self), "old.db")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        db = sqlite3.connect(path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        db.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, connection TEXT NOT NULL,"
                   " plugin_id TEXT NOT NULL, provider TEXT NOT NULL, model_version TEXT NOT NULL)")
        db.execute("CREATE TABLE faulted (run_id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE tripped (connection TEXT PRIMARY KEY, event_type TEXT NOT NULL)")
        db.execute("INSERT INTO meta VALUES ('magic', 'csl-breaker-store/v0')")
        db.commit()
        db.close()
        first = ConnectionBreaker(path)
        self.addCleanup(first.close)
        self.assertTrue(first.report("conn-a", "rate_limit"))
        lease = first.begin_probe("conn-a")
        second = ConnectionBreaker(path)
        self.addCleanup(second.close)
        self.assertTrue(second.report("conn-a", "usage_window_exhausted"))
        self.assertFalse(first.reset_after_probe("conn-a", lease, True))
        self.assertTrue(second.tripped("conn-a"))

    def test_a_cleared_generation_is_not_reused_for_the_next_trip(self) -> None:
        first = self._open()
        second = ConnectionBreaker(self.path)
        self.assertTrue(first.report("conn-a", "rate_limit"))
        lease_a = first.begin_probe("conn-a")
        lease_b = second.begin_probe("conn-a")
        self.assertTrue(second.reset_after_probe("conn-a", lease_b, True))
        second.close()
        reopened = self._open()  # the clearer comes back with an empty lease table
        self.assertTrue(reopened.report("conn-a", "plan_limit"))
        self.assertFalse(first.reset_after_probe("conn-a", lease_a, True))
        self.assertTrue(reopened.tripped("conn-a"))

    def test_two_openers_migrate_a_legacy_store_once(self) -> None:
        uri = "file:legacy-breaker-migration?mode=memory&cache=shared"
        setup = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
        self.addCleanup(setup.close)
        setup.execute("CREATE TABLE tripped (connection TEXT PRIMARY KEY, event_type TEXT NOT NULL)")
        setup.execute("INSERT INTO tripped VALUES ('conn-a', 'rate_limit')")
        barrier = threading.Barrier(2)
        alters: list[str] = []
        unlocked: list[str] = []
        outcome: list[tuple] = []
        prepared = []
        for name in ("A", "B"):
            db = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False, timeout=5)
            db.execute("PRAGMA busy_timeout=5000")
            prepared.append((name, db))

        def open_one(name: str, db) -> None:
            breaker = ConnectionBreaker.__new__(ConnectionBreaker)
            breaker._db = _SchemaRace(db, name, barrier, alters, unlocked)
            try:
                breaker._ensure_generation()
                outcome.append((name, None))
            except BaseException as exc:  # noqa: BLE001 - the assertion is on this thread's result
                outcome.append((name, exc))
            finally:
                breaker._db.close()

        workers = [threading.Thread(target=open_one, args=item) for item in prepared]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(sorted(unlocked), [])
        self.assertEqual([error for _name, error in outcome], [None, None])
        self.assertEqual(len(alters), 1)
        self.assertEqual(setup.execute("SELECT generation FROM tripped").fetchone()[0], 1)
        self.assertEqual(setup.execute("SELECT generation FROM trip_seq").fetchone()[0], 1)

    def test_duplicate_generation_column_is_already_migrated(self) -> None:
        uri = "file:legacy-breaker-duplicate?mode=memory&cache=shared"
        db = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
        db.execute("CREATE TABLE tripped (connection TEXT PRIMARY KEY, event_type TEXT NOT NULL)")
        db.execute("INSERT INTO tripped VALUES ('conn-a', 'rate_limit')")
        db.execute("ALTER TABLE tripped ADD COLUMN generation INTEGER NOT NULL DEFAULT 1")
        db.execute("UPDATE tripped SET generation = 4")
        breaker = ConnectionBreaker.__new__(ConnectionBreaker)
        breaker._db = _HideGeneration(db)
        try:
            breaker._ensure_generation()
            self.assertEqual(db.execute("SELECT generation FROM trip_seq").fetchone()[0], 4)
        finally:
            breaker._db.close()


class StorePathTests(unittest.TestCase):
    def test_a_505_byte_sqlite_path_is_a_refusal_not_a_crash(self) -> None:
        root = temp_dir(self)
        path = _deep_path(root, 505)
        self.assertEqual(len(path.encode()), 505)
        for opener in (create_breaker_store, create_revocation_store):
            with self.subTest(opener=opener.__name__):
                with self.assertRaises(Refused) as ctx:
                    opener(path)
                self.assertEqual(ctx.exception.code, "store_path_too_long")
                self.assertFalse(os.path.exists(path))
        with self.assertRaises(Refused) as ctx:
            create_ledger(path, "ab" * 32)
        self.assertEqual(ctx.exception.code, "store_path_too_long")
        # 504 bytes plus the 8-byte "-journal" suffix is still inside SQLite's 512-byte limit.
        short = _deep_path(temp_dir(self), 504)
        self.assertEqual(len(short.encode()), 504)
        create_breaker_store(short)
        self.assertTrue(os.path.isfile(short))

    def test_a_relative_path_is_judged_by_the_path_sqlite_opens(self) -> None:
        root = temp_dir(self)
        absolute = _deep_path(root, 520)
        relative = os.path.relpath(absolute, root)
        self.assertLessEqual(len(relative.encode()) + len(b"-journal"), 512)
        self.assertGreater(len(absolute.encode()) + len(b"-journal"), 512)
        previous = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, previous)
        for opener in (create_breaker_store, create_revocation_store):
            with self.subTest(opener=opener.__name__):
                with self.assertRaises(Refused) as ctx:
                    opener(relative)
                self.assertEqual(ctx.exception.code, "store_path_too_long")
                self.assertFalse(os.path.exists(absolute))
        with self.assertRaises(Refused) as ctx:
            create_ledger(relative, "ab" * 32)
        self.assertEqual(ctx.exception.code, "store_path_too_long")
        self.assertFalse(os.path.exists(absolute))

    def test_a_component_past_name_max_is_a_refusal_and_leaves_nothing(self) -> None:
        root = temp_dir(self)
        path = os.path.join(root, "n" * 256)
        self.assertLess(len(path.encode()) + len(b"-journal"), 512)
        for opener in (create_breaker_store, create_revocation_store):
            with self.subTest(opener=opener.__name__):
                with self.assertRaises(Refused) as ctx:
                    opener(path)
                self.assertEqual(ctx.exception.code, "store_path_too_long")
                self.assertFalse(os.path.lexists(path))
        with self.assertRaises(Refused) as ctx:
            create_ledger(path, "ab" * 32)
        self.assertEqual(ctx.exception.code, "store_path_too_long")
        self.assertFalse(os.path.lexists(path))

    def test_basename_plus_journal_suffix_respects_name_max(self) -> None:
        root = temp_dir(self)
        limit = os.statvfs(root).f_namemax
        journal = len(b"-journal")
        ok = os.path.join(root, "a" * (limit - journal))
        bad = os.path.join(root, "b" * (limit - journal + 1))
        self.assertEqual(len(os.path.basename(ok).encode()) + journal, limit)
        self.assertEqual(len(os.path.basename(bad).encode()) + journal, limit + 1)
        create_breaker_store(ok)
        self.assertTrue(os.path.isfile(ok))
        other = os.path.join(root, "c" * (limit - journal))
        create_revocation_store(other)
        self.assertTrue(os.path.isfile(other))
        for opener in (create_breaker_store, create_revocation_store):
            with self.subTest(opener=opener.__name__):
                with self.assertRaises(Refused) as ctx:
                    opener(bad)
                self.assertEqual(ctx.exception.code, "store_path_too_long")
                self.assertFalse(os.path.lexists(bad))
                self.assertFalse(os.path.lexists(bad + "-journal"))
        with self.assertRaises(Refused) as ctx:
            create_ledger(bad, "ab" * 32)
        self.assertEqual(ctx.exception.code, "store_path_too_long")
        self.assertFalse(os.path.lexists(bad))

    def test_uri_and_memory_names_are_not_opened(self) -> None:
        root = temp_dir(self)
        previous = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, previous)
        for name in (":memory:", "file:actual.db?mode=rwc", "file:actual.db"):
            with self.subTest(name=name):
                with self.assertRaises(Refused) as ctx:
                    create_breaker_store(name)
                self.assertEqual(ctx.exception.code, "store_path_invalid")
                with self.assertRaises(Refused) as ctx:
                    create_revocation_store(name)
                self.assertEqual(ctx.exception.code, "store_path_invalid")
        self.assertEqual(os.listdir(root), [])

    def test_non_encodable_store_path_is_a_refusal(self) -> None:
        root = temp_dir(self)
        code = (
            "import os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from recursion_core.plugin_security import Refused, create_breaker_store\n"
            "path = os.path.join(sys.argv[2], 'caf\\u00e9.db')\n"
            "try:\n"
            "    create_breaker_store(path)\n"
            "    print('OPENED')\n"
            "except Refused as exc:\n"
            "    print('REFUSED', exc.code)\n"
            "except Exception as exc:\n"
            "    print('ERR', type(exc).__name__)\n"
            "from recursion_core.child_env.policy import _resolve\n"
            "try:\n"
            "    print('RESOLVE', _resolve('/tmp/caf\\u00e9')[2])\n"
            "except Exception as exc:\n"
            "    print('RESOLVE', type(exc).__name__)\n"
        )
        env = os.environ.copy()
        env.update({"LC_ALL": "C", "LANG": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"})
        proc = subprocess.run(
            [sys.executable, "-c", code, os.path.abspath("core"), root],
            capture_output=True, text=True, env=env, cwd=os.path.abspath("."),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.splitlines(),
            ["REFUSED store_path_not_encodable", "RESOLVE value_not_encodable"],
        )


@needs_crypto
class PluginTrustPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dev = Key()
        self.root = temp_dir(self)
        self.source = PLUGIN_SOURCE
        self.raw = manifest_bytes(self.dev.public, source=self.source)
        digest = __import__("hashlib").sha256(self.source).hexdigest()
        from recursion_core.plugin_security.signing import key_id
        self.grant = Grant("demo.plugin", digest, key_id(self.dev.public), ("read", "propose", "order"))
        self.loader = PluginLoader(
            TrustStore({"dev": self.dev.public}), {"demo.plugin": self.grant},
            __import__("recursion_core.plugin_security", fromlist=["ReservedEffects"]).ReservedEffects(),
            __import__("recursion_core.plugin_security", fromlist=["CoreHealth"]).CoreHealth(),
            FakeGilProbe(),
        )

    def test_a_symlinked_install_script_is_refused_by_load(self) -> None:
        from recursion_core.plugin_security.paths import read_within_root
        outside = temp_dir(self)
        secret = os.path.join(outside, "secret.sh")
        with open(secret, "wb") as fh:
            fh.write(b"SECRET\n")
        script = os.path.join(self.root, "install.sh")
        with open(script, "wb") as fh:
            fh.write(b"safe\n")
        raw = manifest_bytes(self.dev.public, source=self.source, install_scripts=("install.sh",))
        loaded = self.loader.load(raw, self.dev.sign(raw), self.source, root=self.root)
        self.assertEqual(loaded.trust, "signed")
        # The open that load uses must be the same descriptor it read. A later
        # path-based reopen, after the name is swapped for a symlink, would see SECRET.
        real_open = os.open

        def open_then_swap(path, flags, mode=0o777, *, dir_fd=None):
            name = path if isinstance(path, str) else os.fsdecode(path)
            if name.endswith("install.sh") and not (flags & os.O_NOFOLLOW):
                if not os.path.islink(script):
                    os.remove(script)
                    os.symlink(secret, script)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        os.open = open_then_swap
        try:
            self.assertEqual(read_within_root(self.root, "install.sh"), b"safe\n")
        finally:
            os.open = real_open
        if not os.path.islink(script):
            os.remove(script)
            os.symlink(secret, script)
        with self.assertRaises(Refused) as ctx:
            self.loader.load(raw, self.dev.sign(raw), self.source, root=self.root)
        self.assertEqual(ctx.exception.code, "plugin_path_escapes_root")
        for declared in ("../" + os.path.basename(outside) + "/secret.sh", secret, "foo/../../etc/passwd"):
            with self.subTest(declared=declared):
                with self.assertRaises(Refused) as ctx:
                    read_within_root(self.root, declared)
                self.assertEqual(ctx.exception.code, "plugin_path_escapes_root")

    def test_a_fifo_install_script_does_not_block_load(self) -> None:
        script = os.path.join(self.root, "install.sh")
        os.mkfifo(script)
        raw = manifest_bytes(self.dev.public, source=self.source, install_scripts=("install.sh",))
        outcome: list = []

        def attempt() -> None:
            try:
                self.loader.load(raw, self.dev.sign(raw), self.source, root=self.root)
                outcome.append("loaded")
            except BaseException as exc:  # noqa: BLE001 - the assertion is on the test thread
                outcome.append(exc)

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive(), "load blocked on a fifo install script")
        self.assertIsInstance(outcome[0], Refused)
        self.assertEqual(outcome[0].code, "plugin_path_escapes_root")

    def test_install_scripts_require_a_root(self) -> None:
        raw = manifest_bytes(self.dev.public, source=self.source, install_scripts=("install.sh",))
        with self.assertRaises(Refused) as ctx:
            self.loader.load(raw, self.dev.sign(raw), self.source)
        self.assertEqual(ctx.exception.code, "plugin_root_required")

    def test_swapped_code_with_the_same_manifest_drops_signed_trust(self) -> None:
        loaded = self.loader.load(self.raw, self.dev.sign(self.raw), self.source)
        self.assertEqual(loaded.trust, "signed")
        self.assertIn("order", loaded.permissions)
        with self.assertRaises(Refused) as ctx:
            self.loader.load(self.raw, self.dev.sign(self.raw), self.source + b"\n# tampered\n")
        self.assertEqual(ctx.exception.code, "artifact_digest_mismatch")
        self.assertEqual(self.loader._active["demo.plugin"].trust, "unsigned")
        self.assertNotIn("order", self.loader.permissions("demo.plugin"))

    def test_install_scripts_cannot_be_absolute_or_dotdot(self) -> None:
        for script in ("../outside.sh", "/tmp/install.sh", "sub/../../etc/passwd"):
            with self.subTest(script=script):
                raw = manifest_bytes(self.dev.public, install_scripts=(script,))
                with self.assertRaises(Refused) as ctx:
                    parse_manifest(raw)
                self.assertEqual(ctx.exception.code, "manifest_install_scripts")

    def test_a_changed_manifest_is_rechecked_and_loses_stale_trust(self) -> None:
        loaded = self.loader.load(self.raw, self.dev.sign(self.raw), self.source)
        self.assertEqual(loaded.trust, "signed")
        self.assertIn("order", loaded.permissions)
        data = parse(self.raw)
        data["version"] = "9.9.9"  # same artifact digest, signature no longer matches
        changed = canonical(data)
        again = self.loader.load(changed, self.dev.sign(self.raw), self.source)
        self.assertEqual(again.trust, "unsigned")
        self.assertNotIn("order", self.loader.permissions("demo.plugin"))
        # The same bytes a second time are still a duplicate, not a fresh trust decision.
        with self.assertRaises(Refused) as ctx:
            self.loader.load(changed, self.dev.sign(self.raw), self.source)
        self.assertEqual(ctx.exception.code, "plugin_already_active")


class RevocationGenerationTests(unittest.TestCase):
    def test_a_stale_trust_store_cannot_roll_the_generation_backward(self) -> None:
        path = os.path.join(temp_dir(self), "revoked.db")
        create_revocation_store(path)
        kid = "ed25519:" + "ab" * 32
        # Both stores are opened before any revoke, so the second one's memory stays at 0
        # while the first store advances the database to 2.
        current = TrustStore({}, revocation_store=path)
        stale = TrustStore({}, revocation_store=path)
        self.addCleanup(current.close)
        self.addCleanup(stale.close)
        self.assertEqual(current.revoke(kid), 1)
        self.assertEqual(current.revoke(kid), 2)
        self.assertEqual(stale.revoke(kid), 3)
        reloaded = TrustStore({}, revocation_store=path)
        self.addCleanup(reloaded.close)
        self.assertEqual(reloaded.generation(kid), 3)
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT generation FROM revoked WHERE key_id=?", (kid,)).fetchone()[0], 3)

    def test_a_failed_write_does_not_stick_later_revokes(self) -> None:
        path = os.path.join(temp_dir(self), "revoked.db")
        create_revocation_store(path)
        kid = "ed25519:" + "ab" * 32
        trust = TrustStore({}, revocation_store=path)
        self.addCleanup(trust.close)
        self.assertEqual(trust.revoke(kid), 1)
        trust._db.execute("PRAGMA query_only=ON")
        with self.assertRaises(Refused) as ctx:
            trust.revoke(kid)
        self.assertEqual(ctx.exception.code, "revocation_not_durable")
        self.assertEqual(trust.generation(kid), 2)
        trust._db.execute("PRAGMA query_only=OFF")
        self.assertEqual(trust.revoke(kid), 3)
        self.assertEqual(trust.revoke(kid), 4)

    def test_a_reopen_after_chdir_keeps_the_original_store(self) -> None:
        root = temp_dir(self)
        original = os.path.join(root, "orig")
        elsewhere = os.path.join(root, "elsewhere")
        os.mkdir(original)
        os.mkdir(elsewhere)
        create_revocation_store(os.path.join(original, "rev.db"))
        create_revocation_store(os.path.join(elsewhere, "rev.db"))
        previous = os.getcwd()
        os.chdir(original)
        self.addCleanup(os.chdir, previous)
        kid = "ed25519:" + "ab" * 32
        trust = TrustStore({}, revocation_store="rev.db")
        self.addCleanup(trust.close)
        self.assertTrue(os.path.isabs(trust._store_path))
        self.assertEqual(trust.revoke(kid), 1)
        os.chdir(elsewhere)
        trust._db.execute("PRAGMA query_only=ON")
        with self.assertRaises(Refused) as ctx:
            trust.revoke(kid)
        self.assertEqual(ctx.exception.code, "revocation_not_durable")
        trust._db.execute("PRAGMA query_only=OFF")
        self.assertEqual(trust.revoke(kid), 3)
        original_db = sqlite3.connect(os.path.join(original, "rev.db"))
        self.addCleanup(original_db.close)
        other_db = sqlite3.connect(os.path.join(elsewhere, "rev.db"))
        self.addCleanup(other_db.close)
        self.assertEqual(
            original_db.execute("SELECT generation FROM revoked WHERE key_id=?", (kid,)).fetchone()[0], 3)
        self.assertIsNone(other_db.execute("SELECT key_id FROM revoked").fetchone())

    def test_a_reopen_that_cannot_connect_stays_typed(self) -> None:
        path = os.path.join(temp_dir(self), "rev.db")
        create_revocation_store(path)
        kid = "ed25519:" + "cd" * 32
        trust = TrustStore({}, revocation_store=path)
        self.addCleanup(trust.close)
        self.assertEqual(trust.revoke(kid), 1)
        trust._db.execute("PRAGMA query_only=ON")
        real_connect = sqlite3.connect

        def fail_connect(*_args, **_kwargs):
            raise sqlite3.OperationalError("unable to open database file")

        sqlite3.connect = fail_connect
        try:
            with self.assertRaises(Refused) as ctx:
                trust.revoke(kid)
            self.assertEqual(ctx.exception.code, "revocation_not_durable")
        finally:
            sqlite3.connect = real_connect


class ChildEnvEncodingTests(unittest.TestCase):
    def test_spawn_turns_a_unicode_encode_error_into_a_refusal(self) -> None:
        from recursion_core.child_env import EnvRefused, build_child_env, spawn
        from recursion_core.child_env.tests.test_child_env import _home
        home = _home(self)
        env = build_child_env("codex", home=home, auth_mode="subscription", parent={"PATH": "/usr/bin:/bin"})

        def boom(*_args, **_kwargs):
            raise UnicodeEncodeError("ascii", "caf", 3, 4, "ordinal not in range(128)")

        with self.assertRaises(EnvRefused) as ctx:
            spawn(["/bin/true"], env, popen=boom)
        self.assertEqual(ctx.exception.cause, "value_not_encodable")


if __name__ == "__main__":
    unittest.main()
