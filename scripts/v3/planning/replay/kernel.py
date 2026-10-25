"""Pure admission, reduction, packet derivation, and replay for M1A planning."""

from __future__ import annotations

import copy
import hashlib

from scripts.v3.planning.validation.canonical import (
    M1AError, canonical_bytes, decode, digest, seal, validate_command_envelope,
    validate_domain_record, validate_event_envelope, validate_genesis, validate_projection,
    validate_receipt,
)
from scripts.v3.planning.validation.contracts import validate_admission


GENESIS_DIGEST = "fb1168e6e4d1267c41ea3d7798ebb88963e6de7c05cc1500ed059ba4b2f75801"

_LIST_FIELDS = (
    "records", "object_states", "processed_command_receipts",
    "consumed_novelty_delta_digests", "open_blocker_refs",
    "open_escalation_refs", "ambiguous_effect_refs",
)
_RECORD_CANONICAL = {}
_OBJECT_STATE_CANONICAL = {}
_INDEX = None


def object_ref(record: dict) -> dict:
    return {
        "object_type": record["object_type"], "id": record["id"],
        "version": record["version"], "content_digest": record["content_digest"],
    }


def lease_ref(record: dict) -> dict:
    return {
        "lease_id": record["id"],
        "lease_epoch": record["payload"]["lease_epoch"],
        "content_digest": record["content_digest"],
    }


def _record_key(record: dict) -> tuple:
    return (record["object_type"], record["id"], record["version"])


def _ref_state_key(ref: dict) -> tuple:
    return (ref["object_type"], ref["id"], ref["version"], ref["content_digest"])


def _working_copy(immutable_state: dict) -> dict:
    state = dict(immutable_state)
    for field in _LIST_FIELDS:
        state[field] = list(immutable_state[field])
    return state


def _index_projection(state: dict) -> dict:
    records = {}
    by_id = {}
    by_type_id = {}
    for item in state["records"]:
        records[_record_key(item)] = item
        by_id.setdefault(item["id"], []).append(item)
        by_type_id.setdefault((item["object_type"], item["id"]), []).append(item)
    state_pos = {}
    state_by_ref = {}
    for index, item in enumerate(state["object_states"]):
        key = _ref_state_key(item["object_ref"])
        state_pos[key] = index
        state_by_ref[key] = item
    return {
        "records": records, "by_id": by_id, "by_type_id": by_type_id,
        "state_pos": state_pos, "state_by_ref": state_by_ref,
    }


def _bind_projection(state: dict) -> None:
    if not isinstance(state, dict):
        raise M1AError("E-SCHEMA-INVALID", "projection must be an object")
    if state.get("projection_schema_version") != "m1a-projection-1":
        raise M1AError("E-SCHEMA-UNKNOWN-VERSION", str(state.get("projection_schema_version")))
    if not isinstance(state.get("records"), list) or not isinstance(state.get("object_states"), list):
        raise M1AError("E-SCHEMA-INVALID", "projection collections")
    if len(_projection_bytes(state, omit_content_digest=False)) > 262_144:
        raise M1AError("E-SCHEMA-BOUND", "canonical byte bound exceeded")


def _cached_canonical(value, cache: dict, key: tuple) -> bytes:
    blob = cache.get(key)
    if blob is None:
        blob = canonical_bytes(value)
        cache[key] = blob
    return blob


def _projection_bytes(state: dict, *, omit_content_digest: bool) -> bytes:
    """RFC 8785 projection bytes, with untouched records taken from cache."""

    keys = [key for key in state if not (omit_content_digest and key == "content_digest")]
    keys.sort(key=lambda key: key.encode("utf-16be"))
    parts = []
    for key in keys:
        value = state[key]
        if key == "records":
            encoded = b"[" + b",".join(
                _cached_canonical(
                    item, _RECORD_CANONICAL,
                    (item["object_type"], item["id"], item["version"], item["content_digest"]),
                )
                for item in value
            ) + b"]"
        elif key == "object_states":
            encoded = b"[" + b",".join(
                _cached_canonical(
                    item, _OBJECT_STATE_CANONICAL,
                    (
                        item["object_ref"]["object_type"], item["object_ref"]["id"],
                        item["object_ref"]["version"], item["object_ref"]["content_digest"],
                        item["state"],
                    ),
                )
                for item in value
            ) + b"]"
        else:
            encoded = canonical_bytes(value)
        parts.append(canonical_bytes(key) + b":" + encoded)
    return b"{" + b",".join(parts) + b"}"


def _projection_digest(state: dict) -> str:
    return hashlib.sha256(_projection_bytes(state, omit_content_digest=True)).hexdigest()


