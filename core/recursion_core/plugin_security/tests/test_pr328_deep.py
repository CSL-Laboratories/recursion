"""Repros for plugin trust, manifest paths, and load isolation on PR #328.

Each test fails on 4f99503d and names the behavior the loader must keep.
"""

from __future__ import annotations

import hashlib
import os
import sys
import unittest

from recursion_core.plugin_security import (
    CoreHealth, Grant, PluginLoader, Refused, ReservedEffects, TrustStore, parse_manifest,
)
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
            self.assertNotIn("evil_helper", sys.modules)
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


if __name__ == "__main__":
    unittest.main()
