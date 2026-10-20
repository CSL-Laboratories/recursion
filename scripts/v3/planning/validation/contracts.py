"""Cross-record and admission validation for the closed M1A planning contract."""

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
_LEASE_TYPES = frozenset({"executive-lease", "work-lease"})
_OBJECT_REF_FIELDS = ("object_type", "id", "version", "content_digest")
_OBJECT_REF_FIELD_SET = set(_OBJECT_REF_FIELDS)
# Sentinel: the index cannot answer this lookup, so the linear scan must run.
_FALLBACK = object()


class _RecordIndex:
    """Records grouped once, in encounter order, for first-match lookups."""

    __slots__ = (
        "by_type", "by_id", "by_type_id", "by_type_id_version", "by_id_version", "leases",
    )

    def __init__(self, records: list) -> None:
        by_type: dict = {}
        by_id: dict = {}
        by_type_id: dict = {}
        by_type_id_version: dict = {}
        by_id_version: dict = {}
        leases = []
        for record in records:
            object_type = record["object_type"]
            record_id = record["id"]
            version = record["version"]
            by_type.setdefault(object_type, []).append(record)
            by_id.setdefault(record_id, []).append(record)
            by_type_id.setdefault((object_type, record_id), []).append(record)
            by_type_id_version.setdefault((object_type, record_id, version), []).append(record)
            by_id_version.setdefault((record_id, version), []).append(record)
            if object_type in _LEASE_TYPES:
                leases.append(record)
        self.by_type = by_type
        self.by_id = by_id
        self.by_type_id = by_type_id
        self.by_type_id_version = by_type_id_version
        self.by_id_version = by_id_version
        self.leases = leases

    def find(self, ref: dict, object_type: str | None):
        expected_id = ref.get("id", ref.get("lease_id"))
        versioned = "version" in ref
        try:
            if object_type:
                if versioned:
                    candidates = self.by_type_id_version.get(
                        (object_type, expected_id, ref["version"]), ()
                    )
                    versioned = False
                else:
                    candidates = self.by_type_id.get((object_type, expected_id), ())
            elif versioned:
                candidates = self.by_id_version.get((expected_id, ref["version"]), ())
                versioned = False
            else:
                candidates = self.by_id.get(expected_id, ())
        except TypeError:
            return _FALLBACK
        check_digest = "content_digest" in ref
        digest_value = ref["content_digest"] if check_digest else None
        for record in candidates:
            if versioned and record["version"] != ref["version"]:
                continue
            if not check_digest or record["content_digest"] == digest_value:
                return record
        return None


class _AdmissionView:
    """Indexes and digest memo built once per validate_admission call."""

    __slots__ = (
        "state", "policy_fixtures", "_records", "_records_ready", "_policies",
        "_policies_ready", "_states", "_states_ready", "_digests",
    )

    def __init__(self, state: dict, policy_fixtures: list) -> None:
        self.state = state
        self.policy_fixtures = policy_fixtures
        self._records = None
        self._records_ready = False
        self._policies = None
        self._policies_ready = False
        self._states = None
        self._states_ready = False
        self._digests = {}

    def record_index(self):
        if not self._records_ready:
            self._records_ready = True
            try:
                records = self.state["records"]
            except (TypeError, KeyError):
                records = None
            if isinstance(records, (list, tuple)):
                try:
                    self._records = _RecordIndex(records)
                except (TypeError, KeyError):
                    self._records = None
        return self._records

    def policy_index(self):
        if not self._policies_ready:
            self._policies_ready = True
            policies = self.policy_fixtures
            if isinstance(policies, (list, tuple)):
                try:
                    buckets: dict = {}
                    for policy in policies:
                        key = (
                            policy["policy_id"], policy["policy_version"],
                            policy["content_digest"],
                        )
                        buckets.setdefault(key, []).append(policy)
                    self._policies = buckets
                except (TypeError, KeyError):
                    self._policies = None
        return self._policies

    def state_index(self):
        if not self._states_ready:
            self._states_ready = True
            try:
                items = self.state["object_states"]
            except (TypeError, KeyError):
                items = None
            if isinstance(items, (list, tuple)):
                try:
                    found: dict = {}
                    for item in items:
                        ref = item["object_ref"]
                        if set(ref) != _OBJECT_REF_FIELD_SET:
                            raise TypeError
                        key = tuple(ref[field] for field in _OBJECT_REF_FIELDS)
                        if key not in found:
                            found[key] = item["state"]
                    self._states = found
                except (TypeError, KeyError):
                    self._states = None
        return self._states

    def policy_digest(self, policy: dict) -> str:
        key = id(policy)
        try:
            return self._digests[key]
        except KeyError:
            value = digest(policy, omit_content_digest=True)
            self._digests[key] = value
            return value


