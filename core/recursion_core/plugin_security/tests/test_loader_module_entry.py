"""PS6 (P2, the Nov 15 sweeps' triage): a plugin module is in ``sys.modules`` while its code runs.

``PluginLoader`` executed the plugin's code in a fresh module that ``sys.modules`` did not hold, so a
dataclass with postponed annotations (``dataclasses`` looks the class's module up there) was refused
``plugin_import_failed``. The entry is there only while the code runs; the one before is put back.
"""
from __future__ import annotations

import hashlib
import linecache
import sys
import threading
import unittest

from recursion_core.plugin_security import (
    CoreHealth, Grant, PluginLoader, Refused, ReservedEffects, TrustStore, canonical,
)

DEV = "22" * 32
NAME = "csl_plugin_demo_plugin"


def _manifest(source, plugin_id="demo.plugin"):
    return canonical({
        "schema": "csl-plugin-manifest/vNext", "plugin_id": plugin_id, "version": "1.0.0",
        "artifact_sha256": hashlib.sha256(source).hexdigest(), "dependency_lock_sha256": "0" * 64,
        "install_scripts": [], "developer_key": DEV, "permissions": ["read", "propose"],
        "network_destinations": [], "credential_refs": [],
    })


def _module_name(plugin_id):
    return "csl_plugin_" + plugin_id.replace(".", "_").replace("-", "_")


def _drop_non_str_module_keys():
    """Drop keys a plugin left in ``sys.modules`` without comparing them."""
    modules = sys.modules
    kept = [(key, value) for key, value in list(dict.items(modules)) if type(key) is str]
    modules.clear()
    for key, value in kept:
        dict.__setitem__(modules, key, value)


def _has_str_key(name):
    return any(type(key) is str and key == name for key in list(dict.keys(sys.modules)))


def _pop_str_key(name):
    """Remove one plain-str entry. A raising comparison drops the non-str keys first."""
    try:
        dict.pop(sys.modules, name, None)
    except Exception:  # noqa: BLE001 - a plugin key's comparison is plugin code
        _drop_non_str_module_keys()
        dict.pop(sys.modules, name, None)


class Probe:
    def __init__(self):
        self.calls = 0

    def gil_enabled(self):
        self.calls += 1
        return False