def _record(state: dict, ref: dict | None, object_type: str | None = None) -> dict | None:
    if ref is None:
        return None
    rec_id = ref.get("id", ref.get("lease_id"))
    index = _INDEX
    if index is not None:
        if object_type is not None:
            candidates = index["by_type_id"].get((object_type, rec_id), ())
        else:
            candidates = index["by_id"].get(rec_id, ())
        for item in candidates:
            if "version" in ref and item["version"] != ref["version"]:
                continue
            if "content_digest" not in ref or item["content_digest"] == ref["content_digest"]:
                return item
        return None
    for item in state["records"]:
        if object_type is not None and item["object_type"] != object_type:
            continue
        if item["id"] != rec_id:
            continue
        if "version" in ref and item["version"] != ref["version"]:
            continue
        if "content_digest" not in ref or item["content_digest"] == ref["content_digest"]:
            return item
    return None


def _state_for(state: dict, ref: dict) -> str | None:
    index = _INDEX
    if index is not None:
        item = index["state_by_ref"].get(_ref_state_key(ref))
        return None if item is None else item["state"]
    for item in state["object_states"]:
        if item["object_ref"] == ref:
            return item["state"]
    return None


def _sealed_state(value: dict) -> dict:
    result = dict(value)
    result.pop("content_digest", None)
    result["records"] = sorted(
        result["records"], key=lambda r: (r["object_type"], r["id"], r["version"])
    )
    result["object_states"] = sorted(
        result["object_states"],
        key=lambda s: (
            s["object_ref"]["object_type"], s["object_ref"]["id"],
            s["object_ref"]["version"],
        ),
    )
    for field in (
        "processed_command_receipts", "open_blocker_refs", "open_escalation_refs",
        "ambiguous_effect_refs",
    ):
        key = "receipt_id" if field == "processed_command_receipts" else "id"
        result[field] = sorted(result[field], key=lambda item: item[key])
    result["consumed_novelty_delta_digests"] = sorted(
        result["consumed_novelty_delta_digests"]
    )
    result["content_digest"] = _projection_digest(result)
    return result


def genesis(genesis_record: dict) -> dict:
    """Create the only valid empty projection from explicit genesis input."""

    validate_genesis(genesis_record)
    return _sealed_state({
        "projection_schema_version": "m1a-projection-1",
        "stream_id": genesis_record["stream_id"],
        "state_version": 0,
        "last_event_sequence": 0,
        "last_event_digest": GENESIS_DIGEST,
        "operative_goal_ref": None,
        "active_arm_run_ref": None,
        "active_plan_ref": None,
        "records": [],
        "object_states": [],
        "processed_command_receipts": [],
        "consumed_novelty_delta_digests": [],
        "open_blocker_refs": [],
        "open_escalation_refs": [],
        "ambiguous_effect_refs": [],
        "current_pause_policy_ref": copy.deepcopy(genesis_record["current_pause_policy_ref"]),
        "current_pause_epoch": genesis_record["current_pause_epoch"],
        "terminal_status": "nonterminal",
    })


def _set_object_state(state: dict, record: dict, disposition: str) -> None:
    target = object_ref(record)
    item = {"object_ref": target, "state": disposition}
    index = _INDEX
    if index is not None:
        key = _ref_state_key(target)
        pos = index["state_pos"].get(key)
        if pos is None:
            index["state_pos"][key] = len(state["object_states"])
            state["object_states"].append(item)
        else:
            state["object_states"][pos] = item
        index["state_by_ref"][key] = item
        return
    state["object_states"] = [
        existing for existing in state["object_states"] if existing["object_ref"] != target
    ]
    state["object_states"].append(item)


def _set_ref_state(state: dict, target: dict | None, disposition: str) -> None:
    record = _record(state, target)
    if record is not None:
        _set_object_state(state, record, disposition)


def _insert_record(state: dict, record: dict) -> None:
    key = _record_key(record)
    index = _INDEX
    if index is not None:
        existing = index["records"].get(key)
        if existing is not None:
            if existing != record:
                raise M1AError("E-DIGEST-MISMATCH", "conflicting immutable record")
            return
    else:
        for existing in state["records"]:
            if _record_key(existing) == key:
                if existing != record:
                    raise M1AError("E-DIGEST-MISMATCH", "conflicting immutable record")
                return
    stored = copy.deepcopy(record)
    state["records"].append(stored)
    if index is not None:
        index["records"][key] = stored
        index["by_id"].setdefault(stored["id"], []).append(stored)
        index["by_type_id"].setdefault((stored["object_type"], stored["id"]), []).append(stored)
    default_state = {
        "owner-intent": "recorded", "goal-spec": "accepted",
        "goal-spec-amendment": "applied", "arm-run": "active",
        "plan-revision": "active", "plan-transition": "applied", "task-node": "ready",
        "work-packet": "leased", "worker-result": "submitted", "gate-result": "recorded",
        "retry-decision": record.get("payload", {}).get("disposition", "recorded"),
        "replan-decision": record.get("payload", {}).get("disposition", "recorded"),
        "blocker": record.get("payload", {}).get("state", "open"),
        "escalation": record.get("payload", {}).get("state", "raised"),
        "plan-verifier-decision": record.get("payload", {}).get("disposition", "recorded"),
        "owner-verifier-exception": "active",
        "executive-lease": record.get("payload", {}).get("state", "active"),
        "work-lease": record.get("payload", {}).get("state", "active"),
        "checkpoint": "recorded",
        "route-assignment": record.get("payload", {}).get("state", "assigned"),
        "capacity-observation": "recorded",
        "budget-reservation": record.get("payload", {}).get("state", "reserved"),
        "pause-directive": "applied",
        "effect-observation": record.get("payload", {}).get("observation", "recorded"),
        "disposition": record.get("payload", {}).get("disposition", "recorded"),
    }[record["object_type"]]
    _set_object_state(state, record, default_state)