def _find_record(state: dict, ref: dict | None, object_type: str | None = None,
                 view: _AdmissionView | None = None) -> dict | None:
    if ref is None:
        return None
    if view is not None:
        index = view.record_index()
        if index is not None:
            found = index.find(ref, object_type)
            if found is not _FALLBACK:
                return found
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


def _object_state(state: dict, record: dict, view: _AdmissionView | None = None) -> str | None:
    target = {
        "object_type": record["object_type"], "id": record["id"], "version": record["version"],
        "content_digest": record["content_digest"],
    }
    if view is not None:
        index = view.state_index()
        if index is not None:
            try:
                return index.get(tuple(target[field] for field in _OBJECT_REF_FIELDS))
            except TypeError:
                pass
    return next((item["state"] for item in state["object_states"] if item["object_ref"] == target), None)


def _policy_digest(policy: dict, view: _AdmissionView | None) -> str:
    if view is None:
        return digest(policy, omit_content_digest=True)
    return view.policy_digest(policy)


def _match_policy(candidates, ref: dict | None, kind: str | None, view: _AdmissionView | None,
                  filtered: bool) -> dict | None:
    for policy in candidates:
        if not filtered and (
            policy["policy_id"] != ref.get("policy_id")
            or policy["policy_version"] != ref.get("policy_version")
            or policy["content_digest"] != ref.get("content_digest")
        ):
            continue
        if _policy_digest(policy, view) != policy["content_digest"]:
            continue
        if kind is not None and policy["policy_kind"] != kind:
            continue
        return policy
    return None


def _policy(policy_fixtures: list[dict], ref: dict | None, kind: str | None = None,
            view: _AdmissionView | None = None) -> dict | None:
    if ref is None:
        return None
    candidates = None
    if view is not None:
        index = view.policy_index()
        if index is not None:
            try:
                key = (ref.get("policy_id"), ref.get("policy_version"), ref.get("content_digest"))
                candidates = index.get(key, ())
            except TypeError:
                candidates = None
    if candidates is None:
        return _match_policy(policy_fixtures, ref, kind, view, False)
    return _match_policy(candidates, ref, kind, view, True)


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
    ordered_tasks = sorted(tasks, key=lambda t: t["id"])
    task_count = len(ordered_tasks)
    for index in range(task_count):
        left = ordered_tasks[index]
        for right_index in range(index + 1, task_count):
            right = ordered_tasks[right_index]
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


