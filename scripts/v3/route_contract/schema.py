"""Build and load the Route Contract v0 JSON Schema.

The committed ``schema.json`` is generated. Frozen M1B definitions are copied
byte for byte from the frozen execution schema (never edited), so the projection
cannot drift without the tests failing. Regenerate with:

    python3 -X utf8 -B -m scripts.v3.route_contract.schema --write
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
FROZEN_PATH = ROOT / "schemas" / "control-plane" / "v3" / "execution" / "schema.json"
CONTRACT_PATH = Path(__file__).resolve().parent / "schema.json"
CONTRACT_ID = "https://recursion.local/scripts/v3/route-contract/schema.json"

PROJECTED = (
    "RouteCapabilityDescriptor",
    "RouteRequirement",
    "SandboxDescriptor",
    "ProcessRequirement",
    "ProcessCapabilityDescriptor",
    "ProcessTerminationObservation",
)

ERROR_TOKENS = (
    "breaker_open",
    "empty_output",
    "limit_or_error",
    "output_too_large",
    "repair_empty_output",
    "repair_limit_or_error",
    "repair_still_invalid",
    "repair_timeout",
    "repair_vendor_error",
    "timeout",
    "unparseable_output",
    "vendor_error_envelope",
)
REFUSAL_CODES = (
    "busy",
    "custody_denied",
    "cwd_refused",
    "secret_refused",
    "tool_policy_refused",
    "unsupported",
)
STATUSES = ("limit_hit", "needs_human_review", "ok", "timeout")
ENGINE_PROFILES = ("claude", "codex")


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/$defs/{name}"}


def _nullable(name: str) -> dict[str, Any]:
    return {"anyOf": [_ref(name), {"type": "null"}]}


def _record(type_name: str, properties: dict[str, Any], **extra: Any) -> dict[str, Any]:
    body = {
        "additionalProperties": False,
        "properties": {"type": {"const": type_name}, **properties},
        "required": ["type", *properties],
        "type": "object",
    }
    body.update(extra)
    return body


def _new_defs() -> dict[str, Any]:
    token = {"maxLength": 96, "minLength": 1, "pattern": "^[^/ ]+$", "type": "string"}
    return {
        "Token": token,
        "CredentialRef": _record(
            "CredentialRef@0",
            {"tenant_id": _ref("Token"), "provider": _ref("Token"), "name": _ref("Token")},
        ),
        "SessionRef": _record(
            "SessionRef@0",
            {"tenant": _ref("Token"), "route": _ref("Token"), "session": _ref("Token")},
        ),
        "EngineProfile": {"enum": list(ENGINE_PROFILES)},
        "ErrorToken": {"enum": list(ERROR_TOKENS)},
        "RefusalCode": {"enum": list(REFUSAL_CODES)},
        "UsageState": {"enum": ["degraded", "exhausted", "ok"]},
        "OutputContract": _record(
            "OutputContract@0",
            {
                "format": {"const": "json"},
                "schema_digest": _ref("Digest"),
                "schema_path_ref": _nullable("OpaqueRef"),
                "repair_attempts": {"const": 1},
                "max_output_bytes": _ref("PositiveCount"),
                "raw_output_retained": {"const": False},
            },
        ),
        "RouteExecutionRequest": _record(
            "RouteExecutionRequest@0",
            {
                "id": _ref("Id"),
                "route_id": _ref("Id"),
                "engine_id": _ref("EngineProfile"),
                "route_descriptor_digest": _ref("Digest"),
                "process_requirement_id": _ref("Id"),
                "workload_id": _ref("Id"),
                "session": _ref("SessionRef"),
                "credential_ref": _ref("CredentialRef"),
                "cli_model": _ref("OpaqueRef"),
                "tool_profile": {"const": "zero-tools"},
                "allowed_tools": {"items": {"type": "string"}, "maxItems": 0, "type": "array"},
                "prompt_channel": {"const": "stdin"},
                "prompt_digest": _ref("Digest"),
                "max_turns": _ref("PositiveCount"),
                "timeout_ms": _ref("PositiveCount"),
                "cwd_ref": _nullable("OpaqueRef"),
                "privacy_class": _ref("PrivacyClass"),
                "output": _ref("OutputContract"),
            },
        ),
        "RouteHealthEvent": _record(
            "RouteHealthEvent@0",
            {
                "engine_id": _ref("EngineProfile"),
                "state": {"enum": ["degraded", "exhausted"]},
                "error": _ref("ErrorToken"),
            },
        ),
        "RouteExecutionResult": _record(
            "RouteExecutionResult@0",
            {
                "request_id": _ref("Id"),
                "status": {"enum": list(STATUSES)},
                "error": {"anyOf": [_ref("ErrorToken"), {"type": "null"}]},
                "dispatched": {"type": "boolean"},
                "repaired": {"type": "boolean"},
                "usage_state": _ref("UsageState"),
                "output_digest": _nullable("Digest"),
                "validated": {"anyOf": [{"type": "object"}, {"type": "null"}]},
                "health_event": _nullable("RouteHealthEvent"),
                "termination": _nullable("ProcessTerminationObservation"),
            },
            allOf=[
                {
                    "if": {"properties": {"status": {"const": "ok"}}, "required": ["status"]},
                    "then": {
                        "properties": {
                            "error": {"type": "null"},
                            "validated": {"type": "object"},
                            "health_event": {"type": "null"},
                            "termination": {"type": "null"},
                            "dispatched": {"const": True},
                            "usage_state": {"const": "ok"},
                        }
                    },
                    "else": {"properties": {"validated": {"type": "null"}, "error": _ref("ErrorToken")}},
                },
                {
                    "if": {
                        "properties": {"status": {"const": "timeout"}},
                        "required": ["status"],
                    },
                    "then": {
                        "properties": {
                            "health_event": _ref("RouteHealthEvent"),
                            "termination": _ref("ProcessTerminationObservation"),
                            "dispatched": {"const": True},
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"status": {"const": "limit_hit"}, "dispatched": {"const": True}},
                        "required": ["status", "dispatched"],
                    },
                    "then": {
                        "properties": {
                            "health_event": _ref("RouteHealthEvent"),
                            "termination": {"type": "null"},
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"status": {"const": "limit_hit"}, "dispatched": {"const": False}},
                        "required": ["status", "dispatched"],
                    },
                    "then": {
                        "properties": {
                            "health_event": {"type": "null"},
                            "termination": {"type": "null"},
                        }
                    },
                },
                {
                    "if": {
                        "properties": {"status": {"const": "needs_human_review"}},
                        "required": ["status"],
                    },
                    "then": {
                        "properties": {
                            "health_event": {"type": "null"},
                            "termination": {"type": "null"},
                        }
                    },
                },
            ],
        ),
        "RouteRefusal": _record(
            "RouteRefusal@0",
            {
                "request_id": _ref("Id"),
                "code": _ref("RefusalCode"),
                "dispatched": {"const": False},
            },
        ),
        "AdapterSupport": _record(
            "AdapterSupport@0",
            {
                "engine_id": _ref("Id"),
                "adapter_kind": {"enum": ["fake", "live"]},
                "state": {"enum": ["supported", "unsupported"]},
                "reason": {"enum": ["awaiting-csl-460", "fake-driver"]},
                "live_calls": {"const": False},
            },
            allOf=[
                {
                    "if": {
                        "properties": {"adapter_kind": {"const": "live"}},
                        "required": ["adapter_kind"],
                    },
                    "then": {
                        "properties": {
                            "state": {"const": "unsupported"},
                            "reason": {"const": "awaiting-csl-460"},
                        }
                    },
                    "else": {
                        "properties": {
                            "state": {"const": "supported"},
                            "reason": {"const": "fake-driver"},
                        }
                    },
                }
            ],
        ),
    }


NEW_RECORDS = (
    "RouteExecutionRequest",
    "RouteExecutionResult",
    "RouteHealthEvent",
    "RouteRefusal",
    "AdapterSupport",
)


def _refs(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/$defs/"):
                found.add(value.rsplit("/", 1)[1])
            else:
                found |= _refs(value)
    elif isinstance(node, list):
        for item in node:
            found |= _refs(item)
    return found


def frozen_closure(frozen: dict[str, Any]) -> dict[str, Any]:
    """Frozen definitions reachable from the projected shapes and the new records."""

    defs = frozen["$defs"]
    new = _new_defs()
    pending = set(PROJECTED)
    for node in new.values():
        pending |= _refs(node) - set(new)
    seen: dict[str, Any] = {}
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen[name] = defs[name]
        pending |= _refs(defs[name]) - set(seen)
    return seen


def build_schema() -> dict[str, Any]:
    frozen = json.loads(FROZEN_PATH.read_text(encoding="utf-8"))
    defs = frozen_closure(frozen)
    defs.update(_new_defs())
    return {
        "$defs": defs,
        "$id": CONTRACT_ID,
        "$schema": frozen["$schema"],
        "oneOf": [_ref(name) for name in (*PROJECTED, *NEW_RECORDS)],
        "title": "Recursion v3 Route Contract v0 (draft)",
    }


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load_schema() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def main(argv: list[str]) -> int:
    text = render(build_schema())
    if argv == ["--write"]:
        CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONTRACT_PATH.write_text(text, encoding="utf-8", newline="\n")
        return 0
    if argv == ["--check"]:
        return 0 if CONTRACT_PATH.read_text(encoding="utf-8") == text else 1
    print("usage: schema.py --write | --check", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
