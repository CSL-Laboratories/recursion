"""Frozen linear admission scans from before CSL-337.

The equivalence test executes this module and compares its output, byte for
byte, with scripts.v3.planning.validation.contracts. Production code must not
import it.
"""

from __future__ import annotations

import copy

from scripts.v3.planning.validation.canonical import (
    M1AError, digest, seal, validate_authority_attestation,
    validate_command_envelope, validate_policy_fixture,
)


PARENT_MUTATIONS = {"accept-goal-spec", "amend-goal-spec", "propose-plan-revision", "propose-replan"}
PAUSE_SENSITIVE = {
    "start-arm-run", "propose-plan-revision", "propose-replan", "propose-retry",
    "derive-work-packet", "reserve-budget", "replace-route", "replace-work-lease",
    "replace-executive-lease", "submit-worker-result", "record-effect-observation",
}
COMMAND_ROLES = {
    "record-owner-intent": "owner", "accept-goal-spec": "owner", "amend-goal-spec": "owner",
    "record-owner-verifier-exception": "owner", "apply-pause": "owner",
    "start-arm-run": "head", "replace-work-lease": "head",
    "replace-executive-lease": "head", "replace-route": "head",
    "propose-plan-revision": "arm", "derive-work-packet": "arm", "reserve-budget": "arm",
    "propose-retry": "arm", "propose-replan": "arm", "raise-blocker": "arm",
    "raise-escalation": "arm", "submit-worker-result": "worker",
    "submit-gate-result": "verifier", "submit-plan-verifier-decision": "verifier",
    "record-capacity-observation": "kernel-fixture-gate", "record-checkpoint": "kernel-fixture-gate",
    "record-effect-observation": "kernel-fixture-gate",
}
BUDGET_FIELDS = (
    "attempts", "cpu_ms", "memory_bytes", "model_turns", "money_minor", "output_bytes", "tokens",
)
LEASE_COMMAND_REPORTING_EXEMPT = {"raise-blocker", "raise-escalation"}


def _find_record(state: dict, ref: dict | None, object_type: str | None = None) -> dict | None:
    if ref is None:
        return None
    expected_id = ref.get("id", ref.get("lease_id"))
    for record in state["records"]:
        if object_type and record["object_type"] != object_type:
            continue
        if record["id"] != expected_id:
            continue
        if "version" in ref and record["version"] != ref["version"]:
            continue
        if "content_digest" not in ref or record["content_digest"] == ref["content_digest"]:
            return record
    return None


def _object_state(state: dict, record: dict) -> str | None:
    target = {
        "object_type": record["object_type"], "id": record["id"], "version": record["version"],
        "content_digest": record["content_digest"],
    }
    return next((item["state"] for item in state["object_states"] if item["object_ref"] == target), None)


def _policy(policy_fixtures: list[dict], ref: dict | None, kind: str | None = None) -> dict | None:
    if ref is None:
        return None
    return next((p for p in policy_fixtures
                 if p["policy_id"] == ref.get("policy_id")
                 and p["policy_version"] == ref.get("policy_version")
                 and p["content_digest"] == ref.get("content_digest")
                 and digest(p, omit_content_digest=True) == p["content_digest"]
                 and (kind is None or p["policy_kind"] == kind)), None)


def _candidate_plan(command: dict) -> tuple[dict, list[dict]] | tuple[None, list]:
    payload = command["payload"]
    if command["command_type"] == "propose-plan-revision":
        return payload["plan_revision"], payload["task_nodes"]
    if command["command_type"] == "propose-replan":
        return payload["new_plan_revision"], payload["task_nodes"]
    if command["command_type"] == "amend-goal-spec":
        return payload["replacement_plan_revision"], payload["task_nodes"]
    return None, []


def _witness(code: str, *, object_ids=(), edges=(), fields=()) -> dict:
    return seal({
        "witness_schema_version": "m1a-error-witness-1", "error_code": code,
        "object_ids": sorted(object_ids),
        "dependency_edges": sorted(copy.deepcopy(list(edges)),
                                   key=lambda e: (e["from_node_id"], e["to_node_id"])),
        "field_paths": sorted(fields),
    })


