#!/usr/bin/env python3
"""Structural gate for the capability-composition conformance suite.

Deterministic and provider-free. Fails unless the suite layout exists, the reconciled
FIXTURE-INDEX lists every expected fixture id, the source reports are present, and the tree is clean
of forbidden foreign-project identifiers and obvious secrets. It does **not** execute any fixture and
certifies structure only.

    python -X utf8 -B experiments/v3/capability-composition/check_suite.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

ROOT = HERE
for _ in range(8):
    if (ROOT / "tools" / "check_public_scope.py").is_file():
        break
    ROOT = ROOT.parent
else:
    print("FAIL: could not locate tools/check_public_scope.py above the suite")
    raise SystemExit(1)

sys.path.insert(0, str(ROOT))
try:
    from tools.check_public_scope import PATTERN as FORBIDDEN_PATTERN
except Exception as exc:  # pragma: no cover - defensive
    print(f"FAIL: could not import tools.check_public_scope.PATTERN: {exc}")
    raise SystemExit(1)

INDEX = HERE / "FIXTURE-INDEX.md"
README = HERE / "README.md"

SOURCES = [
    ROOT / "docs/v3/research-lane/capability-composition-20261002d/rpc/report.md",
    ROOT / "docs/v3/research-lane/capability-composition-20261002d/rui/report.md",
    ROOT / "docs/v3/research-lane/capability-composition-20261002d/rmob/report.md",
    ROOT / "docs/v3/research-lane/ios-android-20261002/ios/report.md",
    ROOT / "docs/v3/research-lane/ios-android-20261002/android/report.md",
]

EXPECTED_IDS = (
    [f"F{i:02d}" for i in range(1, 19)]
    + [f"UI-{i:02d}" for i in range(1, 21)]
    + [f"MOB-F{i:02d}" for i in range(1, 25)]
    + [f"MP{i:02d}" for i in range(1, 12)]
)

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"),
]


def main() -> int:
    errors: list[str] = []

    for path, label in ((README, "README.md"), (INDEX, "FIXTURE-INDEX.md")):
        if not path.exists():
            errors.append(f"missing {label}")
    for sub in ("fixtures", "run"):
        if not (HERE / sub).is_dir():
            errors.append(f"missing directory: {sub}/")
    for source in SOURCES:
        if not source.exists():
            errors.append(f"missing source report: {source.relative_to(ROOT)}")

    if errors:
        print("FAIL: capability-composition suite structural gate")
        for error in errors:
            print(" -", error)
        return 1

    index_text = INDEX.read_text(encoding="utf-8")
    tokens = set(re.findall(r"[A-Za-z0-9-]+", index_text))
    missing = [fid for fid in EXPECTED_IDS if fid not in tokens]
    if missing:
        errors.append(f"FIXTURE-INDEX.md is missing {len(missing)} expected ids (first: {missing[0]})")

    mutants_src = (HERE / "run" / "rpc_mutants.py").read_text(encoding="utf-8")
    mutant_names = re.findall(r"^def (m_\w+)", mutants_src, flags=re.M)
    gates_src = (HERE / "run" / "check_rpc_invariants.py").read_text(encoding="utf-8") + \
        (HERE / "run" / "check_rpc_observed.py").read_text(encoding="utf-8")
    unreferenced = [name for name in mutant_names if name not in gates_src]
    if unreferenced:
        errors.append(f"rpc_mutants.py has unreferenced mutants (fail-closed): {unreferenced}")

    for path in (README, INDEX):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            hit = FORBIDDEN_PATTERN.search(line)
            if hit:
                errors.append(f"{path.name}:{number}: other-project identifier '{hit.group(0)}'")
        for pattern in SECRET_PATTERNS:
            for found in pattern.finditer(text):
                errors.append(f"{path.name}: possible secret material: {found.group(0)[:40]!r}")

    if errors:
        print("FAIL: capability-composition suite structural gate")
        for error in errors:
            print(" -", error)
        return 1

    print(
        "PASS: capability-composition suite structure clean; "
        f"{len(EXPECTED_IDS)} fixture ids registered across R-PC/R-UI/R-MOB/MP; sources present"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
