"""The published control schemas, read by lane SPINE's standard-library reader, for this lane's tests.

``fits(ref, value)`` is ``conformance/rules.py``'s ``fits``: whether a value has the shape a published schema
gives it, with the standard library alone (nothing under ``core/`` may import ``jsonschema``). The reader and
the schemas are lane SPINE's files; this module only loads them from the repository the tests run in. Where
they are not there (an installed copy of the core), ``fits`` is None and the tests that need it are skipped.
"""
from __future__ import annotations

import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[4]
RULES = REPO / 'conformance' / 'rules.py'


def _load():
    if not RULES.is_file() or not (REPO / 'schemas' / 'control' / 'v1' / 'run.record.v1.json').is_file():
        return None
    spec = importlib.util.spec_from_file_location('workspace_tests_conformance_rules', RULES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.fits


fits = _load()