def _path_overlaps(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def validate_dag(plan: dict, tasks: list[dict]) -> tuple[str | None, dict | None]:
    """Validate exact graph membership, acyclicity, and unordered ownership conflicts."""

    node_ids = {task["id"] for task in tasks}
    expected_refs = sorted(
        [{"object_type": t["object_type"], "id": t["id"], "version": t["version"],
          "content_digest": t["content_digest"]} for t in tasks], key=lambda item: item["id"]
    )
    if plan["payload"]["task_node_refs"] != expected_refs:
        missing = sorted({r["id"] for r in plan["payload"]["task_node_refs"]} ^ node_ids)
        return "E-DAG-DEPENDENCY", _witness("E-DAG-DEPENDENCY", object_ids=missing)
    plan_ref = {"object_type": plan["object_type"], "id": plan["id"],
                "version": plan["version"]}
    for task in tasks:
        if task["payload"]["plan_ref"] != plan_ref:
            return "E-DAG-DEPENDENCY", _witness("E-DAG-DEPENDENCY", object_ids=[task["id"]])
    edges = plan["payload"]["dependency_edges"]
    declared_edges = {(edge["from_node_id"], edge["to_node_id"]) for edge in edges}
    node_edges = {(dependency, task["id"]) for task in tasks
                  for dependency in task["payload"]["dependency_node_ids"]}
    self_nodes = sorted({left for left, right in node_edges | declared_edges if left == right})
    if self_nodes:
        return "E-DAG-DEPENDENCY", _witness(
            "E-DAG-DEPENDENCY", object_ids=self_nodes,
            edges=[{"from_node_id": node, "to_node_id": node} for node in self_nodes],
        )
    missing = sorted({endpoint for edge in edges
                      for endpoint in (edge["from_node_id"], edge["to_node_id"])
                      if endpoint not in node_ids})
    for task in tasks:
        missing.extend(dep for dep in task["payload"]["dependency_node_ids"] if dep not in node_ids)
    if missing:
        return "E-DAG-DEPENDENCY", _witness("E-DAG-DEPENDENCY", object_ids=sorted(set(missing)))
    if declared_edges != node_edges:
        differing = declared_edges ^ node_edges
        return "E-DAG-DEPENDENCY", _witness(
            "E-DAG-DEPENDENCY",
            object_ids=sorted({node for edge in differing for node in edge}),
            edges=[{"from_node_id": left, "to_node_id": right}
                   for left, right in sorted(differing)],
        )
    graph = {node: [] for node in node_ids}
    for edge in edges:
        graph[edge["from_node_id"]].append(edge["to_node_id"])
    visiting, visited = set(), set()
    cycle_nodes: set[str] = set()

    def visit(node: str, trail: list[str]) -> bool:
        if node in visiting:
            cycle_nodes.update(trail[trail.index(node):])
            return True
        if node in visited:
            return False
        visiting.add(node)
        for child in sorted(graph[node]):
            if visit(child, [*trail, child]):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    for node in sorted(node_ids):
        if visit(node, [node]):
            return "E-DAG-CYCLE", _witness("E-DAG-CYCLE", object_ids=cycle_nodes, edges=edges)

    # Transitive closure: (source, node) for every node reachable from source.
    reach: set[tuple[str, str]] = set()
    for source in node_ids:
        seen: set[str] = set()
        stack = list(graph[source])
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            reach.add((source, node))
            stack.extend(graph[node])
    ordered = lambda a, b: (a, b) in reach or (b, a) in reach
    for index, left in enumerate(sorted(tasks, key=lambda t: t["id"])):
        for right in sorted(tasks, key=lambda t: t["id"])[index + 1:]:
            if ordered(left["id"], right["id"]):
                continue
            lp, rp = left["payload"], right["payload"]
            conflict = None
            if any(_path_overlaps(a, b) for a in lp["write_resource_paths"] for b in rp["write_resource_paths"]):
                conflict = "write-write"
            elif (any(_path_overlaps(a, b) for a in lp["read_resource_paths"] for b in rp["write_resource_paths"])
                  or any(_path_overlaps(a, b) for a in lp["write_resource_paths"] for b in rp["read_resource_paths"])):
                conflict = "read-write"
            elif set(lp["effect_keys"]) & set(rp["effect_keys"]):
                conflict = "same-effect"
            if conflict:
                return "E-DAG-CONFLICT", _witness(
                    "E-DAG-CONFLICT", object_ids=[left["id"], right["id"]], fields=[conflict]
                )
    return None, None


def _packet_binding_operand(packet: dict) -> dict:
    payload = packet["payload"]
    return {
        field: copy.deepcopy(payload[field])
        for field in (
            "goal_ref", "plan_ref", "task_ref", "authority_policy_ref",
            "privacy_policy_ref", "egress_policy_ref", "route_assignment_ref",
            "budget_reservation_ref", "verifier_policy_ref", "constitution_policy_ref",
        )
    }


def _object_ref(record: dict) -> dict:
    return {
        "object_type": record["object_type"], "id": record["id"],
        "version": record["version"], "content_digest": record["content_digest"],
    }


def _lease_ref(record: dict) -> dict:
    return {
        "lease_id": record["id"], "lease_epoch": record["payload"]["lease_epoch"],
        "content_digest": record["content_digest"],
    }


def _checkpoint_payload(state: dict) -> dict:
    refs = lambda kind: sorted(
        [_object_ref(record) for record in state["records"] if record["object_type"] == kind],
        key=lambda ref: ref["id"],
    )
    leases = sorted(
        [_lease_ref(record) for record in state["records"]
         if record["object_type"] in {"executive-lease", "work-lease"}],
        key=lambda ref: ref["lease_id"],
    )
    return {
        "state_version": state["state_version"], "state_digest": state["content_digest"],
        "active_goal_ref": state["operative_goal_ref"],
        "active_plan_ref": state["active_plan_ref"],
        "open_packet_refs": refs("work-packet"),
        "ambiguous_effect_refs": state["ambiguous_effect_refs"],
        "budget_snapshot_digest": digest(refs("budget-reservation")),
        "route_snapshot_digest": digest(refs("route-assignment")),
        "pause_epoch": state["current_pause_epoch"],
        "lease_epoch_digest": digest(leases),
    }


def _dependencies_satisfied(state: dict, task: dict) -> bool:
    plan = task["payload"]["plan_ref"]
    for dependency_id in task["payload"]["dependency_node_ids"]:
        dependency = next(
            (record for record in state["records"]
             if record["object_type"] == "task-node"
             and record["id"] == dependency_id
             and record["payload"]["plan_ref"] == plan),
            None,
        )
        if dependency is None or _object_state(state, dependency) not in {"verified", "complete"}:
            return False
    return True


def _validate_scope(goal: dict, plan: dict, tasks: list[dict]) -> str | None:
    gp = goal["payload"]
    candidates = [plan["payload"], *(task["payload"] for task in tasks)]
    for candidate in candidates:
        limits = candidate.get("budget_limits", gp["budget_limits"])
        if any(limits[field] > gp["budget_limits"][field] for field in BUDGET_FIELDS):
            return "E-BUDGET-WIDEN"
    for item in candidates:
        subset_pairs = (
            ("authority_ids", "authority_ids"), ("capability_ids", "capability_ids"),
            ("allowed_action_ids", "allowed_action_ids"), ("route_ids", "allowed_route_ids"),
            ("account_class_ids", "allowed_account_class_ids"),
            ("capacity_pool_ids", "allowed_capacity_pool_ids"),
            ("effect_keys", "allowed_effect_keys"),
        )
        for child, parent in subset_pairs:
            if child in item and not set(item[child]).issubset(gp[parent]):
                return "E-SCOPE-WIDEN"
        for field in ("read_resource_paths", "write_resource_paths"):
            if field in item and any(not any(_path_overlaps(path, root) and
                                             (path == root or path.startswith(root + "/"))
                                             for root in gp["allowed_resource_paths"])
                                      for path in item[field]):
                return "E-SCOPE-WIDEN"
        if item.get("privacy_class_id") not in (None, gp["privacy_class_id"]):
            return "E-SCOPE-WIDEN"
        if item.get("egress_class_id") not in (None, gp["egress_class_id"]):
            return "E-SCOPE-WIDEN"
    return None


def _validate_policy_bindings(goal: dict, plan: dict, tasks: list[dict],
                              policy_fixtures: list[dict]) -> bool:
    gp = goal["payload"]
    required_goal = {
        "authority_policy_ref": "authority", "privacy_policy_ref": "privacy-egress",
        "egress_policy_ref": "privacy-egress", "route_policy_ref": "route-envelope",
        "novelty_policy_ref": "novelty", "retry_policy_ref": "retry",
        "verifier_policy_ref": "verifier", "constitution_policy_ref": "constitution",
        "pause_policy_ref": "pause",
    }
    if any(_policy(policy_fixtures, gp[field], kind) is None
           for field, kind in required_goal.items()):
        return False
    if (plan["payload"]["verifier_policy_ref"] != gp["verifier_policy_ref"]
            or plan["payload"]["constitution_policy_ref"] != gp["constitution_policy_ref"]):
        return False
    task_fields = (
        "authority_policy_ref", "privacy_policy_ref", "egress_policy_ref", "retry_policy_ref",
        "verifier_policy_ref", "constitution_policy_ref",
    )
    return all(
        task["payload"][field] == gp[field]
        and _policy(policy_fixtures, task["payload"][field]) is not None
        for task in tasks for field in task_fields
    )


def _required_amendment_targets(state: dict) -> set[tuple[str, str, int]]:
    active_types = {"plan-revision", "task-node", "work-packet", "work-lease", "executive-lease", "effect-observation"}
    terminal = {"complete", "terminal-verified", "terminal-verified-downgraded"}
    targets = set()
    for item in state["object_states"]:
        ref = item["object_ref"]
        if ref["object_type"] in active_types and item["state"] not in terminal:
            targets.add((ref["object_type"], ref["id"], ref["version"]))
    return targets


def _validate_verifier(state: dict, command: dict) -> str | None:
    decision = command["payload"]
    plan = _find_record(state, decision["payload"]["plan_ref"], "plan-revision")
    if plan is None:
        return "E-STALE-PLAN"
    task_refs = plan["payload"]["task_node_refs"]
    passing = [r for r in state["records"] if r["object_type"] == "gate-result"
               and r["payload"]["plan_ref"] == decision["payload"]["plan_ref"]
               and r["payload"]["disposition"] == "pass"]
    passed_tasks = {r["payload"]["task_ref"]["id"] for r in passing}
    if any(ref["id"] not in passed_tasks for ref in task_refs):
        return "E-GATE-MISSING"
    assigned = plan["payload"]["assigned_plan_verifier_actor_id"]
    if assigned != command["actor_id"]:
        return "E-VERIFIER-MISSING"
    return None


def validate_admission(state: dict, command: dict, authority_fixtures: list[dict],
                       policy_fixtures: list[dict]) -> tuple[str | None, dict | None]:
    """Return the single deterministic primary error and optional canonical witness."""

    try:
        validate_command_envelope(command)
    except M1AError as error:
        return error.code, None
    try:
        for authority_fixture in authority_fixtures:
            validate_authority_attestation(authority_fixture)
        for policy_fixture in policy_fixtures:
            validate_policy_fixture(policy_fixture)
    except M1AError as error:
        return error.code, None
    if digest(command, omit_content_digest=True) != command.get("content_digest"):
        return "E-DIGEST-MISMATCH", None
    if digest(command["payload"]) != command.get("payload_digest"):
        return "E-DIGEST-MISMATCH", None
    if command["candidate_digest"] != command["payload_digest"]:
        return "E-DIGEST-MISMATCH", None
    authority = next((a for a in authority_fixtures
                      if a["attestation_id"] == command["auth_ref"]), None)
    if authority is None:
        return "E-AUTH-MISSING", None
    if (digest(authority, omit_content_digest=True) != authority["content_digest"]
            or authority["candidate_digest"] != command["candidate_digest"]):
        return "E-AUTH-MISSING", None
    if (authority["actor_id"] != command["actor_id"]
            or command["command_type"] not in authority["allowed_command_types"]
            or not authority["valid_from_logical_time"] <= command["logical_time"] <= authority["expires_logical_time"]):
        return "E-AUTH-ROLE", None
    role = authority["actor_role"]
    if command["command_type"] in PARENT_MUTATIONS and role in ("worker", "verifier"):
        return "E-AUTH-PARENT-MUTATION", None
    if COMMAND_ROLES.get(command["command_type"]) != role:
        return "E-AUTH-ROLE", None
    if command["expected_state_version"] != state["state_version"]:
        return "E-DIGEST-MISMATCH", None

    if command["goal_ref"] is not None and command["goal_ref"] != state["operative_goal_ref"]:
        return "E-STALE-GOAL", None
    if command["plan_ref"] is not None and command["plan_ref"] != state["active_plan_ref"]:
        return "E-STALE-PLAN", None
    if command["lease_ref"] is not None:
        lease = _find_record(state, command["lease_ref"])
        if (lease is None or _object_state(state, lease) != "active"
                or lease["payload"]["lease_epoch"] != command["lease_ref"]["lease_epoch"]
                or lease["payload"]["subject_actor_id"] != command["actor_id"]
                or (command["command_type"] not in LEASE_COMMAND_REPORTING_EXEMPT
                    and command["command_type"] not in lease["payload"]["allowed_command_types"])
                or not lease["payload"]["issued_logical_time"] <= command["logical_time"]
                < lease["payload"]["expires_logical_time"]
                or (lease["payload"]["goal_ref"] is not None
                    and lease["payload"]["goal_ref"] != command["goal_ref"])
                or (lease["payload"]["plan_ref"] is not None
                    and lease["payload"]["plan_ref"] != command["plan_ref"])):
            return "E-STALE-LEASE", None

    if state["current_pause_epoch"] > 0 and command["command_type"] in PAUSE_SENSITIVE:
        pause_policy = _policy(policy_fixtures, state["current_pause_policy_ref"], "pause")
        if pause_policy is None or pause_policy["operative_pause_mode"] == "current-immediate":
            return "E-PAUSED", None
    if command["command_type"] == "apply-pause":
        directive = command["payload"]["payload"]
        pause_policy = _policy(policy_fixtures, directive["pause_policy_ref"], "pause")
        if pause_policy is None or directive["mode"] != pause_policy["operative_pause_mode"]:
            return "E-POLICY-UNKNOWN", None

    plan, tasks = _candidate_plan(command)
    if plan is not None:
        goal = (command["payload"]["new_goal_spec"]
                if command["command_type"] == "amend-goal-spec"
                else _find_record(state, command["goal_ref"] or plan["payload"]["goal_ref"], "goal-spec"))
        if goal is None or plan["payload"]["goal_ref"] != {
            "object_type": goal["object_type"], "id": goal["id"],
            "version": goal["version"], "content_digest": goal["content_digest"],
        }:
            return "E-STALE-GOAL", None
        if not _validate_policy_bindings(goal, plan, tasks, policy_fixtures):
            return "E-POLICY-UNKNOWN", None
        error = _validate_scope(goal, plan, tasks)
        if error:
            return error, None
        graph_operand = {
            "task_node_refs": plan["payload"]["task_node_refs"],
            "dependency_edges": plan["payload"]["dependency_edges"],
        }
        if plan["payload"]["graph_digest"] != digest(graph_operand):
            return "E-DIGEST-MISMATCH", None
        error, witness = validate_dag(plan, tasks)
        if error:
            return error, witness

    if command["command_type"] == "derive-work-packet":
        task = _find_record(state, command["payload"]["task_ref"], "task-node")
        if (task is None or command["plan_ref"] is None
                or task["payload"]["plan_ref"]["id"] != command["plan_ref"]["id"]
                or task["payload"]["plan_ref"]["version"] != command["plan_ref"]["version"]
                or not _dependencies_satisfied(state, task)):
            return "E-DAG-DEPENDENCY", None

    if (command["command_type"] == "record-checkpoint"
            and command["payload"]["payload"] != _checkpoint_payload(state)):
        return "E-DIGEST-MISMATCH", None

    if command["command_type"] == "propose-retry":
        retry = command["payload"]["payload"]
        prior_packet = _find_record(state, retry["prior_work_packet_ref"], "work-packet")
        if prior_packet is None or retry["next_attempt"] != prior_packet["payload"]["attempt"] + 1:
            return "E-RETRY-EXHAUSTED", None
        goal = _find_record(state, retry["goal_ref"], "goal-spec")
        task = _find_record(state, retry["task_ref"], "task-node")
        retry_policy = (_policy(policy_fixtures, task["payload"]["retry_policy_ref"], "retry")
                        if task is not None else None)
        if (goal is None or task is None or retry_policy is None
                or task["payload"]["retry_policy_ref"] != goal["payload"]["retry_policy_ref"]):
            return "E-POLICY-UNKNOWN", None
        ceiling = min(
            retry_policy["retry_maximum"], task["payload"]["retry_maximum"],
            goal["payload"]["budget_limits"]["attempts"],
            task["payload"]["budget_limits"]["attempts"],
        )
        if retry["retry_maximum"] != ceiling:
            return "E-RETRY-EXHAUSTED", None
        if (retry["next_attempt"] > retry["retry_maximum"] and prior_packet is not None
                and _object_state(state, prior_packet) == "failed"):
            return "E-RETRY-EXHAUSTED", None
        if retry["novelty_delta_digest"] in state["consumed_novelty_delta_digests"]:
            return "E-RETRY-NONNOVEL", None
        if retry["next_attempt"] > retry["retry_maximum"]:
            return "E-RETRY-EXHAUSTED", None
        if state["ambiguous_effect_refs"]:
            return "E-EFFECT-AMBIGUOUS", None
        gate = _find_record(state, retry["failed_gate_result_ref"], "gate-result")
        novelty = copy.deepcopy(retry["novelty_delta"])
        novelty_digest = novelty.pop("delta_digest", None)
        canonical_diff_digest = digest(novelty.get("canonical_diff", []))
        if (retry["goal_ref"] != prior_packet["payload"]["goal_ref"]
                or retry["plan_ref"] != prior_packet["payload"]["plan_ref"]
                or retry["task_ref"] != prior_packet["payload"]["task_ref"]
                or retry["unchanged_binding_digest"] != digest(_packet_binding_operand(prior_packet))
                or retry["novelty_delta_digest"] != retry["novelty_delta"].get("delta_digest")
                or novelty_digest != digest(novelty)
                or retry["novelty_delta"].get("canonical_diff_digest") != canonical_diff_digest
                or gate is None
                or gate["payload"]["goal_ref"] != retry["goal_ref"]
                or gate["payload"]["plan_ref"] != retry["plan_ref"]
                or gate["payload"]["task_ref"] != retry["task_ref"]
                or gate["payload"]["work_packet_ref"] != retry["prior_work_packet_ref"]
                or gate["payload"]["disposition"] != "fail"):
            return "E-DIGEST-MISMATCH", None
        reservation = _find_record(
            state, prior_packet["payload"]["budget_reservation_ref"], "budget-reservation"
        )
        if reservation is None:
            return "E-DIGEST-MISMATCH", None
        expected_remaining = copy.deepcopy(reservation["payload"]["remaining"])
        expected_remaining["attempts"] = ceiling - retry["next_attempt"]
        if retry["remaining_budget"] != expected_remaining or retry["disposition"] != "admitted":
            return "E-DIGEST-MISMATCH", None
    if command["command_type"] == "replace-route":
        route = command["payload"]["new_route"]["payload"]
        goal = _find_record(state, command["goal_ref"], "goal-spec")
        if (route["metered_spend_allowed"] or goal is None
                or route["route_id"] not in goal["payload"]["allowed_route_ids"]):
            return "E-ROUTE-SPEND", None
    if command["command_type"] == "submit-plan-verifier-decision":
        error = _validate_verifier(state, command)
        if error:
            return error, None
    if command["command_type"] == "amend-goal-spec":
        amendment = command["payload"]["amendment"]["payload"]["dispositions"]
        plan_dispositions = command["payload"]["replacement_plan_revision"]["payload"]["dispositions"]
        transition = command["payload"]["plan_transition"]["payload"]["dispositions"]
        if not amendment or amendment != plan_dispositions or amendment != transition:
            return "E-AMENDMENT-INCOMPLETE", None
        dispositions = amendment
        covered = {(d["object_ref"]["object_type"], d["object_ref"]["id"], d["object_ref"]["version"])
                   for d in dispositions}
        if _required_amendment_targets(state) - covered:
            return "E-AMENDMENT-INCOMPLETE", None
    if command["command_type"] == "propose-replan":
        decision = command["payload"]["decision"]["payload"]["dispositions"]
        plan_dispositions = command["payload"]["new_plan_revision"]["payload"]["dispositions"]
        transition = command["payload"]["transition"]["payload"]["dispositions"]
        if not decision or decision != plan_dispositions or decision != transition:
            return "E-AMENDMENT-INCOMPLETE", None
    return None, None
