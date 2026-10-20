#!/usr/bin/env python3
"""Byte-for-byte equivalence of indexed admission against the linear scans."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

from scripts.v3.planning.replay.kernel import admit, genesis  # noqa: E402
from scripts.v3.planning.validation.canonical import (  # noqa: E402
    M1AError, canonical_bytes, digest, seal,
)
from scripts.v3.planning.validation.contracts import (  # noqa: E402
    _AdmissionView, _checkpoint_payload, _find_record, _object_state, _policy,
    validate_admission, validate_dag,
)

_REFERENCE = {}
exec((Path(__file__).with_name("admission_linear_reference.py")).read_text(encoding="utf-8"), _REFERENCE)
reference_admit = _REFERENCE["validate_admission"]
reference_dag = _REFERENCE["validate_dag"]
reference_find = _REFERENCE["_find_record"]
reference_state = _REFERENCE["_object_state"]
reference_policy = _REFERENCE["_policy"]
reference_checkpoint = _REFERENCE["_checkpoint_payload"]


def _load(name: str, case_id: str) -> dict:
    bundle = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return next(case for case in bundle["cases"] if case["case_id"] == case_id)


def _first_case(command_type: str) -> dict:
    for path in sorted(FIXTURE_DIR.glob("F-M1A-*.json")):
        bundle = json.loads(path.read_text(encoding="utf-8"))
        for case in bundle["cases"]:
            commands = case["initial_commands"] + case["commands"]
            if any(command["command_type"] == command_type for command in commands):
                return case
    raise AssertionError(command_type)


def _replay_until(case: dict, command_type: str) -> tuple[dict, dict]:
    state = genesis(case["genesis"])
    journal = []
    for command in case["initial_commands"] + case["commands"]:
        if command["command_type"] == command_type:
            return state, copy.deepcopy(command)
        state, _, receipt, journal = admit(
            state, state["content_digest"], command,
            case["authority_fixtures"], case["policy_fixtures"], journal,
        )
        assert receipt["disposition"] == "accepted", command["command_id"]
    raise AssertionError(command_type)


def _canonical(value):
    if (
        isinstance(value, tuple) and len(value) == 2
        and (value[0] is None or isinstance(value[0], str))
    ):
        error, witness = value
        return canonical_bytes({"error": error, "witness": witness})
    return canonical_bytes(value)


def _capture(fn, args):
    try:
        return ("ok", fn(*args))
    except M1AError as exc:
        return ("m1a", exc.code, exc.detail)
    except Exception as exc:
        return ("exc", type(exc), exc.args)


def _same(fn_old, fn_new, args) -> None:
    before = copy.deepcopy(args)
    left = _capture(fn_old, args)
    assert args == before, "reference mutated an input"
    right = _capture(fn_new, args)
    assert args == before, "indexed admission mutated an input"
    assert left[0] == right[0]
    if left[0] != "ok":
        assert left == right
        return
    assert left[1] == right[1]
    assert _canonical(left[1]) == _canonical(right[1])


def _dag(ids, edges, writes=None, reads=None, effects=None):
    tasks = []
    for node_id in ids:
        tasks.append({
            "object_type": "task-node", "id": node_id, "version": 1,
            "content_digest": "ab" * 32,
            "payload": {
                "plan_ref": {"object_type": "plan-revision", "id": "PLAN-M1A", "version": 1},
                "dependency_node_ids": [
                    edge["from_node_id"] for edge in edges if edge["to_node_id"] == node_id
                ],
                "write_resource_paths": (writes or {}).get(node_id, [f"workspace/output/{node_id}"]),
                "read_resource_paths": (reads or {}).get(node_id, [f"workspace/input/{node_id}"]),
                "effect_keys": (effects or {}).get(node_id, [f"EFFECT-{node_id}"]),
            },
        })
    refs = sorted(
        (
            {
                "object_type": task["object_type"], "id": task["id"],
                "version": task["version"], "content_digest": task["content_digest"],
            }
            for task in tasks
        ),
        key=lambda item: item["id"],
    )
    plan = {
        "object_type": "plan-revision", "id": "PLAN-M1A", "version": 1,
        "payload": {"task_node_refs": refs, "dependency_edges": list(edges)},
    }
    return plan, tasks


def _fixture_cases() -> int:
    checked = 0
    for path in sorted(FIXTURE_DIR.glob("F-M1A-*.json")):
        bundle = json.loads(path.read_text(encoding="utf-8"))
        for case in bundle["cases"]:
            state = genesis(case["genesis"])
            journal = []
            for command in case["initial_commands"] + case["commands"]:
                _same(
                    reference_admit, validate_admission,
                    (state, command, case["authority_fixtures"], case["policy_fixtures"]),
                )
                checked += 1
                payload = command["payload"]
                if command["command_type"] in {
                    "propose-plan-revision", "propose-replan", "amend-goal-spec",
                }:
                    plan = (
                        payload.get("plan_revision")
                        or payload.get("new_plan_revision")
                        or payload.get("replacement_plan_revision")
                    )
                    tasks = payload["task_nodes"]
                    for order in (tasks, list(reversed(tasks))):
                        _same(reference_dag, validate_dag, (plan, order))
                        checked += 1
                state, _, _, journal = admit(
                    state, state["content_digest"], command,
                    case["authority_fixtures"], case["policy_fixtures"], journal,
                )
    return checked


def _adversarial_graphs() -> int:
    checked = 0
    shared = ["TASK-M1A-Z", "TASK-M1A-A", "TASK-M1A-M"]
    plan, tasks = _dag(shared, [], writes={node: ["workspace/shared"] for node in shared})
    _same(reference_dag, validate_dag, (plan, tasks))
    error, witness = validate_dag(plan, tasks)
    assert error == "E-DAG-CONFLICT"
    assert witness["object_ids"] == ["TASK-M1A-A", "TASK-M1A-M"]
    assert witness["field_paths"] == ["write-write"]
    assert canonical_bytes(witness) == canonical_bytes(reference_dag(plan, tasks)[1])
    checked += 1

    samples = []
    samples.append(_dag(
        ["TASK-M1A-B", "TASK-M1A-A"], [],
        writes={
            "TASK-M1A-A": ["workspace/shared"],
            "TASK-M1A-B": ["workspace/shared/child"],
        },
    ))
    samples.append(_dag(
        ["TASK-M1A-B", "TASK-M1A-A"], [],
        writes={"TASK-M1A-A": ["workspace/only-a"], "TASK-M1A-B": ["workspace/shared"]},
        reads={"TASK-M1A-A": ["workspace/shared"], "TASK-M1A-B": ["workspace/only-b"]},
    ))
    samples.append(_dag(
        ["TASK-M1A-B", "TASK-M1A-A"], [],
        effects={"TASK-M1A-A": ["EFFECT-SAME"], "TASK-M1A-B": ["EFFECT-SAME"]},
    ))
    samples.append(_dag(
        ["TASK-M1A-B", "TASK-M1A-A"],
        [{"from_node_id": "TASK-M1A-A", "to_node_id": "TASK-M1A-B"}],
        writes={"TASK-M1A-A": ["workspace/shared"], "TASK-M1A-B": ["workspace/shared"]},
    ))
    samples.append(_dag(
        ["TASK-M1A-A"],
        [{"from_node_id": "TASK-M1A-A", "to_node_id": "TASK-M1A-A"}],
    ))
    samples.append(_dag(
        ["TASK-M1A-A", "TASK-M1A-B"],
        [
            {"from_node_id": "TASK-M1A-A", "to_node_id": "TASK-M1A-B"},
            {"from_node_id": "TASK-M1A-B", "to_node_id": "TASK-M1A-A"},
        ],
    ))
    samples.append(_dag([], []))
    plan, tasks = _dag(["TASK-M1A-A"], [])
    tasks[0]["payload"]["plan_ref"] = {"object_type": "plan-revision", "id": "OTHER", "version": 1}
    samples.append((plan, tasks))
    plan, tasks = _dag(["TASK-M1A-A", "TASK-M1A-B"], [])
    plan["payload"]["task_node_refs"] = plan["payload"]["task_node_refs"][:1]
    samples.append((plan, tasks))
    for plan, tasks in samples:
        _same(reference_dag, validate_dag, (plan, list(reversed(tasks))))
        checked += 1

    # Equal ids keep input order after the stable sort. Both copies conflict.
    plan, tasks = _dag(["TASK-M1A-B", "TASK-M1A-A"], [])
    for task in tasks:
        task["id"] = "TASK-M1A-A"
        task["payload"]["dependency_node_ids"] = []
        task["payload"]["write_resource_paths"] = ["workspace/left"]
    plan["payload"]["task_node_refs"] = sorted(
        (
            {
                "object_type": task["object_type"], "id": task["id"],
                "version": task["version"], "content_digest": task["content_digest"],
            }
            for task in tasks
        ),
        key=lambda item: item["id"],
    )
    plan["payload"]["dependency_edges"] = []
    _same(reference_dag, validate_dag, (plan, tasks))
    checked += 1
    return checked


def _wide_dag(nodes: int, linked: bool) -> int:
    tasks = []
    edges = []
    plan_ref = {"object_type": "plan-revision", "id": "PLAN-M1A", "version": 1}
    for index in range(nodes):
        node_id = f"TASK-M1A-N{index:03d}"
        deps = [f"TASK-M1A-N{index - 1:03d}"] if linked and index else []
        if deps:
            edges.append({"from_node_id": deps[0], "to_node_id": node_id})
        tasks.append({
            "object_type": "task-node", "id": node_id, "version": 1,
            "content_digest": "ab" * 32,
            "payload": {
                "plan_ref": dict(plan_ref),
                "dependency_node_ids": deps,
                "write_resource_paths": [f"workspace/output/{index}"],
                "read_resource_paths": [f"workspace/input/{index}"],
                "effect_keys": [f"EFFECT-N{index:03d}"],
            },
        })
    tasks.reverse()
    refs = sorted(
        (
            {
                "object_type": task["object_type"], "id": task["id"],
                "version": task["version"], "content_digest": task["content_digest"],
            }
            for task in tasks
        ),
        key=lambda item: item["id"],
    )
    plan = {
        "object_type": "plan-revision", "id": "PLAN-M1A", "version": 1,
        "payload": {"task_node_refs": refs, "dependency_edges": edges},
    }
    _same(reference_dag, validate_dag, (plan, tasks))
    return 1


def _scaled_plan() -> int:
    case = _load("F-M1A-001.json", "happy")
    state, command = _replay_until(case, "propose-plan-revision")
    base_task = command["payload"]["task_nodes"][0]
    tasks = []
    count = 64
    for index in range(count):
        task = copy.deepcopy(base_task)
        task["id"] = f"TASK-M1A-N{index:03d}"
        task["payload"]["dependency_node_ids"] = [f"TASK-M1A-N{index - 1:03d}"] if index else []
        task["payload"]["write_resource_paths"] = [f"workspace/output/{index}"]
        task["payload"]["read_resource_paths"] = [f"workspace/input/{index}"]
        tasks.append(seal(task))
    edges = [
        {"from_node_id": f"TASK-M1A-N{index:03d}", "to_node_id": f"TASK-M1A-N{index + 1:03d}"}
        for index in range(count - 1)
    ]
    refs = [
        {
            "object_type": task["object_type"], "id": task["id"],
            "version": task["version"], "content_digest": task["content_digest"],
        }
        for task in tasks
    ]
    plan = command["payload"]["plan_revision"]
    plan["payload"]["task_node_refs"] = refs
    plan["payload"]["dependency_edges"] = edges
    plan["payload"]["graph_digest"] = digest({"task_node_refs": refs, "dependency_edges": edges})
    plan = seal(plan)
    command["payload"]["plan_revision"] = plan
    command["payload"]["task_nodes"] = tasks
    transition = command["payload"]["plan_transition"]
    transition["payload"]["to_plan_ref"] = {
        "object_type": plan["object_type"], "id": plan["id"],
        "version": plan["version"], "content_digest": plan["content_digest"],
    }
    command["payload"]["plan_transition"] = seal(transition)
    command["expected_state_version"] = state["state_version"]
    command["expected_state_digest"] = state["content_digest"]
    command["payload_digest"] = digest(command["payload"])
    command["candidate_digest"] = command["payload_digest"]
    command = seal(command)
    authorities = copy.deepcopy(case["authority_fixtures"])
    authority = next(item for item in authorities if item["attestation_id"] == command["auth_ref"])
    authority["candidate_digest"] = command["candidate_digest"]
    authorities[authorities.index(authority)] = seal(authority)
    _same(
        reference_admit, validate_admission,
        (state, command, authorities, case["policy_fixtures"]),
    )
    assert validate_admission(state, command, authorities, case["policy_fixtures"]) == (None, None)
    return 1


def _record_edges() -> int:
    case = _first_case("derive-work-packet")
    state, command = _replay_until(case, "derive-work-packet")
    authorities = case["authority_fixtures"]
    policies = case["policy_fixtures"]
    task_ref = command["payload"]["task_ref"]
    checked = 0

    decoy_state = copy.deepcopy(state)
    decoy_state["records"] = [{
        "object_type": "task-node", "id": task_ref["id"], "version": task_ref["version"],
        "content_digest": "00" * 32,
        "payload": {
            "plan_ref": {"id": "NOPE", "object_type": "plan-revision", "version": 1},
            "dependency_node_ids": [],
        },
    }] + decoy_state["records"]
    _same(reference_admit, validate_admission, (decoy_state, command, authorities, policies))
    checked += 1

    bad_state = copy.deepcopy(state)
    bad_state["records"] = [{
        "object_type": "task-node", "id": ["UNHASHABLE"], "version": 1,
        "content_digest": "ab" * 32, "payload": {},
    }] + bad_state["records"]
    _same(reference_admit, validate_admission, (bad_state, command, authorities, policies))
    checked += 1

    missing_state = copy.deepcopy(state)
    missing_state["records"] = missing_state["records"] + [{
        "object_type": "work-packet", "id": "PKT-M1A-Z", "content_digest": "ab" * 32,
    }]
    _same(reference_admit, validate_admission, (missing_state, command, authorities, policies))
    checked += 1

    extra_state = copy.deepcopy(state)
    task = next(
        record for record in extra_state["records"]
        if record["object_type"] == "task-node" and record["id"] == task_ref["id"]
    )
    dependency = {
        "object_type": "task-node", "id": "TASK-M1A-DEP", "version": 1,
        "content_digest": "22" * 32,
        "payload": {"plan_ref": task["payload"]["plan_ref"]},
    }
    task["payload"]["dependency_node_ids"] = ["TASK-M1A-DEP"]
    extra_state["records"].append(dependency)
    extra_state["object_states"].append({
        "object_ref": {
            "object_type": "task-node", "id": "TASK-M1A-DEP", "version": 1,
            "content_digest": "22" * 32, "extra": 1,
        },
        "state": "verified",
    })
    _same(reference_admit, validate_admission, (extra_state, command, authorities, policies))
    checked += 1

    tuple_state = copy.deepcopy(state)
    tuple_state["records"] = tuple(tuple_state["records"])
    _same(reference_admit, validate_admission, (tuple_state, command, authorities, tuple(policies)))
    checked += 1

    # Direct lookup: absent content_digest matches the first id, later digest does not.
    records = [
        {"object_type": "task-node", "id": "TASK-M1A-A", "version": 1, "content_digest": "aa" * 32},
        {"object_type": "task-node", "id": "TASK-M1A-A", "version": 2, "content_digest": "bb" * 32},
        {"object_type": "goal-spec", "id": "TASK-M1A-A", "version": 1, "content_digest": "cc" * 32},
    ]
    lookup_state = {"records": records, "object_states": []}
    bare_ref = {"id": "TASK-M1A-A"}
    digest_ref = {"id": "TASK-M1A-A", "version": 2, "content_digest": "bb" * 32}
    view = _AdmissionView(lookup_state, [])
    assert view.record_index() is not None
    for ref, object_type in ((bare_ref, "task-node"), (digest_ref, "task-node"), (bare_ref, None)):
        assert _find_record(lookup_state, ref, object_type, view) == reference_find(
            lookup_state, ref, object_type
        )
        checked += 1
    target = records[1]
    assert _object_state(lookup_state, target, view) == reference_state(lookup_state, target)
    checked += 1
    return checked


def _policy_edges() -> int:
    case = _first_case("apply-pause")
    state, command = _replay_until(case, "apply-pause")
    policies = case["policy_fixtures"]
    target = policies[0]
    decoy = copy.deepcopy(target)
    decoy["allowed_roles"] = list(decoy["allowed_roles"]) + ["decoy-role"]
    reordered = [decoy] + list(policies)
    _same(
        reference_admit, validate_admission,
        (state, command, case["authority_fixtures"], reordered),
    )
    ref = {
        "policy_id": target["policy_id"], "policy_version": target["policy_version"],
        "content_digest": target["content_digest"],
    }
    view = _AdmissionView(state, reordered)
    for _ in range(6):
        assert _policy(reordered, ref, view=view) == reference_policy(reordered, ref)
    assert view.policy_index() is not None
    cached = len(view._digests)
    assert cached == 2
    assert _policy(reordered, ref, view=view) == reference_policy(reordered, ref)
    assert len(view._digests) == cached

    broken = copy.deepcopy(target)
    broken["allowed_roles"] = {"not-a-list"}

    def indexed_policy(fixtures, policy_ref):
        return _policy(fixtures, policy_ref, view=_AdmissionView({"records": []}, fixtures))

    _same(reference_policy, indexed_policy, ([broken], ref))
    _same(reference_policy, indexed_policy, ([broken, target], {
        "policy_id": "POLICY-M1A-ABSENT", "policy_version": 1, "content_digest": "ab" * 32,
    }))
    return 3


def _checkpoint_edges() -> int:
    case = _first_case("record-checkpoint")
    state, command = _replay_until(case, "record-checkpoint")
    state = copy.deepcopy(state)
    state["records"] = list(state["records"]) + [
        {
            "object_type": "work-lease", "id": "LEASE-M1A-SAME", "version": 1,
            "content_digest": "aa" * 32, "payload": {"lease_epoch": 1},
        },
        {
            "object_type": "executive-lease", "id": "LEASE-M1A-SAME", "version": 1,
            "content_digest": "bb" * 32, "payload": {"lease_epoch": 1},
        },
        {
            "object_type": "work-lease", "id": "LEASE-M1A-SAME", "version": 2,
            "content_digest": "cc" * 32, "payload": {"lease_epoch": 1},
        },
    ]
    _same(
        reference_admit, validate_admission,
        (state, command, case["authority_fixtures"], case["policy_fixtures"]),
    )
    view = _AdmissionView(state, case["policy_fixtures"])
    assert view.record_index() is not None
    assert reference_checkpoint(state) == _checkpoint_payload(state, view)
    assert canonical_bytes(reference_checkpoint(state)) == canonical_bytes(
        _checkpoint_payload(state, view)
    )
    return 1


def main() -> int:
    fixtures = _fixture_cases()
    graphs = _adversarial_graphs()
    wide = _wide_dag(256, True) + _wide_dag(256, False)
    scaled = _scaled_plan()
    records = _record_edges()
    policies = _policy_edges()
    checkpoints = _checkpoint_edges()
    total = fixtures + graphs + wide + scaled + records + policies + checkpoints
    print(
        "M1A_ADMISSION_EQUIV_PASS "
        f"fixtures={fixtures} graphs={graphs} wide={wide} scaled={scaled} "
        f"records={records} policies={policies} checkpoints={checkpoints} total={total}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as error:
        print(f"M1A_ADMISSION_EQUIV_FAIL: {error}", file=sys.stderr)
        raise SystemExit(1) from error