def reduce(immutable_state: dict, ordered_event: dict) -> dict:
    """Apply one validated, contiguous event to an immutable projection."""

    global _INDEX
    _bind_projection(immutable_state)
    validate_event_envelope(ordered_event)
    event = ordered_event
    if event.get("event_schema_version") != "m1a-event-1":
        raise M1AError("E-SCHEMA-UNKNOWN-VERSION")
    if digest(event, omit_content_digest=True) != event.get("content_digest"):
        raise M1AError("E-DIGEST-MISMATCH", "event content digest")
    if event["stream_id"] != immutable_state["stream_id"]:
        raise M1AError("E-DIGEST-MISMATCH", "stream mismatch")
    if event["sequence"] != immutable_state["last_event_sequence"] + 1:
        raise M1AError("E-DIGEST-MISMATCH", "event sequence gap")
    if event["previous_event_digest"] != immutable_state["last_event_digest"]:
        raise M1AError("E-DIGEST-MISMATCH", "event chain mismatch")
    delta = {
        "event_type": event["event_type"], "primary_record": event["primary_record"],
        "related_records": event["related_records"],
    }
    if digest(delta) != event["state_delta_digest"]:
        raise M1AError("E-DIGEST-MISMATCH", "state delta digest")

    state = _working_copy(immutable_state)
    _INDEX = _index_projection(state)
    try:
        for record in [event["primary_record"], *event["related_records"]]:
            if digest(record, omit_content_digest=True) != record.get("content_digest"):
                raise M1AError("E-DIGEST-MISMATCH", "record content digest")
            _insert_record(state, record)
            payload = record["payload"]
            if record["object_type"] == "work-packet":
                _set_ref_state(state, payload["supersedes_packet_ref"], "stale")
            elif record["object_type"] in ("work-lease", "executive-lease"):
                old = payload["supersedes_lease_ref"]
                if old:
                    prior = _record(state, old, record["object_type"])
                    if prior is not None:
                        _set_object_state(state, prior, "replaced")
            elif record["object_type"] == "route-assignment":
                _set_ref_state(state, payload["supersedes_route_ref"], "released")

        primary = event["primary_record"]
        payload = primary["payload"]
        event_type = event["event_type"]
        if event_type == "GoalSpecAccepted":
            state["operative_goal_ref"] = object_ref(primary)
        elif event_type == "GoalSpecAmended":
            _set_ref_state(state, payload["old_goal_ref"], "superseded")
            state["operative_goal_ref"] = object_ref(event["related_records"][0])
        elif event_type == "ArmRunStarted":
            state["active_arm_run_ref"] = object_ref(primary)
        elif event_type == "PlanRevisionAdmitted":
            if payload["prior_plan_ref"]:
                _set_ref_state(state, payload["prior_plan_ref"], "superseded")
            state["active_plan_ref"] = object_ref(primary)
            state["terminal_status"] = "nonterminal"
        elif event_type == "WorkerResultRecorded":
            packet_ref = payload["work_packet_ref"]
            _set_ref_state(state, packet_ref, "result-submitted")
            packet = _record(state, packet_ref, "work-packet")
            if packet is not None:
                _set_ref_state(state, packet["payload"]["task_ref"], "gate-pending")
        elif event_type == "GateResultRecorded":
            passed = payload["disposition"] == "pass"
            _set_ref_state(state, payload["task_ref"], "verified" if passed else "failed")
            _set_ref_state(state, payload["work_packet_ref"], "gate-passed" if passed else "failed")
        elif event_type == "RetryAdmitted":
            state["consumed_novelty_delta_digests"].append(payload["novelty_delta_digest"])
            _set_ref_state(state, payload["task_ref"], "ready")
        elif event_type == "BlockerRaised":
            state["open_blocker_refs"].append(object_ref(primary))
            _set_ref_state(state, payload["blocked_object_ref"], "blocked")
        elif event_type == "EscalationRaised":
            state["open_escalation_refs"].append(object_ref(primary))
            _set_ref_state(state, payload["source_blocker_ref"], "escalated")
        elif event_type == "PauseApplied":
            state["current_pause_epoch"] = payload["new_pause_epoch"]
        elif event_type == "EffectObservationRecorded" and payload["observation"] == "ambiguous":
            state["ambiguous_effect_refs"].append(object_ref(primary))
            _set_ref_state(state, payload["work_packet_ref"], "quarantined")
            packet = _record(state, payload["work_packet_ref"], "work-packet")
            if packet is not None:
                _set_ref_state(state, packet["payload"]["task_ref"], "quarantined")
        elif event_type == "PlanVerifierDecisionRecorded":
            active = state["active_plan_ref"]
            exact_active = active is not None and active == payload["plan_ref"]
            plan_tasks = [
                record for record in state["records"] if record["object_type"] == "task-node"
                and record["payload"]["plan_ref"]["id"] == payload["plan_ref"]["id"]
                and record["payload"]["plan_ref"]["version"] == payload["plan_ref"]["version"]
            ]
            verified = {
                (item["object_ref"]["object_type"], item["object_ref"]["id"],
                 item["object_ref"]["version"])
                for item in state["object_states"] if item["state"] == "verified"
            }
            all_verified = bool(plan_tasks) and all(
                ("task-node", task["id"], task["version"]) in verified for task in plan_tasks
            )
            if payload["disposition"] in ("pass", "downgraded-pass") and exact_active and all_verified:
                terminal = (
                    "terminal-verified" if payload["disposition"] == "pass"
                    else "terminal-verified-downgraded"
                )
                state["terminal_status"] = terminal
                _set_ref_state(state, payload["plan_ref"], terminal)
                for task in plan_tasks:
                    _set_object_state(state, task, "complete")
                    for packet in state["records"]:
                        if (packet["object_type"] == "work-packet"
                                and packet["payload"]["task_ref"] == object_ref(task)):
                            _set_object_state(state, packet, "consumed")
        elif event_type in (
            "PlanItemDisposed", "PacketFenced", "LeaseFenced", "EffectDisposed",
            "EvidenceDisposed",
        ):
            target = {
                "fence": "stale", "invalidate": "stale", "quarantine": "quarantined",
                "supersede": "superseded", "cancel": "canceled", "retain-definition": "admitted",
            }.get(payload["disposition"])
            if target is not None:
                _set_ref_state(state, payload["object_ref"], target)

        state["state_version"] += 1
        state["last_event_sequence"] = event["sequence"]
        state["last_event_digest"] = event["content_digest"]
        return _sealed_state(state)
    finally:
        _INDEX = None