def _checkpoint_payload(state: dict, view: _AdmissionView | None = None) -> dict:
    index = view.record_index() if view is not None else None
    if index is None:
        def refs(kind):
            return sorted(
                [_object_ref(record) for record in state["records"] if record["object_type"] == kind],
                key=lambda ref: ref["id"],
            )
        lease_records = [
            record for record in state["records"] if record["object_type"] in _LEASE_TYPES
        ]
    else:
        def refs(kind):
            return sorted(
                (_object_ref(record) for record in index.by_type.get(kind, ())),
                key=lambda ref: ref["id"],
            )
        lease_records = index.leases
    leases = sorted(
        (_lease_ref(record) for record in lease_records), key=lambda ref: ref["lease_id"],
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


def _dependencies_satisfied(state: dict, task: dict, view: _AdmissionView | None = None) -> bool:
    plan = task["payload"]["plan_ref"]
    index = view.record_index() if view is not None else None
    for dependency_id in task["payload"]["dependency_node_ids"]:
        candidates = None
        if index is not None:
            try:
                candidates = index.by_type_id.get(("task-node", dependency_id), ())
            except TypeError:
                candidates = None
        if candidates is None:
            dependency = next(
                (record for record in state["records"]
                 if record["object_type"] == "task-node"
                 and record["id"] == dependency_id
                 and record["payload"]["plan_ref"] == plan),
                None,
            )
        else:
            dependency = next(
                (record for record in candidates if record["payload"]["plan_ref"] == plan),
                None,
            )
        if dependency is None or _object_state(state, dependency, view) not in {"verified", "complete"}:
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
                              policy_fixtures: list[dict],
                              view: _AdmissionView | None = None) -> bool:
    gp = goal["payload"]
    required_goal = {
        "authority_policy_ref": "authority", "privacy_policy_ref": "privacy-egress",
        "egress_policy_ref": "privacy-egress", "route_policy_ref": "route-envelope",
        "novelty_policy_ref": "novelty", "retry_policy_ref": "retry",
        "verifier_policy_ref": "verifier", "constitution_policy_ref": "constitution",
        "pause_policy_ref": "pause",
    }
    if any(_policy(policy_fixtures, gp[field], kind, view) is None
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
        and _policy(policy_fixtures, task["payload"][field], view=view) is not None
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


def _validate_verifier(state: dict, command: dict, view: _AdmissionView | None = None) -> str | None:
    decision = command["payload"]
    plan = _find_record(state, decision["payload"]["plan_ref"], "plan-revision", view)
    if plan is None:
        return "E-STALE-PLAN"
    task_refs = plan["payload"]["task_node_refs"]
    plan_ref = decision["payload"]["plan_ref"]
    index = view.record_index() if view is not None else None
    if index is not None:
        gate_records = index.by_type.get("gate-result", ())
    else:
        gate_records = (
            record for record in state["records"] if record["object_type"] == "gate-result"
        )
    passing = [r for r in gate_records
               if r["payload"]["plan_ref"] == plan_ref
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

    view = _AdmissionView(state, policy_fixtures)
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
        lease = _find_record(state, command["lease_ref"], view=view)
        if (lease is None or _object_state(state, lease, view) != "active"
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
        pause_policy = _policy(policy_fixtures, state["current_pause_policy_ref"], "pause", view)
        if pause_policy is None or pause_policy["operative_pause_mode"] == "current-immediate":
            return "E-PAUSED", None
    if command["command_type"] == "apply-pause":
        directive = command["payload"]["payload"]
        pause_policy = _policy(policy_fixtures, directive["pause_policy_ref"], "pause", view)
        if pause_policy is None or directive["mode"] != pause_policy["operative_pause_mode"]:
            return "E-POLICY-UNKNOWN", None

    plan, tasks = _candidate_plan(command)
    if plan is not None:
        goal = (command["payload"]["new_goal_spec"]
                if command["command_type"] == "amend-goal-spec"
                else _find_record(state, command["goal_ref"] or plan["payload"]["goal_ref"], "goal-spec", view))
        if goal is None or plan["payload"]["goal_ref"] != {
            "object_type": goal["object_type"], "id": goal["id"],
            "version": goal["version"], "content_digest": goal["content_digest"],
        }:
            return "E-STALE-GOAL", None
        if not _validate_policy_bindings(goal, plan, tasks, policy_fixtures, view):
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
        task = _find_record(state, command["payload"]["task_ref"], "task-node", view)
        if (task is None or command["plan_ref"] is None
                or task["payload"]["plan_ref"]["id"] != command["plan_ref"]["id"]
                or task["payload"]["plan_ref"]["version"] != command["plan_ref"]["version"]
                or not _dependencies_satisfied(state, task, view)):
            return "E-DAG-DEPENDENCY", None

    if (command["command_type"] == "record-checkpoint"
            and command["payload"]["payload"] != _checkpoint_payload(state, view)):
        return "E-DIGEST-MISMATCH", None

    if command["command_type"] == "propose-retry":
        retry = command["payload"]["payload"]
        prior_packet = _find_record(state, retry["prior_work_packet_ref"], "work-packet", view)
        if prior_packet is None or retry["next_attempt"] != prior_packet["payload"]["attempt"] + 1:
            return "E-RETRY-EXHAUSTED", None
        goal = _find_record(state, retry["goal_ref"], "goal-spec", view)
        task = _find_record(state, retry["task_ref"], "task-node", view)
        retry_policy = (_policy(policy_fixtures, task["payload"]["retry_policy_ref"], "retry", view)
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
                and _object_state(state, prior_packet, view) == "failed"):
            return "E-RETRY-EXHAUSTED", None
        if retry["novelty_delta_digest"] in state["consumed_novelty_delta_digests"]:
            return "E-RETRY-NONNOVEL", None
        if retry["next_attempt"] > retry["retry_maximum"]:
            return "E-RETRY-EXHAUSTED", None
        if state["ambiguous_effect_refs"]:
            return "E-EFFECT-AMBIGUOUS", None
        gate = _find_record(state, retry["failed_gate_result_ref"], "gate-result", view)
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
            state, prior_packet["payload"]["budget_reservation_ref"], "budget-reservation", view
        )
        if reservation is None:
            return "E-DIGEST-MISMATCH", None
        expected_remaining = copy.deepcopy(reservation["payload"]["remaining"])
        expected_remaining["attempts"] = ceiling - retry["next_attempt"]
        if retry["remaining_budget"] != expected_remaining or retry["disposition"] != "admitted":
            return "E-DIGEST-MISMATCH", None
    if command["command_type"] == "replace-route":
        route = command["payload"]["new_route"]["payload"]
        goal = _find_record(state, command["goal_ref"], "goal-spec", view)
        if (route["metered_spend_allowed"] or goal is None
                or route["route_id"] not in goal["payload"]["allowed_route_ids"]):
            return "E-ROUTE-SPEND", None
    if command["command_type"] == "submit-plan-verifier-decision":
        error = _validate_verifier(state, command, view)
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
