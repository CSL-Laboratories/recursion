# Written by Claude Opus 5.5 (claude-opus-5-5), Claude Code builder lane ENGINE-E5, for Lucas.
"""Checks a value against SPINE's published schemas with SPINE's own standard-library reader.

``conformance/rules.py`` and ``schemas/control/v1/`` are SPINE's files (#368, merged into this lane's branch
for these checks). They are loaded by path, so the check needs neither ``jsonschema`` (absent from the
free-threaded interpreter) nor the repository root on ``sys.path``. Where they are not in the tree,
``AVAILABLE`` is False and the tests that need them skip, saying why.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[6]
RULES = ROOT / "conformance" / "rules.py"
AVAILABLE = RULES.is_file() and (ROOT / "schemas" / "control" / "v1" / "driver.v2.json").is_file()
SKIP_REASON = "SPINE's conformance/rules.py and schemas/control/v1 (#368) are not in this tree"
_rules = None


def rules():
    global _rules
    if _rules is None:
        spec = importlib.util.spec_from_file_location("engine_e5_spine_conformance_rules", RULES)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _rules = module
    return _rules


def fits(ref: str, value) -> bool:
    return rules().fits(ref, value)


def schema_version(name: str) -> str:
    import json
    return json.loads((ROOT / "schemas" / "control" / "v1" / name).read_text(encoding="utf-8"))["x-version"]
