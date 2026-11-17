"""PS6 (P2, the Nov 15 sweeps' triage): a plugin module is in ``sys.modules`` while its code runs.

``PluginLoader`` executed the plugin's code in a fresh module that ``sys.modules`` did not hold, so a
dataclass with postponed annotations (``dataclasses`` looks the class's module up there) was refused
``plugin_import_failed``. The entry is there only while the code runs; the one before is put back.
"""
from __future__ import annotations

import hashlib
import sys
import unittest

from recursion_core.plugin_security import (
    CoreHealth, Grant, PluginLoader, Refused, ReservedEffects, TrustStore, canonical,
)

DEV = "22" * 32
NAME = "csl_plugin_demo_plugin"


def _manifest(source):
    return canonical({
        "schema": "csl-plugin-manifest/vNext", "plugin_id": "demo.plugin", "version": "1.0.0",
        "artifact_sha256": hashlib.sha256(source).hexdigest(), "dependency_lock_sha256": "0" * 64,
        "install_scripts": [], "developer_key": DEV, "permissions": ["read", "propose"],
        "network_destinations": [], "credential_refs": [],
    })


class Probe:
    def __init__(self):
        self.calls = 0

    def gil_enabled(self):
        self.calls += 1
        return False


class ModuleEntryTests(unittest.TestCase):
    def load(self, source):
        grant = Grant("demo.plugin", hashlib.sha256(source).hexdigest(), None, ("read", "propose"))
        self.probe = Probe()
        loader = PluginLoader(TrustStore({}), {"demo.plugin": grant}, ReservedEffects(), CoreHealth(), self.probe)
        return loader.load(_manifest(source), None, source)

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


if __name__ == "__main__":
    unittest.main()
