"""Frozen naive reducer from master before CSL-336.

Used only as an oracle: every digest the incremental kernel emits must match
this implementation byte-for-byte. Do not call it from production code.
"""

from __future__ import annotations

import copy

from scripts.v3.planning.replay.kernel import object_ref
from scripts.v3.planning.validation.canonical import (
    M1AError, digest, seal, validate_domain_record, validate_event_envelope,
    validate_projection,
)


def _record(state: dict, ref: dict | None, object_type: str | None = None) -> dict | None:
    if ref is None:
        return None
    for item in state["records"]:
        if object_type is not None and item["object_type"] != object_type:
            continue
        if item["id"] != ref.get("id", ref.get("lease_id")):
            continue
        if "version" in ref and item["version"] != ref["version"]:
            continue
        if "content_digest" not in ref or item["content_digest"] == ref["content_digest"]:
            return item
    return None


def _sealed_state(value: dict) -> dict:
    result = copy.deepcopy(value)
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
    return seal(result)


def _set_object_state(state: dict, record: dict, disposition: str) -> None:
    target = object_ref(record)
    state["object_states"] = [
        item for item in state["object_states"] if item["object_ref"] != target
    ]
    state["object_states"].append({"object_ref": target, "state": disposition})


def _set_ref_state(state: dict, target: dict | None, disposition: str) -> None:
    record = _record(state, target)
    if record is not None:
        _set_object_state(state, record, disposition)


def _insert_record(state: dict, record: dict) -> None:
    key = (record["object_type"], record["id"], record["version"])
    for existing in state["records"]:
        if (existing["object_type"], existing["id"], existing["version"]) == key:
            if existing != record:
                raise M1AError("E-DIGEST-MISMATCH", "conflicting immutable record")
            return
    state["records"].append(copy.deepcopy(record))
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


def reduce_naive(immutable_state: dict, ordered_event: dict) -> dict:
    """Apply one event with the pre-CSL-336 full-projection validate/copy/digest path."""

    validate_projection(immutable_state)
    validate_event_envelope(ordered_event)
    state = copy.deepcopy(immutable_state)
    event = copy.deepcopy(ordered_event)
    if event.get("event_schema_version") != "m1a-event-1":
        raise M1AError("E-SCHEMA-UNKNOWN-VERSION")
    if digest(event, omit_content_digest=True) != event.get("content_digest"):
        raise M1AError("E-DIGEST-MISMATCH", "event content digest")
    if event["stream_id"] != state["stream_id"]:
        raise M1AError("E-DIGEST-MISMATCH", "stream mismatch")
    if event["sequence"] != state["last_event_sequence"] + 1:
        raise M1AError("E-DIGEST-MISMATCH", "event sequence gap")
    if event["previous_event_digest"] != state["last_event_digest"]:
        raise M1AError("E-DIGEST-MISMATCH", "event chain mismatch")
    delta = {
        "event_type": event["event_type"], "primary_record": event["primary_record"],
        "related_records": event["related_records"],
    }
    if digest(delta) != event["state_delta_digest"]:
        raise M1AError("E-DIGEST-MISMATCH", "state delta digest")

    records = [event["primary_record"], *event["related_records"]]
    for record in records:
        validate_domain_record(record)
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