def _domain_record(object_type: str, schema_version: str, record_id: str, payload: dict,
                   command: dict, *, actor: str = "KERNEL-M1A", under_lease=None) -> dict:
    return seal({
        "object_type": object_type, "schema_version": schema_version, "id": record_id,
        "version": 1, "created_by_actor_id": actor,
        "created_under_lease": copy.deepcopy(under_lease),
        "causation_id": command["command_id"], "correlation_id": "CORR-M1A",
        "created_logical_time": command["logical_time"], "payload": copy.deepcopy(payload),
    })


def derive_work_packet(state: dict, admitted_task_id: str, command: dict | None = None,
                       *, work_lease: dict | None = None, route: dict | None = None,
                       budget_ref: dict | None = None, attempt: int = 1,
                       supersedes: dict | None = None, checkpoint_ref: dict | None = None) -> dict:
    """Derive a WorkPacket only from admitted state and explicit deterministic inputs."""

    task = next((r for r in state["records"] if r["object_type"] == "task-node"
                 and r["id"] == admitted_task_id and _state_for(state, object_ref(r)) in
                 ("ready", "failed", "blocked", "quarantined")), None)
    if task is None:
        raise M1AError("E-DAG-DEPENDENCY", admitted_task_id)
    goal = _record(state, task["payload"].get("goal_ref") or state["operative_goal_ref"], "goal-spec")
    plan = _record(state, task["payload"]["plan_ref"], "plan-revision")
    if goal is None or plan is None or command is None or work_lease is None or route is None:
        raise M1AError("E-SCHEMA-MISSING-FIELD", "packet derivation binding")
    budget = _record(state, budget_ref, "budget-reservation")
    if budget is None:
        raise M1AError("E-SCHEMA-MISSING-FIELD", "budget reservation")
    seed = {
        "command_id": command["command_id"], "task_ref": object_ref(task), "attempt": attempt,
        "work_lease_ref": lease_ref(work_lease), "route_assignment_ref": object_ref(route),
        "budget_reservation_ref": object_ref(budget), "checkpoint_ref": checkpoint_ref,
    }
    paths = sorted(set(task["payload"]["read_resource_paths"] + task["payload"]["write_resource_paths"]))
    payload = {
        "goal_ref": object_ref(goal), "plan_ref": object_ref(plan), "task_ref": object_ref(task),
        "supersedes_packet_ref": copy.deepcopy(supersedes),
        "logical_worker_actor_id": work_lease["payload"]["subject_actor_id"], "attempt": attempt,
        "work_lease_ref": lease_ref(work_lease),
        "expected_goal_fence_epoch": goal["payload"]["fence_epoch"],
        "expected_pause_epoch": state["current_pause_epoch"],
        "authority_ids": copy.deepcopy(task["payload"]["authority_ids"]),
        "capability_ids": copy.deepcopy(task["payload"]["capability_ids"]),
        "allowed_resource_paths": paths,
        "effect_keys": copy.deepcopy(goal["payload"]["allowed_effect_keys"]),
        "privacy_class_id": task["payload"]["privacy_class_id"],
        "egress_class_id": task["payload"]["egress_class_id"],
        "authority_policy_ref": copy.deepcopy(task["payload"]["authority_policy_ref"]),
        "privacy_policy_ref": copy.deepcopy(task["payload"]["privacy_policy_ref"]),
        "egress_policy_ref": copy.deepcopy(task["payload"]["egress_policy_ref"]),
        "verifier_policy_ref": copy.deepcopy(task["payload"]["verifier_policy_ref"]),
        "constitution_policy_ref": copy.deepcopy(task["payload"]["constitution_policy_ref"]),
        "route_assignment_ref": object_ref(route), "budget_reservation_ref": object_ref(budget),
        "budget_limits": copy.deepcopy(budget["payload"]["remaining"]),
        "source_digests": copy.deepcopy(task["payload"]["source_digests"]),
        "gate_requirements": copy.deepcopy(task["payload"]["gate_requirements"]),
        "allowed_action_ids": copy.deepcopy(task["payload"]["allowed_action_ids"]),
        "prohibition_ids": copy.deepcopy(task["payload"]["prohibition_ids"]),
        "inherited_non_goals": copy.deepcopy(goal["payload"]["non_goals"]),
        "output_schema_digest": task["payload"]["output_schema_digest"],
        "deadline_logical_time": work_lease["payload"]["expires_logical_time"],
        "checkpoint_ref": copy.deepcopy(checkpoint_ref),
    }
    executive = command["lease_ref"]
    return _domain_record(
        "work-packet", "m1a-work-packet-1", "PACKET-" + digest(seed)[:32], payload,
        command, under_lease=executive,
    )


