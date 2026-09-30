#!/usr/bin/env python3
"""Deterministic instruction routing, pre-model admission, and refusal evidence.

The gateway records operator prose.  This module classifies only authority-bearing intent; it
never interprets prose as an implementation command.  A proven actor/verb mismatch becomes one
durable internal refusal decision, which the sole actuator couples back to the instruction.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

from pilot_instruction import project_window_open_terms
from transition_receipt import TransitionReceiptError, authenticated_transition, commit_event


IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_INSTRUCTION_BYTES = 512 * 1024
STATIC_FIELDS = (
    "schema_version", "id", "received", "principal", "topic", "project_id", "pilot_id",
    "pilot_memory_key", "routing_target", "reply_correlation", "deduplication_key", "from",
    "chat", "thread", "project",
)
# A Head-Hermes route needs authority-bearing grammar, not the mere presence of a policy word.
# Project plans routinely describe deployed contracts, assist workflows, escalation paths, and
# portfolio reporting.  Treating those nouns as commands reproduces the instruction-707 bug.
_BOUNDARY = r"(?:^\s*|[.!?;]\s*|\b(?:please|then|and)\s+)"
_HEAD_OBJECT = (
    r"(?:portfolio|priorit(?:y|ies|i[sz]ation)|polic(?:y|ies)|spend(?:ing)?|budget|"
    r"credentials?|deployments?|cron|model\s+policy|control[- ]plane|deploy\s+keys?|"
    r"plans?(?:\s+digest)?|pilot\s+windows?|go\s*/?\s*no-go)"
)
HEAD_INTENTS = (
    ("head-hermes-explicit", re.compile(r"\bhead\s+hermes\b", re.I)),
    ("deployment-command", re.compile(
        _BOUNDARY + r"(?:re)?deploy(?:s|ed|ing)?\b", re.I
    )),
    ("merge-command", re.compile(_BOUNDARY + r"merge(?:s|d|ing)?\b", re.I)),
    ("pause-command", re.compile(_BOUNDARY + r"(?:pause|resume)(?:s|d|ing)?\b", re.I)),
    ("priority-command", re.compile(_BOUNDARY + r"prioriti[sz]e(?:s|d|ing)?\b", re.I)),
    ("spend-command", re.compile(_BOUNDARY + r"spend(?:s|ing)?\b", re.I)),
    ("assist-command", re.compile(_BOUNDARY + r"assist\b", re.I)),
    ("escalation-command", re.compile(_BOUNDARY + r"escalat(?:e|es|ed|ing)\b", re.I)),
    ("head-object-command", re.compile(
        _BOUNDARY
        + r"(?:review|change|set|update|approve|authorize|rotate|revoke|audit|inspect|"
          r"configure|preserve|reduce|increase|open|close)\b.{0,160}\b"
        + _HEAD_OBJECT + r"\b", re.I | re.S
    )),
    ("head-object-proposal", re.compile(
        r"^\s*(?:propose|draft|prepare|create)\s+(?:a\s+|an\s+|the\s+)?(?:new\s+)?"
        + _HEAD_OBJECT + r"\b", re.I
    )),
    ("head-object-requirement", re.compile(
        r"^\s*(?:the\s+)?" + _HEAD_OBJECT
        + r"\s+(?:must|should|shall|needs?\s+to|requires?)\b", re.I
    )),
)
PILOT_PLAN_INTENT = re.compile(
    r"\b(?:propose|prepare|draft|create|synthesi[sz]e|author)\b.{0,240}\b(?:pilot\s+)?plan\b",
    re.I | re.S,
)


HISTORICAL_INSTRUCTIONS = {
    "707": {
        "fields": {
            "schema_version": "1", "id": "707", "received": "2026-08-13T22:07:26Z",
            "principal": "8913126684", "topic": "6", "project_id": "example-client",
            "pilot_id": "pilot-example-client", "pilot_memory_key": "telegram-topic:6",
            "routing_target": "head-hermes", "reply_correlation": "telegram:-1003894008732:6:707",
            "deduplication_key": "telegram:-1003894008732:6:707", "from": "8913126684",
            "chat": "-1003894008732", "thread": "6", "project": "example-client",
        },
        "body": (
            "Propose a fresh offline example-client pilot plan to reduce the verified "
            "0x6c shade-tops under-emission in user-client/render, using new v2 plan, goal, and "
            "task identities and the deployed required seed, scope, provider, and forced-on gate "
            "contracts. Do not reuse or edit the closed v1 plan, goal, task, or window."
        ),
        "failed_actions": {
            "action-bb38398a6e0e3859a1a7d263":
                "af458e205eeb003b107a14207169132da6671747841a950a31d25461410e6faa",
            "action-4a90005df9c9dfe6cbb7fbad":
                "c5c9d98446a503f003bb9e19411b4904f3f46aa05c9486494c7736520952a425",
            "action-cbba158e3295fb82bdf6e06c":
                "70b808b373755be5a25a789ef4fe2522cec875d8d889d8cfdf1f05b2efda4c7f",
        },
        "initial_document_sha256":
            "8cb7171363d5c174073ac7f79f6c001ce3045b270667f7bdb6c257d57760e4e8",
    },
    "732": {
        "fields": {
            "schema_version": "1", "id": "732", "received": "2026-08-13T22:11:01Z",
            "principal": "8913126684", "topic": "6", "project_id": "example-client",
            "pilot_id": "pilot-example-client", "pilot_memory_key": "telegram-topic:6",
            "routing_target": "pilot-example-client",
            "reply_correlation": "telegram:-1003894008732:6:732",
            "deduplication_key": "telegram:-1003894008732:6:732", "from": "8913126684",
            "chat": "-1003894008732", "thread": "6", "project": "example-client",
        },
        "body": "Authorize one Head Hermes turn to escalate instruction 707 as a routing blocker.",
    },
}


class RoutingError(ValueError):
    pass


def _reject_json_constant(value: str) -> object:
    raise RoutingError(f"non-finite JSON constant is forbidden: {value}")


def _parse_json_arg(raw: str) -> object:
    if len(raw.encode("utf-8")) > MAX_INSTRUCTION_BYTES:
        raise RoutingError("routing JSON argument exceeds the 512 KiB bound")
    try:
        return json.loads(raw, parse_constant=_reject_json_constant)
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError, OverflowError,
            RecursionError) as exc:
        raise RoutingError(f"routing JSON argument is invalid: {exc}") from exc


def _read_json_path(path: Path, label: str) -> object:
    path = _audit_path(path, label)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RoutingError(f"{label} is unreadable") from exc
    if len(raw) > MAX_INSTRUCTION_BYTES:
        raise RoutingError(f"{label} exceeds the 512 KiB bound")
    try:
        return json.loads(raw.decode("utf-8", "strict"), parse_constant=_reject_json_constant)
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError, OverflowError,
            RecursionError) as exc:
        raise RoutingError(f"{label} is invalid JSON") from exc


def _is_reparse(info: os.stat_result) -> bool:
    # S_IFLNK (0o120000) is a VALUE in the S_IFMT field, not an independent bit, so
    # `st_mode & S_IFLNK` is not a symlink test: a regular file (0o100644) shares a bit with it
    # and yields 0o100000, which is truthy. That misclassified every regular file as a symlink
    # and made _audit_path refuse legitimate instruction files. Directories (0o040755) mask to 0
    # and escaped, which is why only the final file component tripped it.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _audit_path(path: Path, label: str) -> Path:
    candidate = Path(path)
    if any(part in {".", ".."} for part in candidate.parts):
        raise RoutingError(f"{label} contains traversal components")
    if not candidate.is_absolute():
        candidate = Path(os.path.abspath(candidate))
    current = Path(candidate.anchor or os.sep)
    for part in candidate.parts[1:] if candidate.anchor else candidate.parts:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise RoutingError(f"{label} is unreadable") from exc
        if _is_reparse(info):
            raise RoutingError(f"{label} contains a symlink/reparse component")
    return candidate


def compact(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        raise RoutingError(f"routing JSON is not canonical: {exc}") from exc


def stable_id(prefix: str, *parts: object) -> str:
    return prefix + hashlib.sha256(compact(parts)).hexdigest()[:24]


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_instruction(path: Path) -> dict:
    path = _audit_path(path, "instruction")
    try:
        info = path.lstat()
        if _is_reparse(info) or not stat.S_ISREG(info.st_mode):
            raise RoutingError(f"instruction is not a regular file: {path}")
        raw = path.read_bytes()
    except (OSError, RoutingError) as exc:
        raise RoutingError(f"instruction is unreadable: {path}") from exc
    if len(raw) > MAX_INSTRUCTION_BYTES:
        raise RoutingError(f"instruction exceeds the 512 KiB bound: {path}")
    document = raw.decode("utf-8", errors="surrogateescape")
    # Preserve the historical parser's universal-newline behavior for fields/body while
    # retaining the raw digest needed to bind reviewed recovery evidence byte-for-byte.
    document = document.replace("\r\n", "\n").replace("\r", "\n")
    lines = document.splitlines()
    fields: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            break
        if ":" in line and not line.startswith((" ", "\t")):
            key, value = line.split(":", 1)
            fields.setdefault(key.strip(), value.strip())
    match = re.search(r"(?m)^## INSTRUCTION\s*$\n?", document)
    body = document[match.end():] if match else ""
    body = re.split(r"(?m)^##\s+", body, maxsplit=1)[0].strip()
    return {
        "path": path, "document": document, "document_sha256": hashlib.sha256(raw).hexdigest(),
        "fields": fields, "body": body,
    }


def instruction_path(base: Path, instruction_id: str) -> Path:
    if instruction_id.isdigit():
        return base / "state/instructions" / f"{int(instruction_id):08d}.md"
    return base / "state/instructions" / f"{instruction_id}.md"


def configured_project_ids(base: Path) -> list[str]:
    """Read only canonical project identifiers needed to classify cross-project requests."""
    values = {path.stem for path in (base / "state/project-config").glob("*.json")}
    portfolio = base / "state/portfolio.tsv"
    try:
        for line in portfolio.read_text(encoding="utf-8", errors="replace").splitlines():
            if line and not line.startswith("#"):
                project = line.split("\t", 1)[0].strip()
                if IDENT.fullmatch(project):
                    values.add(project)
    except OSError:
        pass
    return sorted(values)


def instruction_signature(record: dict) -> str:
    fields = record["fields"]
    snapshot = {"fields": {key: fields.get(key, "") for key in STATIC_FIELDS}, "body": record["body"]}
    return hashlib.sha256(compact(snapshot)).hexdigest()


def head_intent(body: str) -> str | None:
    for reason, pattern in HEAD_INTENTS:
        if pattern.search(body):
            return reason
    return None


def cross_project(body: str, current: str, project_ids: list[str]) -> bool:
    for other in project_ids:
        if other != current and re.search(
            r"(?<![A-Za-z0-9_-])" + re.escape(other) + r"(?![A-Za-z0-9_-])", body, re.I
        ):
            return True
    return False


def route_for(body: str, binding: dict, project_ids: list[str]) -> str:
    if not isinstance(binding, dict) or not isinstance(project_ids, list) \
            or any(not isinstance(project, str) for project in project_ids):
        raise RoutingError("routing binding and project identifiers must be JSON arrays/objects")
    if not binding or not binding.get("pilot_id"):
        return "head-hermes"
    if head_intent(body) is not None:
        return "head-hermes"
    if cross_project(body, str(binding.get("project") or ""), project_ids):
        return "head-hermes"
    return str(binding["pilot_id"])


def requested_authority(record: dict, project_ids: list[str] | None = None) \
        -> tuple[str | None, str | None, str | None]:
    fields, body = record["fields"], record["body"]
    if project_ids and cross_project(body, fields.get("project_id") or "", project_ids):
        return "head-hermes", "head-control", "cross-project"
    reason = head_intent(body)
    if reason is not None:
        verb = "escalate" if reason in {"head-hermes-explicit", "escalation-command"} \
            else "head-control"
        return "head-hermes", verb, reason
    if PILOT_PLAN_INTENT.search(body):
        pilot_id = fields.get("pilot_id") or ""
        return (pilot_id or None), "pilot-plan-propose", "project-plan-proposal"
    return None, None, None


def routing_mismatch(record: dict, project_ids: list[str] | None = None) -> dict | None:
    fields = record["fields"]
    routed = fields.get("routing_target") or "head-hermes"
    required, verb, reason = requested_authority(record, project_ids)
    if required is None or required == routed:
        return None
    if not IDENT.fullmatch(routed) or not IDENT.fullmatch(required):
        raise RoutingError("instruction routing authority is not canonical")
    return {"routed_actor": routed, "required_actor": required,
            "requested_verb": verb, "reason": reason}


def allowed_principals() -> set[str]:
    return {part.strip() for part in os.environ.get("RECURSION_INTAKE_ALLOW", "8913126684").split(",")
            if part.strip()}


def validate_authenticated(record: dict) -> None:
    fields = record["fields"]
    if fields.get("principal") not in allowed_principals():
        raise RoutingError("instruction principal is not authenticated")
    for key in ("id", "project_id", "pilot_id", "routing_target"):
        if not IDENT.fullmatch(fields.get(key) or ""):
            raise RoutingError(f"instruction {key} is not canonical")


def validate_instruction_identity(base: Path, record: dict) -> None:
    """Bind the parsed header identity to the canonical instruction filename."""
    iid = record["fields"].get("id") or ""
    if not IDENT.fullmatch(iid) or (iid.isdigit() and iid != str(int(iid))):
        raise RoutingError("instruction id is not canonical")
    if Path(os.path.abspath(record["path"])) != Path(os.path.abspath(instruction_path(base, iid))):
        raise RoutingError("instruction filename and header id differ")


def validate_historical(base: Path, record: dict, *, require_dependency: bool) -> None:
    iid = record["fields"].get("id") or ""
    expected = HISTORICAL_INSTRUCTIONS.get(iid)
    if expected is None:
        return
    if any(record["fields"].get(key) != value for key, value in expected["fields"].items()) \
            or record["body"] != expected["body"]:
        raise RoutingError(f"historical instruction {iid} differs from reviewed recovery evidence")
    for action_id, digest in expected.get("failed_actions", {}).items():
        path = base / "state/actions" / f"{action_id}.json"
        try:
            observed = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise RoutingError(f"historical action evidence is absent: {action_id}") from exc
        if observed != digest:
            raise RoutingError(f"historical action evidence changed: {action_id}")
    if iid == "707":
        observed_noops = set()
        for path in sorted((base / "state/actions").glob("*.json")):
            try:
                action = _read_json_path(path, "historical action")
            except RoutingError:
                continue
            if not isinstance(action, dict):
                continue
            if str(action.get("instruction_id") or "") == "707" \
                    and action.get("verb") == "noop":
                observed_noops.add(path.stem)
        if observed_noops != set(expected.get("failed_actions", {})):
            raise RoutingError("historical instruction 707 failed noop evidence set differs")
    if iid == "732" and require_dependency:
        require_707_terminal(base)


def validate_initial_recovery(record: dict) -> None:
    """Require the reviewed pre-recovery projection before minting its sole decision."""
    fields, document = record["fields"], record["document"]
    iid = fields.get("id") or ""
    if iid == "707":
        first = "action-bb38398a6e0e3859a1a7d263"
        second = "action-4a90005df9c9dfe6cbb7fbad"
        third = "action-cbba158e3295fb82bdf6e06c"
        expected_sha256 = HISTORICAL_INSTRUCTIONS["707"]["initial_document_sha256"]
        resulting = re.findall(r"(?m)^resulting actions: (action-[0-9a-f]{24})$", document)
        reopened = re.findall(
            r"(?m)^deferred/failed actions reopened instruction: (action-[0-9a-f]{24})", document
        )
        if fields.get("status") != "open" or fields.get("action_ids") != third \
                or record["document_sha256"] != expected_sha256 \
                or document.count("## CONSUMED ") != 3 or document.count("## OPEN ") != 3 \
                or document.count(f"resulting actions: {first}") != 1 \
                or document.count(f"resulting actions: {second}") != 1 \
                or document.count(f"resulting actions: {third}") != 1 \
                or document.count(f"reopened instruction: {first}") != 1 \
                or document.count(f"reopened instruction: {second}") != 1 \
                or document.count(f"reopened instruction: {third}") != 1 \
                or document.count("cannot-bind noop evidence is absent") != 3 \
                or resulting != [first, second, third] or reopened != [first, second, third]:
            raise RoutingError("historical instruction 707 recovery annotations differ")
    elif iid == "732":
        if fields.get("status") != "open" or fields.get("action_ids") != "" \
                or re.search(r"(?m)^## (?:CONSUMED|OPEN|ACKNOWLEDGED) ", document):
            raise RoutingError("historical instruction 732 is not the reviewed untouched open record")


def require_707_terminal(base: Path) -> None:
    prior = read_instruction(instruction_path(base, "707"))
    if prior["fields"].get("status") != "acknowledged":
        raise RoutingError("instruction 732 recovery requires instruction 707 to be acknowledged first")
    matches = []
    for path in sorted((base / "state/actions").glob("*.json")):
        try:
            action = _read_json_path(path, "historical action")
        except RoutingError:
            continue
        if not isinstance(action, dict):
            continue
        if str(action.get("instruction_id") or "") == "707" and action.get("verb") == "routing-refuse" \
                and action.get("decision_source") == "routing-reconciler" \
                and action.get("status") == "succeeded":
            matches.append(action)
    if len(matches) != 1:
        raise RoutingError("instruction 707 lacks exactly one succeeded routing-refusal action")
    action = matches[0]
    coupled = {part.strip() for part in prior["fields"].get("action_ids", "").split(",") if part.strip()}
    if action.get("id") not in coupled or action.get("actor") != "head-hermes" \
            or action.get("routed_decider") != "head-hermes" \
            or action.get("goal_id") != "routing-integrity" or action.get("attempts") != 1:
        raise RoutingError("instruction 707 routing-refusal action is not coupled to reviewed authority")
    path = base / "state/actions" / f"{action['id']}.json"
    try:
        prepared = authenticated_transition(
            base, str(action.get("last_transition_id") or ""), journal="state/action-events.jsonl"
        )
    except (OSError, TransitionReceiptError) as exc:
        raise RoutingError(f"instruction 707 routing-refusal action receipt is invalid: {exc}") from exc
    event = prepared.get("event") if isinstance(prepared.get("event"), dict) else {}
    if event.get("action_id") != action.get("id") or event.get("status") != "succeeded" \
            or event.get("attempt") != 1 or event.get("evidence") != action.get("result_evidence") \
            or {str(item.get("path") or "") for item in prepared.get("mutations", [])} \
               != {path.relative_to(base).as_posix()}:
        raise RoutingError("instruction 707 routing-refusal action receipt disagrees")


def load_decision_queue(base: Path):
    path = base / "scripts/decision-queue.py"
    spec = importlib.util.spec_from_file_location("recursion_decision_queue", path)
    if spec is None or spec.loader is None:
        raise RoutingError("decision queue module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def exact_head_window_decision(base: Path, record: dict) -> dict | None:
    """Shape one exact authenticated project-aware window command without a model call."""
    terms = project_window_open_terms(record["body"])
    if terms is None:
        return None
    project, goal_id, window_id, seconds = terms
    fields = record["fields"]
    if fields.get("principal") not in allowed_principals():
        raise RoutingError("exact Head window instruction principal is not authenticated")
    if fields.get("routing_target") != "head-hermes":
        raise RoutingError("exact Head window instruction is not routed to Head Hermes")
    if fields.get("status") != "open":
        raise RoutingError("exact Head window instruction is not open")
    if not all(IDENT.fullmatch(value) for value in (project, goal_id, window_id)):
        raise RoutingError("exact Head window instruction bounds are not canonical")
    if not seconds.isdigit() or not 1 <= int(seconds) <= 86400:
        raise RoutingError("exact Head window instruction duration is outside bounds")
    config_path = base / "state/project-config" / f"{project}.json"
    if config_path.is_symlink() or not config_path.is_file():
        raise RoutingError("exact Head window instruction project is not configured")
    try:
        config = _read_json_path(config_path, "project configuration")
    except (OSError, RoutingError, json.JSONDecodeError) as exc:
        raise RoutingError("exact Head window instruction project configuration is unreadable") from exc
    if not isinstance(config, dict):
        raise RoutingError("exact Head window instruction project configuration is not an object")
    pilot_id = str(config.get("pilot_id") or "")
    if config.get("id") != project or config.get("status") != "active" \
            or not IDENT.fullmatch(pilot_id):
        raise RoutingError("exact Head window instruction project binding is invalid")
    if fields.get("project_id") not in {"", project} \
            or fields.get("pilot_id") not in {"", pilot_id}:
        raise RoutingError("exact Head window instruction carries conflicting scoped ownership")
    pause = base / "state/PAUSE"
    if pause.is_symlink() or not pause.is_file():
        raise RoutingError("exact Head window instruction requires PAUSE")
    iid = fields.get("id") or ""
    signature = instruction_signature(record)
    raw = {
        "actor": "head-hermes",
        "verb": "pilot-window-open",
        "args": terms,
        "text": "Authorize the exact authenticated bounded pilot window while PAUSE remains asserted.",
        "instruction_id": iid,
        "project": project,
        "goal": goal_id,
        "why": f"Instruction {iid} exactly authorizes the named bounded pilot window while PAUSE is asserted.",
        "wrong_if": "the authenticated instruction, project binding, goal, window, duration, or PAUSE changes",
        "decision_source": "instruction-reconciler",
        "routed_decider": "head-hermes",
        "idempotency_key": f"instruction-window-open:{iid}:{signature}",
    }
    return load_decision_queue(base).normalize(raw, stamp=now_iso())


def refusal_decision(base: Path, record: dict) -> dict:
    validate_authenticated(record)
    validate_historical(base, record, require_dependency=True)
    validate_initial_recovery(record)
    mismatch = routing_mismatch(record, configured_project_ids(base))
    if mismatch is None:
        raise RoutingError("instruction has no deterministic actor/verb mismatch")
    fields = record["fields"]
    iid, project = fields["id"], fields["project_id"]
    signature = instruction_signature(record)
    why = (
        f"Instruction {iid} requests {mismatch['requested_verb']} but is routed to "
        f"{mismatch['routed_actor']}; refuse and escalate without a model call."
    )
    raw = {
        "actor": mismatch["routed_actor"], "verb": "routing-refuse",
        "args": [project, signature], "project": project, "goal": "routing-integrity",
        "instruction_id": iid, "why": why,
        "wrong_if": "the authenticated instruction routing or requested authority changes",
        "decision_source": "routing-reconciler", "routed_decider": mismatch["routed_actor"],
        "idempotency_key": f"routing-refusal:{iid}:{signature}",
    }
    return load_decision_queue(base).normalize(raw, stamp=now_iso())


def admit(base: Path, path: Path, *, enqueue: bool) -> tuple[int, dict | None]:
    record = read_instruction(path)
    # A classifier change must never bypass pinned recovery evidence. Bind identity and validate
    # every known historical record before the ordinary no-mismatch success path is available.
    validate_instruction_identity(base, record)
    validate_historical(base, record, require_dependency=True)
    if (record["fields"].get("id") or "") in HISTORICAL_INSTRUCTIONS:
        validate_initial_recovery(record)
    if routing_mismatch(record, configured_project_ids(base)) is None:
        decision = exact_head_window_decision(base, record)
        if decision is None:
            return 0, None
        queue_path = None
        if enqueue:
            queue_path = load_decision_queue(base).enqueue(base, decision, journal=True)
        return 11, {"decision": decision, "queue_path": str(queue_path) if queue_path else None}
    decision = refusal_decision(base, record)
    queue_path = None
    if enqueue:
        queue_path = load_decision_queue(base).enqueue(base, decision, journal=True)
    return 10, {"decision": decision, "queue_path": str(queue_path) if queue_path else None}


def refusal_event(base: Path, decision: dict) -> dict:
    if decision.get("decision_source") != "routing-reconciler" \
            or decision.get("verb") != "routing-refuse" \
            or decision.get("actor") != decision.get("routed_decider"):
        raise RoutingError("routing-refusal decision provenance is invalid")
    iid = str(decision.get("instruction_id") or "")
    args = decision.get("args", decision.get("arguments", [])) or []
    if len(args) != 2 or not IDENT.fullmatch(str(args[0])) or not SHA256.fullmatch(str(args[1])):
        raise RoutingError("routing-refusal decision arguments are invalid")
    record = read_instruction(instruction_path(base, iid))
    validate_authenticated(record)
    validate_historical(base, record, require_dependency=True)
    mismatch = routing_mismatch(record, configured_project_ids(base))
    if mismatch is None:
        raise RoutingError("routing-refusal no longer has an actor/verb mismatch")
    signature = instruction_signature(record)
    if args != [record["fields"].get("project_id"), signature] \
            or decision.get("actor") != mismatch["routed_actor"]:
        raise RoutingError("routing-refusal decision disagrees with current instruction evidence")
    detail = {
        "instruction_id": iid, "instruction_signature": signature,
        "routed_actor": mismatch["routed_actor"], "required_actor": mismatch["required_actor"],
        "requested_verb": mismatch["requested_verb"], "reason": "actor-verb-mismatch",
    }
    return {
        "schema_version": 1, "id": stable_id("source-routing-refused-", iid, signature),
        "ts": now_iso(), "event": "instruction-routing-refused", "task": "",
        "project": record["fields"]["project_id"], "goal": "routing-integrity",
        "pid": os.getpid(), "detail": detail,
    }


def perform(base: Path, decision: dict) -> tuple[int, str]:
    try:
        event = refusal_event(base, decision)
        if os.environ.get("RECURSION_ROUTING_FAILPOINT") == "perform-hang":
            time.sleep(300)
        return 0, str(event["id"])
    except (RoutingError, OSError, ValueError, json.JSONDecodeError) as exc:
        return 4, str(exc)


def record_refusal(base: Path, decision: dict) -> tuple[int, str]:
    try:
        event = refusal_event(base, decision)
        if os.environ.get("RECURSION_ROUTING_FAILPOINT") == "record-error":
            return 4, "routing failpoint: record-error"
        if os.environ.get("RECURSION_ROUTING_FAILPOINT") == "before-event":
            return 75, "routing failpoint: before-event"
        commit_event(base, event)
        if os.environ.get("RECURSION_ROUTING_FAILPOINT") == "after-event":
            return 75, "routing failpoint: after-event"
        return 0, str(event["id"])
    except TransitionReceiptError as exc:
        if "transition failpoint:" in str(exc):
            return 75, str(exc)
        return 4, str(exc)
    except (RoutingError, OSError, ValueError, json.JSONDecodeError) as exc:
        return 4, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.environ.get("RECURSION_ROOT"))
    sub = parser.add_subparsers(dest="command", required=True)
    route = sub.add_parser("route")
    route.add_argument("body")
    route.add_argument("binding_json")
    route.add_argument("project_ids_json")
    admission = sub.add_parser("admit")
    admission.add_argument("instruction")
    admission.add_argument("--enqueue", action="store_true")
    execute = sub.add_parser("perform")
    execute.add_argument("decision_json")
    record = sub.add_parser("record")
    record.add_argument("decision_json")
    args = parser.parse_args()
    base = _audit_path(Path(args.root or Path(__file__).resolve().parents[1]), "routing root")
    try:
        if args.command == "route":
            print(route_for(args.body, _parse_json_arg(args.binding_json),
                            _parse_json_arg(args.project_ids_json)))
            return 0
        if args.command == "admit":
            rc, result = admit(base, Path(args.instruction), enqueue=args.enqueue)
            if result is not None:
                print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            return rc
        decision = _parse_json_arg(args.decision_json)
        rc, evidence = (perform if args.command == "perform" else record_refusal)(base, decision)
        stream = sys.stdout if rc == 0 else sys.stderr
        print(evidence, file=stream)
        return rc
    except (RoutingError, OSError, ValueError, TypeError, OverflowError, RecursionError,
            UnicodeError, json.JSONDecodeError, subprocess.TimeoutExpired):
        print("instruction-routing: operation refused", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