class ModuleEntryTests(unittest.TestCase):
    def load(self, source):
        return self.load_as("demo.plugin", source)

    def load_as(self, plugin_id, source):
        digest = hashlib.sha256(source).hexdigest()
        grant = Grant(plugin_id, digest, None, ("read", "propose"))
        self.probe = Probe()
        loader = PluginLoader(TrustStore({}), {plugin_id: grant}, ReservedEffects(), CoreHealth(), self.probe)
        self.addCleanup(_pop_str_key, _module_name(plugin_id))
        return loader.load(_manifest(source, plugin_id), None, source)

    def setUp(self):
        self.addCleanup(sys.modules.pop, NAME, None)

    def test_PS6_dataclass_with_postponed_annotations_loads(self):
        source = (b"from __future__ import annotations\n"
                  b"from dataclasses import dataclass\n"
                  b"@dataclass\n"
                  b"class Config:\n"
                  b"    enabled: bool = True\n"
                  b"VALUE = Config().enabled\n")
        loaded = self.load(source)
        self.assertIs(loaded.module.VALUE, True)
        self.assertEqual(self.probe.calls, 1)

    def test_the_module_is_its_own_entry_while_it_runs_and_none_after(self):
        source = (b"import sys\n"
                  b"SEEN = sys.modules.get(__name__)\n")
        loaded = self.load(source)
        self.assertIs(loaded.module.SEEN, loaded.module)
        self.assertNotIn(NAME, sys.modules)

    def test_an_earlier_entry_of_that_name_is_put_back(self):
        marker = object()
        sys.modules[NAME] = marker
        self.load(b"VALUE = 1\n")
        self.assertIs(sys.modules[NAME], marker)

    def test_a_failing_plugin_leaves_no_entry_and_the_gil_is_read(self):
        with self.assertRaises(Refused) as caught:
            self.load(b"raise ValueError('no')\n")
        self.assertEqual(caught.exception.code, "plugin_import_failed")
        self.assertNotIn(NAME, sys.modules)
        self.assertEqual(self.probe.calls, 1)

    def test_a_plugin_that_rebinds_sys_modules_is_still_gil_checked(self):
        source = (b"import sys\n"
                  b"sys.modules = list(sys.modules)\n")
        saved = sys.modules
        self.addCleanup(setattr, sys, "modules", saved)
        try:
            self.load(source)
        except Refused:
            pass
        finally:
            sys.modules = saved
        self.assertEqual(self.probe.calls, 1)

    def test_an_earlier_entry_is_displaced_only_while_the_plugin_runs(self):
        marker = object()
        sys.modules[NAME] = marker
        source = b"import sys\nSEEN = sys.modules.get(__name__)\n"
        loaded = self.load(source)
        self.assertIs(loaded.module.SEEN, loaded.module)
        self.assertIs(sys.modules[NAME], marker)
        self.assertEqual(self.probe.calls, 1)

    def test_a_hostile_modules_key_does_not_skip_the_gil_read(self):
        # F1: a key left by a loaded plugin, whose comparison raises, must not skip the GIL read
        # and must not escape as an untyped error on the next load.
        attacker = (
            b"import sys\n"
            b"class K:\n"
            b"    def __hash__(self):\n"
            b"        return hash('csl_plugin_victim')\n"
            b"    def __eq__(self, other):\n"
            b"        raise RuntimeError('boom-eq')\n"
            b"sys.modules[K()] = object()\n"
        )
        self.addCleanup(_drop_non_str_module_keys)
        self.load_as("attacker", attacker)
        loaded = self.load_as("victim", b"VALUE = 1\n")
        self.assertEqual(loaded.module.VALUE, 1)
        self.assertEqual(self.probe.calls, 1)
        self.assertFalse(_has_str_key("csl_plugin_victim"))

    def test_a_poisoned_entry_does_not_skip_the_gil_read_on_retry(self):
        # F1 again: the plugin replaces its own entry with a raising key, then fails. The retry
        # still reads the GIL and does not escape as an untyped error.
        poison = (
            b"import sys\n"
            b"class K:\n"
            b"    def __hash__(self):\n"
            b"        return hash('csl_plugin_poison')\n"
            b"    def __eq__(self, other):\n"
            b"        raise RuntimeError('boom-eq')\n"
            b"sys.modules.pop(__name__, None)\n"
            b"sys.modules[K()] = object()\n"
            b"raise ValueError('the plugin fails after poisoning')\n"
        )
        self.addCleanup(_drop_non_str_module_keys)
        with self.assertRaises(Refused) as caught:
            self.load_as("poison", poison)
        self.assertEqual(caught.exception.code, "plugin_import_failed")
        self.assertGreaterEqual(self.probe.calls, 1)
        loaded = self.load_as("poison", b"VALUE = 1\n")
        self.assertEqual(loaded.module.VALUE, 1)
        self.assertEqual(self.probe.calls, 1)

    def test_two_loaders_do_not_hand_each_other_the_module(self):
        # F3: two loaders, one name. Each plugin must see its own module, and the name is gone after.
        source = (
            b"import sys\n"
            b"import time\n"
            b"time.sleep(0.01)\n"
            b"seen = sys.modules.get(__name__)\n"
            b"OWN = seen is not None and seen.__dict__ is globals()\n"
        )
        name = _module_name("same.slot")
        self.addCleanup(sys.modules.pop, name, None)
        bad = 0
        errors = []

        def once(out, tag):
            digest = hashlib.sha256(source).hexdigest()
            probe = Probe()
            grant = Grant("same.slot", digest, None, ("read", "propose"))
            loader = PluginLoader(TrustStore({}), {"same.slot": grant}, ReservedEffects(), CoreHealth(), probe)
            try:
                out[tag] = loader.load(_manifest(source, "same.slot"), None, source)
            except BaseException as exc:  # noqa: BLE001 - the race used to escape untyped
                out[tag] = exc

        for _ in range(20):
            out = {}
            left = threading.Thread(target=once, args=(out, "a"))
            right = threading.Thread(target=once, args=(out, "b"))
            left.start()
            right.start()
            left.join(10)
            right.join(10)
            self.assertFalse(left.is_alive())
            self.assertFalse(right.is_alive())
            for tag in ("a", "b"):
                got = out.get(tag)
                if not hasattr(got, "module"):
                    errors.append(repr(got))
                elif getattr(got.module, "OWN", None) is not True:
                    bad += 1
            sys.modules.pop(name, None)
        self.assertEqual(errors, [])
        self.assertEqual(bad, 0)
        self.assertNotIn(name, sys.modules)

    def test_a_rebind_of_sys_modules_drops_the_entry_from_the_old_dict(self):
        # F2: the loader's own reference is cleaned even when the plugin rebinds sys.modules.
        saved = sys.modules
        self.addCleanup(setattr, sys, "modules", saved)
        source = (
            b"import sys\n"
            b"sys.modules = dict(sys.modules)\n"
        )
        try:
            self.load(source)
        finally:
            sys.modules = saved
        self.assertEqual(self.probe.calls, 1)
        self.assertNotIn(NAME, saved)

    def test_an_interruption_after_the_entry_is_published_still_reads_the_gil(self):
        # F4: a KeyboardInterrupt on the line after the publish must not leave the entry
        # and must not skip the GIL read.
        armed = {"on": False, "fired": False}

        def tracer(frame, event, arg):
            if event != "line" or armed["fired"]:
                return tracer
            if frame.f_code.co_name != "_activate" or not frame.f_code.co_filename.endswith("loader.py"):
                return tracer
            src = linecache.getline(frame.f_code.co_filename, frame.f_lineno)
            if "dict.__setitem__" in src:
                armed["on"] = True
                return tracer
            if armed["on"]:
                armed["fired"] = True
                armed["on"] = False
                raise KeyboardInterrupt
            return tracer

        sys.settrace(tracer)
        threading.settrace(tracer)
        try:
            try:
                self.load(b"VALUE = 1\n")
            except (KeyboardInterrupt, Refused):
                pass
        finally:
            sys.settrace(None)
            threading.settrace(None)
        self.assertTrue(armed["fired"])
        self.assertEqual(self.probe.calls, 1)
        self.assertNotIn(NAME, sys.modules)


if __name__ == "__main__":
    unittest.main()