def _copy_packet(state: dict, command: dict, *, route: dict | None = None,
                 checkpoint_ref: dict | None = None, retry: dict | None = None) -> dict:
    if retry is not None:
        prior_ref = retry["payload"]["prior_work_packet_ref"]
        attempt = retry["payload"]["next_attempt"]
    else:
        old_route = command["payload"]["old_route_ref"]
        candidates = [r for r in state["records"] if r["object_type"] == "work-packet"
                      and r["payload"]["route_assignment_ref"] == old_route]
        prior = sorted(candidates, key=lambda r: (r["created_logical_time"], r["id"]))[-1]
        prior_ref = object_ref(prior)
        attempt = prior["payload"]["attempt"]
    prior = _record(state, prior_ref, "work-packet")
    if prior is None:
        raise M1AError("E-DAG-DEPENDENCY", "prior packet")
    payload = copy.deepcopy(prior["payload"])
    payload["supersedes_packet_ref"] = object_ref(prior)
    payload["attempt"] = attempt
    if route is not None:
        payload["route_assignment_ref"] = object_ref(route)
        payload["checkpoint_ref"] = copy.deepcopy(checkpoint_ref)
    seed = {
        "command_id": command["command_id"], "task_ref": payload["task_ref"], "attempt": attempt,
        "work_lease_ref": payload["work_lease_ref"],
        "route_assignment_ref": payload["route_assignment_ref"],
        "budget_reservation_ref": payload["budget_reservation_ref"],
        "checkpoint_ref": payload["checkpoint_ref"],
    }
    return _domain_record(
        "work-packet", "m1a-work-packet-1", "PACKET-" + digest(seed)[:32], payload,
        command, under_lease=prior["created_under_lease"],
    )


_SIMPLE_EVENTS = {
    "record-owner-intent": "OwnerIntentRecorded", "accept-goal-spec": "GoalSpecAccepted",
    "record-capacity-observation": "CapacityObservationRecorded", "reserve-budget": "BudgetReserved",
    "submit-worker-result": "WorkerResultRecorded", "submit-gate-result": "GateResultRecorded",
    "submit-plan-verifier-decision": "PlanVerifierDecisionRecorded",
    "record-owner-verifier-exception": "OwnerVerifierExceptionRecorded",
    "record-checkpoint": "CheckpointRecorded", "raise-blocker": "BlockerRaised",
    "raise-escalation": "EscalationRaised", "apply-pause": "PauseApplied",
    "record-effect-observation": "EffectObservationRecorded",
}


def _disposition_event(record: dict) -> str:
    disposition = record["payload"]["disposition"]
    object_type = record["payload"]["object_ref"]["object_type"]
    if object_type == "work-packet":
        return "PacketFenced"
    if object_type in ("work-lease", "executive-lease"):
        return "LeaseFenced"
    if object_type == "effect-observation":
        return "EffectDisposed"
    if object_type in ("evidence", "worker-result", "gate-result", "checkpoint"):
        return "EvidenceDisposed"
    return "PlanItemDisposed"


