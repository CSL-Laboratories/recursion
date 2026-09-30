"""Pure source boundary for the Recursion MVP control surface and watchdog.

This module does not read or write controller state, contact Telegram, remove PAUSE,
enqueue notifications, or execute an approved intent.  It validates a fresh,
content-addressed snapshot and returns a content-addressed result or intent for the
existing authenticated intake/outbox/actuator layers to durably process.

The second-box watchdog is similarly inert.  It authenticates controller heartbeats
with a pre-provisioned in-memory key registry and emits at most one outbox-compatible
attention intent for each observed heartbeat episode.  Key provisioning, transport,
durable state, and delivery are explicit deployment seams.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import wraps
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import mvp_contracts as contracts


READ_COMMANDS = frozenset({"/today", "/status", "/why", "/next", "/budget"})
MUTATING_COMMANDS = frozenset({"/pause", "/resume", "/approve"})
COMMANDS = READ_COMMANDS | MUTATING_COMMANDS
SHA256_ZERO = "0" * 64
MAX_ITEMS = 64
MAX_MESSAGE = 8_192
MAX_CLOCK_SKEW_SECONDS = 30
_INPUT_ERRORS = (TypeError, ValueError, OverflowError, RecursionError, UnicodeError)


class ControlSurfaceError(ValueError):
    """Typed fail-closed control-surface error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _boundary(kind: str):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except ControlSurfaceError:
                raise
            except (contracts.ContractError, *_INPUT_ERRORS) as exc:
                raise ControlSurfaceError("INVALID_INPUT", f"invalid {kind}") from exc
        return wrapped
    return decorate


def _plain(value: Any, *, active: set[int] | None = None, depth: int = 0,
           count: list[int] | None = None) -> Any:
    if active is None:
        active, count = set(), [0]
    assert count is not None
    if depth > contracts.MAX_DEPTH:
        raise ControlSurfaceError("INPUT_TOO_DEEP", "control data is too deeply nested")
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in active:
            raise ControlSurfaceError("INVALID_INPUT", "cyclic control data")
        active.add(marker); count[0] += 1
        if count[0] > contracts.MAX_NODES:
            raise ControlSurfaceError("INPUT_TOO_LARGE", "control data has too many values")
        try:
            return {key: _plain(item, active=active, depth=depth + 1, count=count)
                    for key, item in value.items()}
        finally:
            active.remove(marker)
    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in active:
            raise ControlSurfaceError("INVALID_INPUT", "cyclic control data")
        active.add(marker); count[0] += 1
        if count[0] > contracts.MAX_NODES:
            raise ControlSurfaceError("INPUT_TOO_LARGE", "control data has too many values")
        try:
            return [_plain(item, active=active, depth=depth + 1, count=count)
                    for item in value]
        finally:
            active.remove(marker)
    return value


def _canonical(value: Any) -> bytes:
    try:
        return contracts.canonical_json(_plain(value))
    except ControlSurfaceError:
        raise
    except contracts.ContractError as exc:
        raise ControlSurfaceError("INVALID_INPUT", str(exc)) from exc


def _digest(value: Mapping[str, Any], digest_field: str) -> str:
    body = _plain(value)
    body.pop(digest_field, None)
    return hashlib.sha256(_canonical(body)).hexdigest()


