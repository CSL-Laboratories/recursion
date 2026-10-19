"""Pure M1A strict JSON decoding and RFC 8785-compatible canonical digests."""

from __future__ import annotations

import copy
import hashlib
import json
import re


MAX_IJSON_INTEGER = 9_007_199_254_740_991
MAX_INPUT_BYTES = 1_048_576
MAX_COLLECTION_ITEMS = 2_048
MAX_DEPTH = 16

COMMAND_FIELDS = {
    "command_schema_version", "command_id", "command_type", "actor_id", "auth_ref",
    "lease_ref", "logical_time", "expected_state_version", "expected_state_digest",
    "goal_ref", "plan_ref", "payload", "payload_digest", "candidate_digest",
    "content_digest",
}
EVENT_FIELDS = {
    "event_schema_version", "event_id", "stream_id", "sequence", "previous_event_digest",
    "logical_time", "command_id", "command_digest", "event_type", "primary_record",
    "related_records", "state_delta_digest", "content_digest",
}
DOMAIN_FIELDS = {
    "object_type", "schema_version", "id", "version", "created_by_actor_id",
    "created_under_lease", "causation_id", "correlation_id", "created_logical_time",
    "payload", "content_digest",
}
RECEIPT_FIELDS = {
    "receipt_schema_version", "receipt_id", "command_id", "command_digest",
    "disposition", "primary_error", "related_errors", "witness",
    "state_before_digest", "state_after_digest", "event_ids",
    "first_event_sequence", "last_event_sequence", "content_digest",
}
PROJECTION_FIELDS = {
    "projection_schema_version", "stream_id", "state_version", "last_event_sequence",
    "last_event_digest", "operative_goal_ref", "active_arm_run_ref", "active_plan_ref",
    "records", "object_states", "processed_command_receipts",
    "consumed_novelty_delta_digests", "open_blocker_refs", "open_escalation_refs",
    "ambiguous_effect_refs", "current_pause_policy_ref", "current_pause_epoch",
    "terminal_status", "content_digest",
}
GENESIS_FIELDS = {
    "genesis_schema_version", "stream_id", "current_pause_policy_ref",
    "current_pause_epoch",
}
AUTHORITY_FIELDS = {
    "attestation_schema_version", "attestation_id", "actor_id", "actor_role",
    "allowed_command_types", "candidate_digest", "authority_policy_ref",
    "constitution_policy_ref", "valid_from_logical_time", "expires_logical_time",
    "content_digest",
}
POLICY_FIELDS = {
    "policy_schema_version", "policy_id", "policy_version", "policy_kind",
    "allowed_roles", "allowed_command_types", "privacy_class_ids",
    "privacy_at_most_edges", "egress_class_ids", "egress_at_most_edges",
    "route_ids", "account_class_ids", "capacity_pool_ids", "allowed_spend_classes",
    "accepted_evidence_types", "failure_capability_requirements",
    "required_verifier_dimensions", "owner_verifier_exception_allowed",
    "operative_pause_mode", "representable_future_pause_modes", "retry_maximum",
    "content_digest",
}

DOMAIN_VERSIONS = {
    "arm-run": "m1a-arm-run-1",
    "blocker": "m1a-blocker-1",
    "budget-reservation": "m1a-budget-reservation-1",
    "capacity-observation": "m1a-capacity-observation-1",
    "checkpoint": "m1a-checkpoint-1",
    "disposition": "m1a-disposition-1",
    "effect-observation": "m1a-effect-observation-1",
    "escalation": "m1a-escalation-1",
    "executive-lease": "m1a-executive-lease-1",
    "gate-result": "m1a-gate-result-1",
    "goal-spec-amendment": "m1a-goal-spec-amendment-1",
    "goal-spec": "m1a-goal-spec-1",
    "owner-intent": "m1a-owner-intent-1",
    "owner-verifier-exception": "m1a-owner-verifier-exception-1",
    "pause-directive": "m1a-pause-directive-1",
    "plan-revision": "m1a-plan-revision-1",
    "plan-transition": "m1a-plan-transition-1",
    "plan-verifier-decision": "m1a-plan-verifier-decision-1",
    "replan-decision": "m1a-replan-decision-1",
    "retry-decision": "m1a-retry-decision-1",
    "route-assignment": "m1a-route-assignment-1",
    "task-node": "m1a-task-node-1",
    "work-lease": "m1a-work-lease-1",
    "work-packet": "m1a-work-packet-1",
    "worker-result": "m1a-worker-result-1",
}

DOMAIN_PAYLOAD_FIELDS = {
    "arm-run": {"goal_ref", "logical_actor_id", "eligible_route_ids", "eligible_account_class_ids", "eligible_capacity_pool_ids", "budget_limits", "source_digests", "executive_lease_ref", "verifier_policy_ref"},
    "blocker": {"blocked_object_ref", "kind", "evidence_digests", "required_actor_id", "allowed_recovery_ids", "retry_after_observation_ref", "failure_fingerprint_digest", "state"},
    "budget-reservation": {"goal_ref", "plan_ref", "task_ref", "limits", "consumed", "remaining", "state"},
    "capacity-observation": {"route_id", "account_class_id", "capacity_pool_id", "spend_class", "available", "remaining_units", "observation_digest"},
    "checkpoint": {"state_version", "state_digest", "active_goal_ref", "active_plan_ref", "open_packet_refs", "ambiguous_effect_refs", "budget_snapshot_digest", "route_snapshot_digest", "pause_epoch", "lease_epoch_digest"},
    "disposition": {"object_ref", "disposition", "replacement_ref", "reason_digest"},
    "effect-observation": {"effect_id", "effect_key", "work_packet_ref", "idempotency_key_digest", "dispatch_state", "receipt_digest", "provider_operation_id_digest", "observation", "quarantine"},
    "escalation": {"source_blocker_ref", "target_role", "failure_history_digests", "requested_decision", "option_digests", "recommendation_digest", "frozen_scope_digest", "state"},
    "executive-lease": {"subject_actor_id", "goal_ref", "plan_ref", "task_ref", "supersedes_lease_ref", "lease_epoch", "nonce_digest", "issued_logical_time", "expires_logical_time", "allowed_command_types", "state"},
    "gate-result": {"goal_ref", "plan_ref", "task_ref", "work_packet_ref", "candidate_digest", "source_digests", "evidence_digests", "gate_requirement", "procedure_version", "observation_digest", "expected_digest", "actual_digest", "independence", "disposition", "limitation_digests"},
    "goal-spec-amendment": {"old_goal_ref", "new_goal_ref", "changed_field_paths", "rationale_digest", "owner_authority_attestation_id", "effective_fence_epoch", "checkpoint_rule", "compatibility_inputs", "dispositions", "plan_transition_ref"},
    "goal-spec": {"owner_intent_ref", "outcome", "acceptance_gate_ids", "allowed_resource_paths", "non_goals", "authority_ids", "capability_ids", "allowed_action_ids", "prohibition_ids", "authority_policy_ref", "privacy_class_id", "egress_class_id", "privacy_policy_ref", "egress_policy_ref", "route_policy_ref", "allowed_route_ids", "allowed_account_class_ids", "allowed_capacity_pool_ids", "budget_limits", "source_digests", "novelty_policy_ref", "retry_policy_ref", "verifier_policy_ref", "constitution_policy_ref", "pause_policy_ref", "allowed_effect_keys", "publication_allowed", "terminal_owner_seam_ids", "fence_epoch", "state"},
    "owner-intent": {"intent_text", "source_kind", "source_digest", "privacy_policy_ref", "supersedes_ref"},
    "owner-verifier-exception": {"goal_ref", "plan_ref", "candidate_digest", "verifier_policy_ref", "constitution_policy_ref", "missing_dimensions", "reason_digest", "owner_authority_attestation_id", "issued_logical_time", "expires_logical_time"},
    "pause-directive": {"pause_policy_ref", "mode", "new_pause_epoch", "reason_digest"},
    "plan-revision": {"goal_ref", "arm_run_ref", "prior_plan_ref", "trigger", "task_node_refs", "dependency_edges", "graph_digest", "acceptance_gate_ids", "budget_limits", "read_resource_paths", "write_resource_paths", "effect_keys", "source_digests", "verifier_policy_ref", "constitution_policy_ref", "assigned_plan_verifier_actor_id", "change_rationale_digest", "dispositions"},
    "plan-transition": {"from_plan_ref", "to_plan_ref", "trigger_evidence_digests", "dispositions", "executive_lease_ref", "expected_goal_fence_epoch", "checkpoint_ref"},
    "plan-verifier-decision": {"goal_ref", "plan_ref", "candidate_digest", "event_log_digest", "gate_result_digests", "evidence_digests", "verifier_policy_ref", "constitution_policy_ref", "independence", "disposition", "assurance_grade", "owner_exception_ref", "dissent_digest", "limitation_digests"},
    "replan-decision": {"goal_ref", "old_plan_ref", "new_plan_ref", "trigger_evidence_digests", "dispositions", "narrowed_binding_digest", "blocker_ref", "escalation_ref", "disposition"},
    "retry-decision": {"goal_ref", "plan_ref", "task_ref", "failed_gate_result_ref", "prior_work_packet_ref", "novelty_delta", "novelty_delta_digest", "unchanged_binding_digest", "next_attempt", "remaining_budget", "retry_maximum", "disposition"},
    "route-assignment": {"logical_actor_id", "route_id", "route_capability_ids", "account_class_id", "capacity_pool_id", "privacy_policy_ref", "egress_policy_ref", "route_policy_ref", "metered_spend_allowed", "capacity_observation_ref", "supersedes_route_ref", "assigned_logical_time", "expires_logical_time", "state"},
    "task-node": {"plan_ref", "semantic_definition_digest", "output_schema_digest", "dependency_node_ids", "input_digests", "source_digests", "read_resource_paths", "write_resource_paths", "effect_keys", "authority_ids", "capability_ids", "allowed_action_ids", "prohibition_ids", "route_ids", "route_capability_ids", "account_class_ids", "capacity_pool_ids", "authority_policy_ref", "privacy_class_id", "egress_class_id", "privacy_policy_ref", "egress_policy_ref", "budget_limits", "gate_requirements", "retry_policy_ref", "verifier_policy_ref", "constitution_policy_ref", "terminal_contribution_digest", "retry_maximum"},
    "work-lease": {"subject_actor_id", "goal_ref", "plan_ref", "task_ref", "supersedes_lease_ref", "lease_epoch", "nonce_digest", "issued_logical_time", "expires_logical_time", "allowed_command_types", "state"},
    "work-packet": {"goal_ref", "plan_ref", "task_ref", "supersedes_packet_ref", "logical_worker_actor_id", "attempt", "work_lease_ref", "expected_goal_fence_epoch", "expected_pause_epoch", "authority_ids", "capability_ids", "allowed_resource_paths", "effect_keys", "privacy_class_id", "egress_class_id", "authority_policy_ref", "privacy_policy_ref", "egress_policy_ref", "verifier_policy_ref", "constitution_policy_ref", "route_assignment_ref", "budget_reservation_ref", "budget_limits", "source_digests", "gate_requirements", "allowed_action_ids", "prohibition_ids", "inherited_non_goals", "output_schema_digest", "deadline_logical_time", "checkpoint_ref"},
    "worker-result": {"work_packet_ref", "candidate_digest", "evidence_digests", "output_digest", "observed_effect_ref", "disposition"},
}

