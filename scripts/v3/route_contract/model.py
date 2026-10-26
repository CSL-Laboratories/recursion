"""Request and result values. ``to_record`` emits the schema's JSON shapes.

Prompt text and raw engine output are never part of a record: records carry
digests only (``raw_output_retained`` is fixed to false).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from scripts.v3.route_adapter.credentials import CredentialRef
from scripts.v3.route_adapter.session_key import SessionKey

STATUS_OK = "ok"
STATUS_NEEDS_HUMAN = "needs_human_review"
STATUS_LIMIT_HIT = "limit_hit"
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"


def sha256_digest(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class ExecutionRequest:
    request_id: str
    route_id: str
    engine_id: str
    route_descriptor_digest: str
    process_requirement_id: str
    workload_id: str
    session: SessionKey
    credential: CredentialRef
    cli_model: str
    prompt: str
    output_schema: dict[str, Any]
    max_turns: int = 6
    timeout_s: float = 120.0
    cwd: str | None = None
    cwd_ref: str | None = None
    schema_path: str | None = None
    schema_path_ref: str | None = None
    allowed_tools: tuple[str, ...] = ()
    privacy_class: str = "internal"
    max_output_bytes: int = 262_144

    def to_record(self) -> dict[str, object]:
        return {
            "type": "RouteExecutionRequest@0",
            "id": self.request_id,
            "route_id": self.route_id,
            "engine_id": self.engine_id,
            "route_descriptor_digest": self.route_descriptor_digest,
            "process_requirement_id": self.process_requirement_id,
            "workload_id": self.workload_id,
            "session": {
                "type": "SessionRef@0",
                "tenant": self.session.tenant,
                "route": self.session.route,
                "session": self.session.session,
            },
            "credential_ref": {"type": "CredentialRef@0", **self.credential.public_dict()},
            "cli_model": self.cli_model,
            "tool_profile": "zero-tools",
            "allowed_tools": list(self.allowed_tools),
            "prompt_channel": "stdin",
            "prompt_digest": sha256_digest(self.prompt),
            "max_turns": self.max_turns,
            "timeout_ms": int(self.timeout_s * 1000),
            "cwd_ref": self.cwd_ref,
            "privacy_class": self.privacy_class,
            "output": {
                "type": "OutputContract@0",
                "format": "json",
                "schema_digest": sha256_digest(canonical_json(self.output_schema)),
                "schema_path_ref": self.schema_path_ref,
                "repair_attempts": 1,
                "max_output_bytes": self.max_output_bytes,
                "raw_output_retained": False,
            },
        }


@dataclass
class ExecutionResult:
    status: str
    error: str | None = None
    dispatched: bool = True
    repaired: bool = False
    usage_state: str = "ok"
    validated: dict[str, Any] | None = None
    session_id: str | None = None
    output_digest: str | None = None
    health_event: dict[str, object] | None = None
    termination: dict[str, object] | None = None
    events: tuple[dict[str, object], ...] = field(default=(), compare=False)

    def to_record(self, request: ExecutionRequest, *, process_epoch: int = 0) -> dict[str, object]:
        termination = None
        if self.termination is not None:
            termination = {
                "process_requirement_id": request.process_requirement_id,
                "process_epoch": process_epoch,
                "exit_evidence_digest": sha256_digest(f"{request.request_id}:{self.error}"),
                **self.termination,
            }
        return {
            "type": "RouteExecutionResult@0",
            "request_id": request.request_id,
            "status": self.status,
            "error": self.error,
            "dispatched": self.dispatched,
            "repaired": self.repaired,
            "usage_state": self.usage_state,
            "output_digest": self.output_digest,
            "validated": self.validated,
            "health_event": self.health_event,
            "termination": termination,
        }
