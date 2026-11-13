"""Runs the conformance suite of the published control records under the hosted gate.

The gate (``tools/ci_gate.py``) runs every file named ``tools/test_*.py`` and does not reach the
``conformance/`` folder, so until this file existed those checks ran only where someone ran them by
hand. This file is the bridge and nothing more: it loads the test cases of ``conformance/tests.py``
into this module, where the gate's own runner collects them. It adds no check of its own and edits
nothing in the gate.

It needs what the gate already installs: Python 3.12 and ``jsonschema`` (``requirements-ci.txt``).
Nothing here, and nothing it loads, imports the product.
"""
import importlib.util
import sys
from pathlib import Path

CONFORMANCE = Path(__file__).resolve().parent.parent / "conformance"

# Loaded by path, under a name of its own: a module called ``tests`` could be something else on a
# given sys.path.
_spec = importlib.util.spec_from_file_location("wire_conformance_tests", CONFORMANCE / "tests.py")
_suite = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _suite
_spec.loader.exec_module(_suite)

PublishedFilesTest = _suite.PublishedFilesTest
FixtureTest = _suite.FixtureTest
VectorTest = _suite.VectorTest
