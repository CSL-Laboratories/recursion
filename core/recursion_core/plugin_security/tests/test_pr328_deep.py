"""Repros for plugin trust, manifest paths, and load isolation on PR #328.

Each test fails on 4f99503d and names the behavior the loader must keep.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import threading
import types
import unittest

from recursion_core.plugin_security import (
    CoreHealth, Grant, PluginLoader, Refused, ReservedEffects, TrustStore, parse_manifest,
)
from recursion_core.plugin_security.loader import _module_name
from recursion_core.plugin_security.manifest import _untrusted_path
from recursion_core.plugin_security.signing import key_id
from recursion_core.plugin_security.tests.support import (
    PLUGIN_SOURCE, FakeGilProbe, Key, manifest_bytes, needs_crypto, temp_dir,
)


def _loader(trust, grants, probe=None):
    return PluginLoader(trust, grants, ReservedEffects(frozenset(), ("venue:",)),
                        CoreHealth(), probe or FakeGilProbe())


@needs_crypto
class StaleRevocationTests(unittest.TestCase):
    def test_a_stale_trust_store_cannot_roll_order_back_to_an_older_generation(self) -> None:
        dev = Key()
        kid = key_id(dev.public)
        path = os.path.join(temp_dir(self), "revoked.db")
        from recursion_core.plugin_security import create_revocation_store
        create_revocation_store(path)
        # Both stores are opened before any revoke, so the second one's memory stays at 0.
        current = TrustStore({"dev": dev.public}, revocation_store=path)
        stale = TrustStore({"dev": dev.public}, revocation_store=path)
        self.addCleanup(current.close)
        self.addCleanup(stale.close)
        self.assertEqual(current.revoke(kid), 1)
        self.assertEqual(current.revoke(kid), 2)
        written = stale.revoke(kid)
        self.assertGreaterEqual(written, 3)
        reloaded = TrustStore({"dev": dev.public}, revocation_store=path)
        self.addCleanup(reloaded.close)
        self.assertGreaterEqual(reloaded.generation(kid), 3)
        # Naming the key with no generation covers generation 1 only. After two
        # revokes that grant must not get order back.
        digest = hashlib.sha256(PLUGIN_SOURCE).hexdigest()
        grant = Grant("demo.plugin", digest, None, ("read", "propose", "order"), True, kid)
        loaded = _loader(reloaded, {"demo.plugin": grant}).load(
            manifest_bytes(dev.public), dev.sign(manifest_bytes(dev.public)), PLUGIN_SOURCE)
        self.assertNotIn("order", loaded.permissions)


@needs_crypto
class ManifestPathTests(unittest.TestCase):
    def test_a_credential_ref_cannot_walk_into_a_reserved_name(self) -> None:
        dev = Key()
        reserved = ReservedEffects(frozenset(), ("venue:",))
        escaped = "a/../venue:live-key"
        with self.subTest("reserved check"):
            self.assertTrue(reserved.credential_reserved(escaped))
        with self.subTest("manifest"):
            raw = manifest_bytes(dev.public, permissions=("credential",), credentials=(escaped,))
            with self.assertRaises(Refused) as ctx:
                parse_manifest(raw)
            self.assertEqual(ctx.exception.code, "manifest_credential_refs")
        with self.subTest("plain ref still parses"):
            raw = manifest_bytes(dev.public, permissions=("credential",), credentials=("venue:key",))
            self.assertEqual(parse_manifest(raw).credential_refs, ("venue:key",))

    def test_an_install_script_cannot_leave_the_plugin_tree(self) -> None:
        dev = Key()
        for script in ("../outside.sh", "/tmp/install.sh", "sub/../../etc/passwd"):
            with self.subTest(script=script):
                raw = manifest_bytes(dev.public, install_scripts=(script,))
                with self.assertRaises(Refused) as ctx:
                    parse_manifest(raw)
                self.assertEqual(ctx.exception.code, "manifest_install_scripts")
        raw = manifest_bytes(dev.public, install_scripts=("scripts/install.sh",))
        self.assertEqual(parse_manifest(raw).install_scripts, ("scripts/install.sh",))


@needs_crypto
class LoadIsolationTests(unittest.TestCase):
    def test_a_failed_load_does_not_leave_modules_behind(self) -> None:
        dev = Key()
        kid = key_id(dev.public)
        trust = TrustStore({"dev": dev.public})
        good_src = (
            b"import sys, types\n"
            b"m = types.ModuleType('good_helper')\n"
            b"m.marker = 'kept'\n"
            b"sys.modules['good_helper'] = m\n"
            b"VALUE = 1\n"
        )
        good_raw = manifest_bytes(dev.public, plugin_id="good.plugin", source=good_src, permissions=("read",))
        grant = Grant("good.plugin", hashlib.sha256(good_src).hexdigest(), kid, ("read",))
        loader = _loader(trust, {"good.plugin": grant})
        loader.load(good_raw, dev.sign(good_raw), good_src)
        saved_json = sys.modules.get("json")
        bad_src = (
            b"import sys, types\n"
            b"m = types.ModuleType('evil_helper')\n"
            b"m.marker = 'pwned'\n"
            b"sys.modules['evil_helper'] = m\n"
            b"sys.modules['json'] = types.ModuleType('json')\n"
            b"raise RuntimeError('x')\n"
        )
        bad_raw = manifest_bytes(dev.public, plugin_id="bad.plugin", source=bad_src, permissions=("read",))
        try:
            with self.assertRaises(Refused) as ctx:
                loader.load(bad_raw, dev.sign(bad_raw), bad_src)
            self.assertEqual(ctx.exception.code, "plugin_import_failed")
            # A name planted outside this plugin's namespace, and not under a
            # plugin root, is left in place. Rollback still restores a replaced
            # entry (``json``) and does not touch an earlier plugin's module.
            self.assertIn("evil_helper", sys.modules)
            self.assertIs(sys.modules.get("json"), saved_json)
            self.assertEqual(getattr(sys.modules.get("good_helper"), "marker", None), "kept")
            self.assertEqual(loader.permissions("good.plugin"), ("read",))
            self.assertEqual(loader.permissions("bad.plugin"), ())
        finally:
            sys.modules.pop("evil_helper", None)
            sys.modules.pop("good_helper", None)
            if saved_json is not None:
                sys.modules["json"] = saved_json

    def test_distinct_plugin_ids_do_not_share_an_import_name(self) -> None:
        dev = Key()
        kid = key_id(dev.public)
        trust = TrustStore({"dev": dev.public})
        first = (
            b"import sys, types\n"
            b"m = types.ModuleType(__name__)\n"
            b"m.marker = 'from-first'\n"
            b"sys.modules[__name__] = m\n"
            b"VALUE = 'first'\n"
        )
        later = b"import sys\nVALUE = getattr(sys.modules.get(__name__), 'marker', 'none')\n"
        ids = ("ab.cd", "ab-cd", "ab_cd")
        sources = {"ab.cd": first, "ab-cd": later, "ab_cd": later}
        grants = {}
        raws = {}
        for plugin_id in ids:
            source = sources[plugin_id]
            raws[plugin_id] = manifest_bytes(
                dev.public, plugin_id=plugin_id, source=source, permissions=("read",))
            grants[plugin_id] = Grant(plugin_id, hashlib.sha256(source).hexdigest(), kid, ("read",))
        loader = _loader(trust, grants)
        loaded = {}
        try:
            for plugin_id in ids:
                loaded[plugin_id] = loader.load(raws[plugin_id], dev.sign(raws[plugin_id]), sources[plugin_id])
            names = [loaded[plugin_id].module.__name__ for plugin_id in ids]
            self.assertEqual(len(set(names)), 3, names)
            self.assertEqual(loaded["ab-cd"].module.VALUE, "none")
            self.assertEqual(loaded["ab_cd"].module.VALUE, "none")
        finally:
            for module in list(sys.modules):
                if module.startswith("csl_plugin_"):
                    sys.modules.pop(module, None)


@needs_crypto
class WindowsPathTests(unittest.TestCase):
    def test_drive_paths_are_refused_on_every_host(self) -> None:
        dev = Key()
        for script in (
            "C:/Windows/Temp/install.sh",
            "c:/Windows/Temp/install.sh",
            "c:../outside.sh",
            "C:outside.sh",
        ):
            with self.subTest(script=script):
                raw = manifest_bytes(dev.public, install_scripts=(script,))
                with self.assertRaises(Refused) as ctx:
                    parse_manifest(raw)
                self.assertEqual(ctx.exception.code, "manifest_install_scripts")
        reserved = ReservedEffects(frozenset(), ("venue:",))
        escaped = "c:../venue:live-key"
        self.assertTrue(reserved.credential_reserved(escaped))
        # A ``..`` left after either fold is refused even when it is not a reserved prefix.
        self.assertTrue(reserved.credential_reserved("zz/../../not-reserved"))
        raw = manifest_bytes(dev.public, permissions=("credential",), credentials=(escaped,))
        with self.assertRaises(Refused) as ctx:
            parse_manifest(raw)
        self.assertEqual(ctx.exception.code, "manifest_credential_refs")
        self.assertTrue(_untrusted_path("scripts/\0install.sh"))
        self.assertTrue(_untrusted_path("\0"))


@needs_crypto
class SharedImportRollbackTests(unittest.TestCase):
    def _load_failing(self, source: bytes, *, plugin_id: str = "bad.plugin", root: str | None = None):
        dev = Key()
        kid = key_id(dev.public)
        trust = TrustStore({"dev": dev.public})
        raw = manifest_bytes(dev.public, plugin_id=plugin_id, source=source, permissions=("read",))
        grant = Grant(plugin_id, hashlib.sha256(source).hexdigest(), kid, ("read",))
        loader = _loader(trust, {plugin_id: grant})
        with self.assertRaises(Refused) as ctx:
            loader.load(raw, dev.sign(raw), source, root=root)
        self.assertEqual(ctx.exception.code, "plugin_import_failed")
        return plugin_id

    def test_a_failed_load_keeps_stdlib_imported_during_the_load(self) -> None:
        sys.modules.pop("graphlib", None)
        plugin_id = "bad.plugin"
        source = (
            b"import graphlib, sys, types\n"
            b"planted = types.ModuleType(__name__)\n"
            b"sys.modules[__name__] = planted\n"
            b"raise RuntimeError('boom')\n"
        )
        try:
            self._load_failing(source, plugin_id=plugin_id)
            self.assertIn("graphlib", sys.modules)
            self.assertNotIn(_module_name(plugin_id), sys.modules)
        finally:
            sys.modules.pop("graphlib", None)
            sys.modules.pop(_module_name(plugin_id), None)

    def test_a_failed_load_keeps_a_module_imported_by_another_thread(self) -> None:
        sys.modules.pop("netrc", None)
        gate = threading.Event()
        done = threading.Event()
        coord = types.ModuleType("csl_coord_deep")
        coord.gate = gate
        coord.done = done
        sys.modules["csl_coord_deep"] = coord

        def importer() -> None:
            if not gate.wait(5):
                return
            import netrc  # noqa: F401 - the assertion is that this name survives rollback
            done.set()

        worker = threading.Thread(target=importer)
        worker.start()
        source = (
            b"import csl_coord_deep\n"
            b"csl_coord_deep.gate.set()\n"
            b"csl_coord_deep.done.wait(5)\n"
            b"raise RuntimeError('boom')\n"
        )
        try:
            self._load_failing(source)
            self.assertTrue(done.is_set())
            self.assertIn("netrc", sys.modules)
        finally:
            worker.join(5)
            sys.modules.pop("netrc", None)
            sys.modules.pop("csl_coord_deep", None)

    def test_a_failed_load_drops_modules_under_the_plugin_root_only(self) -> None:
        root = temp_dir(self)
        outside = temp_dir(self)
        inside = os.path.join(root, "planted.py")
        spec_inside = os.path.join(root, "spec_planted.py")
        external = os.path.join(outside, "external.py")
        for path in (inside, spec_inside, external):
            with open(path, "wb") as fh:
                fh.write(b"x = 1\n")
        source = (
            "import sys, types\n"
            "inside = types.ModuleType('under_root_mod')\n"
            "inside.__file__ = %r\n"
            "sys.modules['under_root_mod'] = inside\n"
            "spec_mod = types.ModuleType('under_root_spec')\n"
            "spec_mod.__spec__ = types.SimpleNamespace(origin=%r)\n"
            "sys.modules['under_root_spec'] = spec_mod\n"
            "outside = types.ModuleType('outside_mod')\n"
            "outside.__file__ = %r\n"
            "sys.modules['outside_mod'] = outside\n"
            "raise RuntimeError('boom')\n" % (inside, spec_inside, external)
        ).encode()
        try:
            self._load_failing(source, root=root)
            self.assertNotIn("under_root_mod", sys.modules)
            self.assertNotIn("under_root_spec", sys.modules)
            self.assertIn("outside_mod", sys.modules)
        finally:
            for name in ("under_root_mod", "under_root_spec", "outside_mod"):
                sys.modules.pop(name, None)


class RevocationPublishTests(unittest.TestCase):
    def test_a_failed_revoke_does_not_publish_a_generation_below_the_row(self) -> None:
        path = os.path.join(temp_dir(self), "revoked.db")
        from recursion_core.plugin_security import create_revocation_store
        create_revocation_store(path)
        kid = "ed25519:" + "ab" * 16
        current = TrustStore({}, revocation_store=path)
        stale = TrustStore({}, revocation_store=path)
        self.addCleanup(current.close)
        self.addCleanup(stale.close)
        self.assertEqual(current.revoke(kid), 1)
        self.assertEqual(current.revoke(kid), 2)
        self.assertIsNone(stale.generation(kid))
        stale._db.execute("PRAGMA query_only=ON")
        with self.assertRaises(Refused) as ctx:
            stale.revoke(kid)
        self.assertEqual(ctx.exception.code, "revocation_not_durable")
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        row = db.execute("SELECT generation FROM revoked WHERE key_id = ?", (kid,)).fetchone()[0]
        self.assertEqual(row, 2)
        seen = stale.generation(kid)
        self.assertNotEqual(seen, 1)
        if seen is not None:
            self.assertGreaterEqual(seen, row)
        # The speculative value is not published before the write commits.
        fresh = TrustStore({}, revocation_store=path)
        self.addCleanup(fresh.close)
        during: dict[str, int | None] = {}
        db = fresh._db

        class _ExecSpy:
            def execute(self, sql, *args, **kwargs):
                if isinstance(sql, str) and "MAX(revoked.generation" in sql:
                    during["value"] = fresh.generation(kid)
                return db.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(db, name)

        fresh._db = _ExecSpy()
        written = fresh.revoke(kid)
        self.assertEqual(written, 3)
        # Still the generation loaded from the row, not the speculative next value.
        self.assertEqual(during["value"], 2)
        self.assertEqual(fresh.generation(kid), written)


class SymlinkedParentTests(unittest.TestCase):
    def test_a_symlinked_parent_dir_is_refused(self) -> None:
        from recursion_core.plugin_security.paths import read_within_root
        root = temp_dir(self)
        outside = temp_dir(self)
        nested = os.path.join(outside, "nested")
        os.mkdir(nested)
        with open(os.path.join(nested, "install.sh"), "wb") as fh:
            fh.write(b"SECRET\n")
        os.symlink(nested, os.path.join(root, "linked"))
        with self.assertRaises(Refused) as ctx:
            read_within_root(root, "linked/install.sh")
        # O_NOFOLLOW|O_DIRECTORY on a symlinked directory returns ENOTDIR, which
        # read_within_root already refuses. The bytes behind the link are not read.
        self.assertEqual(ctx.exception.code, "plugin_path_unreadable")


if __name__ == "__main__":
    unittest.main()