def _event_specs(state: dict, command: dict) -> list[tuple[str, dict, list[dict]]]:
    kind, payload = command["command_type"], command["payload"]
    if kind in _SIMPLE_EVENTS:
        return [(_SIMPLE_EVENTS[kind], payload, [])]
    if kind == "start-arm-run":
        return [("ArmRunStarted", payload["arm_run"], [payload["executive_lease"]])]
    if kind == "propose-plan-revision":
        return [("PlanRevisionAdmitted", payload["plan_revision"],
                 [payload["plan_transition"], *payload["task_nodes"]])]
    if kind == "derive-work-packet":
        packet = derive_work_packet(
            state, payload["task_ref"]["id"], command, work_lease=payload["work_lease"],
            route=payload["route_assignment"], budget_ref=payload["budget_reservation_ref"],
            attempt=payload["attempt"],
        )
        if packet["content_digest"] != payload["expected_work_packet_digest"]:
            raise M1AError("E-DIGEST-MISMATCH", "derived packet")
        return [("WorkPacketDerived", packet, [payload["route_assignment"], payload["work_lease"]])]
    if kind == "propose-retry":
        packet = _copy_packet(state, command, retry=payload)
        return [("RetryAdmitted", payload, []), ("WorkPacketDerived", packet, [])]
    if kind == "replace-route":
        packet = _copy_packet(
            state, command, route=payload["new_route"], checkpoint_ref=payload["checkpoint_ref"]
        )
        if packet["content_digest"] != payload["expected_new_packet_digest"]:
            raise M1AError("E-DIGEST-MISMATCH", "replacement packet")
        return [("RouteAssignmentReplaced", payload["new_route"], []),
                ("WorkPacketDerived", packet, [])]
    if kind == "replace-work-lease":
        return [("WorkLeaseReplaced", payload["new_lease"], [])]
    if kind == "replace-executive-lease":
        return [("ExecutiveLeaseReplaced", payload["new_lease"], [])]
    if kind == "amend-goal-spec":
        specs = [
            ("GoalSpecAmended", payload["amendment"], [payload["new_goal_spec"]]),
            ("PlanRevisionAdmitted", payload["replacement_plan_revision"],
             [payload["plan_transition"], *payload["task_nodes"]]),
        ]
        for disposition in payload["replacement_plan_revision"]["payload"]["dispositions"]:
            record = _make_disposition(command, disposition)
            specs.append((_disposition_event(record), record, []))
        return specs
    if kind == "propose-replan":
        specs = [
            ("ReplanAdmitted", payload["decision"],
             [*payload["budget_reservations"], *payload["route_assignments"]]),
            ("PlanRevisionAdmitted", payload["new_plan_revision"],
             [payload["transition"], *payload["task_nodes"]]),
        ]
        for disposition in payload["new_plan_revision"]["payload"]["dispositions"]:
            record = _make_disposition(command, disposition)
            specs.append((_disposition_event(record), record, []))
        return specs
    raise M1AError("E-SCHEMA-UNKNOWN-VERSION", f"unsupported command {kind}")


def _make_disposition(command: dict, disposition: dict) -> dict:
    record_id = "DISPOSITION-" + digest({
        "command_id": command["command_id"], "disposition": disposition,
    })[:32]
    return _domain_record(
        "disposition", "m1a-disposition-1", record_id, disposition, command,
        actor="KERNEL-M1A", under_lease=None,
    )


def _make_event(state: dict, command: dict, position: int, event_type: str,
                primary: dict, related: list[dict]) -> dict:
    suffix = command["command_id"][4:] if command["command_id"].startswith("CMD-") else command["command_id"]
    related = sorted(copy.deepcopy(related), key=lambda r: (r["object_type"], r["id"], r["version"]))
    return seal({
        "event_schema_version": "m1a-event-1", "event_id": f"EV-{suffix}-{position:04d}",
        "stream_id": state["stream_id"], "sequence": state["last_event_sequence"] + 1,
        "previous_event_digest": state["last_event_digest"],
        "logical_time": command["logical_time"], "command_id": command["command_id"],
        "command_digest": command["content_digest"], "event_type": event_type,
        "primary_record": copy.deepcopy(primary), "related_records": related,
        "state_delta_digest": digest({
            "event_type": event_type, "primary_record": primary, "related_records": related,
        }),
    })


def _receipt(command: dict, disposition: str, error: str | None, witness: dict | None,
             before: str, after: str, events: list[dict]) -> dict:
    if disposition == "conflict":
        receipt_id = "RCPT-CONFLICT-" + digest({
            "command_id": command["command_id"], "command_digest": command["content_digest"],
        })[:32]
    else:
        suffix = command["command_id"][4:] if command["command_id"].startswith("CMD-") else command["command_id"]
        receipt_id = "RCPT-" + suffix
    return seal({
        "receipt_schema_version": "m1a-receipt-1", "receipt_id": receipt_id,
        "command_id": command["command_id"], "command_digest": command["content_digest"],
        "disposition": disposition, "primary_error": error, "related_errors": [],
        "witness": copy.deepcopy(witness), "state_before_digest": before,
        "state_after_digest": after, "event_ids": sorted(e["event_id"] for e in events),
        "first_event_sequence": events[0]["sequence"] if events else None,
        "last_event_sequence": events[-1]["sequence"] if events else None,
    })