EVENT_PRIMARY_TYPES = {
    "OwnerIntentRecorded": "owner-intent", "GoalSpecAccepted": "goal-spec",
    "GoalSpecAmended": "goal-spec-amendment",
    "OwnerVerifierExceptionRecorded": "owner-verifier-exception",
    "ArmRunStarted": "arm-run", "PlanRevisionAdmitted": "plan-revision",
    "WorkPacketDerived": "work-packet", "WorkLeaseReplaced": "work-lease",
    "ExecutiveLeaseReplaced": "executive-lease",
    "RouteAssignmentReplaced": "route-assignment",
    "CapacityObservationRecorded": "capacity-observation",
    "CheckpointRecorded": "checkpoint", "BudgetReserved": "budget-reservation",
    "WorkerResultRecorded": "worker-result", "GateResultRecorded": "gate-result",
    "RetryAdmitted": "retry-decision", "ReplanAdmitted": "replan-decision",
    "BlockerRaised": "blocker", "EscalationRaised": "escalation",
    "PlanVerifierDecisionRecorded": "plan-verifier-decision",
    "PauseApplied": "pause-directive", "EffectObservationRecorded": "effect-observation",
    "PlanItemDisposed": "disposition", "PacketFenced": "disposition",
    "LeaseFenced": "disposition", "EffectDisposed": "disposition",
    "EvidenceDisposed": "disposition",
}

SIMPLE_COMMAND_TYPES = {
    "record-owner-intent": "owner-intent", "accept-goal-spec": "goal-spec",
    "propose-retry": "retry-decision", "raise-blocker": "blocker",
    "raise-escalation": "escalation", "submit-worker-result": "worker-result",
    "submit-gate-result": "gate-result",
    "submit-plan-verifier-decision": "plan-verifier-decision",
    "record-owner-verifier-exception": "owner-verifier-exception",
    "record-capacity-observation": "capacity-observation",
    "record-checkpoint": "checkpoint", "reserve-budget": "budget-reservation",
    "record-effect-observation": "effect-observation", "apply-pause": "pause-directive",
}
COMMAND_COMPOSITE_FIELDS = {
    "start-arm-run": {"executive_lease", "arm_run"},
    "amend-goal-spec": {"new_goal_spec", "amendment", "replacement_plan_revision", "plan_transition", "task_nodes", "arm_proposal_attestation_id"},
    "propose-plan-revision": {"plan_revision", "plan_transition", "task_nodes"},
    "derive-work-packet": {"task_ref", "attempt", "work_lease", "route_assignment", "budget_reservation_ref", "expected_work_packet_digest"},
    "replace-work-lease": {"old_lease_ref", "new_lease"},
    "replace-executive-lease": {"old_lease_ref", "new_lease"},
    "replace-route": {"old_route_ref", "new_route", "checkpoint_ref", "expected_new_packet_digest"},
    "propose-replan": {"decision", "new_plan_revision", "transition", "task_nodes", "route_assignments", "budget_reservations"},
}
COMMAND_RECORD_FIELDS = {
    "start-arm-run": {"executive_lease": "executive-lease", "arm_run": "arm-run"},
    "amend-goal-spec": {"new_goal_spec": "goal-spec", "amendment": "goal-spec-amendment", "replacement_plan_revision": "plan-revision", "plan_transition": "plan-transition"},
    "propose-plan-revision": {"plan_revision": "plan-revision", "plan_transition": "plan-transition"},
    "derive-work-packet": {"work_lease": "work-lease", "route_assignment": "route-assignment"},
    "replace-work-lease": {"new_lease": "work-lease"},
    "replace-executive-lease": {"new_lease": "executive-lease"},
    "replace-route": {"new_route": "route-assignment"},
    "propose-replan": {"decision": "replan-decision", "new_plan_revision": "plan-revision", "transition": "plan-transition"},
}
COMMAND_RECORD_LIST_FIELDS = {
    "amend-goal-spec": {"task_nodes": "task-node"},
    "propose-plan-revision": {"task_nodes": "task-node"},
    "propose-replan": {"task_nodes": "task-node", "route_assignments": "route-assignment", "budget_reservations": "budget-reservation"},
}

EVENT_RELATED_RULES = {
    "ArmRunStarted": ({"executive-lease"}, 1, 1),
    "GoalSpecAmended": ({"goal-spec"}, 1, 1),
    "PlanRevisionAdmitted": ({"plan-transition", "task-node"}, 2, 257),
    "ReplanAdmitted": ({"route-assignment", "budget-reservation"}, 2, 512),
    "WorkPacketDerived": ({"route-assignment", "work-lease"}, 0, 2),
}

OBJECT_REF_FIELDS = {"object_type", "id", "version", "content_digest"}
LEASE_REF_FIELDS = {"lease_id", "lease_epoch", "content_digest"}
POLICY_REF_FIELDS = {"policy_id", "policy_version", "content_digest"}
RECEIPT_IDENTITY_FIELDS = {"receipt_id", "command_id", "command_digest"}
BUDGET_FIELDS = {"model_turns", "attempts", "tokens", "money_minor", "cpu_ms", "memory_bytes", "output_bytes"}
DISPOSITION_FIELDS = {"object_ref", "disposition", "replacement_ref", "reason_digest"}
DEPENDENCY_EDGE_FIELDS = {"from_node_id", "to_node_id"}
EVIDENCE_REF_FIELDS = {"evidence_id", "evidence_type", "locator", "content_digest", "schema_digest"}
GATE_REQUIREMENT_FIELDS = {"gate_id", "procedure_digest", "evidence_schema_digest", "candidate_binding_digest", "verifier_policy_digest", "assigned_verifier_actor_id"}
INDEPENDENCE_FIELDS = {"model_family", "provider", "host", "session", "evidence_store", "implementation_actor_id", "verifier_actor_id", "missing_dimensions"}
CANONICAL_DIFF_ENTRY_FIELDS = {"path_segments", "old_value", "new_value"}
CANONICAL_VALUE_FIELDS = {"state", "digest"}
GOAL_COMPATIBILITY_REF_FIELDS = {"goal_id", "version", "content_digest", "fence_epoch"}
PRIOR_RESULT_FIELDS = {"state", "candidate_digest", "evidence_digests", "gate_result_digests", "plan_verifier_decision_digest", "assurance_grade"}
PRIOR_EFFECT_FIELDS = {"state", "effect_id", "intent_digest", "receipt_digest"}
COMPATIBILITY_INPUT_FIELDS = {"compatibility_schema_version", "old_goal_ref", "new_goal_ref", "old_node", "new_node", "replacement_node_id", "replacement_node_digest", "prior_result", "prior_effect", "consumed_budget"}
NODE_COMPATIBILITY_FIELDS = {"node_id", "node_version", "node_digest", "semantic_definition_digest", "output_schema_digest", "dependency_node_ids", "input_digests", "source_digests", "read_resource_paths", "write_resource_paths", "effect_keys", "authority_ids", "capability_ids", "allowed_action_ids", "prohibition_ids", "route_ids", "route_capability_ids", "account_class_ids", "capacity_pool_ids", "privacy_class_id", "egress_class_id", "authority_policy_digest", "privacy_policy_digest", "egress_policy_digest", "budget_limits", "gate_requirements", "retry_policy_digest", "verifier_policy_digest", "constitution_policy_digest", "terminal_contribution_digest", "retry_maximum"}
ERROR_WITNESS_FIELDS = {"witness_schema_version", "error_code", "object_ids", "dependency_edges", "field_paths", "content_digest"}
NOVELTY_COMMON_FIELDS = {"novelty_schema_version", "delta_id", "prior_attempt_digest", "addresses_failure_ids", "old_descriptor_digest", "new_descriptor_digest", "canonical_diff", "canonical_diff_digest", "delta_digest", "kind"}
NOVELTY_KIND_FIELDS = {
    "evidence-added": {"old_evidence_refs", "new_evidence_refs"},
    "hypothesis-replaced": {"old_hypothesis", "new_hypothesis", "changed_proposition_ids"},
    "strategy-changed": {"old_strategy", "new_strategy", "changed_operation_ids"},
    "route-capability-changed": {"old_route_descriptor", "new_route_descriptor", "newly_observed_capability_ids"},
    "task-narrowed": {"old_scope", "new_scope"},
    "dependency-repaired": {"predicate_id", "predicate_version", "predicate_input_schema_digest", "old_input", "new_input", "old_result_digest", "repair_receipt_digest", "new_result_digest"},
    "owner-amended": {"old_goal_ref", "new_goal_ref", "accepted_owner_command_receipt_digest", "amendment_disposition_digest", "retained_node_id", "compatibility_input_digest"},
}

ID_PATTERN = r"^[A-Z][A-Z0-9]*(?:-[A-Za-z0-9][A-Za-z0-9._-]*)+$"
TOKEN_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"
RESOURCE_PATH_PATTERN = r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$"
EVIDENCE_ID_PATTERN = r"^evidence-[a-z0-9][a-z0-9-]{0,62}$"
DELTA_ID_PATTERN = r"^delta-[a-z0-9][a-z0-9-]{0,62}$"
PROPOSITION_ID_PATTERN = r"^proposition-[a-z0-9][a-z0-9-]{0,62}$"
OPERATION_ID_PATTERN = r"^operation-[a-z0-9][a-z0-9-]{0,62}$"
CAPABILITY_ID_PATTERN = r"^capability-[a-z0-9][a-z0-9-]{0,62}$"

