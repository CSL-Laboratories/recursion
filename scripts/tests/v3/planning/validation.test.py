#!/usr/bin/env python3
"""Focused tests for strict decoding, canonicalization, and DAG validation."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

from scripts.v3.planning.validation.canonical import (  # noqa: E402
    M1AError, canonical_bytes, decode, digest,
)
from scripts.v3.planning.validation.contracts import validate_dag  # noqa: E402


def test_digest_regression() -> None:
    """Check digest results are unchanged and input is not mutated in both modes."""
    import copy
    import hashlib

    # 1. Mode: omit_content_digest=False with content_digest
    doc_with_digest = {
        "command_id": "cmd-001",
        "command_type": "accept-goal-spec",
        "content_digest": "sha256:preexisting-digest",
        "nested": {"items": [1, 2, 3], "flag": True},
    }
    snapshot_with = copy.deepcopy(doc_with_digest)
    d_false = digest(doc_with_digest, omit_content_digest=False)
    assert doc_with_digest == snapshot_with, "input mutated when omit_content_digest=False"
    assert "content_digest" in doc_with_digest
    expected_false = hashlib.sha256(canonical_bytes(snapshot_with)).hexdigest()
    assert d_false == expected_false

    # Default omit_content_digest is False
    assert digest(doc_with_digest) == d_false
    assert doc_with_digest == snapshot_with

    # 2. Mode: omit_content_digest=True with content_digest
    d_true = digest(doc_with_digest, omit_content_digest=True)
    assert doc_with_digest == snapshot_with, "input mutated when omit_content_digest=True"
    assert "content_digest" in doc_with_digest
    doc_without_digest = {
        "command_id": "cmd-001",
        "command_type": "accept-goal-spec",
        "nested": {"items": [1, 2, 3], "flag": True},
    }
    expected_true = hashlib.sha256(canonical_bytes(doc_without_digest)).hexdigest()
    assert d_true == expected_true
    assert d_true != d_false

    # 3. Mode: omit_content_digest=True without content_digest
    snapshot_without = copy.deepcopy(doc_without_digest)
    d_true_nodigest = digest(doc_without_digest, omit_content_digest=True)
    assert doc_without_digest == snapshot_without, "input mutated when omit_content_digest=True without content_digest"
    assert d_true_nodigest == expected_true

    # 4. Mode: omit_content_digest=False without content_digest
    d_false_nodigest = digest(doc_without_digest, omit_content_digest=False)
    assert doc_without_digest == snapshot_without, "input mutated when omit_content_digest=False without content_digest"
    assert d_false_nodigest == expected_true

    # 5. Non-dict inputs in both modes
    list_input = [1, 2, {"a": "b"}, [3, 4]]
    snapshot_list = copy.deepcopy(list_input)
    assert digest(list_input, omit_content_digest=False) == hashlib.sha256(canonical_bytes(snapshot_list)).hexdigest()
    assert list_input == snapshot_list, "list input mutated when omit_content_digest=False"
    assert digest(list_input, omit_content_digest=True) == hashlib.sha256(canonical_bytes(snapshot_list)).hexdigest()
    assert list_input == snapshot_list, "list input mutated when omit_content_digest=True"

    # 6. Verify deepcopy is ONLY called when omit_content_digest=True requires mutating
    class UncopyableDict(dict):
        def __deepcopy__(self, memo):
            raise RuntimeError("deepcopy should not be called")

    # omit_content_digest=False must NOT call deepcopy
    uncopyable_with = UncopyableDict({"k": "v", "content_digest": "present"})
    assert digest(uncopyable_with, omit_content_digest=False) == hashlib.sha256(canonical_bytes(uncopyable_with)).hexdigest()

    # omit_content_digest=True without content_digest must NOT call deepcopy
    uncopyable_without = UncopyableDict({"k": "v"})
    assert digest(uncopyable_without, omit_content_digest=True) == hashlib.sha256(canonical_bytes(uncopyable_without)).hexdigest()

    # omit_content_digest=True with content_digest MUST call deepcopy
    try:
        digest(uncopyable_with, omit_content_digest=True)
    except RuntimeError as exc:
        assert str(exc) == "deepcopy should not be called"
    else:
        raise AssertionError("expected RuntimeError from deepcopy when mutating was required")

    # 7. Dict subclass overriding __contains__ to always return False and record a side effect
    side_effects: list[str] = []

    class SideEffectDict(dict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.override_called = False

        def __contains__(self, key):
            self.override_called = True
            side_effects.append(key)
            return False

    subclass_input = SideEffectDict(doc_with_digest)
    subclass_snapshot = copy.deepcopy(subclass_input)
    assert digest(subclass_input, omit_content_digest=True) == digest(doc_without_digest)
    assert subclass_input == subclass_snapshot, "input mutated when omit_content_digest=True on dict subclass"
    assert subclass_input.override_called is False, "__contains__ override was called on input"
    assert side_effects == [], f"side effect recorded by __contains__: {side_effects}"



def main() -> int:
    decoded = dag_cases = 0
    for path in sorted(FIXTURE_DIR.glob("F-M1A-*.json")):
        for case in json.loads(path.read_text(encoding="utf-8"))["cases"]:
            for raw, expected in zip(
                case["decode_inputs"], case["expected_decode_failures"], strict=True
            ):
                try:
                    decode(raw, expected["schema_epoch"])
                except M1AError as error:
                    assert error.code == expected["primary_error"]
                else:
                    raise AssertionError("strict decoder accepted a negative oracle")
                decoded += 1
            for command, receipt in zip(
                case["initial_commands"] + case["commands"],
                case["initial_receipts"] + case["expected_receipts"], strict=True,
            ):
                assert digest(command, omit_content_digest=True) == command["content_digest"]
                assert canonical_bytes(command) == canonical_bytes(
                    dict(reversed(list(command.items())))
                )
                if command["command_type"] not in ("propose-plan-revision", "propose-replan"):
                    continue
                payload = command["payload"]
                plan = payload.get("plan_revision", payload.get("new_plan_revision"))
                error, witness = validate_dag(plan, payload["task_nodes"])
                if receipt["primary_error"] in {"E-DAG-CYCLE", "E-DAG-DEPENDENCY", "E-DAG-CONFLICT"}:
                    assert error == receipt["primary_error"]
                    assert witness == receipt["witness"]
                elif receipt["primary_error"] is None:
                    assert error is None and witness is None
                dag_cases += 1
    assert canonical_bytes({"\uffff": 1, "\U00010000": 2}) == b'{"\xf0\x90\x80\x80":2,"\xef\xbf\xbf":1}'
    test_digest_regression()
    print(f"M1A_VALIDATION_PASS decode_failures={decoded} dag_cases={dag_cases}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as error:
        print(f"M1A_VALIDATION_FAIL: {error}", file=sys.stderr)
        raise SystemExit(1) from error