def admit(immutable_state: dict, expected_state_digest: str, command: dict,
          authority_fixtures: list[dict], policy_fixtures: list[dict],
          receipt_journal: list[dict]) -> tuple[dict, list[dict], dict, list[dict]]:
    """Admit one command and return projection, events, receipt, and complete journal."""

    validate_projection(immutable_state)
    if not isinstance(command, dict):
        raise M1AError("E-SCHEMA-INVALID", "command must be an object")
    missing_identity = {"command_id", "content_digest", "expected_state_digest"} - set(command)
    if missing_identity:
        raise M1AError("E-SCHEMA-MISSING-FIELD", min(missing_identity))
    state = copy.deepcopy(immutable_state)
    journal = copy.deepcopy(receipt_journal)
    before = state["content_digest"]
    for item in journal:
        validate_receipt(item)
    journal_keys = {}
    for item in journal:
        key = (item["command_id"], item["command_digest"])
        if key in journal_keys and journal_keys[key] != item:
            raise M1AError("E-DIGEST-MISMATCH", "conflicting receipt journal entry")
        journal_keys[key] = item
    projected = {
        (item["receipt_id"], item["command_id"], item["command_digest"])
        for item in state["processed_command_receipts"]
    }
    journaled = {
        (item["receipt_id"], item["command_id"], item["command_digest"])
        for item in journal if item["disposition"] == "accepted"
    }
    if projected != journaled:
        raise M1AError("E-DIGEST-MISMATCH", "incomplete receipt journal")
    schema_error = None
    try:
        validate_command_envelope(command)
    except M1AError as error:
        schema_error = error.code
    if schema_error is None and (
        digest(command, omit_content_digest=True) != command.get("content_digest")
        or digest(command["payload"]) != command.get("payload_digest")
        or command.get("candidate_digest") != command.get("payload_digest")
    ):
        schema_error = "E-DIGEST-MISMATCH"
    same_id = [item for item in journal if item["command_id"] == command["command_id"]]
    original = next((item for item in same_id
                     if item["command_digest"] == command["content_digest"]), None)
    if schema_error is not None:
        try:
            receipt = _receipt(command, "refused", schema_error, None, before, before, [])
            validate_receipt(receipt)
        except (AttributeError, KeyError, TypeError, M1AError) as error:
            raise M1AError(schema_error, "command identity cannot bind a receipt") from error
        journal.append(copy.deepcopy(receipt))
        return state, [], receipt, journal
    if original is not None:
        journal.append(copy.deepcopy(original))
        return state, [], copy.deepcopy(original), journal
    if same_id:
        error, witness, disposition = "E-IDEMPOTENCY-CONFLICT", None, "conflict"
    elif expected_state_digest != before or command["expected_state_digest"] != before:
        error, witness, disposition = "E-DIGEST-MISMATCH", None, "refused"
    else:
        error, witness = validate_admission(state, command, authority_fixtures, policy_fixtures)
        disposition = "refused" if error else "accepted"
    if disposition != "accepted":
        receipt = _receipt(command, disposition, error, witness, before, before, [])
        journal.append(copy.deepcopy(receipt))
        return state, [], receipt, journal

    events = []
    for position, (event_type, primary, related) in enumerate(_event_specs(state, command), 1):
        event = _make_event(state, command, position, event_type, primary, related)
        state = reduce(state, event)
        events.append(event)
    identity = {
        "receipt_id": "RCPT-" + (command["command_id"][4:] if command["command_id"].startswith("CMD-") else command["command_id"]),
        "command_id": command["command_id"], "command_digest": command["content_digest"],
    }
    state["processed_command_receipts"].append(identity)
    state = _sealed_state(state)
    receipt = _receipt(command, "accepted", None, None, before, state["content_digest"], events)
    journal.append(copy.deepcopy(receipt))
    return state, events, receipt, journal


def _checkpoint_ref_digest(state: dict, object_type: str) -> str:
    refs = [object_ref(record) for record in state["records"]
            if record["object_type"] == object_type]
    return digest(sorted(refs, key=lambda ref: ref["id"]))


