"""Runs the conformance suite of the published control records under the hosted gate.

The gate (``tools/ci_gate.py``) runs every file named ``tools/test_*.py`` and does not reach the
``conformance/`` folder, so until this file existed those checks ran only where someone ran them by
hand. This file is the bridge and nothing more: it loads the test cases of ``conformance/tests.py``
into this module, where the gate's own runner collects them. Its one check of its own is that it
carries all of them. It edits nothing in the gate.

It needs what the gate already installs: Python 3.12 and ``jsonschema`` (``requirements-ci.txt``).
Nothing here, and nothing it loads, imports the product.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

CONFORMANCE = Path(__file__).resolve().parent.parent / "conformance"

# Loaded by path, under a name of its own: a module called ``tests`` could be something else on a
# given sys.path.
_spec = importlib.util.spec_from_file_location("wire_conformance_tests", CONFORMANCE / "tests.py")
_suite = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _suite
_spec.loader.exec_module(_suite)

# EVERY test class of the suite, under its own name, so that a class added there runs here with no edit to
# this file. (Until Nov 13, 2026 three classes were named here one by one, and a fourth, added to the suite,
# ran by hand and not under the gate.) The count below is checked against the suite's own.
_classes = {name: value for name, value in vars(_suite).items()
            if isinstance(value, type) and issubclass(value, unittest.TestCase)}
globals().update(_classes)


class BridgeTest(unittest.TestCase):
    """The one test that is this file's own: the bridge carries the whole suite."""

    def test_every_test_of_the_conformance_suite_is_collected_here(self):
        # A runner collects the test classes it finds among this module's own names, whatever name it loaded
        # the module under (the gate loads it under one of its own): so each class must be one of those names.
        loader = unittest.TestLoader()
        there = loader.loadTestsFromModule(_suite).countTestCases()
        mine = globals()
        here = sum(loader.loadTestsFromTestCase(value).countTestCases() for name, value in _classes.items()
                   if mine.get(name) is value)
        self.assertGreater(there, 2000, "the conformance suite did not load")
        self.assertEqual(here, there, "this file collects every conformance test")
        self.assertLessEqual({"PublishedFilesTest", "FixtureTest", "VectorTest", "RulesTest"}, set(_classes))