def _strict(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ControlSurfaceError("INVALID_SCHEMA", f"{label} must be an object")
    result = _plain(value)
    if set(result) != fields:
        raise ControlSurfaceError("INVALID_SCHEMA", f"{label} fields are not strict")
    return result


def _text(value: Any, label: str, *, maximum: int = 2_048) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ControlSurfaceError("INVALID_FIELD", f"{label} must be bounded text")
    _canonical(value)
    return value


def _ident(value: Any, label: str) -> str:
    try:
        contracts._id(value, label)  # one canonical ID alphabet across the MVP
    except contracts.ContractError as exc:
        raise ControlSurfaceError("INVALID_FIELD", f"{label} is not canonical") from exc
    return value


def _sha256(value: Any, label: str) -> str:
    try:
        contracts._digest(value, label)
    except contracts.ContractError as exc:
        raise ControlSurfaceError("INVALID_FIELD", f"{label} is not a SHA-256 digest") from exc
    return value


def _source_sha(value: Any, label: str = "source_sha") -> str:
    if not isinstance(value, str) or not contracts.SHA_RE.fullmatch(value):
        raise ControlSurfaceError("INVALID_FIELD", f"{label} is not a commit")
    return value


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ControlSurfaceError("INVALID_FIELD", f"{label} must be an integer >= {minimum}")
    return value


def _instant(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ControlSurfaceError("INVALID_FIELD", f"{label} must be canonical UTC time")
    try:
        return contracts.parse_time(value)
    except contracts.ContractError as exc:
        raise ControlSurfaceError("INVALID_FIELD", f"{label} must be canonical UTC time") from exc


def _reference_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ControlSurfaceError("INVALID_FIELD", "reference time must be timezone-aware")
        return value.astimezone(timezone.utc)
    return _instant(value, "reference_time")


def _ids(value: Any, label: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_ITEMS or (not allow_empty and not value):
        raise ControlSurfaceError("INVALID_FIELD", f"{label} must be a bounded list")
    result = [_ident(item, f"{label} item") for item in value]
    if len(set(result)) != len(result):
        raise ControlSurfaceError("INVALID_FIELD", f"{label} contains duplicates")
    return result


def _evidence(value: Any) -> tuple[list[dict[str, str]], set[str]]:
    if not isinstance(value, list) or not value or len(value) > MAX_ITEMS:
        raise ControlSurfaceError("INVALID_FIELD", "evidence_refs must be a non-empty bounded list")
    fields = {"id", "sha256", "pointer"}
    records, identifiers = [], set()
    for item in value:
        record = _strict(item, fields, "evidence reference")
        ident = _ident(record["id"], "evidence id")
        if ident in identifiers:
            raise ControlSurfaceError("INVALID_FIELD", "duplicate evidence id")
        identifiers.add(ident)
        _sha256(record["sha256"], "evidence sha256")
        _text(record["pointer"], "evidence pointer", maximum=1_024)
        records.append(record)
    return records, identifiers


_WORK_FIELDS = {"id", "project_id", "status", "summary", "reason", "due_at",
                "assignment_digest", "evidence_refs"}


def _work_items(value: Any, label: str, evidence_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ControlSurfaceError("INVALID_FIELD", f"{label} must be a bounded list")
    result, seen = [], set()
    for item in value:
        record = _strict(item, _WORK_FIELDS, f"{label} item")
        ident = _ident(record["id"], f"{label} id")
        if ident in seen:
            raise ControlSurfaceError("INVALID_FIELD", f"{label} contains duplicate work")
        seen.add(ident)
        _ident(record["project_id"], f"{label} project_id")
        if record["status"] not in {"queued", "running", "verifying", "blocked", "passed", "deferred"}:
            raise ControlSurfaceError("INVALID_FIELD", f"{label} status is invalid")
        _text(record["summary"], f"{label} summary", maximum=512)
        if record["reason"] is not None:
            _text(record["reason"], f"{label} reason", maximum=1_024)
        if record["due_at"] is not None:
            _instant(record["due_at"], f"{label} due_at")
        if record["assignment_digest"] is not None:
            _sha256(record["assignment_digest"], f"{label} assignment_digest")
        refs = _ids(record["evidence_refs"], f"{label} evidence_refs", allow_empty=False)
        if not set(refs).issubset(evidence_ids):
            raise ControlSurfaceError("EVIDENCE_MISMATCH", f"{label} references absent evidence")
        result.append(record)
    return result


def _validate_budget(value: Any) -> dict[str, Any]:
    fields = {"quota_status", "active_workers", "worker_capacity", "review_queue",
              "review_capacity", "month_spend_cents", "month_ceiling_cents",
              "automatic_metered_spend_allowed", "observations"}
    record = _strict(value, fields, "budget status")
    if record["quota_status"] not in {"available", "constrained", "exhausted", "unknown"}:
        raise ControlSurfaceError("INVALID_FIELD", "quota status is invalid")
    for field in ("active_workers", "worker_capacity", "review_queue", "review_capacity",
                  "month_spend_cents", "month_ceiling_cents"):
        _integer(record[field], f"budget {field}")
    if record["active_workers"] > record["worker_capacity"]:
        raise ControlSurfaceError("BUDGET_CONTRADICTION", "active workers exceed capacity")
    if record["month_ceiling_cents"] > 2_000:
        raise ControlSurfaceError("BUDGET_CONTRADICTION", "monthly automatic ceiling exceeds policy")
    if type(record["automatic_metered_spend_allowed"]) is not bool:
        raise ControlSurfaceError("INVALID_FIELD", "automatic metered-spend flag must be boolean")
    if record["automatic_metered_spend_allowed"]:
        raise ControlSurfaceError("SPEND_FORBIDDEN", "MVP snapshot cannot authorize automatic metered spend")
    if not isinstance(record["observations"], list) or len(record["observations"]) > MAX_ITEMS:
        raise ControlSurfaceError("INVALID_FIELD", "budget observations must be bounded")
    for observation in record["observations"]:
        _text(observation, "budget observation")
    return record


_SNAPSHOT_FIELDS = {"schema_version", "kind", "source_sha", "observed_at", "expires_at",
                    "pause_observed", "today", "active_work", "reasons", "next_work",
                    "budget", "evidence_refs", "snapshot_digest"}


@_boundary("control snapshot")
def validate_snapshot(value: Any, *, reference_time: Any | None = None) -> dict[str, Any]:
    record = _strict(value, _SNAPSHOT_FIELDS, "control snapshot")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1 \
            or record["kind"] != "control_snapshot":
        raise ControlSurfaceError("INVALID_SCHEMA", "unsupported control snapshot schema")
    _source_sha(record["source_sha"])
    observed, expires = _instant(record["observed_at"], "observed_at"), _instant(record["expires_at"], "expires_at")
    if expires <= observed:
        raise ControlSurfaceError("INVALID_FRESHNESS", "snapshot validity interval is reversed")
    if type(record["pause_observed"]) is not bool:
        raise ControlSurfaceError("INVALID_FIELD", "pause_observed must be boolean")
    _, evidence_ids = _evidence(record["evidence_refs"])
    _work_items(record["today"], "today", evidence_ids)
    _work_items(record["active_work"], "active_work", evidence_ids)
    _work_items(record["next_work"], "next_work", evidence_ids)
    if not isinstance(record["reasons"], list) or len(record["reasons"]) > MAX_ITEMS:
        raise ControlSurfaceError("INVALID_FIELD", "reasons must be a bounded list")
    for reason in record["reasons"]:
        _text(reason, "reason")
    _validate_budget(record["budget"])
    _sha256(record["snapshot_digest"], "snapshot_digest")
    if not hmac.compare_digest(record["snapshot_digest"], _digest(record, "snapshot_digest")):
        raise ControlSurfaceError("DIGEST_MISMATCH", "snapshot digest mismatch")
    if reference_time is not None:
        now = _reference_time(reference_time)
        if now < observed or now > expires:
            raise ControlSurfaceError("STALE_SNAPSHOT", "control snapshot is not current")
    return copy.deepcopy(record)


_REQUEST_FIELDS = {"schema_version", "kind", "id", "command", "arguments", "principal",
                   "source_sha", "snapshot_digest", "created_at", "expires_at", "request_digest"}


@_boundary("command request")
def validate_request(value: Any, *, snapshot: Any, reference_time: Any) -> dict[str, Any]:
    current = validate_snapshot(snapshot, reference_time=reference_time)
    record = _strict(value, _REQUEST_FIELDS, "command request")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1 \
            or record["kind"] != "command_request":
        raise ControlSurfaceError("INVALID_SCHEMA", "unsupported command request schema")
    _ident(record["id"], "request id")
    if record["command"] not in COMMANDS:
        raise ControlSurfaceError("UNKNOWN_COMMAND", "unsupported control command")
    if not isinstance(record["arguments"], list) or len(record["arguments"]) > 2:
        raise ControlSurfaceError("INVALID_FIELD", "command arguments must be a bounded list")
    for argument in record["arguments"]:
        _text(argument, "command argument", maximum=256)
    expected_arguments = 2 if record["command"] == "/approve" else 0
    if len(record["arguments"]) != expected_arguments:
        raise ControlSurfaceError("INVALID_FIELD", "command argument count is invalid")
    _text(record["principal"], "request principal", maximum=128)
    _source_sha(record["source_sha"])
    _sha256(record["snapshot_digest"], "request snapshot_digest")
    if record["source_sha"] != current["source_sha"] or record["snapshot_digest"] != current["snapshot_digest"]:
        raise ControlSurfaceError("SNAPSHOT_MISMATCH", "request is not bound to the current snapshot")
    created, expires = _instant(record["created_at"], "created_at"), _instant(record["expires_at"], "expires_at")
    now = _reference_time(reference_time)
    if expires <= created or now < created or now > expires:
        raise ControlSurfaceError("STALE_REQUEST", "command request is not current")
    if (now - created).total_seconds() > MAX_CLOCK_SKEW_SECONDS:
        raise ControlSurfaceError("STALE_REQUEST", "command request exceeds the freshness window")
    _sha256(record["request_digest"], "request_digest")
    if not hmac.compare_digest(record["request_digest"], _digest(record, "request_digest")):
        raise ControlSurfaceError("DIGEST_MISMATCH", "request digest mismatch")
    return copy.deepcopy(record)


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Typed output of the existing Telegram/intake authentication boundary."""

    principal: str
    authentication_event_digest: str
    authenticated_at: str
    authenticated: bool = True

    def validate(self, *, reference_time: Any) -> None:
        _text(self.principal, "authenticated principal", maximum=128)
        _sha256(self.authentication_event_digest, "authentication_event_digest")
        observed = _instant(self.authenticated_at, "authenticated_at")
        now = _reference_time(reference_time)
        if type(self.authenticated) is not bool or not self.authenticated:
            raise ControlSurfaceError("UNAUTHENTICATED", "principal authentication is absent")
        if now < observed or (now - observed).total_seconds() > MAX_CLOCK_SKEW_SECONDS:
            raise ControlSurfaceError("STALE_AUTHENTICATION", "principal authentication is stale")


_INTENT_FIELDS = {"schema_version", "kind", "id", "event_type", "action", "request_id",
                  "request_digest", "principal", "authentication_event_digest", "source_sha",
                  "snapshot_digest", "created_at", "approval_required", "approved_intent_id",
                  "approved_intent_digest", "disposition", "dedup_key", "evidence_refs",
                  "intent_digest"}


@_boundary("control intent")
def validate_intent(value: Any) -> dict[str, Any]:
    record = _strict(value, _INTENT_FIELDS, "control intent")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1 \
            or record["kind"] != "control_intent":
        raise ControlSurfaceError("INVALID_SCHEMA", "unsupported control intent schema")
    for field in ("id", "request_id"):
        _ident(record[field], field)
    if record["event_type"] != "lucas-attention-required":
        raise ControlSurfaceError("INVALID_FIELD", "control intent is not outbox-compatible")
    if record["action"] not in {"pause-request", "resume-request", "approval"}:
        raise ControlSurfaceError("INVALID_FIELD", "control intent action is invalid")
    _sha256(record["request_digest"], "intent request_digest")
    _text(record["principal"], "intent principal", maximum=128)
    _sha256(record["authentication_event_digest"], "authentication_event_digest")
    _source_sha(record["source_sha"])
    _sha256(record["snapshot_digest"], "intent snapshot_digest")
    _instant(record["created_at"], "intent created_at")
    if type(record["approval_required"]) is not bool:
        raise ControlSurfaceError("INVALID_FIELD", "approval_required must be boolean")
    if record["action"] == "pause-request":
        if record["approval_required"] is not False:
            raise ControlSurfaceError("INVALID_FIELD", "pause request cannot require approval")
        if record["approved_intent_id"] is not None or record["approved_intent_digest"] is not None:
            raise ControlSurfaceError("INVALID_FIELD", "non-approval intent carries approval target")
    elif record["action"] == "resume-request":
        if record["approval_required"] is not True:
            raise ControlSurfaceError("INVALID_FIELD", "resume request must require approval")
        if record["approved_intent_id"] is not None or record["approved_intent_digest"] is not None:
            raise ControlSurfaceError("INVALID_FIELD", "non-approval intent carries approval target")
    elif record["action"] == "approval":
        if record["approval_required"] is not False:
            raise ControlSurfaceError("INVALID_FIELD", "approval intent cannot require approval")
        _ident(record["approved_intent_id"], "approved_intent_id")
        _sha256(record["approved_intent_digest"], "approved_intent_digest")
    if record["disposition"] not in {"ready-for-canonical-outbox", "held-paused"}:
        raise ControlSurfaceError("INVALID_FIELD", "control intent disposition is invalid")
    _text(record["dedup_key"], "dedup_key", maximum=256)
    _ids(record["evidence_refs"], "intent evidence_refs", allow_empty=False)
    _sha256(record["intent_digest"], "intent_digest")
    if not hmac.compare_digest(record["intent_digest"], _digest(record, "intent_digest")):
        raise ControlSurfaceError("DIGEST_MISMATCH", "intent digest mismatch")
    return record


def _seal(value: dict[str, Any], field: str) -> dict[str, Any]:
    value[field] = _digest(value, field)
    return value


def _result(request: dict[str, Any], snapshot: dict[str, Any], now: datetime, *, status: str,
            message: str, evidence_refs: list[str], intent: dict[str, Any] | None = None) -> dict[str, Any]:
    _text(message, "result message", maximum=MAX_MESSAGE)
    body = {
        "schema_version": 1, "kind": "control_result",
        "id": f"result-{request['request_digest'][:24]}", "request_id": request["id"],
        "request_digest": request["request_digest"], "command": request["command"],
        "source_sha": snapshot["source_sha"], "snapshot_digest": snapshot["snapshot_digest"],
        "observed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": status,
        "message": message, "evidence_refs": evidence_refs, "intent": intent,
        "result_digest": "",
    }
    return _seal(body, "result_digest")


def _format_items(prefix: str, items: Sequence[Mapping[str, Any]]) -> str:
    if not items:
        return f"{prefix}: none recorded in the current snapshot."
    displayed = list(items[:8])
    rendered = [f"{item['id']} — {item['summary']} [{item['status']}]" for item in displayed]
    suffix = f"; plus {len(items) - len(displayed)} more" if len(items) > len(displayed) else ""
    return f"{prefix}: " + "; ".join(rendered) + suffix


def _read_message(command: str, snapshot: Mapping[str, Any]) -> str:
    if command == "/today":
        return _format_items("Landing today", snapshot["today"])
    if command == "/status":
        pause = "PAUSED" if snapshot["pause_observed"] else "active"
        return f"Controller: {pause}. " + _format_items("Active work", snapshot["active_work"])
    if command == "/why":
        reasons = snapshot["reasons"]
        displayed = reasons[:8]
        suffix = f"; plus {len(reasons) - len(displayed)} more" if len(reasons) > len(displayed) else ""
        return "Why: " + ("; ".join(displayed) + suffix if reasons else "no rationale recorded in the current snapshot.")
    if command == "/next":
        return _format_items("Next", snapshot["next_work"])
    budget = snapshot["budget"]
    return (f"Budget: quota {budget['quota_status']}; workers {budget['active_workers']}/"
            f"{budget['worker_capacity']}; review queue {budget['review_queue']}/"
            f"{budget['review_capacity']}; observed monthly spend {budget['month_spend_cents']} cents; "
            "automatic metered spend disabled.")


def _intent(request: dict[str, Any], snapshot: dict[str, Any], auth: AuthenticatedPrincipal,
            now: datetime, action: str, *, approval_required: bool,
            approved: dict[str, Any] | None = None) -> dict[str, Any]:
    approved_id = approved["id"] if approved else None
    approved_digest = approved["intent_digest"] if approved else None
    identity = hashlib.sha256(_canonical([request["request_digest"], action, approved_digest])).hexdigest()[:24]
    evidence_ids = [item["id"] for item in snapshot["evidence_refs"]]
    body = {
        "schema_version": 1, "kind": "control_intent", "id": f"intent-{identity}",
        "event_type": "lucas-attention-required", "action": action,
        "request_id": request["id"], "request_digest": request["request_digest"],
        "principal": auth.principal,
        "authentication_event_digest": auth.authentication_event_digest,
        "source_sha": snapshot["source_sha"], "snapshot_digest": snapshot["snapshot_digest"],
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "approval_required": approval_required, "approved_intent_id": approved_id,
        "approved_intent_digest": approved_digest,
        "disposition": "held-paused" if snapshot["pause_observed"] else "ready-for-canonical-outbox",
        "dedup_key": f"control:{request['request_digest']}:{action}:{approved_digest or '-'}",
        "evidence_refs": evidence_ids, "intent_digest": "",
    }
    return _seal(body, "intent_digest")


@_boundary("control command")
def handle_command(request: Any, snapshot: Any, *, reference_time: Any,
                   authentication: AuthenticatedPrincipal | None = None,
                   pending_intents: Sequence[Mapping[str, Any]] = (),
                   consumed_approval_digests: Sequence[str] = ()) -> dict[str, Any]:
    current = validate_snapshot(snapshot, reference_time=reference_time)
    command = validate_request(request, snapshot=current, reference_time=reference_time)
    now = _reference_time(reference_time)
    evidence_ids = [item["id"] for item in current["evidence_refs"]]
    if command["command"] in READ_COMMANDS:
        if authentication is not None:
            authentication.validate(reference_time=now)
            if authentication.principal != command["principal"]:
                raise ControlSurfaceError("PRINCIPAL_MISMATCH", "request and authentication principals differ")
        return _result(command, current, now, status="ok",
                       message=_read_message(command["command"], current),
                       evidence_refs=evidence_ids)

    if not isinstance(authentication, AuthenticatedPrincipal):
        raise ControlSurfaceError("UNAUTHENTICATED", "mutating command requires authenticated Lucas")
    authentication.validate(reference_time=now)
    if authentication.principal != "lucas" or command["principal"] != "lucas":
        raise ControlSurfaceError("UNAUTHORIZED", "only Lucas may request a control mutation")

    if command["command"] == "/pause":
        if current["pause_observed"]:
            return _result(command, current, now, status="ok", message="Controller is already PAUSED; no intent emitted.", evidence_refs=evidence_ids)
        intent = _intent(command, current, authentication, now, "pause-request", approval_required=False)
        return _result(command, current, now, status="intent-created",
                       message="Authenticated pause intent created for the canonical actuator; no state was changed here.",
                       evidence_refs=evidence_ids, intent=intent)

    if command["command"] == "/resume":
        if not current["pause_observed"]:
            return _result(command, current, now, status="refused", message="Controller is not PAUSED; no resume intent emitted.", evidence_refs=evidence_ids)
        intent = _intent(command, current, authentication, now, "resume-request", approval_required=True)
        return _result(command, current, now, status="held-paused",
                       message="Resume requires exact human approval and remains held locally under PAUSE; PAUSE was not removed.",
                       evidence_refs=evidence_ids, intent=intent)

    target_id, target_digest = command["arguments"]
    _ident(target_id, "approved intent id"); _sha256(target_digest, "approved intent digest")
    consumed = {_sha256(item, "consumed approval digest") for item in consumed_approval_digests}
    if target_digest in consumed:
        raise ControlSurfaceError("APPROVAL_REPLAY", "approval target was already consumed")
    matches = []
    for pending in pending_intents:
        checked = validate_intent(pending)
        if checked["id"] == target_id or checked["intent_digest"] == target_digest:
            matches.append(checked)
    if len(matches) != 1 or matches[0]["id"] != target_id or matches[0]["intent_digest"] != target_digest:
        raise ControlSurfaceError("APPROVAL_MISMATCH", "approval does not exactly match one pending intent")
    target = matches[0]
    if not target["approval_required"] or target["action"] != "resume-request":
        raise ControlSurfaceError("APPROVAL_MISMATCH", "target is not an approval-required resume intent")
    if target["source_sha"] != current["source_sha"] or target["snapshot_digest"] != current["snapshot_digest"]:
        raise ControlSurfaceError("STALE_APPROVAL", "approval target is not bound to the current snapshot")
    intent = _intent(command, current, authentication, now, "approval",
                     approval_required=False, approved=target)
    return _result(command, current, now,
                   status="held-paused" if current["pause_observed"] else "intent-created",
                   message="Exact one-shot approval intent created; execution and PAUSE removal remain outside this source boundary.",
                   evidence_refs=evidence_ids, intent=intent)


@dataclass(frozen=True)
class WatchdogState:
    controller_id: str
    last_sequence: int = -1
    last_heartbeat_digest: str | None = None
    last_sent_at: str | None = None
    last_source_sha: str | None = None
    last_evidence_refs: tuple[str, ...] = ()
    episode: int = 0
    notified_heartbeat_digest: str | None = None

    def __post_init__(self) -> None:
        _ident(self.controller_id, "watchdog controller_id")
        if type(self.last_sequence) is not int or self.last_sequence < -1:
            raise ControlSurfaceError("INVALID_STATE", "watchdog sequence is invalid")
        _integer(self.episode, "watchdog episode")
        if self.last_sequence == -1:
            if any(value is not None for value in (self.last_heartbeat_digest, self.last_sent_at,
                                                    self.last_source_sha, self.notified_heartbeat_digest)) \
                    or self.last_evidence_refs or self.episode != 0:
                raise ControlSurfaceError("INVALID_STATE", "empty watchdog state is contradictory")
        else:
            _sha256(self.last_heartbeat_digest, "last heartbeat digest")
            _instant(self.last_sent_at, "last heartbeat sent_at")
            _source_sha(self.last_source_sha, "last heartbeat source_sha")
            for item in self.last_evidence_refs:
                _ident(item, "watchdog evidence ref")
            if self.notified_heartbeat_digest is not None:
                _sha256(self.notified_heartbeat_digest, "notified heartbeat digest")
                if self.notified_heartbeat_digest != self.last_heartbeat_digest:
                    raise ControlSurfaceError("INVALID_STATE", "watchdog notification is not bound to current episode")


_HEARTBEAT_FIELDS = {"schema_version", "kind", "controller_id", "sequence", "source_sha",
                     "sent_at", "status", "signer_id", "evidence_refs", "heartbeat_digest",
                     "authentication_tag"}


class HeartbeatVerifier:
    """Authenticate watchdog input against a pre-provisioned, non-persisted key registry."""

    def __init__(self, keys: Mapping[str, bytes]):
        if not isinstance(keys, Mapping) or not keys:
            raise ControlSurfaceError("INVALID_KEY_REGISTRY", "heartbeat key registry is absent")
        normalized: dict[str, bytes] = {}
        for identity, key in keys.items():
            _ident(identity, "heartbeat signer id")
            if not isinstance(key, bytes) or len(key) < 16:
                raise ControlSurfaceError("INVALID_KEY_REGISTRY", "heartbeat key is too short")
            normalized[identity] = bytes(key)
        self._keys = MappingProxyType(normalized)

    @_boundary("controller heartbeat")
    def verify(self, value: Any) -> dict[str, Any]:
        record = _strict(value, _HEARTBEAT_FIELDS, "controller heartbeat")
        if type(record["schema_version"]) is not int or record["schema_version"] != 1 \
                or record["kind"] != "controller_heartbeat":
            raise ControlSurfaceError("INVALID_SCHEMA", "unsupported heartbeat schema")
        _ident(record["controller_id"], "heartbeat controller_id")
        _integer(record["sequence"], "heartbeat sequence")
        _source_sha(record["source_sha"], "heartbeat source_sha")
        _instant(record["sent_at"], "heartbeat sent_at")
        if record["status"] not in {"healthy", "degraded"}:
            raise ControlSurfaceError("INVALID_FIELD", "heartbeat status is invalid")
        signer = _ident(record["signer_id"], "heartbeat signer_id")
        refs = _ids(record["evidence_refs"], "heartbeat evidence_refs", allow_empty=False)
        _sha256(record["heartbeat_digest"], "heartbeat_digest")
        _sha256(record["authentication_tag"], "heartbeat authentication_tag")
        if not hmac.compare_digest(record["heartbeat_digest"], _digest(record, "heartbeat_digest")):
            # authentication_tag is outside the content digest; avoid a circular signature.
            unsigned = dict(record); unsigned.pop("authentication_tag")
            if not hmac.compare_digest(record["heartbeat_digest"], _digest(unsigned, "heartbeat_digest")):
                raise ControlSurfaceError("DIGEST_MISMATCH", "heartbeat digest mismatch")
        key = self._keys.get(signer)
        if key is None:
            raise ControlSurfaceError("UNAUTHENTICATED_HEARTBEAT", "heartbeat signer is unknown")
        expected = hmac.new(key, record["heartbeat_digest"].encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(record["authentication_tag"], expected):
            raise ControlSurfaceError("UNAUTHENTICATED_HEARTBEAT", "heartbeat authentication failed")
        record["evidence_refs"] = refs
        return record


@_boundary("heartbeat digest input")
def heartbeat_digest(value: Mapping[str, Any]) -> str:
    """Return the digest a second-box signer must authenticate (never the key)."""
    if not isinstance(value, Mapping):
        raise ControlSurfaceError("INVALID_INPUT", "heartbeat must be an object")
    record = _plain(value)
    record.pop("authentication_tag", None)
    return _digest(record, "heartbeat_digest")


def _death_intent(state: WatchdogState, now: datetime, paused: bool) -> dict[str, Any]:
    assert state.last_heartbeat_digest and state.last_sent_at and state.last_source_sha
    identity = hashlib.sha256(_canonical([state.controller_id, state.episode,
                                          state.last_heartbeat_digest])).hexdigest()[:24]
    body = {
        "schema_version": 1, "kind": "watchdog_intent", "id": f"watchdog-{identity}",
        "event_type": "lucas-attention-required", "action": "controller-death-notification",
        "controller_id": state.controller_id, "heartbeat_digest": state.last_heartbeat_digest,
        "heartbeat_sequence": state.last_sequence, "episode": state.episode,
        "source_sha": state.last_source_sha, "last_seen_at": state.last_sent_at,
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "disposition": "held-paused" if paused else "ready-for-canonical-outbox",
        "dedup_key": f"watchdog:{state.controller_id}:{state.episode}:{state.last_heartbeat_digest}",
        "evidence_refs": list(state.last_evidence_refs), "intent_digest": "",
    }
    return _seal(body, "intent_digest")


@_boundary("watchdog evaluation")
def evaluate_watchdog(state: WatchdogState, verifier: HeartbeatVerifier, *, reference_time: Any,
                      threshold_seconds: int, heartbeat: Any | None = None,
                      pause_observed: bool = False) -> tuple[WatchdogState, dict[str, Any] | None]:
    if not isinstance(state, WatchdogState) or not isinstance(verifier, HeartbeatVerifier):
        raise ControlSurfaceError("INVALID_INPUT", "watchdog state and verifier are required")
    threshold = _integer(threshold_seconds, "watchdog threshold", 1)
    if threshold > 86_400:
        raise ControlSurfaceError("INVALID_FIELD", "watchdog threshold exceeds one day")
    if type(pause_observed) is not bool:
        raise ControlSurfaceError("INVALID_FIELD", "pause_observed must be boolean")
    now = _reference_time(reference_time)
    current = state
    if heartbeat is not None:
        checked = verifier.verify(heartbeat)
        if checked["controller_id"] != state.controller_id:
            raise ControlSurfaceError("HEARTBEAT_MISMATCH", "heartbeat names another controller")
        if checked["sequence"] < state.last_sequence:
            raise ControlSurfaceError("STALE_HEARTBEAT", "heartbeat sequence regressed")
        if checked["sequence"] == state.last_sequence:
            if checked["heartbeat_digest"] != state.last_heartbeat_digest:
                raise ControlSurfaceError("HEARTBEAT_CONFLICT", "heartbeat sequence was reused")
        else:
            sent = _instant(checked["sent_at"], "heartbeat sent_at")
            if state.last_sent_at is not None and sent <= _instant(state.last_sent_at, "last heartbeat sent_at"):
                raise ControlSurfaceError("STALE_HEARTBEAT", "heartbeat sent_at did not advance")
            if sent > now and (sent - now).total_seconds() > MAX_CLOCK_SKEW_SECONDS:
                raise ControlSurfaceError("FUTURE_HEARTBEAT", "heartbeat is too far in the future")
            current = WatchdogState(
                controller_id=state.controller_id, last_sequence=checked["sequence"],
                last_heartbeat_digest=checked["heartbeat_digest"], last_sent_at=checked["sent_at"],
                last_source_sha=checked["source_sha"], last_evidence_refs=tuple(checked["evidence_refs"]),
                episode=state.episode + 1, notified_heartbeat_digest=None,
            )
    if current.last_sequence < 0:
        return current, None
    last_seen = _instant(current.last_sent_at, "last heartbeat sent_at")
    if (now - last_seen).total_seconds() <= threshold:
        return current, None
    if current.notified_heartbeat_digest == current.last_heartbeat_digest:
        return current, None
    intent = _death_intent(current, now, pause_observed)
    return replace(current, notified_heartbeat_digest=current.last_heartbeat_digest), intent


__all__ = [
    "AuthenticatedPrincipal", "COMMANDS", "ControlSurfaceError", "HeartbeatVerifier",
    "MUTATING_COMMANDS", "READ_COMMANDS", "WatchdogState", "evaluate_watchdog",
    "handle_command", "heartbeat_digest", "validate_intent", "validate_request",
    "validate_snapshot",
]
