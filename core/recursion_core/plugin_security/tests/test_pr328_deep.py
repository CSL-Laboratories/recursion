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
            # An assignment into ``sys.modules`` is not a genuine import, so the
            # planted name is removed. Rollback still restores a replaced entry
            # (``json``) and does not touch an earlier plugin's module.
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

    def test_a_component_windows_strips_is_refused_on_every_host(self) -> None:
        # Win32 drops trailing spaces and dots, so ``.. `` and ``...`` walk up.
        dev = Key()
        for script in (
            "sub/.. /.. /etc/passwd",
            "sub/.../etc/passwd",
            "scripts/install.sh.",
            "scripts/install.sh ",
        ):
            with self.subTest(script=script):
                raw = manifest_bytes(dev.public, install_scripts=(script,))
                with self.assertRaises(Refused) as ctx:
                    parse_manifest(raw)
                self.assertEqual(ctx.exception.code, "manifest_install_scripts")

    def test_an_install_script_stream_component_is_refused_on_every_host(self) -> None:
        # A colon in a component is a Win32 alternate data stream.
        dev = Key()
        for script in (
            "scripts/install.sh:stream",
            "scripts/install.sh::$DATA",
            "..:$DATA",
            "..::$INDEX_ALLOCATION",
        ):
            with self.subTest(script=script):
                raw = manifest_bytes(dev.public, install_scripts=(script,))
                with self.assertRaises(Refused) as ctx:
                    parse_manifest(raw)
                self.assertEqual(ctx.exception.code, "manifest_install_scripts")
        raw = manifest_bytes(dev.public, install_scripts=("scripts/install.sh",))
        self.assertEqual(parse_manifest(raw).install_scripts, ("scripts/install.sh",))

    def test_win32_device_basenames_are_refused_on_every_host(self) -> None:
        # NUL, CON, PRN, AUX, COM1-9, LPT1-9, CONIN$, CONOUT$, any case, with or without an extension.
        dev = Key()
        scripts = []
        for name in ("NUL", "CON", "PRN", "AUX", "COM1", "COM9", "LPT1", "LPT9", "CONIN$", "CONOUT$"):
            scripts.append(name)
            scripts.append(name.lower() + ".txt")
            scripts.append("scripts/" + name.swapcase())
        scripts.extend(("dir/com1.sh", "lpt2.dat", "NuL.TxT", "scripts/CONOUT$.log"))
        for script in scripts:
            with self.subTest(script=script):
                raw = manifest_bytes(dev.public, install_scripts=(script,))
                with self.assertRaises(Refused) as ctx:
                    parse_manifest(raw)
                self.assertEqual(ctx.exception.code, "manifest_install_scripts")
        for script in ("scripts/install.sh", "scripts/console.sh", "scripts/COM10.txt", "scripts/commit.py"):
            raw = manifest_bytes(dev.public, install_scripts=(script,))
            self.assertEqual(parse_manifest(raw).install_scripts, (script,))

    def test_a_device_name_with_a_space_before_the_dot_is_refused(self) -> None:
        """Win32 cuts a component at the first ``.`` or ``:``, then strips trailing spaces.

        ``nul .txt`` is ``nul`` after that fold. The space sits before the dot, so a
        pattern that only allows an optional extension glued to the device name
        accepts it.
        """
        dev = Key()
        for script in (
            "nul .txt",
            "COM1 .txt",
            "CONIN$ .txt",
            "scripts/nul .txt",
            "lpt1 .bin",
            "NuL .TxT",
            "scripts/CONOUT$ .log",
            "aux  .dat",
            "com9 .txt",
        ):
            with self.subTest(script=script):
                raw = manifest_bytes(dev.public, install_scripts=(script,))
                with self.assertRaises(Refused) as ctx:
                    parse_manifest(raw)
                self.assertEqual(ctx.exception.code, "manifest_install_scripts")
        for script in (
            "scripts/install.sh",
            "scripts/console.sh",
            "scripts/COM10.txt",
            "scripts/commit.py",
            "scripts/nulled.txt",
        ):
            raw = manifest_bytes(dev.public, install_scripts=(script,))
            self.assertEqual(parse_manifest(raw).install_scripts, (script,))