COMMAND_TYPES = set(SIMPLE_COMMAND_TYPES) | set(COMMAND_COMPOSITE_FIELDS)
ERROR_CODES = {
    "E-SCHEMA-UNKNOWN-FIELD", "E-SCHEMA-MISSING-FIELD", "E-SCHEMA-UNKNOWN-VERSION",
    "E-SCHEMA-INVALID", "E-SCHEMA-DUPLICATE", "E-SCHEMA-BOUND", "E-DIGEST-MISMATCH",
    "E-AUTH-MISSING", "E-AUTH-PARENT-MUTATION", "E-AUTH-ROLE", "E-SCOPE-WIDEN",
    "E-BUDGET-WIDEN", "E-DAG-CYCLE", "E-DAG-DEPENDENCY", "E-DAG-CONFLICT",
    "E-STALE-GOAL", "E-STALE-PLAN", "E-STALE-LEASE", "E-GATE-MISSING",
    "E-VERIFIER-MISSING", "E-RETRY-NONNOVEL", "E-RETRY-EXHAUSTED", "E-ROUTE-SPEND",
    "E-PAUSED", "E-EFFECT-AMBIGUOUS", "E-IDEMPOTENCY-CONFLICT",
    "E-AMENDMENT-INCOMPLETE", "E-POLICY-UNKNOWN",
}
DOMAIN_ENUM_FIELDS = {
    ("owner-intent", "source_kind"): {"owner-prompt", "owner-amendment"},
    ("goal-spec", "state"): {"accepted"},
    ("goal-spec-amendment", "checkpoint_rule"): {"current-immediate", "safe-checkpoint"},
    ("plan-revision", "trigger"): {"initial", "retry", "replan", "amendment"},
    ("worker-result", "disposition"): {"submitted", "failed"},
    ("gate-result", "disposition"): {"pass", "fail", "error", "missing", "ambiguous"},
    ("retry-decision", "disposition"): {"admitted", "refused", "exhausted"},
    ("replan-decision", "disposition"): {"admitted", "refused"},
    ("blocker", "kind"): {"authority", "capacity", "dependency", "effect-ambiguity", "gate", "policy", "provider-refusal", "source", "verifier"},
    ("blocker", "state"): {"open", "deferred", "resolved", "superseded", "escalated"},
    ("escalation", "target_role"): {"head", "owner"},
    ("escalation", "state"): {"raised", "acknowledged", "resolved", "declined", "superseded"},
    ("plan-verifier-decision", "disposition"): {"pass", "downgraded-pass", "fail", "blocked"},
    ("plan-verifier-decision", "assurance_grade"): {"full", "downgraded", "none"},
    ("executive-lease", "state"): {"active", "expired", "revoked", "replaced"},
    ("work-lease", "state"): {"active", "expired", "revoked", "replaced"},
    ("route-assignment", "state"): {"assigned", "deferred", "released", "expired"},
    ("capacity-observation", "spend_class"): {"none", "included"},
    ("budget-reservation", "state"): {"reserved", "released", "consumed"},
    ("pause-directive", "mode"): {"current-immediate", "checkpoint", "interrupt", "drain"},
    ("effect-observation", "dispatch_state"): {"undispatched", "dispatched"},
    ("effect-observation", "observation"): {"observed-success", "observed-failure", "ambiguous"},
    ("disposition", "disposition"): {"retain-definition", "retain-terminal", "invalidate", "quarantine", "supersede", "cancel", "fence"},
}
NULLABLE_DOMAIN_REFS = {
    ("owner-intent", "supersedes_ref"), ("plan-revision", "prior_plan_ref"),
    ("plan-transition", "from_plan_ref"), ("plan-transition", "checkpoint_ref"),
    ("work-packet", "supersedes_packet_ref"), ("work-packet", "checkpoint_ref"),
    ("worker-result", "observed_effect_ref"), ("replan-decision", "blocker_ref"),
    ("replan-decision", "escalation_ref"), ("blocker", "retry_after_observation_ref"),
    ("plan-verifier-decision", "owner_exception_ref"),
    ("executive-lease", "plan_ref"), ("executive-lease", "task_ref"),
    ("executive-lease", "supersedes_lease_ref"),
    ("work-lease", "plan_ref"), ("work-lease", "task_ref"),
    ("work-lease", "supersedes_lease_ref"),
    ("route-assignment", "supersedes_route_ref"),
    ("checkpoint", "active_goal_ref"), ("checkpoint", "active_plan_ref"),
}
NULLABLE_DOMAIN_DIGESTS = {
    ("plan-verifier-decision", "dissent_digest"),
    ("effect-observation", "receipt_digest"),
    ("effect-observation", "provider_operation_id_digest"),
}


