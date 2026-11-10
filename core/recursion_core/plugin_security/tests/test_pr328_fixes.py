"""Fixes on the PR 328 branch: trip generation, plugin path trust, long store paths, encoding.

Each test names the behaviour that was wrong on head 9f501110 before the fix.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
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

    def test_manifest_paths_cannot_leave_the_trusted_root(self) -> None:
        from recursion_core.plugin_security.paths import resolve_within_root
        outside = temp_dir(self)
        secret = os.path.join(outside, "secret.json")
        with open(secret, "wb") as fh:
            fh.write(b"nope")
        for declared in ("../" + os.path.basename(outside) + "/secret.json", secret, "foo/../../etc/passwd"):
            with self.subTest(declared=declared):
                with self.assertRaises(Refused) as ctx:
                    resolve_within_root(self.root, declared)
                self.assertEqual(ctx.exception.code, "plugin_path_escapes_root")
        link = os.path.join(self.root, "manifest.json")
        os.symlink(secret, link)
        with self.assertRaises(Refused) as ctx:
            resolve_within_root(self.root, "manifest.json")
        self.assertEqual(ctx.exception.code, "plugin_path_escapes_root")
        inside = os.path.join(self.root, "kept.json")
        with open(inside, "wb") as fh:
            fh.write(self.raw)
        self.assertEqual(resolve_within_root(self.root, "kept.json"), inside)

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