def _validate_replay_checkpoint(checkpoint: dict, decoded_events: list[dict]) -> dict:
    if not isinstance(checkpoint, dict):
        raise M1AError("E-SCHEMA-INVALID", "replay checkpoint")
    unknown = set(checkpoint) - {"checkpoint_record", "projection_snapshot"}
    missing = {"checkpoint_record", "projection_snapshot"} - set(checkpoint)
    if unknown:
        raise M1AError("E-SCHEMA-UNKNOWN-FIELD", min(unknown))
    if missing:
        raise M1AError("E-SCHEMA-MISSING-FIELD", min(missing))
    record = checkpoint["checkpoint_record"]
    snapshot = checkpoint["projection_snapshot"]
    validate_domain_record(record)
    validate_projection(snapshot)
    if record["object_type"] != "checkpoint":
        raise M1AError("E-SCHEMA-INVALID", "checkpoint record type")
    if digest(record, omit_content_digest=True) != record["content_digest"]:
        raise M1AError("E-DIGEST-MISMATCH", "checkpoint record digest")
    payload = record["payload"]
    packet_refs = sorted(
        [object_ref(item) for item in snapshot["records"]
         if item["object_type"] == "work-packet"],
        key=lambda ref: ref["id"],
    )
    lease_refs = sorted(
        [lease_ref(item) for item in snapshot["records"]
         if item["object_type"] in ("executive-lease", "work-lease")],
        key=lambda ref: ref["lease_id"],
    )
    expected = {
        "state_version": snapshot["state_version"],
        "state_digest": snapshot["content_digest"],
        "active_goal_ref": snapshot["operative_goal_ref"],
        "active_plan_ref": snapshot["active_plan_ref"],
        "open_packet_refs": packet_refs,
        "ambiguous_effect_refs": snapshot["ambiguous_effect_refs"],
        "budget_snapshot_digest": _checkpoint_ref_digest(snapshot, "budget-reservation"),
        "route_snapshot_digest": _checkpoint_ref_digest(snapshot, "route-assignment"),
        "pause_epoch": snapshot["current_pause_epoch"],
        "lease_epoch_digest": digest(lease_refs),
    }
    if payload != expected:
        raise M1AError("E-DIGEST-MISMATCH", "checkpoint projection binding")
    if not decoded_events:
        raise M1AError("E-DIGEST-MISMATCH", "checkpoint event missing")
    first = decoded_events[0]
    if first["event_type"] != "CheckpointRecorded" or first["primary_record"] != record:
        raise M1AError("E-DIGEST-MISMATCH", "checkpoint first event binding")
    return copy.deepcopy(snapshot)


def replay(genesis_record: dict, event_inputs: list[str | bytes], commands: list[dict],
           receipts: list[dict], checkpoint: dict | None = None) -> dict:
    """Replay a complete or checkpointed stream, including receipt-journal boundaries."""

    decoded_events = [decode(raw, "m1a-1") for raw in event_inputs]
    state = (_validate_replay_checkpoint(checkpoint, decoded_events)
             if checkpoint else genesis(genesis_record))
    for command in commands:
        validate_command_envelope(command)
    for receipt in receipts:
        validate_receipt(receipt)
    command_pairs = [(item["command_id"], item["content_digest"]) for item in commands]
    receipt_pairs = [(item["command_id"], item["command_digest"]) for item in receipts]
    if len(command_pairs) != len(set(command_pairs)) or len(receipt_pairs) != len(set(receipt_pairs)):
        raise M1AError("E-DIGEST-MISMATCH", "duplicate replay journal key")
    if command_pairs != receipt_pairs:
        raise M1AError("E-DIGEST-MISMATCH", "command receipt journal order")
    command_map = dict(zip(command_pairs, commands, strict=True))
    receipt_map = dict(zip(receipt_pairs, receipts, strict=True))
    remaining = {
        key for key, item in receipt_map.items()
        if item["disposition"] == "accepted" and item["event_ids"]
    }
    index = 0
    while index < len(decoded_events):
        event = decoded_events[index]
        key = (event["command_id"], event["command_digest"])
        command = command_map.get(key)
        receipt = receipt_map.get(key)
        if command is None or receipt is None or event["command_digest"] != command["content_digest"]:
            raise M1AError("E-DIGEST-MISMATCH", "event journal binding")
        if key not in remaining:
            raise M1AError("E-DIGEST-MISMATCH", "event for non-accepted receipt")
        if (command["expected_state_version"] != state["state_version"]
                or command["expected_state_digest"] != state["content_digest"]
                or receipt["state_before_digest"] != state["content_digest"]):
            raise M1AError("E-DIGEST-MISMATCH", "command state boundary")
        group = []
        while index < len(decoded_events):
            candidate = decoded_events[index]
            candidate_key = (candidate["command_id"], candidate["command_digest"])
            if candidate_key != key:
                break
            group.append(candidate)
            index += 1
        if [item["event_id"] for item in group] != receipt["event_ids"]:
            raise M1AError("E-DIGEST-MISMATCH", "receipt event boundary")
        if (receipt["first_event_sequence"] != group[0]["sequence"]
                or receipt["last_event_sequence"] != group[-1]["sequence"]):
            raise M1AError("E-DIGEST-MISMATCH", "receipt sequence boundary")
        for grouped_event in group:
            state = reduce(state, grouped_event)
        state["processed_command_receipts"].append({
            "receipt_id": receipt["receipt_id"], "command_id": receipt["command_id"],
            "command_digest": receipt["command_digest"],
        })
        state = _sealed_state(state)
        if state["content_digest"] != receipt["state_after_digest"]:
            raise M1AError("E-DIGEST-MISMATCH", "receipt state-after boundary")
        remaining.discard(key)
    if remaining:
        raise M1AError("E-DIGEST-MISMATCH", "missing command events")
    return state