class M1AError(ValueError):
    """A closed, stable M1A failure rather than a host-language parser exception."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def _parse_int(raw: str) -> int:
    if raw == "-0":
        raise M1AError("E-SCHEMA-INVALID", "lexical negative zero")
    digits = raw[1:] if raw.startswith("-") else raw
    if len(digits) > 16:
        raise M1AError("E-SCHEMA-BOUND", "integer bound exceeded")
    try:
        value = int(raw)
    except ValueError as exc:
        raise M1AError("E-SCHEMA-INVALID", "invalid integer") from exc
    if abs(value) > MAX_IJSON_INTEGER:
        raise M1AError("E-SCHEMA-BOUND", "integer bound exceeded")
    return value


def _reject_float(_raw: str):
    raise M1AError("E-SCHEMA-INVALID", "floating-point value")


def _reject_constant(raw: str):
    raise M1AError("E-SCHEMA-INVALID", f"non-I-JSON constant {raw}")


def _pairs_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise M1AError("E-SCHEMA-DUPLICATE", f"duplicate key {key}")
        result[key] = value
    return result


def _check_tree(value, *, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise M1AError("E-SCHEMA-BOUND", "nesting depth exceeded")
    if isinstance(value, str):
        if "\x00" in value:
            raise M1AError("E-SCHEMA-INVALID", "NUL string")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise M1AError("E-SCHEMA-INVALID", "unpaired surrogate") from exc
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, int):
        if abs(value) > MAX_IJSON_INTEGER:
            raise M1AError("E-SCHEMA-BOUND", "integer bound exceeded")
    elif isinstance(value, list):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise M1AError("E-SCHEMA-BOUND", "array count exceeded")
        for item in value:
            _check_tree(item, depth=depth + 1)
    elif isinstance(value, dict):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise M1AError("E-SCHEMA-BOUND", "object count exceeded")
        for key, item in value.items():
            _check_tree(key, depth=depth + 1)
            _check_tree(item, depth=depth + 1)
    else:
        raise M1AError("E-SCHEMA-INVALID", "unsupported JSON value")


def strict_loads(raw: str | bytes):
    if isinstance(raw, bytes):
        if len(raw) > MAX_INPUT_BYTES:
            raise M1AError("E-SCHEMA-BOUND", "input byte bound exceeded")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise M1AError("E-SCHEMA-INVALID", "invalid UTF-8") from exc
    elif isinstance(raw, str):
        try:
            size = len(raw.encode("utf-8", errors="strict"))
        except UnicodeEncodeError as exc:
            raise M1AError("E-SCHEMA-INVALID", "unpaired surrogate") from exc
        if size > MAX_INPUT_BYTES:
            raise M1AError("E-SCHEMA-BOUND", "input byte bound exceeded")
        text = raw
    else:
        raise M1AError("E-SCHEMA-INVALID", "input must be bytes or text")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_no_duplicates,
            parse_int=_parse_int,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except M1AError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise M1AError("E-SCHEMA-INVALID", "malformed JSON") from exc
    _check_tree(value)
    return value


def _require_closed(value: dict, allowed: set[str]) -> None:
    if any(not isinstance(key, str) for key in value):
        raise M1AError("E-SCHEMA-UNKNOWN-FIELD", "non-text object key")
    unknown = set(value) - allowed
    if unknown:
        raise M1AError("E-SCHEMA-UNKNOWN-FIELD", min(unknown))
    missing = allowed - set(value)
    if missing:
        raise M1AError("E-SCHEMA-MISSING-FIELD", min(missing))


def _require_dict(value, detail: str) -> dict:
    if not isinstance(value, dict):
        raise M1AError("E-SCHEMA-INVALID", detail)
    return value


def _invalid(detail: str) -> None:
    raise M1AError("E-SCHEMA-INVALID", detail)


def _validate_enum(value, allowed, detail: str) -> None:
    if not isinstance(value, str) or value not in allowed:
        _invalid(detail)


def _validate_id(value, detail: str = "id", *, maximum: int = 128) -> None:
    if (not isinstance(value, str) or not 3 <= len(value) <= maximum
            or re.fullmatch(ID_PATTERN, value) is None):
        _invalid(detail)


def _validate_token(value, detail: str = "token") -> None:
    if (not isinstance(value, str) or not 1 <= len(value) <= 64
            or re.fullmatch(TOKEN_PATTERN, value) is None):
        _invalid(detail)


def _validate_digest(value, detail: str = "digest", *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or re.fullmatch(DIGEST_PATTERN, value) is None:
        _invalid(detail)


def _validate_uint(value, detail: str = "uint", *, positive: bool = False) -> None:
    minimum = 1 if positive else 0
    maximum = 2_147_483_647 if positive else MAX_IJSON_INTEGER
    if (not isinstance(value, int) or isinstance(value, bool)
            or not minimum <= value <= maximum):
        _invalid(detail)


def _validate_text(value, detail: str = "text", *, nonempty: bool = False,
                   maximum_bytes: int = 4096) -> None:
    if not isinstance(value, str) or (nonempty and not value):
        _invalid(detail)
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise M1AError("E-SCHEMA-INVALID", detail) from exc
    if "\x00" in value or len(value) > maximum_bytes or len(encoded) > maximum_bytes:
        raise M1AError("E-SCHEMA-BOUND", detail)


def _validate_resource_path(value, detail: str = "resource path") -> None:
    if (not isinstance(value, str) or len(value) > 4096
            or len(value.encode("utf-8")) > 4096
            or re.fullmatch(RESOURCE_PATH_PATTERN, value) is None):
        _invalid(detail)


def _validate_array(value, item_validator, detail: str, *, minimum: int = 0,
                    maximum: int = 256, sort_key=None, unique_key=None) -> None:
    if not isinstance(value, list):
        _invalid(detail)
    if not minimum <= len(value) <= maximum:
        raise M1AError("E-SCHEMA-BOUND", detail)
    for item in value:
        item_validator(item)
    key = sort_key or (lambda item: item)
    unique = unique_key or key
    try:
        if value != sorted(value, key=key):
            _invalid(detail + " order")
        keys = [unique(item) for item in value]
        if len(keys) != len(set(keys)):
            raise M1AError("E-SCHEMA-DUPLICATE", detail)
    except TypeError as exc:
        raise M1AError("E-SCHEMA-INVALID", detail) from exc


def _validate_canonical_size(value, maximum: int = 262_144) -> None:
    if len(canonical_bytes(value)) > maximum:
        raise M1AError("E-SCHEMA-BOUND", "canonical byte bound exceeded")


def _validate_id_set(value, detail: str) -> None:
    _validate_array(value, _validate_id, detail)


def _validate_token_set(value, detail: str) -> None:
    _validate_array(value, _validate_token, detail)


def _validate_digest_set(value, detail: str) -> None:
    _validate_array(value, _validate_digest, detail)


def _validate_text_set(value, detail: str) -> None:
    _validate_array(value, _validate_text, detail)


def _validate_resource_path_set(value, detail: str) -> None:
    _validate_array(value, _validate_resource_path, detail)


def _validate_object_ref(value) -> None:
    ref = _require_dict(value, "object ref")
    _require_closed(ref, OBJECT_REF_FIELDS)
    _validate_token(ref["object_type"], "object ref type")
    _validate_id(ref["id"], "object ref id")
    _validate_uint(ref["version"], "object ref version", positive=True)
    _validate_digest(ref["content_digest"], "object ref digest")


def _validate_plan_identity_ref(value) -> None:
    ref = _require_dict(value, "plan identity ref")
    _require_closed(ref, {"object_type", "id", "version"})
    if ref["object_type"] != "plan-revision":
        _invalid("plan identity type")
    _validate_id(ref["id"], "plan identity id")
    _validate_uint(ref["version"], "plan identity version", positive=True)


def _validate_lease_ref(value) -> None:
    ref = _require_dict(value, "lease ref")
    _require_closed(ref, LEASE_REF_FIELDS)
    _validate_id(ref["lease_id"], "lease id")
    _validate_uint(ref["lease_epoch"], "lease epoch")
    _validate_digest(ref["content_digest"], "lease digest")


def _validate_policy_ref(value) -> None:
    ref = _require_dict(value, "policy ref")
    _require_closed(ref, POLICY_REF_FIELDS)
    _validate_id(ref["policy_id"], "policy id")
    _validate_uint(ref["policy_version"], "policy version", positive=True)
    _validate_digest(ref["content_digest"], "policy digest")


def _validate_receipt_identity(value) -> None:
    identity = _require_dict(value, "receipt identity")
    _require_closed(identity, RECEIPT_IDENTITY_FIELDS)
    _validate_id(identity["receipt_id"], "receipt id")
    _validate_id(identity["command_id"], "command id", maximum=120)
    _validate_digest(identity["command_digest"], "command digest")


def _validate_budget(value) -> None:
    budget = _require_dict(value, "budget")
    _require_closed(budget, BUDGET_FIELDS)
    for field in BUDGET_FIELDS:
        _validate_uint(budget[field], "budget " + field)


def _validate_dependency_edge(value) -> None:
    edge = _require_dict(value, "dependency edge")
    _require_closed(edge, DEPENDENCY_EDGE_FIELDS)
    _validate_id(edge["from_node_id"], "dependency from")
    _validate_id(edge["to_node_id"], "dependency to")


def _validate_disposition(value) -> None:
    disposition = _require_dict(value, "disposition")
    _require_closed(disposition, DISPOSITION_FIELDS)
    _validate_object_ref(disposition["object_ref"])
    _validate_enum(
        disposition["disposition"],
        DOMAIN_ENUM_FIELDS[("disposition", "disposition")],
        "disposition value",
    )
    if disposition["replacement_ref"] is not None:
        _validate_object_ref(disposition["replacement_ref"])
    _validate_digest(disposition["reason_digest"], "disposition reason")


def _validate_evidence_ref(value) -> None:
    evidence = _require_dict(value, "evidence ref")
    _require_closed(evidence, EVIDENCE_REF_FIELDS)
    evidence_id = evidence["evidence_id"]
    if not isinstance(evidence_id, str) or re.fullmatch(EVIDENCE_ID_PATTERN, evidence_id) is None:
        _invalid("evidence id")
    _validate_enum(
        evidence["evidence_type"],
        {"gate-output", "fixture-observation", "synthetic-source", "repair-receipt"},
        "evidence type",
    )
    if evidence["locator"] != "fixture://evidence/" + evidence_id:
        _invalid("evidence locator")
    _validate_digest(evidence["content_digest"], "evidence digest")
    _validate_digest(evidence["schema_digest"], "evidence schema digest")


def _validate_gate_requirement(value) -> None:
    gate = _require_dict(value, "gate requirement")
    _require_closed(gate, GATE_REQUIREMENT_FIELDS)
    _validate_id(gate["gate_id"], "gate id")
    for field in ("procedure_digest", "evidence_schema_digest", "candidate_binding_digest", "verifier_policy_digest"):
        _validate_digest(gate[field], field)
    _validate_id(gate["assigned_verifier_actor_id"], "assigned verifier")


def _validate_independence(value) -> None:
    independence = _require_dict(value, "independence")
    _require_closed(independence, INDEPENDENCE_FIELDS)
    for field in ("model_family", "provider", "host", "session", "evidence_store"):
        _validate_token(independence[field], field)
    _validate_id(independence["implementation_actor_id"], "implementation actor")
    _validate_id(independence["verifier_actor_id"], "verifier actor")
    _validate_token_set(independence["missing_dimensions"], "missing dimensions")


def _validate_canonical_value(value) -> None:
    item = _require_dict(value, "canonical value")
    _require_closed(item, CANONICAL_VALUE_FIELDS)
    if item["state"] == "present":
        _validate_digest(item["digest"], "canonical value digest")
    elif item["state"] == "absent":
        if item["digest"] is not None:
            _invalid("absent canonical value digest")
    else:
        _invalid("canonical value state")


def _validate_canonical_diff_entry(value) -> None:
    entry = _require_dict(value, "canonical diff entry")
    _require_closed(entry, CANONICAL_DIFF_ENTRY_FIELDS)
    _validate_array(
        entry["path_segments"], _validate_text, "canonical diff path",
        maximum=64, sort_key=lambda _item: 0, unique_key=lambda item: item,
    )
    _validate_canonical_value(entry["old_value"])
    _validate_canonical_value(entry["new_value"])


def _validate_pattern_id(value, pattern: str, detail: str) -> None:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        _invalid(detail)


def _validate_hypothesis(value) -> None:
    hypothesis = _require_dict(value, "hypothesis")
    _require_closed(hypothesis, {"hypothesis_digest", "propositions"})
    _validate_digest(hypothesis["hypothesis_digest"], "hypothesis digest")

    def validate_proposition(value) -> None:
        proposition = _require_dict(value, "proposition")
        _require_closed(proposition, {"proposition_id", "proposition_digest"})
        _validate_pattern_id(
            proposition["proposition_id"], PROPOSITION_ID_PATTERN, "proposition id",
        )
        _validate_digest(proposition["proposition_digest"], "proposition digest")

    _validate_array(
        hypothesis["propositions"], validate_proposition, "propositions",
        minimum=1, sort_key=lambda item: item["proposition_id"],
        unique_key=lambda item: item["proposition_id"],
    )


def _validate_strategy(value) -> None:
    strategy = _require_dict(value, "strategy")
    _require_closed(
        strategy,
        {
            "strategy_digest", "operations", "edges", "goal_ceiling_digest",
            "resource_ceiling_digest", "effect_ceiling_digest",
        },
    )

    def validate_operation(value) -> None:
        operation = _require_dict(value, "strategy operation")
        _require_closed(operation, {"operation_id", "operation_digest"})
        _validate_pattern_id(operation["operation_id"], OPERATION_ID_PATTERN, "operation id")
        _validate_digest(operation["operation_digest"], "operation digest")

    def validate_edge(value) -> None:
        edge = _require_dict(value, "strategy edge")
        _require_closed(edge, {"from_operation_id", "to_operation_id"})
        _validate_pattern_id(edge["from_operation_id"], OPERATION_ID_PATTERN, "edge from")
        _validate_pattern_id(edge["to_operation_id"], OPERATION_ID_PATTERN, "edge to")

    _validate_digest(strategy["strategy_digest"], "strategy digest")
    _validate_array(
        strategy["operations"], validate_operation, "strategy operations",
        sort_key=lambda item: item["operation_id"],
        unique_key=lambda item: item["operation_id"],
    )
    _validate_array(
        strategy["edges"], validate_edge, "strategy edges",
        sort_key=lambda item: (item["from_operation_id"], item["to_operation_id"]),
        unique_key=lambda item: (item["from_operation_id"], item["to_operation_id"]),
    )
    for field in ("goal_ceiling_digest", "resource_ceiling_digest", "effect_ceiling_digest"):
        _validate_digest(strategy[field], field)


def _validate_route_descriptor(value) -> None:
    descriptor = _require_dict(value, "route descriptor")
    _require_closed(
        descriptor,
        {
            "descriptor_digest", "route_id", "account_class_id", "capacity_pool_id",
            "spend_class", "capability_ids", "observation_digest",
            "allowed_envelope_digest",
        },
    )
    _validate_digest(descriptor["descriptor_digest"], "route descriptor digest")
    for field in ("route_id", "account_class_id", "capacity_pool_id"):
        _validate_id(descriptor[field], field)
    _validate_enum(descriptor["spend_class"], {"none", "included"}, "route descriptor spend class")
    _validate_id_set(descriptor["capability_ids"], "route descriptor capabilities")
    _validate_digest(descriptor["observation_digest"], "route observation digest")
    _validate_digest(descriptor["allowed_envelope_digest"], "route envelope digest")


def _validate_scope_descriptor(value) -> None:
    scope = _require_dict(value, "scope descriptor")
    fields = {
        "scope_digest", "authority_ids", "capability_ids", "allowed_action_ids",
        "prohibition_ids", "route_ids", "route_capability_ids", "account_class_ids",
        "capacity_pool_ids", "read_resource_paths", "write_resource_paths",
        "effect_keys", "privacy_class_id", "egress_class_id", "authority_policy_digest",
        "privacy_policy_digest", "egress_policy_digest", "budget_limits",
        "required_output_schema_digest", "gate_requirements",
    }
    _require_closed(scope, fields)
    for field in (
        "scope_digest", "authority_policy_digest", "privacy_policy_digest",
        "egress_policy_digest", "required_output_schema_digest",
    ):
        _validate_digest(scope[field], field)
    for field in (
        "authority_ids", "capability_ids", "allowed_action_ids", "prohibition_ids",
        "route_ids", "route_capability_ids", "account_class_ids", "capacity_pool_ids",
        "effect_keys",
    ):
        _validate_id_set(scope[field], field)
    for field in ("read_resource_paths", "write_resource_paths"):
        _validate_resource_path_set(scope[field], field)
    _validate_id(scope["privacy_class_id"], "scope privacy class")
    _validate_id(scope["egress_class_id"], "scope egress class")
    _validate_budget(scope["budget_limits"])
    _validate_array(
        scope["gate_requirements"], _validate_gate_requirement, "scope gate requirements",
        sort_key=lambda item: item["gate_id"], unique_key=lambda item: item["gate_id"],
    )


def _validate_dependency_input(value) -> None:
    item = _require_dict(value, "dependency input")
    _require_closed(item, {"required_artifact_digest", "observed_artifact_digests"})
    _validate_digest(item["required_artifact_digest"], "required artifact digest")
    _validate_digest_set(item["observed_artifact_digests"], "observed artifact digests")


def _validate_novelty(value) -> None:
    novelty = _require_dict(value, "novelty delta")
    if novelty.get("novelty_schema_version") != "novelty-delta-1":
        raise M1AError("E-SCHEMA-UNKNOWN-VERSION", str(novelty.get("novelty_schema_version")))
    kind = novelty.get("kind")
    if not isinstance(kind, str):
        _invalid("novelty kind")
    kind_fields = NOVELTY_KIND_FIELDS.get(kind)
    if kind_fields is None:
        _invalid("novelty kind")
    _require_closed(novelty, NOVELTY_COMMON_FIELDS | kind_fields)
    _validate_pattern_id(novelty["delta_id"], DELTA_ID_PATTERN, "delta id")
    _validate_digest(novelty["prior_attempt_digest"], "prior attempt digest")
    _validate_array(
        novelty["addresses_failure_ids"], _validate_id, "failure ids", minimum=1,
    )
    for field in ("old_descriptor_digest", "new_descriptor_digest", "canonical_diff_digest", "delta_digest"):
        _validate_digest(novelty[field], field)
    _validate_array(
        novelty["canonical_diff"], _validate_canonical_diff_entry, "canonical diff",
        maximum=2048,
        sort_key=lambda item: tuple(_utf16_key(segment) for segment in item["path_segments"]),
        unique_key=lambda item: tuple(item["path_segments"]),
    )
    kind = novelty["kind"]
    if kind == "evidence-added":
        for field in ("old_evidence_refs", "new_evidence_refs"):
            _validate_array(
                novelty[field], _validate_evidence_ref, field,
                sort_key=lambda item: item["evidence_id"],
                unique_key=lambda item: item["evidence_id"],
            )
    elif kind == "hypothesis-replaced":
        _validate_hypothesis(novelty["old_hypothesis"])
        _validate_hypothesis(novelty["new_hypothesis"])
        _validate_array(
            novelty["changed_proposition_ids"],
            lambda item: _validate_pattern_id(item, PROPOSITION_ID_PATTERN, "proposition id"),
            "changed proposition ids", minimum=1,
        )
    elif kind == "strategy-changed":
        _validate_strategy(novelty["old_strategy"])
        _validate_strategy(novelty["new_strategy"])
        _validate_array(
            novelty["changed_operation_ids"],
            lambda item: _validate_pattern_id(item, OPERATION_ID_PATTERN, "operation id"),
            "changed operation ids", minimum=1,
        )
    elif kind == "route-capability-changed":
        _validate_route_descriptor(novelty["old_route_descriptor"])
        _validate_route_descriptor(novelty["new_route_descriptor"])
        _validate_array(
            novelty["newly_observed_capability_ids"],
            lambda item: _validate_pattern_id(item, CAPABILITY_ID_PATTERN, "capability id"),
            "newly observed capability ids", minimum=1,
        )
    elif kind == "task-narrowed":
        _validate_scope_descriptor(novelty["old_scope"])
        _validate_scope_descriptor(novelty["new_scope"])
    elif kind == "dependency-repaired":
        if novelty["predicate_id"] != "dependency-artifact-present":
            _invalid("predicate id")
        if novelty["predicate_version"] != 1:
            _invalid("predicate version")
        for field in ("predicate_input_schema_digest", "old_result_digest", "repair_receipt_digest", "new_result_digest"):
            _validate_digest(novelty[field], field)
        _validate_dependency_input(novelty["old_input"])
        _validate_dependency_input(novelty["new_input"])
    elif kind == "owner-amended":
        _validate_object_ref(novelty["old_goal_ref"])
        _validate_object_ref(novelty["new_goal_ref"])
        for field in ("accepted_owner_command_receipt_digest", "amendment_disposition_digest", "compatibility_input_digest"):
            _validate_digest(novelty[field], field)
        _validate_id(novelty["retained_node_id"], "retained node")


def _validate_node_compatibility(value) -> None:
    node = _require_dict(value, "node compatibility")
    _require_closed(node, NODE_COMPATIBILITY_FIELDS)
    _validate_id(node["node_id"], "node id")
    _validate_uint(node["node_version"], "node version", positive=True)
    for field in (
        "node_digest", "semantic_definition_digest", "output_schema_digest",
        "authority_policy_digest", "privacy_policy_digest", "egress_policy_digest",
        "retry_policy_digest", "verifier_policy_digest", "constitution_policy_digest",
        "terminal_contribution_digest",
    ):
        _validate_digest(node[field], field)
    for field in (
        "dependency_node_ids", "effect_keys", "authority_ids", "capability_ids",
        "allowed_action_ids", "prohibition_ids", "route_ids", "route_capability_ids",
        "account_class_ids", "capacity_pool_ids",
    ):
        _validate_id_set(node[field], field)
    for field in ("input_digests", "source_digests"):
        _validate_digest_set(node[field], field)
    for field in ("read_resource_paths", "write_resource_paths"):
        _validate_resource_path_set(node[field], field)
    _validate_id(node["privacy_class_id"], "privacy class")
    _validate_id(node["egress_class_id"], "egress class")
    _validate_budget(node["budget_limits"])
    _validate_array(
        node["gate_requirements"], _validate_gate_requirement, "gate requirements",
        sort_key=lambda item: item["gate_id"], unique_key=lambda item: item["gate_id"],
    )
    _validate_uint(node["retry_maximum"], "retry maximum")


def _validate_prior_result(value) -> None:
    result = _require_dict(value, "prior result")
    _require_closed(result, PRIOR_RESULT_FIELDS)
    _validate_enum(
        result["state"],
        {"result-submitted", "gate-pending", "verified", "complete", "failed"},
        "prior result state",
    )
    _validate_digest(result["candidate_digest"], "prior candidate")
    _validate_digest_set(result["evidence_digests"], "prior evidence")
    _validate_digest_set(result["gate_result_digests"], "prior gates")
    _validate_digest(result["plan_verifier_decision_digest"], "prior verifier decision")
    _validate_enum(result["assurance_grade"], {"full", "downgraded", "none"}, "prior assurance grade")


def _validate_prior_effect(value) -> None:
    effect = _require_dict(value, "prior effect")
    _require_closed(effect, PRIOR_EFFECT_FIELDS)
    _validate_enum(
        effect["state"],
        {"none", "prepared-undispatched", "observed-success", "observed-failure", "ambiguous", "reconciled"},
        "prior effect state",
    )
    if effect["effect_id"] is not None:
        _validate_id(effect["effect_id"], "prior effect id")
    _validate_digest(effect["intent_digest"], "prior intent", nullable=True)
    _validate_digest(effect["receipt_digest"], "prior receipt", nullable=True)


def _validate_goal_compatibility_ref(value) -> None:
    ref = _require_dict(value, "goal compatibility ref")
    _require_closed(ref, GOAL_COMPATIBILITY_REF_FIELDS)
    _validate_id(ref["goal_id"], "goal compatibility id")
    _validate_uint(ref["version"], "goal compatibility version", positive=True)
    _validate_digest(ref["content_digest"], "goal compatibility digest")
    _validate_uint(ref["fence_epoch"], "goal compatibility fence")


def _validate_compatibility_input(value) -> None:
    item = _require_dict(value, "compatibility input")
    _require_closed(item, COMPATIBILITY_INPUT_FIELDS)
    if item["compatibility_schema_version"] != "compatibility-input-1":
        raise M1AError("E-SCHEMA-UNKNOWN-VERSION", str(item["compatibility_schema_version"]))
    _validate_goal_compatibility_ref(item["old_goal_ref"])
    _validate_goal_compatibility_ref(item["new_goal_ref"])
    _validate_node_compatibility(item["old_node"])
    _validate_node_compatibility(item["new_node"])
    if item["replacement_node_id"] is not None:
        _validate_id(item["replacement_node_id"], "replacement node id")
    _validate_digest(item["replacement_node_digest"], "replacement node digest", nullable=True)
    if item["prior_result"] is not None:
        _validate_prior_result(item["prior_result"])
    _validate_prior_effect(item["prior_effect"])
    _validate_budget(item["consumed_budget"])


def _validate_error_witness(value) -> None:
    witness = _require_dict(value, "error witness")
    _require_closed(witness, ERROR_WITNESS_FIELDS)
    if witness["witness_schema_version"] != "m1a-error-witness-1":
        raise M1AError("E-SCHEMA-UNKNOWN-VERSION", str(witness["witness_schema_version"]))
    _validate_enum(witness["error_code"], ERROR_CODES, "witness error code")
    _validate_id_set(witness["object_ids"], "witness object ids")
    _validate_array(
        witness["dependency_edges"], _validate_dependency_edge, "witness edges",
        sort_key=lambda item: (item["from_node_id"], item["to_node_id"]),
        unique_key=lambda item: (item["from_node_id"], item["to_node_id"]),
    )
    _validate_text_set(witness["field_paths"], "witness fields")
    _validate_digest(witness["content_digest"], "witness digest")


def _validate_domain_payload(object_type: str, payload: dict) -> None:
    if object_type == "disposition":
        _validate_disposition(payload)
        return
    for field, value in payload.items():
        enum = DOMAIN_ENUM_FIELDS.get((object_type, field))
        if enum is not None:
            _validate_enum(value, enum, f"{object_type}.{field}")
        elif field in {"intent_text", "outcome", "requested_decision"}:
            _validate_text(value, field, nonempty=True)
        elif field in {"publication_allowed", "metered_spend_allowed"}:
            if value is not False:
                _invalid(f"{object_type}.{field}")
        elif field in {"available", "quarantine"}:
            if not isinstance(value, bool):
                _invalid(f"{object_type}.{field}")
        elif field == "plan_ref" and object_type == "task-node":
            _validate_plan_identity_ref(value)
        elif field.endswith("_policy_ref"):
            _validate_policy_ref(value)
        elif field.endswith("_lease_ref") or field == "executive_lease_ref":
            if value is None:
                if (object_type, field) not in NULLABLE_DOMAIN_REFS:
                    _invalid(f"{object_type}.{field}")
            else:
                _validate_lease_ref(value)
        elif field.endswith("_ref"):
            if value is None:
                if (object_type, field) not in NULLABLE_DOMAIN_REFS:
                    _invalid(f"{object_type}.{field}")
            else:
                _validate_object_ref(value)
        elif field in {"task_node_refs", "open_packet_refs", "ambiguous_effect_refs"}:
            _validate_array(
                value, _validate_object_ref, field,
                sort_key=lambda item: item["id"], unique_key=lambda item: item["id"],
            )
        elif field == "dependency_edges":
            _validate_array(
                value, _validate_dependency_edge, field,
                sort_key=lambda item: (item["from_node_id"], item["to_node_id"]),
                unique_key=lambda item: (item["from_node_id"], item["to_node_id"]),
            )
        elif field == "dispositions":
            _validate_array(
                value, _validate_disposition, field,
                sort_key=lambda item: (
                    item["object_ref"]["object_type"], item["object_ref"]["id"],
                    item["object_ref"]["version"],
                ),
                unique_key=lambda item: (
                    item["object_ref"]["object_type"], item["object_ref"]["id"],
                    item["object_ref"]["version"],
                ),
            )
        elif field == "compatibility_inputs":
            _validate_array(
                value, _validate_compatibility_input, field,
                sort_key=lambda item: item["old_node"]["node_id"],
                unique_key=lambda item: item["old_node"]["node_id"],
            )
        elif field == "gate_requirements":
            _validate_array(
                value, _validate_gate_requirement, field,
                sort_key=lambda item: item["gate_id"], unique_key=lambda item: item["gate_id"],
            )
        elif field == "gate_requirement":
            _validate_gate_requirement(value)
        elif field == "independence":
            _validate_independence(value)
        elif field == "novelty_delta":
            _validate_novelty(value)
        elif field in {"budget_limits", "limits", "consumed", "remaining", "remaining_budget"}:
            _validate_budget(value)
        elif field == "allowed_command_types":
            _validate_array(
                value,
                lambda item: _validate_enum(item, COMMAND_TYPES, "allowed command"),
                field,
            )
        elif field in {"read_resource_paths", "write_resource_paths", "allowed_resource_paths"}:
            _validate_resource_path_set(value, field)
        elif field in {"non_goals", "inherited_non_goals", "changed_field_paths"}:
            _validate_text_set(value, field)
        elif field == "missing_dimensions":
            _validate_token_set(value, field)
        elif field.endswith("_ids") or field in {"effect_keys", "allowed_effect_keys"}:
            _validate_id_set(value, field)
        elif field.endswith("_digests"):
            _validate_digest_set(value, field)
        elif field.endswith("_digest"):
            _validate_digest(
                value, field, nullable=(object_type, field) in NULLABLE_DOMAIN_DIGESTS,
            )
        elif field.endswith("_id") or field == "effect_key":
            _validate_id(value, field)
        elif field in {"attempt", "next_attempt", "procedure_version"}:
            _validate_uint(value, field, positive=True)
        elif (field.endswith("_logical_time") or field.endswith("_epoch")
              or field in {"state_version", "remaining_units", "retry_maximum"}):
            _validate_uint(value, field)
        else:
            _invalid(f"unvalidated {object_type}.{field}")

    if object_type == "blocker":
        if payload["state"] == "deferred":
            if payload["kind"] != "capacity" or payload["retry_after_observation_ref"] is None:
                _invalid("deferred blocker recovery")
        elif payload["retry_after_observation_ref"] is not None:
            _invalid("non-deferred blocker recovery")
    elif object_type == "plan-verifier-decision":
        disposition = payload["disposition"]
        assurance = payload["assurance_grade"]
        owner_exception = payload["owner_exception_ref"]
        if disposition == "pass":
            valid = assurance == "full" and owner_exception is None
        elif disposition == "downgraded-pass":
            valid = assurance == "downgraded" and owner_exception is not None
        else:
            valid = assurance == "none" and owner_exception is None
        if not valid:
            _invalid("plan verifier disposition")
    elif object_type == "effect-observation":
        observation = payload["observation"]
        if observation == "observed-success":
            valid = (
                payload["dispatch_state"] == "dispatched"
                and payload["receipt_digest"] is not None
                and payload["provider_operation_id_digest"] is not None
                and payload["quarantine"] is False
            )
        elif observation == "ambiguous":
            valid = payload["quarantine"] is True
        else:
            valid = payload["quarantine"] is False
        if not valid:
            _invalid("effect observation disposition")


def _validate_structural_objects(value) -> None:
    """Recursively close the common nested record structures used by M1A."""

    if isinstance(value, list):
        for item in value:
            _validate_structural_objects(item)
        return
    if not isinstance(value, dict):
        return
    keys = set(value)
    if "receipt_schema_version" in value:
        for item in value.values():
            _validate_structural_objects(item)
        return
    if "schema_version" in value and "object_type" in value:
        validate_domain_record(value)
        return
    if "novelty_schema_version" in value:
        _validate_novelty(value)
        return
    if "lease_id" in value:
        _validate_lease_ref(value)
        return
    if "policy_id" in value:
        _validate_policy_ref(value)
        return
    if "receipt_id" in value:
        _validate_receipt_identity(value)
        return
    if "evidence_id" in value:
        _validate_evidence_ref(value)
        return
    if "gate_id" in value:
        _validate_gate_requirement(value)
        return
    if "compatibility_schema_version" in value:
        _validate_compatibility_input(value)
        return
    if "node_id" in value:
        _validate_node_compatibility(value)
        return
    if "witness_schema_version" in value:
        _validate_error_witness(value)
        return
    if OBJECT_REF_FIELDS.issubset(keys):
        _validate_object_ref(value)
        return
    if {"object_type", "id", "version"}.issubset(keys):
        _validate_plan_identity_ref(value)
        return
    if {"object_ref", "disposition", "replacement_ref", "reason_digest"}.issubset(keys):
        _validate_disposition(value)
        return
    if {"from_node_id", "to_node_id"}.issubset(keys):
        _validate_dependency_edge(value)
        return
    if BUDGET_FIELDS.issubset(keys):
        _validate_budget(value)
        return
    if INDEPENDENCE_FIELDS.issubset(keys):
        _validate_independence(value)
        return
    if CANONICAL_DIFF_ENTRY_FIELDS.issubset(keys):
        _validate_canonical_diff_entry(value)
        return
    if CANONICAL_VALUE_FIELDS.issubset(keys):
        _validate_canonical_value(value)
        return
    if GOAL_COMPATIBILITY_REF_FIELDS.issubset(keys):
        _validate_goal_compatibility_ref(value)
        return
    if PRIOR_RESULT_FIELDS.issubset(keys):
        _validate_prior_result(value)
        return
    if PRIOR_EFFECT_FIELDS.issubset(keys):
        _validate_prior_effect(value)
        return
    for item in value.values():
        _validate_structural_objects(item)


def validate_domain_record(value: dict) -> None:
    """Validate a registered domain arm without consulting ambient schema files."""

    record = _require_dict(value, "domain record must be an object")
    object_type = record.get("object_type")
    if not isinstance(object_type, str):
        _invalid("record object type")
    expected_version = DOMAIN_VERSIONS.get(object_type)
    if expected_version is None or record.get("schema_version") != expected_version:
        raise M1AError(
            "E-SCHEMA-UNKNOWN-VERSION",
            f"{object_type!s}/{record.get('schema_version')!s}",
        )
    _require_closed(record, DOMAIN_FIELDS)
    payload = _require_dict(record.get("payload"), "domain payload must be an object")
    _require_closed(payload, DOMAIN_PAYLOAD_FIELDS[object_type])
    _validate_token(record["object_type"], "record object type")
    _validate_token(record["schema_version"], "record schema version")
    _validate_id(record["id"], "record id")
    _validate_uint(record["version"], "record version", positive=True)
    _validate_id(record["created_by_actor_id"], "record actor")
    if record["created_under_lease"] is not None:
        _validate_lease_ref(record["created_under_lease"])
    _validate_id(record["causation_id"], "record causation")
    _validate_id(record["correlation_id"], "record correlation")
    _validate_uint(record["created_logical_time"], "record logical time")
    _validate_digest(record["content_digest"], "record content digest")
    _check_tree(record)
    _validate_domain_payload(object_type, payload)
    _validate_structural_objects(payload)
    _validate_canonical_size(record)


def validate_event_envelope(value: dict) -> None:
    event = _require_dict(value, "event must be an object")
    if event.get("event_schema_version") != "m1a-event-1":
        raise M1AError("E-SCHEMA-UNKNOWN-VERSION", str(event.get("event_schema_version")))
    _require_closed(event, EVENT_FIELDS)
    _validate_id(event["event_id"], "event id")
    _validate_id(event["stream_id"], "event stream")
    _validate_uint(event["sequence"], "event sequence", positive=True)
    _validate_digest(event["previous_event_digest"], "previous event digest")
    _validate_uint(event["logical_time"], "event logical time")
    _validate_id(event["command_id"], "event command id", maximum=120)
    _validate_digest(event["command_digest"], "event command digest")
    _validate_digest(event["state_delta_digest"], "event delta digest")
    _validate_digest(event["content_digest"], "event content digest")
    event_type = event.get("event_type")
    if not isinstance(event_type, str):
        _invalid("event type")
    expected_primary = EVENT_PRIMARY_TYPES.get(event_type)
    if expected_primary is None:
        raise M1AError("E-SCHEMA-UNKNOWN-VERSION", str(event.get("event_type")))
    primary = _require_dict(event.get("primary_record"), "primary record")
    validate_domain_record(primary)
    if primary["object_type"] != expected_primary:
        raise M1AError("E-SCHEMA-INVALID", "event primary record discriminator")
    related = event.get("related_records")
    if not isinstance(related, list):
        raise M1AError("E-SCHEMA-INVALID", "related records")
    for record in related:
        validate_domain_record(record)
    rule = EVENT_RELATED_RULES.get(event["event_type"])
    if rule is None:
        if related:
            raise M1AError("E-SCHEMA-INVALID", "event related record cardinality")
    else:
        allowed, minimum, maximum = rule
        related_types = {record["object_type"] for record in related}
        if (not minimum <= len(related) <= maximum
                or not related_types.issubset(allowed)
                or (related and related_types != allowed)
                or (not related and minimum > 0)):
            raise M1AError("E-SCHEMA-INVALID", "event related record discriminator")
    serialized = [canonical_bytes(record) for record in related]
    if len(serialized) != len(set(serialized)):
        raise M1AError("E-SCHEMA-DUPLICATE", "event related records")
    if event["event_type"] == "PlanRevisionAdmitted":
        if sum(record["object_type"] == "plan-transition" for record in related) != 1:
            raise M1AError("E-SCHEMA-INVALID", "event transition cardinality")
    elif event["event_type"] == "ReplanAdmitted":
        keys = [(record["object_type"], record["id"], record["version"]) for record in related]
        if len(keys) != len(set(keys)):
            raise M1AError("E-SCHEMA-DUPLICATE", "event related record identity")
    ordered = sorted(related, key=lambda record: (record["object_type"], record["id"], record["version"]))
    if related != ordered:
        raise M1AError("E-SCHEMA-INVALID", "event related record order")
    _validate_canonical_size(event)


def validate_command_envelope(value: dict) -> None:
    command = _require_dict(value, "command must be an object")
    if command.get("command_schema_version") != "m1a-command-1":
        raise M1AError("E-SCHEMA-UNKNOWN-VERSION", str(command.get("command_schema_version")))
    _require_closed(command, COMMAND_FIELDS)
    _check_tree(command)
    command_type = command.get("command_type")
    _validate_id(command["command_id"], "command id", maximum=120)
    if not isinstance(command_type, str) or command_type not in COMMAND_TYPES:
        raise M1AError("E-SCHEMA-UNKNOWN-VERSION", str(command_type))
    _validate_id(command["actor_id"], "command actor")
    _validate_id(command["auth_ref"], "command authority")
    if command["lease_ref"] is not None:
        _validate_lease_ref(command["lease_ref"])
    _validate_uint(command["logical_time"], "command logical time")
    _validate_uint(command["expected_state_version"], "expected state version")
    _validate_digest(command["expected_state_digest"], "expected state digest")
    for field in ("goal_ref", "plan_ref"):
        if command[field] is not None:
            _validate_object_ref(command[field])
    for field in ("payload_digest", "candidate_digest", "content_digest"):
        _validate_digest(command[field], field)
    payload = _require_dict(command.get("payload"), "command payload")
    simple_type = SIMPLE_COMMAND_TYPES.get(command_type)
    if simple_type is not None:
        validate_domain_record(payload)
        if payload["object_type"] != simple_type:
            raise M1AError("E-SCHEMA-INVALID", "command payload discriminator")
        _validate_canonical_size(command)
        return
    fields = COMMAND_COMPOSITE_FIELDS[command_type]
    _require_closed(payload, fields)
    for field, object_type in COMMAND_RECORD_FIELDS[command_type].items():
        record = _require_dict(payload[field], field)
        validate_domain_record(record)
        if record["object_type"] != object_type:
            raise M1AError("E-SCHEMA-INVALID", f"{field} discriminator")
    for field, object_type in COMMAND_RECORD_LIST_FIELDS.get(command_type, {}).items():
        records = payload[field]
        def validate_record(record, expected=object_type, label=field):
            validate_domain_record(record)
            if record["object_type"] != expected:
                raise M1AError("E-SCHEMA-INVALID", f"{label} discriminator")
        _validate_array(
            records, validate_record, field,
            minimum=(1 if command_type == "propose-replan"
                     and field in {"route_assignments", "budget_reservations"} else 0),
            sort_key=lambda item: item["id"], unique_key=lambda item: item["id"],
        )
    if "arm_proposal_attestation_id" in payload:
        _validate_id(payload["arm_proposal_attestation_id"], "arm proposal attestation")
    if "attempt" in payload:
        _validate_uint(payload["attempt"], "packet attempt", positive=True)
    for field in ("expected_work_packet_digest", "expected_new_packet_digest"):
        if field in payload:
            _validate_digest(payload[field], field)
    _validate_structural_objects(payload)
    _validate_canonical_size(command)


def validate_receipt(value: dict) -> None:
    receipt = _require_dict(value, "receipt must be an object")
    if receipt.get("receipt_schema_version") != "m1a-receipt-1":
        raise M1AError("E-SCHEMA-UNKNOWN-VERSION", str(receipt.get("receipt_schema_version")))
    _require_closed(receipt, RECEIPT_FIELDS)
    _validate_id(receipt["receipt_id"], "receipt id")
    _validate_id(receipt["command_id"], "receipt command id", maximum=120)
    _validate_digest(receipt["command_digest"], "receipt command digest")
    if receipt["primary_error"] is not None:
        _validate_enum(receipt["primary_error"], ERROR_CODES, "receipt primary error")
    _validate_array(
        receipt["related_errors"],
        lambda item: _validate_enum(item, ERROR_CODES, "receipt related error"),
        "receipt related errors", maximum=64,
    )
    for field in ("state_before_digest", "state_after_digest", "content_digest"):
        _validate_digest(receipt[field], field)
    _validate_array(receipt["event_ids"], _validate_id, "receipt event ids")
    for field in ("first_event_sequence", "last_event_sequence"):
        if receipt[field] is not None:
            _validate_uint(receipt[field], field)
    if digest(receipt, omit_content_digest=True) != receipt.get("content_digest"):
        raise M1AError("E-DIGEST-MISMATCH", "receipt journal digest")
    disposition = receipt.get("disposition")
    _validate_enum(disposition, {"accepted", "refused", "duplicate", "conflict"}, "receipt disposition")
    if disposition == "accepted":
        if (receipt["primary_error"] is not None or receipt["related_errors"]
                or receipt["witness"] is not None or not receipt["event_ids"]
                or not isinstance(receipt["first_event_sequence"], int)
                or not isinstance(receipt["last_event_sequence"], int)):
            raise M1AError("E-SCHEMA-INVALID", "accepted receipt shape")
    elif disposition in {"refused", "conflict"}:
        if (receipt["primary_error"] is None or receipt["event_ids"]
                or receipt["first_event_sequence"] is not None
                or receipt["last_event_sequence"] is not None):
            raise M1AError("E-SCHEMA-INVALID", "refused receipt shape")
    elif (receipt["primary_error"] is not None or receipt["related_errors"]
          or receipt["witness"] is not None or receipt["event_ids"]
          or receipt["first_event_sequence"] is not None
          or receipt["last_event_sequence"] is not None):
        raise M1AError("E-SCHEMA-INVALID", "duplicate receipt shape")
    _validate_structural_objects(receipt)
    _validate_canonical_size(receipt)


def validate_projection(value: dict) -> None:
    projection = _require_dict(value, "projection must be an object")
    if projection.get("projection_schema_version") != "m1a-projection-1":
        raise M1AError("E-SCHEMA-UNKNOWN-VERSION", str(projection.get("projection_schema_version")))
    _require_closed(projection, PROJECTION_FIELDS)
    _validate_id(projection["stream_id"], "projection stream")
    _validate_uint(projection["state_version"], "state version")
    _validate_uint(projection["last_event_sequence"], "last event sequence")
    _validate_digest(projection["last_event_digest"], "last event digest")
    for field in ("operative_goal_ref", "active_arm_run_ref", "active_plan_ref"):
        if projection[field] is not None:
            _validate_object_ref(projection[field])
    _validate_array(
        projection["records"], validate_domain_record, "projection records", maximum=2048,
        sort_key=lambda item: (item["object_type"], item["id"], item["version"]),
        unique_key=lambda item: (item["object_type"], item["id"], item["version"]),
    )
    def validate_object_state(item):
        state = _require_dict(item, "object state")
        _require_closed(state, {"object_ref", "state"})
        _validate_object_ref(state["object_ref"])
        _validate_token(state["state"], "object state token")
    _validate_array(
        projection["object_states"], validate_object_state, "object states", maximum=2048,
        sort_key=lambda item: (
            item["object_ref"]["object_type"], item["object_ref"]["id"],
            item["object_ref"]["version"],
        ),
        unique_key=lambda item: (
            item["object_ref"]["object_type"], item["object_ref"]["id"],
            item["object_ref"]["version"],
        ),
    )
    _validate_array(
        projection["processed_command_receipts"], _validate_receipt_identity,
        "processed receipts", maximum=2048,
        sort_key=lambda item: item["receipt_id"], unique_key=lambda item: item["receipt_id"],
    )
    _validate_digest_set(projection["consumed_novelty_delta_digests"], "consumed novelty")
    for field in ("open_blocker_refs", "open_escalation_refs", "ambiguous_effect_refs"):
        _validate_array(
            projection[field], _validate_object_ref, field,
            sort_key=lambda item: item["id"], unique_key=lambda item: item["id"],
        )
    _validate_policy_ref(projection["current_pause_policy_ref"])
    _validate_uint(projection["current_pause_epoch"], "current pause epoch")
    _validate_enum(
        projection["terminal_status"],
        {"nonterminal", "terminal-verified", "terminal-verified-downgraded", "terminal-failed"},
        "terminal status",
    )
    _validate_digest(projection["content_digest"], "projection content digest")
    if digest(projection, omit_content_digest=True) != projection.get("content_digest"):
        raise M1AError("E-DIGEST-MISMATCH", "projection content digest")
    for record in projection.get("records", []):
        validate_domain_record(record)
        if digest(record, omit_content_digest=True) != record.get("content_digest"):
            raise M1AError("E-DIGEST-MISMATCH", "projection record digest")
    _validate_structural_objects(projection)
    _validate_canonical_size(projection)


def validate_genesis(value: dict) -> None:
    genesis_value = _require_dict(value, "genesis")
    _require_closed(genesis_value, GENESIS_FIELDS)
    if genesis_value["genesis_schema_version"] != "m1a-genesis-1":
        raise M1AError("E-SCHEMA-UNKNOWN-VERSION", str(genesis_value["genesis_schema_version"]))
    _validate_id(genesis_value["stream_id"], "genesis stream")
    _validate_policy_ref(genesis_value["current_pause_policy_ref"])
    _validate_uint(genesis_value["current_pause_epoch"], "genesis pause epoch")


def validate_authority_attestation(value: dict) -> None:
    authority = _require_dict(value, "authority attestation")
    _require_closed(authority, AUTHORITY_FIELDS)
    if authority["attestation_schema_version"] != "m1a-fixture-authority-1":
        raise M1AError("E-SCHEMA-UNKNOWN-VERSION", str(authority["attestation_schema_version"]))
    _validate_id(authority["attestation_id"], "attestation id")
    _validate_id(authority["actor_id"], "authority actor")
    _validate_enum(
        authority["actor_role"],
        {"owner", "head", "arm", "worker", "verifier", "kernel-fixture-gate"},
        "authority role",
    )
    _validate_array(
        authority["allowed_command_types"],
        lambda item: _validate_enum(item, COMMAND_TYPES, "authority command"),
        "authority commands",
    )
    _validate_digest(authority["candidate_digest"], "authority candidate")
    _validate_policy_ref(authority["authority_policy_ref"])
    _validate_policy_ref(authority["constitution_policy_ref"])
    _validate_uint(authority["valid_from_logical_time"], "authority valid from")
    _validate_uint(authority["expires_logical_time"], "authority expires")
    _validate_digest(authority["content_digest"], "authority digest")


def _validate_order_edge(value) -> None:
    edge = _require_dict(value, "policy order edge")
    _require_closed(edge, {"less_or_equal_id", "greater_or_equal_id"})
    _validate_id(edge["less_or_equal_id"], "less policy id")
    _validate_id(edge["greater_or_equal_id"], "greater policy id")


def _validate_failure_evidence(value) -> None:
    item = _require_dict(value, "failure evidence")
    _require_closed(item, {"failure_id", "evidence_types"})
    _validate_id(item["failure_id"], "failure id")
    _validate_token_set(item["evidence_types"], "evidence types")


def _validate_failure_capabilities(value) -> None:
    item = _require_dict(value, "failure capabilities")
    _require_closed(item, {"failure_id", "capability_ids"})
    _validate_id(item["failure_id"], "failure id")
    _validate_id_set(item["capability_ids"], "failure capability ids")


def validate_policy_fixture(value: dict) -> None:
    policy = _require_dict(value, "policy fixture")
    _require_closed(policy, POLICY_FIELDS)
    if policy["policy_schema_version"] != "m1a-policy-fixture-1":
        raise M1AError("E-SCHEMA-UNKNOWN-VERSION", str(policy["policy_schema_version"]))
    _validate_id(policy["policy_id"], "policy id")
    _validate_uint(policy["policy_version"], "policy version", positive=True)
    kind = policy["policy_kind"]
    _validate_enum(
        kind,
        {"authority", "privacy-egress", "route-envelope", "novelty", "verifier", "constitution", "pause", "retry"},
        "policy kind",
    )
    _validate_token_set(policy["allowed_roles"], "allowed roles")
    _validate_array(
        policy["allowed_command_types"],
        lambda item: _validate_enum(item, COMMAND_TYPES, "policy command"),
        "policy commands",
    )
    for field in ("privacy_class_ids", "egress_class_ids", "route_ids", "account_class_ids", "capacity_pool_ids"):
        _validate_id_set(policy[field], field)
    for field in ("privacy_at_most_edges", "egress_at_most_edges"):
        _validate_array(
            policy[field], _validate_order_edge, field,
            sort_key=lambda item: (item["less_or_equal_id"], item["greater_or_equal_id"]),
            unique_key=lambda item: (item["less_or_equal_id"], item["greater_or_equal_id"]),
        )
    _validate_array(
        policy["allowed_spend_classes"],
        lambda item: _validate_enum(item, {"none", "included"}, "spend class"),
        "allowed spend classes",
    )
    _validate_array(
        policy["accepted_evidence_types"], _validate_failure_evidence,
        "accepted evidence types", sort_key=lambda item: item["failure_id"],
        unique_key=lambda item: item["failure_id"],
    )
    _validate_array(
        policy["failure_capability_requirements"], _validate_failure_capabilities,
        "failure capability requirements", sort_key=lambda item: item["failure_id"],
        unique_key=lambda item: item["failure_id"],
    )
    _validate_token_set(policy["required_verifier_dimensions"], "verifier dimensions")
    if not isinstance(policy["owner_verifier_exception_allowed"], bool):
        _invalid("owner verifier exception flag")
    if policy["operative_pause_mode"] != "current-immediate":
        _invalid("operative pause mode")
    _validate_array(
        policy["representable_future_pause_modes"],
        lambda item: _validate_enum(item, {"checkpoint", "interrupt", "drain"}, "future pause mode"),
        "future pause modes",
    )
    _validate_uint(policy["retry_maximum"], "policy retry maximum")
    _validate_digest(policy["content_digest"], "policy digest")

    nonempty = {
        "authority": {"allowed_roles", "allowed_command_types"},
        "privacy-egress": {"privacy_class_ids", "privacy_at_most_edges", "egress_class_ids", "egress_at_most_edges"},
        "route-envelope": {"route_ids", "account_class_ids", "capacity_pool_ids", "allowed_spend_classes"},
        "novelty": {"route_ids", "account_class_ids", "capacity_pool_ids", "allowed_spend_classes", "accepted_evidence_types", "failure_capability_requirements"},
        "verifier": {"required_verifier_dimensions"},
        "constitution": set(),
        "pause": {"representable_future_pause_modes"},
        "retry": set(),
    }[kind]
    array_fields = {
        "allowed_roles", "allowed_command_types", "privacy_class_ids", "privacy_at_most_edges",
        "egress_class_ids", "egress_at_most_edges", "route_ids", "account_class_ids",
        "capacity_pool_ids", "allowed_spend_classes", "accepted_evidence_types",
        "failure_capability_requirements", "required_verifier_dimensions",
        "representable_future_pause_modes",
    }
    if any(not policy[field] for field in nonempty):
        _invalid("policy required fields")
    if any(policy[field] for field in array_fields - nonempty):
        _invalid("policy inactive fields")
    if kind == "retry":
        if policy["retry_maximum"] < 1 or policy["owner_verifier_exception_allowed"]:
            _invalid("retry policy fields")
    elif policy["retry_maximum"] != 0:
        _invalid("inactive retry maximum")
    if kind not in {"verifier", "constitution"} and policy["owner_verifier_exception_allowed"]:
        _invalid("inactive verifier exception flag")


def decode(raw: str | bytes, schema_epoch: str = "m1a-1"):
    """Decode one closed M1A envelope without observing ambient schema files."""

    if schema_epoch != "m1a-1":
        raise M1AError("E-SCHEMA-UNKNOWN-VERSION", schema_epoch)
    value = strict_loads(raw)
    if not isinstance(value, dict):
        raise M1AError("E-SCHEMA-INVALID", "root must be an object")
    if "command_schema_version" in value:
        validate_command_envelope(value)
    elif "event_schema_version" in value:
        validate_event_envelope(value)
    elif "schema_version" in value or "object_type" in value:
        if "schema_version" not in value:
            raise M1AError("E-SCHEMA-MISSING-FIELD", "schema_version")
        validate_domain_record(value)
    else:
        raise M1AError("E-SCHEMA-MISSING-FIELD", "registered envelope discriminator")
    return value


def _utf16_key(value: str) -> bytes:
    return value.encode("utf-16be")


def canonical_bytes(value) -> bytes:
    """Return the RFC 8785 bytes for M1A's integer-only I-JSON subset."""

    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > MAX_IJSON_INTEGER:
            raise M1AError("E-SCHEMA-BOUND", "integer bound exceeded")
        return str(value).encode("ascii")
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise M1AError("E-SCHEMA-INVALID", "unpaired surrogate") from exc
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if isinstance(value, list):
        return b"[" + b",".join(canonical_bytes(item) for item in value) + b"]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise M1AError("E-SCHEMA-INVALID", "object key must be text")
        keys = sorted(value, key=_utf16_key)
        return b"{" + b",".join(
            canonical_bytes(key) + b":" + canonical_bytes(value[key]) for key in keys
        ) + b"}"
    raise M1AError("E-SCHEMA-INVALID", "unsupported canonical value")


def digest(value, *, omit_content_digest: bool = False) -> str:
    if omit_content_digest and isinstance(value, dict) and dict.__contains__(value, "content_digest"):
        operand = copy.deepcopy(value)
        operand.pop("content_digest", None)
    else:
        operand = value
    return hashlib.sha256(canonical_bytes(operand)).hexdigest()


def seal(value: dict) -> dict:
    result = copy.deepcopy(value)
    result.pop("content_digest", None)
    result["content_digest"] = digest(result)
    return result