@needs_crypto
class SharedImportRollbackTests(unittest.TestCase):
    def _prepared(self, source: bytes, plugin_id: str = "bad.plugin"):
        dev = Key()
        kid = key_id(dev.public)
        trust = TrustStore({"dev": dev.public})
        raw = manifest_bytes(dev.public, plugin_id=plugin_id, source=source, permissions=("read",))
        grant = Grant(plugin_id, hashlib.sha256(source).hexdigest(), kid, ("read",))
        return _loader(trust, {plugin_id: grant}), raw, dev.sign(raw), source

    def _run_failing(self, source: bytes, *, plugin_id: str = "bad.plugin",
                     root: str | None = None) -> str | None:
        loader, raw, sig, source = self._prepared(source, plugin_id)
        try:
            loader.load(raw, sig, source, root=root)
        except Refused as exc:
            return exc.code
        return None

    def _load_failing(self, source: bytes, *, plugin_id: str = "bad.plugin", root: str | None = None):
        self.assertEqual(self._run_failing(source, plugin_id=plugin_id, root=root), "plugin_import_failed")
        return plugin_id

    def _assert_import_consistent(self, name: str):
        """``sys.modules`` and the parent attribute name the same module."""
        import importlib
        module = importlib.import_module(name)
        self.assertIs(sys.modules.get(name), module, name)
        parent_name, dot, attr = name.rpartition(".")
        if dot:
            parent = sys.modules.get(parent_name)
            self.assertIsNotNone(parent, name)
            self.assertIs(getattr(parent, attr), module, name)
        return module

    def _loader_for(self, sources: dict[str, bytes]):
        dev = Key()
        kid = key_id(dev.public)
        trust = TrustStore({"dev": dev.public})
        grants = {}
        raws = {}
        sigs = {}
        for plugin_id, source in sources.items():
            raw = manifest_bytes(dev.public, plugin_id=plugin_id, source=source, permissions=("read",))
            raws[plugin_id] = raw
            sigs[plugin_id] = dev.sign(raw)
            grants[plugin_id] = Grant(plugin_id, hashlib.sha256(source).hexdigest(), kid, ("read",))
        return _loader(trust, grants), raws, sigs

    def test_a_failed_load_keeps_stdlib_imported_during_the_load(self) -> None:
        """A stdlib import from a failed attempt does not split the import tables.

        The attempt drops the module it added. Importing it again yields one
        module: ``sys.modules`` and the parent attribute, when there is one,
        name that same object. A module the plugin assigns under its own name
        is still dropped.
        """
        saved = sys.modules.pop("graphlib", None)
        plugin_id = "bad.plugin"
        source = (
            b"import graphlib, sys, types\n"
            b"planted = types.ModuleType(__name__)\n"
            b"sys.modules[__name__] = planted\n"
            b"raise RuntimeError('boom')\n"
        )
        try:
            self._load_failing(source, plugin_id=plugin_id)
            module = self._assert_import_consistent("graphlib")
            self.assertTrue(hasattr(module, "TopologicalSorter"))
            self.assertNotEqual(getattr(module, "marker", None), "pwned")
            self.assertIsNone(sys.modules.get(_module_name(plugin_id)))
        finally:
            sys.modules.pop("graphlib", None)
            if saved is not None:
                sys.modules["graphlib"] = saved
            sys.modules.pop(_module_name(plugin_id), None)

    def test_a_failed_load_keeps_a_module_imported_by_another_thread(self) -> None:
        """A module imported by another thread during the attempt does not split the tables.

        The attempt may drop it. A later import agrees with ``sys.modules`` and
        with the parent attribute. The other thread can still hold the module
        it imported; that reference is not the import table.
        """
        saved = sys.modules.pop("graphlib", None)
        gate = threading.Event()
        done = threading.Event()
        coord = types.ModuleType("csl_coord_deep")
        coord.gate = gate
        coord.done = done
        coord.mod = None
        coord.cls = None
        sys.modules["csl_coord_deep"] = coord

        def importer() -> None:
            if not gate.wait(5):
                return
            import graphlib
            coord.mod = graphlib
            coord.cls = graphlib.TopologicalSorter
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
            self.assertIsNotNone(coord.mod)
            again = self._assert_import_consistent("graphlib")
            self.assertTrue(hasattr(again, "TopologicalSorter"))
            self.assertIsInstance(again.TopologicalSorter(), again.TopologicalSorter)
        finally:
            worker.join(5)
            sys.modules.pop("graphlib", None)
            if saved is not None:
                sys.modules["graphlib"] = saved
            sys.modules.pop("csl_coord_deep", None)

    def test_a_failed_load_drops_planted_modules_inside_and_outside_the_root(self) -> None:
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
            self.assertNotIn("outside_mod", sys.modules)
        finally:
            for name in ("under_root_mod", "under_root_spec", "outside_mod"):
                sys.modules.pop(name, None)

    def test_a_failed_load_drops_assigned_names_and_keeps_a_real_import(self) -> None:
        planted = (
            "ossaudiodev",
            "recursion_core.child_env.launch",
            _module_name("other.plugin"),
        )
        saved = {name: sys.modules.pop(name) for name in planted if name in sys.modules}
        saved_graphlib = sys.modules.pop("graphlib", None)
        source = (
            "import graphlib, sys, types\n"
            "for name in (%r, %r, %r):\n"
            "    planted = types.ModuleType(name)\n"
            "    planted.marker = 'pwned'\n"
            "    sys.modules[name] = planted\n"
            "raise RuntimeError('boom')\n" % planted
        ).encode()
        try:
            self._load_failing(source)
            # A real import made by the failed attempt is dropped with everything
            # else the attempt added. Importing it again is one consistent module.
            # Planted names do not come back as the attacker's module.
            module = self._assert_import_consistent("graphlib")
            self.assertTrue(hasattr(module, "TopologicalSorter"))
            self.assertNotEqual(getattr(module, "marker", None), "pwned")
            import importlib
            import warnings
            for name in planted:
                self.assertNotEqual(getattr(sys.modules.get(name), "marker", None), "pwned", name)
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", DeprecationWarning)
                        module = importlib.import_module(name)
                except ImportError:
                    self.assertNotIn(name, sys.modules, name)
                else:
                    self.assertNotEqual(getattr(module, "marker", None), "pwned", name)
        finally:
            sys.modules.pop("graphlib", None)
            if saved_graphlib is not None:
                sys.modules["graphlib"] = saved_graphlib
            for name in planted:
                sys.modules.pop(name, None)
                if name in saved:
                    sys.modules[name] = saved[name]

    def test_a_failed_load_drops_an_assigned_module_that_copies_a_real_spec(self) -> None:
        import importlib.util
        sys.modules.pop("calendar", None)
        spec = importlib.util.find_spec("calendar")
        self.assertIsNotNone(spec)
        self.assertIsInstance(spec.origin, str)
        source = (
            "import importlib.machinery, sys, types\n"
            "fake = types.ModuleType('calendar')\n"
            "fake.marker = 'pwned'\n"
            "fake.__spec__ = importlib.machinery.ModuleSpec('calendar', None, origin=%r)\n"
            "fake.__file__ = %r\n"
            "fake.__loader__ = fake.__spec__.loader\n"
            "fake.__name__ = 'calendar'\n"
            "sys.modules['calendar'] = fake\n"
            "raise RuntimeError('boom')\n" % (spec.origin, spec.origin)
        ).encode()
        try:
            self._load_failing(source)
            self.assertNotIn("calendar", sys.modules)
            import importlib
            real = importlib.import_module("calendar")
            self.assertNotEqual(getattr(real, "marker", None), "pwned")
            self.assertTrue(hasattr(real, "Calendar"))
        finally:
            sys.modules.pop("calendar", None)

    def test_a_failed_load_keeps_a_genuine_core_import(self) -> None:
        """A core import during a failed load does not split ``sys.modules`` from the parent.

        The pre-load binding wins, so the name added by the attempt is gone.
        A later import loads one module, and the parent attribute names it.
        """
        name = "recursion_core.child_env.launch"
        saved = sys.modules.pop(name, None)
        parent = sys.modules.get("recursion_core.child_env")
        saved_attr = getattr(parent, "launch", None) if parent is not None else None
        if parent is not None and "launch" in vars(parent):
            delattr(parent, "launch")
        source = b"import recursion_core.child_env.launch\nraise RuntimeError('boom')\n"
        try:
            self._load_failing(source)
            self.assertIsNone(sys.modules.get(name))
            if parent is not None:
                self.assertNotIn("launch", vars(parent))
                self.assertIs(vars(parent).get("launch"), sys.modules.get(name))
            module = self._assert_import_consistent(name)
            self.assertTrue(hasattr(module, "spawn"))
            self.assertNotEqual(getattr(module, "marker", None), "pwned")
        finally:
            if parent is not None:
                if saved_attr is None:
                    if "launch" in vars(parent):
                        delattr(parent, "launch")
                else:
                    parent.launch = saved_attr
            if saved is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved

    def test_a_failed_load_drops_an_import_found_only_on_an_added_path(self) -> None:
        outside = temp_dir(self)
        with open(os.path.join(outside, "planted_helper.py"), "wb") as fh:
            fh.write(b"marker = 'from-added-path'\n")
        sys.modules.pop("planted_helper", None)
        source = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import planted_helper\n"
            "raise RuntimeError('boom')\n" % outside
        ).encode()
        try:
            self._load_failing(source)
            self.assertNotIn("planted_helper", sys.modules)
            self.assertNotIn(outside, sys.path)
        finally:
            sys.modules.pop("planted_helper", None)
            while outside in sys.path:
                sys.path.remove(outside)

    def test_a_failed_load_drops_a_path_shadow_rewritten_to_the_stdlib_file(self) -> None:
        """A path-shadowed stdlib module is dropped after its spec is rewritten.

        The plugin loads ``colorsys`` from a directory it inserted, then points
        ``__spec__.origin`` and ``__file__`` at the real stdlib file. Cleanup
        must not read that live spec and keep the attacker module.
        """
        import importlib.util
        name = "colorsys"
        saved = sys.modules.pop(name, None)
        real = importlib.util.find_spec(name)
        self.assertIsNotNone(real)
        self.assertIsInstance(real.origin, str)
        sys.modules.pop(name, None)
        shadow = temp_dir(self)
        with open(os.path.join(shadow, "colorsys.py"), "w", encoding="utf-8") as fh:
            fh.write("marker = 'pwned'\n")
        stdlib_dir = os.path.dirname(real.origin)
        had_cache = stdlib_dir in sys.path_importer_cache
        saved_cache = sys.path_importer_cache.get(stdlib_dir)
        saved_hooks = list(sys.path_hooks)
        source = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "import colorsys\n"
            "colorsys.__spec__.origin = %r\n"
            "colorsys.__file__ = %r\n"
            "def _hook(path):\n"
            "    return None\n"
            "sys.path_hooks.append(_hook)\n"
            "raise RuntimeError('boom')\n" % (shadow, real.origin, real.origin)
        ).encode()
        try:
            self._load_failing(source)
            self.assertIsNone(sys.modules.get(name))
            self.assertNotIn(shadow, sys.path)
            self.assertEqual(list(sys.path_hooks), saved_hooks)
            import importlib
            module = importlib.import_module(name)
            self.assertTrue(hasattr(module, "rgb_to_hsv"))
            self.assertNotEqual(getattr(module, "marker", None), "pwned")
            had_cache = stdlib_dir in sys.path_importer_cache
            saved_cache = sys.path_importer_cache.get(stdlib_dir)
            cache_source = (
                "import sys\n"
                "sys.path_importer_cache[%r] = None\n"
                "raise RuntimeError('boom')\n" % stdlib_dir
            ).encode()
            self._load_failing(cache_source, plugin_id="bad.cache")
            if had_cache:
                self.assertIs(sys.path_importer_cache.get(stdlib_dir), saved_cache)
            else:
                self.assertNotIn(stdlib_dir, sys.path_importer_cache)
        finally:
            sys.modules.pop(name, None)
            if saved is not None:
                sys.modules[name] = saved
            while shadow in sys.path:
                sys.path.remove(shadow)
            sys.path_hooks[:] = saved_hooks
            if had_cache:
                sys.path_importer_cache[stdlib_dir] = saved_cache
            elif stdlib_dir in sys.path_importer_cache and sys.path_importer_cache[stdlib_dir] is None:
                sys.path_importer_cache.pop(stdlib_dir, None)

    def test_a_failed_load_drops_a_meta_path_spec_with_an_evil_loader(self) -> None:
        """A finder at index 1 cannot leave its module in ``sys.modules``.

        The finder returns a ``ModuleSpec`` whose loader is the attacker's and
        whose origin is the real file, or ``built-in``. That includes a fake
        ``recursion_core.child_env.launch``. The failed load drops both names.
        """
        import importlib.machinery
        import importlib.util
        launch_name = "recursion_core.child_env.launch"
        saved_launch = sys.modules.pop(launch_name, None)
        parent = sys.modules.get("recursion_core.child_env")
        saved_attr = getattr(parent, "launch", None) if parent is not None else None
        spec = importlib.util.find_spec(launch_name)
        self.assertIsNotNone(spec)
        self.assertIsInstance(spec.origin, str)
        sys.modules.pop(launch_name, None)
        builtin_name = next(
            name for name in sys.builtin_module_names
            if name not in sys.modules and name.isidentifier())
        saved_builtin = sys.modules.pop(builtin_name, None)

        class _EvilLoader:
            def create_module(self, module_spec):
                return None

            def exec_module(self, module) -> None:
                module.marker = "pwned"
                helper.loaded.append(module.__name__)

        class _EvilFinder:
            def find_spec(self, fullname, path, target=None):
                if fullname == launch_name:
                    return importlib.machinery.ModuleSpec(
                        fullname, _EvilLoader(), origin=spec.origin)
                if fullname == builtin_name:
                    return importlib.machinery.ModuleSpec(
                        fullname, _EvilLoader(), origin="built-in")
                return None

        helper = types.ModuleType("csl_a2_helper")
        helper.loaded = []
        helper.install = lambda: sys.meta_path.insert(1, _EvilFinder())
        helper.finder_type = _EvilFinder
        sys.modules["csl_a2_helper"] = helper
        source = (
            "import csl_a2_helper\n"
            "csl_a2_helper.install()\n"
            "import %s\n"
            "import %s\n"
            "raise RuntimeError('boom')\n" % (launch_name, builtin_name)
        ).encode()
        try:
            self._load_failing(source)
            self.assertEqual(helper.loaded, [launch_name, builtin_name])
            self.assertIsNone(sys.modules.get(launch_name))
            self.assertIsNone(sys.modules.get(builtin_name))
            self.assertNotEqual(getattr(sys.modules.get(launch_name), "marker", None), "pwned")
            self.assertNotEqual(getattr(sys.modules.get(builtin_name), "marker", None), "pwned")
            self.assertFalse(any(isinstance(item, _EvilFinder) for item in sys.meta_path))
        finally:
            sys.meta_path[:] = [item for item in sys.meta_path if not isinstance(item, _EvilFinder)]
            sys.modules.pop(launch_name, None)
            sys.modules.pop(builtin_name, None)
            sys.modules.pop("csl_a2_helper", None)
            if saved_launch is not None:
                sys.modules[launch_name] = saved_launch
            if saved_builtin is not None:
                sys.modules[builtin_name] = saved_builtin
            if parent is not None:
                if saved_attr is None:
                    if hasattr(parent, "launch"):
                        try:
                            delattr(parent, "launch")
                        except (AttributeError, TypeError):
                            pass
                else:
                    parent.launch = saved_attr

    def test_a_failed_load_restores_a_package_path_and_drops_the_submodule(self) -> None:
        """``logging.__path__`` is put back and the attacker submodule is dropped."""
        import logging
        attacker = temp_dir(self)
        with open(os.path.join(attacker, "pwned_sub.py"), "w", encoding="utf-8") as fh:
            fh.write("marker = 'pwned'\n")
        before_path = list(logging.__path__)
        source = (
            "import logging\n"
            "logging.__path__.insert(0, %r)\n"
            "import logging.pwned_sub\n"
            "raise RuntimeError('boom')\n" % attacker
        ).encode()
        try:
            self._load_failing(source)
            self.assertIsNone(sys.modules.get("logging.pwned_sub"))
            self.assertFalse(hasattr(logging, "pwned_sub"))
            self.assertNotIn(attacker, list(logging.__path__))
            self.assertEqual(list(logging.__path__), before_path)
            self.assertNotEqual(getattr(sys.modules.get("logging.pwned_sub"), "marker", None), "pwned")
        finally:
            sys.modules.pop("logging.pwned_sub", None)
            if hasattr(logging, "pwned_sub"):
                try:
                    delattr(logging, "pwned_sub")
                except (AttributeError, TypeError):
                    pass
            try:
                logging.__path__[:] = before_path
            except (AttributeError, TypeError, ValueError):
                pass

    def test_a_failed_child_env_import_keeps_one_validated_env(self) -> None:
        """``from recursion_core import child_env`` is the module ``import_module`` returns.

        ``ValidatedEnv`` and the policy lock stay one object, so spawn's type check
        still names the class ``build_child_env`` returned.
        """
        import importlib
        prefix = "recursion_core.child_env"
        saved = {}
        for key in list(sys.modules):
            if key == prefix or key.startswith(prefix + "."):
                saved[key] = sys.modules.pop(key)
        parent = sys.modules["recursion_core"]
        missing = object()
        saved_attr = getattr(parent, "child_env", missing)
        if saved_attr is not missing:
            delattr(parent, "child_env")
        source = b"import recursion_core.child_env\nraise RuntimeError('boom')\n"
        try:
            self._load_failing(source)
            from recursion_core import child_env
            again = importlib.import_module("recursion_core.child_env")
            self.assertIs(child_env, again)
            self.assertIs(getattr(parent, "child_env", None), again)
            self.assertIs(child_env.ValidatedEnv, again.policy.ValidatedEnv)
            self.assertIs(child_env.policy.ValidatedEnv, again.launch.ValidatedEnv)
            self.assertIs(child_env.policy._TMP_LOCK, again.policy._TMP_LOCK)
        finally:
            for key in list(sys.modules):
                if key == prefix or key.startswith(prefix + "."):
                    sys.modules.pop(key, None)
            sys.modules.update(saved)
            if saved_attr is missing:
                if hasattr(parent, "child_env"):
                    delattr(parent, "child_env")
            else:
                parent.child_env = saved_attr

    def _namespace_fixture(self):
        import importlib
        base = temp_dir(self)
        os.makedirs(os.path.join(base, "nsrepro"))
        evil_base = temp_dir(self)
        evil_portion = os.path.join(evil_base, "nsrepro")
        os.makedirs(evil_portion)
        with open(os.path.join(evil_portion, "pwned_mod.py"), "w", encoding="utf-8") as fh:
            fh.write("marker = 'pwned'\n")
        sys.path.insert(0, base)
        ns = importlib.import_module("nsrepro")
        self.assertFalse(isinstance(ns.__path__, list))
        return ns, evil_portion, base

    def _clear_namespace(self, base: str) -> None:
        import importlib
        sys.modules.pop("nsrepro.pwned_mod", None)
        sys.modules.pop("nsrepro", None)
        while base in sys.path:
            sys.path.remove(base)
        importlib.invalidate_caches()

    def test_a_failed_load_restores_a_namespace_path_append(self) -> None:
        import importlib
        ns, evil_portion, base = self._namespace_fixture()
        original = ns.__path__
        before = list(original)
        source = (
            "import nsrepro\n"
            "nsrepro.__path__.append(%r)\n"
            "raise RuntimeError('boom')\n" % evil_portion
        ).encode()
        try:
            self._load_failing(source)
            self.assertIs(ns.__path__, original)
            self.assertNotIn(evil_portion, list(ns.__path__))
            self.assertEqual(list(ns.__path__), before)
            self.assertNotIn("nsrepro.pwned_mod", sys.modules)
            with self.assertRaises(ImportError):
                importlib.import_module("nsrepro.pwned_mod")
        finally:
            self._clear_namespace(base)

    def test_a_failed_load_restores_a_rebound_namespace_path(self) -> None:
        import importlib
        ns, evil_portion, base = self._namespace_fixture()
        original = ns.__path__
        before = list(original)
        source = (
            "import nsrepro\n"
            "nsrepro.__path__ = [%r]\n"
            "raise RuntimeError('boom')\n" % evil_portion
        ).encode()
        try:
            self._load_failing(source)
            self.assertIs(ns.__path__, original)
            self.assertNotIn(evil_portion, list(ns.__path__))
            self.assertEqual(list(ns.__path__), before)
            with self.assertRaises(ImportError):
                importlib.import_module("nsrepro.pwned_mod")
        finally:
            self._clear_namespace(base)

    def test_a_nested_load_on_the_same_thread_is_refused(self) -> None:
        inner_source = b"raise RuntimeError('inner')\n"
        inner_loader, raw, sig, source_bytes = self._prepared(inner_source, "bad.inner")
        holder = types.ModuleType("csl_nest_deep5")
        holder.loader = inner_loader
        holder.raw = raw
        holder.sig = sig
        holder.source = source_bytes
        holder.code = None
        sys.modules["csl_nest_deep5"] = holder
        outer = (
            "import csl_nest_deep5\n"
            "try:\n"
            "    csl_nest_deep5.loader.load(csl_nest_deep5.raw, csl_nest_deep5.sig, csl_nest_deep5.source)\n"
            "except Exception as exc:\n"
            "    csl_nest_deep5.code = getattr(exc, 'code', type(exc).__name__)\n"
            "else:\n"
            "    csl_nest_deep5.code = 'returned'\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        box: dict = {}

        def run() -> None:
            try:
                box["outer"] = self._run_failing(outer, plugin_id="bad.outer")
            except Exception as exc:  # noqa: BLE001 - the watchdog reads this on the main thread
                box["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(5)
        try:
            self.assertFalse(worker.is_alive(), "nested load hung")
            self.assertIsNone(box.get("error"))
            self.assertEqual(holder.code, "plugin_load_busy")
            self.assertEqual(box.get("outer"), "plugin_import_failed")
        finally:
            sys.modules.pop("csl_nest_deep5", None)

    def test_a_cross_thread_load_is_refused_instead_of_waiting(self) -> None:
        holder = types.ModuleType("csl_cycle_deep5")
        holder.started = threading.Event()
        holder.inner_done = threading.Event()
        holder.inner_code = None
        holder.ran = False
        sys.modules["csl_cycle_deep5"] = holder
        outer = (
            "import csl_cycle_deep5\n"
            "csl_cycle_deep5.started.set()\n"
            "if not csl_cycle_deep5.inner_done.wait(2):\n"
            "    raise RuntimeError('inner did not finish')\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        inner = (
            "import csl_cycle_deep5\n"
            "csl_cycle_deep5.ran = True\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        box: dict = {}

        def run_inner() -> None:
            if not holder.started.wait(5):
                return
            try:
                holder.inner_code = self._run_failing(inner, plugin_id="bad.cycle.inner")
            except Exception as exc:  # noqa: BLE001
                holder.inner_code = type(exc).__name__
            finally:
                holder.inner_done.set()

        def run_outer() -> None:
            try:
                box["outer"] = self._run_failing(outer, plugin_id="bad.cycle.outer")
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc

        threads = [
            threading.Thread(target=run_outer, daemon=True),
            threading.Thread(target=run_inner, daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(8)
        try:
            self.assertFalse(any(thread.is_alive() for thread in threads), "cross-thread load hung")
            self.assertIsNone(box.get("error"))
            self.assertFalse(holder.ran)
            self.assertEqual(holder.inner_code, "plugin_load_busy")
            self.assertEqual(box.get("outer"), "plugin_import_failed")
        finally:
            for thread in threads:
                thread.join(1)
            sys.modules.pop("csl_cycle_deep5", None)

    def test_concurrent_loads_do_not_share_one_import_recorder(self) -> None:
        """Two loads must not share one import recorder.

        A load that starts while another activation is inside exec is refused
        instead of waiting. Recorders that do run are distinct objects.
        """
        holder = types.ModuleType("csl_coord_a6")
        holder.box = []
        holder.codes = []
        holder.lock = threading.Lock()
        sys.modules["csl_coord_a6"] = holder
        source = (
            "import csl_coord_a6, sys\n"
            "import recursion_core.plugin_security.loader as loader_mod\n"
            "finder = sys.meta_path[0]\n"
            "recorder = getattr(loader_mod, '_imports', None)\n"
            "if recorder is None:\n"
            "    recorder = getattr(finder, '_record', None)\n"
            "with csl_coord_a6.lock:\n"
            "    csl_coord_a6.box.append(id(recorder.created))\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        errors: list = []

        def run(plugin_id: str) -> None:
            try:
                code = self._run_failing(source, plugin_id=plugin_id)
            except Exception as exc:  # noqa: BLE001 - the assertion is on the main thread
                errors.append(exc)
                return
            with holder.lock:
                holder.codes.append(code)

        threads = [
            threading.Thread(target=run, args=("bad.one",)),
            threading.Thread(target=run, args=("bad.two",)),
        ]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(holder.codes), 2)
            self.assertTrue(set(holder.codes) <= {"plugin_import_failed", "plugin_load_busy"})
            self.assertIn("plugin_import_failed", holder.codes)
            self.assertEqual(len(holder.box), len(set(holder.box)))
            self.assertGreaterEqual(len(holder.box), 1)
        finally:
            for thread in threads:
                thread.join(5)
            sys.modules.pop("csl_coord_a6", None)

    def test_a_failed_load_drops_a_forged_builtin_from_the_path_cache(self) -> None:
        """A finder planted in ``path_importer_cache`` can report origin ``built-in``.

        The keep-rule trusts that origin when the meta-path finder was already
        installed, so the forged module stays.
        """
        entry = next(p for p in sys.path if isinstance(p, str) and p)
        saved = sys.path_importer_cache.get(entry, None)
        had = entry in sys.path_importer_cache
        source = (
            "import importlib.machinery, sys\n"
            "entry = %r\n"
            "class EvilLoader:\n"
            "    def create_module(self, spec):\n"
            "        return None\n"
            "    def exec_module(self, module):\n"
            "        module.marker = 'pwned'\n"
            "class EvilFinder:\n"
            "    def find_spec(self, fullname, target=None):\n"
            "        if fullname == 'forged_builtin_mod':\n"
            "            return importlib.machinery.ModuleSpec(\n"
            "                fullname, EvilLoader(), origin='built-in')\n"
            "        return None\n"
            "sys.path_importer_cache[entry] = EvilFinder()\n"
            "import forged_builtin_mod\n"
            "raise RuntimeError('boom')\n" % entry
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.forge.cache")
            self.assertNotIn("forged_builtin_mod", sys.modules)
            self.assertNotEqual(getattr(sys.modules.get("forged_builtin_mod"), "marker", None), "pwned")
        finally:
            sys.modules.pop("forged_builtin_mod", None)
            if had:
                sys.path_importer_cache[entry] = saved
            else:
                sys.path_importer_cache.pop(entry, None)

    def test_a_failed_load_drops_a_forged_frozen_origin_from_filefinder(self) -> None:
        import importlib.machinery
        real = importlib.machinery.FileFinder.find_spec
        source = (
            "import importlib.machinery\n"
            "real = importlib.machinery.FileFinder.find_spec\n"
            "class EvilLoader:\n"
            "    def create_module(self, spec):\n"
            "        return None\n"
            "    def exec_module(self, module):\n"
            "        module.marker = 'pwned'\n"
            "def patched(self, fullname, target=None):\n"
            "    if fullname == 'forged_frozen_mod':\n"
            "        return importlib.machinery.ModuleSpec(\n"
            "            fullname, EvilLoader(), origin='frozen')\n"
            "    return real(self, fullname, target)\n"
            "importlib.machinery.FileFinder.find_spec = patched\n"
            "import forged_frozen_mod\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.forge.frozen")
            self.assertNotIn("forged_frozen_mod", sys.modules)
            self.assertNotEqual(getattr(sys.modules.get("forged_frozen_mod"), "marker", None), "pwned")
        finally:
            importlib.machinery.FileFinder.find_spec = real
            sys.modules.pop("forged_frozen_mod", None)

    def test_a_failed_load_drops_a_file_created_in_a_searchable_directory(self) -> None:
        root = temp_dir(self)
        sys.path.insert(0, root)
        source = (
            "open(%r, 'w', encoding='utf-8').write('marker = %%r\\n' %% 'pwned')\n"
            "import csl_r6_sneaky\n"
            "raise RuntimeError('boom')\n" % os.path.join(root, "csl_r6_sneaky.py")
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.forge.file")
            self.assertNotIn("csl_r6_sneaky", sys.modules)
            self.assertNotEqual(getattr(sys.modules.get("csl_r6_sneaky"), "marker", None), "pwned")
        finally:
            sys.modules.pop("csl_r6_sneaky", None)
            while root in sys.path:
                sys.path.remove(root)

    def test_a_failed_load_drops_a_module_stuffed_into_the_recorder(self) -> None:
        """Writing ``finder._record.created`` makes a planted module look trusted."""
        source = (
            "import importlib.machinery, sys, types\n"
            "mod = types.ModuleType('stuffed_evil')\n"
            "mod.marker = 'pwned'\n"
            "mod.__spec__ = importlib.machinery.ModuleSpec('stuffed_evil', None, origin='built-in')\n"
            "sys.modules['stuffed_evil'] = mod\n"
            "finder = sys.meta_path[0]\n"
            "finder._record.created['stuffed_evil'] = (mod, 'built-in', sys.meta_path[1])\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.forge.stuff")
            self.assertNotIn("stuffed_evil", sys.modules)
            self.assertNotEqual(getattr(sys.modules.get("stuffed_evil"), "marker", None), "pwned")
        finally:
            sys.modules.pop("stuffed_evil", None)

    def test_a_failed_load_drops_bytes_rewritten_after_the_spec_is_noted(self) -> None:
        """``settrace`` rewrites the file inside ``get_data``, after the origin is recorded."""
        root = temp_dir(self)
        path = os.path.join(root, "csl_r6_clean.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("marker = 'clean'\n")
        sys.path.insert(0, root)
        source = (
            "import sys\n"
            "path = %r\n"
            "def trace(frame, event, arg):\n"
            "    if event == 'call' and frame.f_code.co_name == 'get_data':\n"
            "        if frame.f_locals.get('path') == path:\n"
            "            open(path, 'w', encoding='utf-8').write('marker = %%r\\n' %% 'pwned')\n"
            "    return trace\n"
            "sys.settrace(trace)\n"
            "import csl_r6_clean\n"
            "sys.settrace(None)\n"
            "raise RuntimeError('boom')\n" % path
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.forge.trace")
            self.assertNotIn("csl_r6_clean", sys.modules)
            self.assertNotEqual(getattr(sys.modules.get("csl_r6_clean"), "marker", None), "pwned")
        finally:
            sys.settrace(None)
            sys.modules.pop("csl_r6_clean", None)
            while root in sys.path:
                sys.path.remove(root)

    def test_a_failed_load_drops_a_module_from_a_patched_exec_module(self) -> None:
        import importlib.machinery
        name = "netrc"
        saved = sys.modules.pop(name, None)
        real = importlib.machinery.SourceFileLoader.exec_module
        source = (
            "import importlib.machinery\n"
            "real = importlib.machinery.SourceFileLoader.exec_module\n"
            "def wrapped(self, module):\n"
            "    module.marker = 'pwned'\n"
            "    return real(self, module)\n"
            "importlib.machinery.SourceFileLoader.exec_module = wrapped\n"
            "import netrc\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.forge.exec")
            self.assertNotEqual(getattr(sys.modules.get(name), "marker", None), "pwned")
        finally:
            importlib.machinery.SourceFileLoader.exec_module = real
            sys.modules.pop(name, None)
            if saved is not None:
                sys.modules[name] = saved

    def test_a_failed_load_drops_a_module_from_a_patched_pathfinder(self) -> None:
        import importlib.machinery
        import importlib.util
        real = importlib.machinery.PathFinder.find_spec
        origin = importlib.util.find_spec("tokenize").origin
        self.assertIsInstance(origin, str)
        source = (
            "import importlib.machinery\n"
            "real = importlib.machinery.PathFinder.find_spec.__func__\n"
            "class EvilLoader:\n"
            "    def create_module(self, spec):\n"
            "        return None\n"
            "    def exec_module(self, module):\n"
            "        module.marker = 'pwned'\n"
            "def patched(cls, fullname, path=None, target=None):\n"
            "    if fullname == 'forged_path_mod':\n"
            "        return importlib.machinery.ModuleSpec(\n"
            "            fullname, EvilLoader(), origin=%r)\n"
            "    return real(cls, fullname, path, target)\n"
            "importlib.machinery.PathFinder.find_spec = classmethod(patched)\n"
            "import forged_path_mod\n"
            "raise RuntimeError('boom')\n" % origin
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.forge.path")
            self.assertNotIn("forged_path_mod", sys.modules)
            self.assertNotEqual(getattr(sys.modules.get("forged_path_mod"), "marker", None), "pwned")
        finally:
            importlib.machinery.PathFinder.find_spec = real
            sys.modules.pop("forged_path_mod", None)

    def test_a_failed_load_drops_a_zip_member_added_during_the_load(self) -> None:
        import zipfile
        root = temp_dir(self)
        zpath = os.path.join(root, "mods.zip")
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("placeholder.py", "x = 1\n")
        sys.path.insert(0, zpath)
        source = (
            "import importlib, zipfile\n"
            "with zipfile.ZipFile(%r, 'a') as zf:\n"
            "    zf.writestr('csl_r6_zip_evil.py', 'marker = %%r\\n' %% 'pwned')\n"
            "importlib.invalidate_caches()\n"
            "import csl_r6_zip_evil\n"
            "raise RuntimeError('boom')\n" % zpath
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.forge.zip")
            self.assertNotIn("csl_r6_zip_evil", sys.modules)
            self.assertNotEqual(getattr(sys.modules.get("csl_r6_zip_evil"), "marker", None), "pwned")
        finally:
            sys.modules.pop("csl_r6_zip_evil", None)
            while zpath in sys.path:
                sys.path.remove(zpath)

    def test_a_failed_load_drops_a_namespace_portion_written_during_the_load(self) -> None:
        import importlib
        base = temp_dir(self)
        os.makedirs(os.path.join(base, "cslr6ns"))
        sys.path.insert(0, base)
        importlib.invalidate_caches()
        importlib.import_module("cslr6ns")
        target = os.path.join(base, "cslr6ns", "evil_mod.py")
        source = (
            "open(%r, 'w', encoding='utf-8').write('marker = %%r\\n' %% 'pwned')\n"
            "import cslr6ns.evil_mod\n"
            "raise RuntimeError('boom')\n" % target
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.forge.ns")
            self.assertNotIn("cslr6ns.evil_mod", sys.modules)
            self.assertNotEqual(getattr(sys.modules.get("cslr6ns.evil_mod"), "marker", None), "pwned")
        finally:
            sys.modules.pop("cslr6ns.evil_mod", None)
            sys.modules.pop("cslr6ns", None)
            while base in sys.path:
                sys.path.remove(base)
            importlib.invalidate_caches()

    def test_a_failed_load_does_not_delete_a_preexisting_package_attribute(self) -> None:
        """``pkg.encoder`` was ``json`` before the load. Setting ``sys.modules`` must not delete it."""
        import json
        pkg = types.ModuleType("encpkg")
        pkg.__path__ = []
        pkg.encoder = json
        sys.modules["encpkg"] = pkg
        source = b"import json, sys\nsys.modules['encpkg.encoder'] = json\nraise RuntimeError('boom')\n"
        try:
            self._load_failing(source, plugin_id="bad.encoder")
            self.assertIs(getattr(pkg, "encoder", None), json)
            self.assertNotIn("encpkg.encoder", sys.modules)
        finally:
            sys.modules.pop("encpkg.encoder", None)
            sys.modules.pop("encpkg", None)

    def _child_env_cleared(self):
        prefix = "recursion_core.child_env"
        saved = {}
        for key in list(sys.modules):
            if key == prefix or key.startswith(prefix + "."):
                saved[key] = sys.modules.pop(key)
        parent = sys.modules["recursion_core"]
        missing = object()
        saved_attr = getattr(parent, "child_env", missing)
        if saved_attr is not missing:
            delattr(parent, "child_env")
        return prefix, saved, parent, missing, saved_attr

    def _child_env_restore(self, prefix, saved, parent, missing, saved_attr) -> None:
        for key in list(sys.modules):
            if key == prefix or key.startswith(prefix + "."):
                sys.modules.pop(key, None)
        sys.modules.update(saved)
        if saved_attr is missing:
            if hasattr(parent, "child_env"):
                delattr(parent, "child_env")
        else:
            parent.child_env = saved_attr

    def test_a_failed_load_does_not_split_child_env_when_sys_modules_is_replaced(self) -> None:
        prefix, saved, parent, missing, saved_attr = self._child_env_cleared()
        source = (
            "import sys, types\n"
            "import recursion_core.child_env as real\n"
            "fake = types.ModuleType('recursion_core.child_env')\n"
            "fake.marker = 'pwned'\n"
            "sys.modules['recursion_core.child_env'] = fake\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.child.replace")
            loaded = sys.modules.get(prefix)
            bound = vars(parent).get("child_env")
            self.assertIs(loaded, bound)
            self.assertIsNone(loaded)
            self.assertNotEqual(getattr(loaded, "marker", None), "pwned")
            leftover = [key for key in sys.modules if key == prefix or key.startswith(prefix + ".")]
            self.assertEqual(leftover, [])
        finally:
            self._child_env_restore(prefix, saved, parent, missing, saved_attr)

    def test_a_failed_load_does_not_split_child_env_when_sys_modules_is_popped(self) -> None:
        prefix, saved, parent, missing, saved_attr = self._child_env_cleared()
        source = (
            "import sys\n"
            "import recursion_core.child_env\n"
            "sys.modules.pop('recursion_core.child_env', None)\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.child.pop")
            loaded = sys.modules.get(prefix)
            bound = vars(parent).get("child_env")
            self.assertIs(loaded, bound)
            self.assertIsNone(loaded)
            leftover = [key for key in sys.modules if key == prefix or key.startswith(prefix + ".")]
            self.assertEqual(leftover, [])
        finally:
            self._child_env_restore(prefix, saved, parent, missing, saved_attr)

    def test_a_nested_load_on_the_same_loader_is_refused(self) -> None:
        """``load`` on the loader that is already inside ``load`` must not take the lock."""
        holder = types.ModuleType("csl_same_nest_r6")
        holder.code = None
        sys.modules["csl_same_nest_r6"] = holder
        inner = b"raise RuntimeError('inner')\n"
        outer = (
            "import csl_same_nest_r6\n"
            "try:\n"
            "    csl_same_nest_r6.loader.load(\n"
            "        csl_same_nest_r6.raw, csl_same_nest_r6.sig, csl_same_nest_r6.source)\n"
            "except Exception as exc:\n"
            "    csl_same_nest_r6.code = getattr(exc, 'code', type(exc).__name__)\n"
            "else:\n"
            "    csl_same_nest_r6.code = 'returned'\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        loader, raws, sigs = self._loader_for({"bad.same.outer": outer, "bad.same.inner": inner})
        holder.loader = loader
        holder.raw = raws["bad.same.inner"]
        holder.sig = sigs["bad.same.inner"]
        holder.source = inner
        box: dict = {}

        def run() -> None:
            try:
                loader.load(raws["bad.same.outer"], sigs["bad.same.outer"], outer)
                box["outer"] = "returned"
            except Refused as exc:
                box["outer"] = exc.code
            except Exception as exc:  # noqa: BLE001 - the watchdog reads this on the main thread
                box["error"] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(5)
        try:
            self.assertFalse(worker.is_alive(), "same-loader nested load hung")
            self.assertIsNone(box.get("error"))
            self.assertEqual(holder.code, "plugin_load_busy")
            self.assertEqual(box.get("outer"), "plugin_import_failed")
        finally:
            sys.modules.pop("csl_same_nest_r6", None)

    def test_a_cross_thread_load_on_the_same_loader_is_refused(self) -> None:
        """A second thread calling ``load`` on the same loader is refused instead of queued."""
        holder = types.ModuleType("csl_same_cross_r6")
        holder.started = threading.Event()
        holder.inner_done = threading.Event()
        holder.inner_code = None
        holder.ran = False
        sys.modules["csl_same_cross_r6"] = holder
        outer = (
            "import csl_same_cross_r6\n"
            "csl_same_cross_r6.started.set()\n"
            "csl_same_cross_r6.inner_done.wait()\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        inner = (
            "import csl_same_cross_r6\n"
            "csl_same_cross_r6.ran = True\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        loader, raws, sigs = self._loader_for({
            "bad.same.cross.outer": outer,
            "bad.same.cross.inner": inner,
        })
        box: dict = {}

        def run_inner() -> None:
            if not holder.started.wait(5):
                holder.inner_code = "never-started"
                holder.inner_done.set()
                return
            try:
                loader.load(
                    raws["bad.same.cross.inner"], sigs["bad.same.cross.inner"], inner)
                holder.inner_code = "returned"
            except Refused as exc:
                holder.inner_code = exc.code
            except Exception as exc:  # noqa: BLE001
                holder.inner_code = type(exc).__name__
            finally:
                holder.inner_done.set()

        def run_outer() -> None:
            try:
                loader.load(
                    raws["bad.same.cross.outer"], sigs["bad.same.cross.outer"], outer)
                box["outer"] = "returned"
            except Refused as exc:
                box["outer"] = exc.code
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc

        threads = [
            threading.Thread(target=run_outer, daemon=True),
            threading.Thread(target=run_inner, daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(8)
        try:
            self.assertFalse(any(thread.is_alive() for thread in threads),
                             "same-loader cross-thread load hung")
            self.assertIsNone(box.get("error"))
            self.assertFalse(holder.ran)
            self.assertEqual(holder.inner_code, "plugin_load_busy")
            self.assertEqual(box.get("outer"), "plugin_import_failed")
        finally:
            holder.inner_done.set()
            for thread in threads:
                thread.join(1)
            sys.modules.pop("csl_same_cross_r6", None)

    def _clear_hunt_modules(self) -> None:
        import gc
        gc.collect()
        for name, mod in list(sys.modules.items()):
            if not name.startswith("hunt_"):
                continue
            cls = type(mod)
            if cls is not types.ModuleType:
                try:
                    cls.__del__ = lambda _dead: None
                except Exception:
                    pass
        for name in list(sys.modules):
            if name.startswith("hunt_"):
                sys.modules.pop(name, None)
        gc.collect()

    def test_a_failed_load_restores_os_path_when_the_parent_is_not_a_package(self) -> None:
        """``os`` has no ``__path__``. Replacing ``os.path`` must not outlive the failure.

        ``sys.modules['os.path']`` is put back to the pre-load module. ``os.path``
        and ``from os import path`` name that same module, whose ``join`` is not
        the attacker's.
        """
        import os
        real_mod = sys.modules["os.path"]
        real_attr = os.path
        self.assertIs(real_attr, real_mod)
        source = (
            "import os, sys, types\n"
            "fake = types.ModuleType('os.path')\n"
            "fake.join = lambda *parts: 'pwned'\n"
            "sys.modules['os.path'] = fake\n"
            "os.path = fake\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.os.path")
            self.assertIs(sys.modules.get("os.path"), real_mod)
            self.assertIs(os.path, real_attr)
            from os import path
            self.assertIs(path, real_attr)
            self.assertNotEqual(path.join("a", "b"), "pwned")
        finally:
            sys.modules["os.path"] = real_mod
            os.path = real_attr

    def test_a_failed_load_drops_an_attribute_on_a_non_package_parent(self) -> None:
        """``json.encoder`` is a module. A name added under it is removed from both tables."""
        import json.encoder
        encoder = json.encoder
        self.assertNotIn("pwn", vars(encoder))
        source = (
            "import json.encoder, sys, types\n"
            "mod = types.ModuleType('json.encoder.pwn')\n"
            "mod.marker = 'pwned'\n"
            "sys.modules['json.encoder.pwn'] = mod\n"
            "json.encoder.pwn = mod\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.encoder.pwn")
            self.assertNotIn("json.encoder.pwn", sys.modules)
            self.assertNotIn("pwn", vars(encoder))
            self.assertFalse(hasattr(encoder, "pwn"))
            self.assertNotEqual(getattr(getattr(encoder, "pwn", None), "marker", None), "pwned")
        finally:
            sys.modules.pop("json.encoder.pwn", None)
            if "pwn" in vars(encoder):
                del encoder.pwn

    def test_a_failed_load_restores_a_module_attribute_by_identity(self) -> None:
        """A pre-existing package attribute is put back even when ``__name__`` does not match.

        ``encpkg_match.encoder`` was the ``json`` module. The failed load sets it
        to ``ModuleType('unrelated')``. Identity with the snapshot decides, not
        ``__name__``.
        """
        import json
        pkg = types.ModuleType("encpkg_match")
        pkg.__path__ = []
        pkg.encoder = json
        sys.modules["encpkg_match"] = pkg
        source = (
            "import encpkg_match, types\n"
            "encpkg_match.encoder = types.ModuleType('unrelated')\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.encpkg.match")
            self.assertIs(getattr(pkg, "encoder", None), json)
            self.assertNotEqual(getattr(pkg.encoder, "__name__", None), "unrelated")
        finally:
            sys.modules.pop("encpkg_match.encoder", None)
            sys.modules.pop("encpkg_match", None)

    def test_a_failed_load_restores_a_module_attribute_when_sys_modules_stays(self) -> None:
        """Poisoning ``json.encoder`` without touching its ``sys.modules`` entry is rolled back."""
        import json
        import json.encoder as encoder
        real = encoder
        self.assertIs(sys.modules["json.encoder"], real)
        self.assertIs(json.encoder, real)
        source = (
            "import json, types\n"
            "json.encoder = types.ModuleType('unrelated')\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.json.encoder.attr")
            self.assertIs(json.encoder, real)
            self.assertIs(sys.modules["json.encoder"], real)
        finally:
            json.encoder = real
            sys.modules["json.encoder"] = real

    def test_a_failed_load_drops_a_module_a_finalizer_puts_back(self) -> None:
        """Popping a module runs ``__del__``, which reinserts ``hunt_zombie3``. That name is dropped too."""
        source = (
            "import sys, types\n"
            "def plant():\n"
            "    class Zombie(types.ModuleType):\n"
            "        def __del__(self):\n"
            "            revived = types.ModuleType('hunt_zombie3')\n"
            "            revived.marker = 'pwned'\n"
            "            sys.modules['hunt_zombie3'] = revived\n"
            "    sys.modules['hunt_carrier'] = Zombie('hunt_carrier')\n"
            "plant()\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.zombie.ref")
            self.assertNotIn("hunt_zombie3", sys.modules)
            self.assertNotIn("hunt_carrier", sys.modules)
            self.assertNotEqual(getattr(sys.modules.get("hunt_zombie3"), "marker", None), "pwned")
        finally:
            self._clear_hunt_modules()

    def test_a_failed_load_drops_a_module_a_cyclic_finalizer_puts_back(self) -> None:
        """A cycle keeps the finalizer until ``gc.collect()``. The name it reinserts is dropped."""
        import gc
        source = (
            "import sys, types\n"
            "def plant():\n"
            "    class Zombie:\n"
            "        def __del__(self):\n"
            "            revived = types.ModuleType('hunt_cycle_z')\n"
            "            revived.marker = 'pwned'\n"
            "            sys.modules['hunt_cycle_z'] = revived\n"
            "    first = types.ModuleType('hunt_cycle_z')\n"
            "    other = types.ModuleType('hunt_cycle_o')\n"
            "    first.other = other\n"
            "    other.first = first\n"
            "    first.zombie = Zombie()\n"
            "    first.marker = 'pwned'\n"
            "    sys.modules['hunt_cycle_z'] = first\n"
            "    sys.modules['hunt_cycle_o'] = other\n"
            "plant()\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.zombie.cycle")
            self.assertNotIn("hunt_cycle_z", sys.modules)
            self.assertNotIn("hunt_cycle_o", sys.modules)
            gc.collect()
            self.assertNotIn("hunt_cycle_z", sys.modules)
            self.assertNotIn("hunt_cycle_o", sys.modules)
            self.assertNotEqual(getattr(sys.modules.get("hunt_cycle_z"), "marker", None), "pwned")
        finally:
            self._clear_hunt_modules()

    def test_a_rearming_finalizer_past_the_bound_is_refused(self) -> None:
        """A finalizer that inserts itself again on every pop is refused once the bound is past.

        The bound is finite. A finalizer that keeps re-arming past it can still
        leave ``hunt_zombie3``; that residue is not cleaned here.
        """
        source = (
            "import sys, types\n"
            "def plant():\n"
            "    class Rearm(types.ModuleType):\n"
            "        def __del__(self):\n"
            "            n = getattr(Rearm, '_n', 0) + 1\n"
            "            Rearm._n = n\n"
            "            if n > 1000:\n"
            "                return\n"
            "            again = Rearm('hunt_zombie3')\n"
            "            again.marker = 'pwned'\n"
            "            sys.modules['hunt_zombie3'] = again\n"
            "    sys.modules['hunt_zombie3'] = Rearm('hunt_zombie3')\n"
            "plant()\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            code = self._run_failing(source, plugin_id="bad.zombie.rearm")
            self.assertEqual(code, "plugin_import_state_unrestored")
        finally:
            self._clear_hunt_modules()

    def _repair_module(self, module: types.ModuleType, *names: str) -> None:
        """Put a test-mutated module back without going through its class."""
        try:
            object.__setattr__(module, "__class__", types.ModuleType)
        except Exception:
            pass
        try:
            mapping = types.ModuleType.__dict__["__dict__"].__get__(module)
        except Exception:
            return
        for name in names:
            live = mapping.get(name)
            cls = type(live)
            if isinstance(live, types.ModuleType) and cls is not types.ModuleType:
                try:
                    cls._stop = True
                except Exception:
                    pass
        for name in names:
            mapping.pop(name, None)

    def test_a_rearming_binding_finalizer_is_not_an_ordinary_import_failure(self) -> None:
        """``__del__`` puts ``json.encoder.pwn`` back after each delete.

        ``sys.modules`` can be clean while that attribute is still the
        attacker's module. The load must be ``plugin_import_state_unrestored``,
        not ``plugin_import_failed``. Fails on 255fedcf, where settle accepts
        a clean module table with a dirty binding.
        """
        import json.encoder as encoder
        source = (
            "import json.encoder as enc, types\n"
            "def plant():\n"
            "    class Rearm(types.ModuleType):\n"
            "        def __del__(self):\n"
            "            if getattr(Rearm, '_stop', False):\n"
            "                return\n"
            "            again = Rearm('json.encoder.pwn')\n"
            "            again.marker = 'pwned'\n"
            "            enc.pwn = again\n"
            "    mod = Rearm('json.encoder.pwn')\n"
            "    mod.marker = 'pwned'\n"
            "    enc.pwn = mod\n"
            "plant()\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            code = self._run_failing(source, plugin_id="bad.encoder.rearm.del")
            pwned = getattr(getattr(encoder, "pwn", None), "marker", None) == "pwned"
            if pwned:
                self.assertEqual(code, "plugin_import_state_unrestored")
            else:
                self.assertEqual(code, "plugin_import_failed")
            self.assertFalse(pwned and code == "plugin_import_failed")
        finally:
            self._repair_module(encoder, "pwn")

    def test_a_rearming_weakref_on_a_binding_is_not_an_ordinary_import_failure(self) -> None:
        """A weakref callback puts ``json.encoder.pwn`` back when it is deleted.

        Same rule as a re-arming ``__del__``: a dirty binding is not an
        ordinary ``plugin_import_failed``. Fails on 255fedcf.
        """
        import json.encoder as encoder
        source = (
            "import json.encoder as enc, types, weakref\n"
            "def arm(ref):\n"
            "    if getattr(enc, '_stop', False):\n"
            "        return\n"
            "    again = types.ModuleType('json.encoder.pwn')\n"
            "    again.marker = 'pwned'\n"
            "    enc.pwn = again\n"
            "    enc._arm = weakref.ref(again, arm)\n"
            "def plant():\n"
            "    mod = types.ModuleType('json.encoder.pwn')\n"
            "    mod.marker = 'pwned'\n"
            "    enc._arm = weakref.ref(mod, arm)\n"
            "    enc.pwn = mod\n"
            "plant()\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            code = self._run_failing(source, plugin_id="bad.encoder.rearm.weak")
            pwned = getattr(getattr(encoder, "pwn", None), "marker", None) == "pwned"
            if pwned:
                self.assertEqual(code, "plugin_import_state_unrestored")
            else:
                self.assertEqual(code, "plugin_import_failed")
            self.assertFalse(pwned and code == "plugin_import_failed")
        finally:
            try:
                mapping = types.ModuleType.__dict__["__dict__"].__get__(encoder)
                mapping["_stop"] = True
            except Exception:
                pass
            self._repair_module(encoder, "pwn", "_arm", "_stop")

    def test_a_failed_load_writes_a_binding_when_setattr_raises(self) -> None:
        """A swapped module class whose ``__setattr__`` raises cannot keep ``pwn``.

        The write goes through the module's real dict, and the class is put
        back first. Fails on 255fedcf, which swallows the ``setattr`` error
        and reports ``plugin_import_failed`` while ``json.encoder.pwn`` stays.
        """
        import json.encoder as encoder
        source = (
            "import json.encoder as enc, types\n"
            "class Guard(types.ModuleType):\n"
            "    def __setattr__(self, name, value):\n"
            "        raise RuntimeError('no-set')\n"
            "    def __delattr__(self, name):\n"
            "        raise RuntimeError('no-del')\n"
            "mod = types.ModuleType('pwn')\n"
            "mod.marker = 'pwned'\n"
            "enc.pwn = mod\n"
            "enc.__class__ = Guard\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.encoder.guard")
            self.assertIs(type(encoder), types.ModuleType)
            self.assertFalse(hasattr(encoder, "pwn"))
            self.assertNotEqual(getattr(getattr(encoder, "pwn", None), "marker", None), "pwned")
        finally:
            self._repair_module(encoder, "pwn")

    def test_a_failed_load_restores_a_module_class_hiding_a_binding(self) -> None:
        """A module-class descriptor must not outlive the failed load.

        ``vars`` still shows the snapshotted module while ``getattr`` returns
        the attacker's. The class is restored, then the binding is checked
        with ``getattr``. Fails on 255fedcf.
        """
        host = types.ModuleType("desc_host")
        real = types.ModuleType("desc_host.real")
        real.marker = "kept"
        host.real = real
        sys.modules["desc_host"] = host
        source = (
            "import desc_host, types\n"
            "evil = types.ModuleType('evil')\n"
            "evil.marker = 'pwned'\n"
            "class Shadow(types.ModuleType):\n"
            "    @property\n"
            "    def real(self):\n"
            "        return evil\n"
            "    @property\n"
            "    def pwn(self):\n"
            "        return evil\n"
            "desc_host.__class__ = Shadow\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.desc.host")
            self.assertIs(type(host), types.ModuleType)
            self.assertIs(getattr(host, "real", None), real)
            self.assertEqual(getattr(real, "marker", None), "kept")
            self.assertNotEqual(getattr(getattr(host, "pwn", None), "marker", None), "pwned")
            self.assertFalse(hasattr(host, "pwn"))
        finally:
            self._repair_module(host, "pwn")
            host.real = real
            sys.modules.pop("desc_host", None)
            sys.modules.pop("desc_host.real", None)
            sys.modules.pop("desc_host.pwn", None)

    def test_a_failed_load_drops_a_module_a_deferred_cycle_plants(self) -> None:
        """A finalizer that arms another cycle must not plant after the load returns.

        One ``gc.collect()`` runs the first finalizer and leaves the new cycle
        for a later collection. Fails on 255fedcf: the later collection inserts
        ``hunt_deferred`` after an ordinary ``plugin_import_failed``.
        """
        import gc
        source = (
            "import sys, types\n"
            "def plant():\n"
            "    class First:\n"
            "        def __del__(self):\n"
            "            class Second:\n"
            "                def __del__(self):\n"
            "                    planted = types.ModuleType('hunt_deferred')\n"
            "                    planted.marker = 'pwned'\n"
            "                    sys.modules['hunt_deferred'] = planted\n"
            "            a = Second()\n"
            "            b = Second()\n"
            "            a.other = b\n"
            "            b.other = a\n"
            "    carrier = types.ModuleType('hunt_carrier')\n"
            "    obj = First()\n"
            "    carrier.obj = obj\n"
            "    obj.carrier = carrier\n"
            "    sys.modules['hunt_carrier'] = carrier\n"
            "plant()\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.deferred.cycle")
            self.assertNotIn("hunt_carrier", sys.modules)
            for _ in range(4):
                gc.collect()
            self.assertNotIn("hunt_deferred", sys.modules)
            self.assertNotEqual(getattr(sys.modules.get("hunt_deferred"), "marker", None), "pwned")
        finally:
            self._clear_hunt_modules()
            sys.modules.pop("hunt_deferred", None)

    def test_a_failed_load_drops_a_module_reachable_only_as_an_attribute(self) -> None:
        """A module that is not in ``sys.modules`` is still snapshotted.

        ``nest_only.child`` and ``nest_only.child.grand`` are only attributes.
        Bindings added on them are removed. Fails on 255fedcf, which leaves
        ``nest_only.child.pwn`` reachable after ``plugin_import_failed``.
        """
        nest = types.ModuleType("nest_only")
        child = types.ModuleType("nest_only.child")
        grand = types.ModuleType("nest_only.child.grand")
        child.marker = "kept"
        grand.marker = "kept"
        nest.child = child
        child.grand = grand
        sys.modules["nest_only"] = nest
        source = (
            "import nest_only, types\n"
            "pwn = types.ModuleType('pwn')\n"
            "pwn.marker = 'pwned'\n"
            "nest_only.child.pwn = pwn\n"
            "deeper = types.ModuleType('deeper')\n"
            "deeper.marker = 'pwned'\n"
            "nest_only.child.grand.pwn = deeper\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            self._load_failing(source, plugin_id="bad.nest.only")
            self.assertIs(nest.child, child)
            self.assertIs(child.grand, grand)
            self.assertFalse(hasattr(child, "pwn"))
            self.assertFalse(hasattr(grand, "pwn"))
            self.assertEqual(child.marker, "kept")
            self.assertEqual(grand.marker, "kept")
            self.assertNotEqual(getattr(getattr(child, "pwn", None), "marker", None), "pwned")
            self.assertNotEqual(getattr(getattr(grand, "pwn", None), "marker", None), "pwned")
        finally:
            self._repair_module(child, "pwn")
            self._repair_module(grand, "pwn")
            sys.modules.pop("nest_only", None)
            sys.modules.pop("nest_only.child", None)
            sys.modules.pop("nest_only.child.grand", None)
            sys.modules.pop("nest_only.child.pwn", None)

    def test_a_successful_load_keeps_a_preexisting_nested_module(self) -> None:
        """Walking nested modules for the snapshot does not disturb a good load."""
        nest = types.ModuleType("nest_keep")
        child = types.ModuleType("nest_keep.child")
        child.marker = "kept"
        nest.child = child
        sys.modules["nest_keep"] = nest
        source = (
            "import nest_keep\n"
            "VALUE = nest_keep.child.marker\n"
        ).encode()
        try:
            dev_loader, raws, sigs = self._loader_for({"good.nest.keep": source})
            loaded = dev_loader.load(raws["good.nest.keep"], sigs["good.nest.keep"], source)
            self.assertEqual(loaded.module.VALUE, "kept")
            self.assertIs(nest.child, child)
            self.assertFalse(hasattr(child, "pwn"))
            self.assertEqual(dev_loader.permissions("good.nest.keep"), ("read",))
        finally:
            sys.modules.pop("nest_keep", None)
            sys.modules.pop("nest_keep.child", None)

    def _clear_import_latch(self) -> None:
        import recursion_core.plugin_security.loader as loader_mod
        loader_mod._import_restore_latched = False

    def _repair_sys(self, modules, meta, hooks, cache, path) -> None:
        """Put ``sys`` back if a repro left its class or import tables swapped."""
        try:
            object.__setattr__(sys, "__class__", types.ModuleType)
        except Exception:
            pass
        try:
            namespace = types.ModuleType.__dict__["__dict__"].__get__(sys)
        except Exception:
            self._clear_import_latch()
            return
        namespace["modules"] = modules
        namespace["meta_path"] = meta
        namespace["path_hooks"] = hooks
        namespace["path_importer_cache"] = cache
        namespace["path"] = path
        modules.pop("pwn_sys", None)
        self._clear_import_latch()

    def test_a_raising_sys_class_cannot_keep_the_import_tables(self) -> None:
        """A Guard class on ``sys`` must not abort settle before the tables are put back.

        ``sys.modules = modules`` used to run while ``sys.__class__`` still
        raised, so the ``RuntimeError`` skipped the class restore and left
        the attacker's module dict and meta_path installed. That is not a
        clean ``plugin_import_failed``. Fails on b1125f27.
        """
        source = (
            "import sys, types\n"
            "class Guard(types.ModuleType):\n"
            "    def __setattr__(self, name, value):\n"
            "        raise RuntimeError('no-set')\n"
            "    def __delattr__(self, name):\n"
            "        raise RuntimeError('no-del')\n"
            "mapping = types.ModuleType.__dict__['__dict__'].__get__(sys)\n"
            "evil = dict(mapping['modules'])\n"
            "planted = types.ModuleType('pwn_sys')\n"
            "planted.marker = 'pwned'\n"
            "evil['pwn_sys'] = planted\n"
            "mapping['modules'] = evil\n"
            "mapping['meta_path'] = [object()]\n"
            "object.__setattr__(sys, '__class__', Guard)\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        saved_modules = sys.modules
        saved_meta = sys.meta_path
        saved_hooks = sys.path_hooks
        saved_cache = sys.path_importer_cache
        saved_path = sys.path
        try:
            try:
                code = self._run_failing(source, plugin_id="bad.sys.guard")
            except Exception as exc:
                code = type(exc).__name__
            attacker = (
                type(sys) is not types.ModuleType
                or sys.modules is not saved_modules
                or "pwn_sys" in sys.modules
                or sys.meta_path is not saved_meta
            )
            self.assertFalse(attacker and code in ("plugin_import_failed", "RuntimeError"))
            self.assertFalse(attacker)
            self.assertIs(type(sys), types.ModuleType)
            self.assertIs(sys.modules, saved_modules)
            self.assertNotIn("pwn_sys", sys.modules)
            self.assertIs(sys.meta_path, saved_meta)
            self.assertEqual(code, "plugin_import_failed")
            good = b"VALUE = 1\n"
            dev_loader, raws, sigs = self._loader_for({"good.after.sys": good})
            loaded = dev_loader.load(raws["good.after.sys"], sigs["good.after.sys"], good)
            self.assertEqual(loaded.module.VALUE, 1)
        finally:
            self._repair_sys(saved_modules, saved_meta, saved_hooks, saved_cache, saved_path)
            for name in list(sys.modules):
                if name.startswith("csl_plugin_"):
                    sys.modules.pop(name, None)

    def test_a_failed_class_descriptor_restore_refuses_later_loads(self) -> None:
        """A heap class attribute that cannot be removed is not a clean import failure.

        The failed step latches the process: a later load is refused with
        ``plugin_import_state_unrestored`` instead of running more plugin code.
        Fails on b1125f27, which reports ``plugin_import_failed`` and leaves
        the attribute in place.
        """
        class Meta(type):
            def __delattr__(cls, name):
                if name == "stuck":
                    raise RuntimeError("no-del-class")
                return type.__delattr__(cls, name)

        class Heap(types.ModuleType, metaclass=Meta):
            pass

        host = Heap("latch_host")
        sys.modules["latch_host"] = host
        source = (
            "import latch_host\n"
            "type(latch_host).stuck = 'pwned'\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            code = self._run_failing(source, plugin_id="bad.latch.class")
            stuck = Heap.__dict__.get("stuck") == "pwned"
            self.assertFalse(stuck and code == "plugin_import_failed")
            if stuck:
                self.assertEqual(code, "plugin_import_state_unrestored")
                later = self._run_failing(b"VALUE = 1\n", plugin_id="later.latch")
                self.assertEqual(later, "plugin_import_state_unrestored")
            else:
                self.assertEqual(code, "plugin_import_failed")
        finally:
            try:
                type.__delattr__(Heap, "stuck")
            except Exception:
                pass
            sys.modules.pop("latch_host", None)
            self._clear_import_latch()

    def test_a_heap_module_property_cannot_plant_during_the_proof(self) -> None:
        """A heap module property must not plant a module or a class attribute.

        ``getattr`` during the proof used to run the property after the raw
        dict had already been scanned. The property returns the snapshotted
        module, writes another module into the dict, and adds ``pwn_crypto``.
        That residue with ``plugin_import_failed`` is the bug. Fails on b1125f27.
        A cryptography ``_ModuleWithDeprecations`` module is checked the same
        way when that package is installed.
        """
        real = types.ModuleType("heap_host.real")
        real.marker = "kept"
        flag = types.ModuleType("heap_flag")
        flag.on = False
        sys.modules["heap_flag"] = flag

        class Heap(types.ModuleType):
            @property
            def real(self):
                if sys.modules["heap_flag"].on:
                    planted = types.ModuleType("heap_host.planted")
                    planted.marker = "pwned"
                    namespace = types.ModuleType.__dict__["__dict__"].__get__(self)
                    namespace["planted"] = planted
                    type(self).pwn_crypto = property(lambda self, planted=planted: planted)
                return real

        host = Heap("heap_host")
        namespace = types.ModuleType.__dict__["__dict__"].__get__(host)
        namespace["real"] = real
        sys.modules["heap_host"] = host
        source = (
            "import heap_flag, heap_host\n"
            "heap_flag.on = True\n"
            "heap_host.real\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            code = self._run_failing(source, plugin_id="bad.heap.prop")
            flag.on = False
            namespace = types.ModuleType.__dict__["__dict__"].__get__(host)
            planted = namespace.get("planted")
            residue = (
                planted is not None
                or "pwn_crypto" in type(host).__dict__
                or getattr(planted, "marker", None) == "pwned"
            )
            self.assertFalse(residue and code == "plugin_import_failed")
            if residue:
                self.assertEqual(code, "plugin_import_state_unrestored")
            else:
                self.assertEqual(code, "plugin_import_failed")
                self.assertIs(type(host), Heap)
                self.assertIs(namespace.get("real"), real)
                self.assertNotIn("planted", namespace)
                self.assertNotIn("pwn_crypto", Heap.__dict__)
                self.assertIs(host.real, real)
        finally:
            flag.on = False
            try:
                namespace = types.ModuleType.__dict__["__dict__"].__get__(host)
                namespace.pop("planted", None)
            except Exception:
                pass
            if "pwn_crypto" in Heap.__dict__:
                try:
                    del Heap.pwn_crypto
                except Exception:
                    try:
                        type.__delattr__(Heap, "pwn_crypto")
                    except Exception:
                        pass
            sys.modules.pop("heap_host", None)
            sys.modules.pop("heap_flag", None)
            sys.modules.pop("heap_host.planted", None)
            sys.modules.pop("heap_host.real", None)
            self._clear_import_latch()

        crypto_types = None
        try:
            import cryptography.hazmat.primitives.asymmetric.types as crypto_types
        except Exception:
            crypto_types = None
        if crypto_types is None:
            return
        crypto_cls = type(crypto_types)
        crypto_flag = types.ModuleType("crypto_heap_flag")
        crypto_flag.on = False
        sys.modules["crypto_heap_flag"] = crypto_flag
        crypto_source = (
            "import crypto_heap_flag, types\n"
            "import cryptography.hazmat.primitives.asymmetric.types as target\n"
            "expected = types.ModuleType.__dict__['__dict__'].__get__(target).get('_module')\n"
            "def getter(self):\n"
            "    if not crypto_heap_flag.on:\n"
            "        return expected\n"
            "    planted = types.ModuleType('crypto_heap.planted')\n"
            "    planted.marker = 'pwned'\n"
            "    namespace = types.ModuleType.__dict__['__dict__'].__get__(self)\n"
            "    namespace['planted'] = planted\n"
            "    type(self).pwn_crypto = property(lambda self, planted=planted: planted)\n"
            "    return expected\n"
            "type(target)._module = property(getter)\n"
            "crypto_heap_flag.on = True\n"
            "target._module\n"
            "raise RuntimeError('boom')\n"
        ).encode()
        try:
            code = self._run_failing(crypto_source, plugin_id="bad.crypto.heap")
            crypto_flag.on = False
            namespace = types.ModuleType.__dict__["__dict__"].__get__(crypto_types)
            shadow = crypto_cls.__dict__.get("_module")
            residue = (
                "planted" in namespace
                or "pwn_crypto" in crypto_cls.__dict__
                or isinstance(shadow, property)
            )
            self.assertFalse(residue and code == "plugin_import_failed")
            if residue:
                self.assertEqual(code, "plugin_import_state_unrestored")
            else:
                self.assertEqual(code, "plugin_import_failed")
                self.assertNotIn("planted", namespace)
                self.assertNotIn("pwn_crypto", crypto_cls.__dict__)
                self.assertNotIsInstance(crypto_cls.__dict__.get("_module"), property)
        finally:
            crypto_flag.on = False
            try:
                namespace = types.ModuleType.__dict__["__dict__"].__get__(crypto_types)
                namespace.pop("planted", None)
            except Exception:
                pass
            for name in ("pwn_crypto", "_module"):
                if name in crypto_cls.__dict__ and (
                        name == "pwn_crypto" or isinstance(crypto_cls.__dict__.get(name), property)):
                    try:
                        delattr(crypto_cls, name)
                    except Exception:
                        try:
                            type.__delattr__(crypto_cls, name)
                        except Exception:
                            pass
            sys.modules.pop("crypto_heap_flag", None)
            sys.modules.pop("crypto_heap.planted", None)
            self._clear_import_latch()


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

    def test_a_publish_failure_still_treats_the_committed_key_as_revoked(self) -> None:
        path = os.path.join(temp_dir(self), "revoked.db")
        from recursion_core.plugin_security import create_revocation_store
        create_revocation_store(path)
        kid = "ed25519:" + "cd" * 16
        store = TrustStore({}, revocation_store=path)
        self.addCleanup(store.close)

        def boom(kid_arg, generation):
            raise RuntimeError("publish failed")

        store._publish_generation = boom
        with self.assertRaises(RuntimeError):
            store.revoke(kid)
        self.assertIn(kid, store.revoked)
        self.assertEqual(store.generation(kid), 1)
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        row = db.execute("SELECT generation FROM revoked WHERE key_id = ?", (kid,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1)


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
