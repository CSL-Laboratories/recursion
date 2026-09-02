#!/usr/bin/env python3
"""Red control: engine manifest field-rule validation, resolve-engine.py --validate.

Frozen contract: docs/plans/engine-plugin-architecture.md, "Controls" #1, and the
worked spec handed down with this control set. Neither scripts/resolve-engine.py nor
scripts/engines/*.json exist yet, so every case in this file is expected to fail until
Component 1 (manifests) and Component 2 (resolver --validate mode) land.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
RESOLVE_ENGINE = SOURCE / "scripts" / "resolve-engine.py"
ENGINES_DIR = SOURCE / "scripts" / "engines"

SHIPPED_MANIFESTS = ["gemini.json", "cursor.json", "opencode.json", "opencode-local.json"]

BASE_MANIFEST = {
    "schema_version": 1,
    "name": "sampleeng",
    "description": "Sample engine for red-control tests",
    "executable": "sampleeng",
    "argv": ["{{EXE}}", "-p", "{{PROMPT}}"],
    "model": {"required": False, "argv_suffix": ["--model", "{{MODEL}}"]},
    "roles": ["implementer"],
    "billing": {"basis": "flat-subscription", "provider": "sample-provider"},
    "network": {"policy": "provider-only", "endpoints": ["sample.example.com"]},
    "availability_probe": None,
    "concurrency_cap": None,
    "env_passthrough": [],
}

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"ok - {label}")
    else:
        print(f"FAIL - {label}")
        failures.append(label)


def run_validate(engine: str, engine_dir: Path | None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if engine_dir is None:
        env.pop("RECURSION_ENGINE_DIR", None)
    else:
        env["RECURSION_ENGINE_DIR"] = str(engine_dir)
    argv = [sys.executable, str(RESOLVE_ENGINE), engine, "--validate"]
    return subprocess.run(argv, capture_output=True, text=True, env=env)


def write_manifest(engine_dir: Path, filename: str, data: dict) -> None:
    (engine_dir / filename).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def run_case(label: str, filename: str, engine_arg: str, data: dict, expected: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        write_manifest(tmp_dir, filename, data)
        result = run_validate(engine_arg, tmp_dir)
        check(
            result.returncode == expected,
            f"{label} (exit {result.returncode}, want {expected})",
        )


def test_shipped_manifests() -> None:
    for filename in SHIPPED_MANIFESTS:
        engine = filename[: -len(".json")]
        path = ENGINES_DIR / filename
        if not path.is_file():
            check(False, f"shipped manifest present: {filename}")
            continue
        result = run_validate(engine, None)
        check(result.returncode == 0, f"shipped manifest validates: {filename} (exit {result.returncode})")

    cursor_path = ENGINES_DIR / "cursor.json"
    cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
    check(cursor.get("model", {}).get("required") is True, "cursor.json model.required is true (no Auto fallback)")


def test_missing_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        result = run_validate("ghostengine", Path(tmp))
        check(result.returncode == 3, f"missing manifest file exits 3 (got {result.returncode})")


def test_mutations() -> None:
    baseline = copy.deepcopy(BASE_MANIFEST)
    run_case("valid manifest in temp dir validates", "sampleeng.json", "sampleeng", baseline, 0)

    missing_field = copy.deepcopy(BASE_MANIFEST)
    del missing_field["roles"]
    run_case("missing required field (roles) rejected", "sampleeng.json", "sampleeng", missing_field, 2)

    name_mismatch = copy.deepcopy(BASE_MANIFEST)
    name_mismatch["name"] = "othername"
    run_case("name not matching filename stem rejected", "sampleeng.json", "sampleeng", name_mismatch, 2)

    builtin_collision = copy.deepcopy(BASE_MANIFEST)
    builtin_collision["name"] = "claude"
    builtin_collision["executable"] = "claude"
    run_case("builtin-name collision (claude) rejected", "claude.json", "claude", builtin_collision, 2)

    exe_not_first = copy.deepcopy(BASE_MANIFEST)
    exe_not_first["argv"] = ["not-exe", "-p", "{{PROMPT}}"]
    run_case("argv element 0 not {{EXE}} rejected", "sampleeng.json", "sampleeng", exe_not_first, 2)

    missing_prompt = copy.deepcopy(BASE_MANIFEST)
    missing_prompt["argv"] = ["{{EXE}}", "-p", "hello"]
    run_case("argv missing {{PROMPT}} rejected", "sampleeng.json", "sampleeng", missing_prompt, 2)

    double_prompt = copy.deepcopy(BASE_MANIFEST)
    double_prompt["argv"] = ["{{EXE}}", "{{PROMPT}}", "{{PROMPT}}"]
    run_case("argv with two {{PROMPT}} elements rejected", "sampleeng.json", "sampleeng", double_prompt, 2)

    unknown_placeholder = copy.deepcopy(BASE_MANIFEST)
    unknown_placeholder["argv"] = ["{{EXE}}", "{{OTHER}}", "{{PROMPT}}"]
    run_case("unknown placeholder element rejected", "sampleeng.json", "sampleeng", unknown_placeholder, 2)

    bad_billing = copy.deepcopy(BASE_MANIFEST)
    bad_billing["billing"] = {"basis": "prepaid", "provider": "x"}
    run_case("billing.basis 'prepaid' rejected", "sampleeng.json", "sampleeng", bad_billing, 2)

    bad_passthrough = copy.deepcopy(BASE_MANIFEST)
    bad_passthrough["env_passthrough"] = ["lower_case"]
    run_case("env_passthrough lowercase entry rejected", "sampleeng.json", "sampleeng", bad_passthrough, 2)

    bad_probe = copy.deepcopy(BASE_MANIFEST)
    bad_probe["availability_probe"] = {
        "type": "tcp",
        "url_env": "RECURSION_ENGINE_SAMPLEENG_HEALTH_URL",
        "timeout_s": 5,
    }
    run_case("availability_probe type 'tcp' rejected", "sampleeng.json", "sampleeng", bad_probe, 2)

    bad_cap = copy.deepcopy(BASE_MANIFEST)
    bad_cap["concurrency_cap"] = 0
    run_case("concurrency_cap 0 rejected", "sampleeng.json", "sampleeng", bad_cap, 2)


def main() -> int:
    test_shipped_manifests()
    test_missing_file()
    test_mutations()

    if failures:
        print(f"engine-manifest.test: {len(failures)} FAILED")
        return 1
    print("engine-manifest.test: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
