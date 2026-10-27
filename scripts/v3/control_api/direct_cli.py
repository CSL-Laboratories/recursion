"""Thin direct-CLI fallback: one fake-driver run, no Control API.

NOT X2. This path bypasses the Control API: no authentication, no tenant, no
journal, no session registry beyond the adapter's own busy-reject. It exists only
so a demo has something to show if the Control API slips, and every output says so.
Fake driver only; the transport is the in-process fake, so no provider can run.
"""

from __future__ import annotations

from typing import Any

from scripts.v3.control_api.auth import SCOPE_OBSERVE
from scripts.v3.control_api.fake_engines import (
    FAKE_CREDENTIAL,
    FAKE_MODEL,
    FAKE_PROVIDER,
    VERDICT_SCHEMA,
    build_fake_stack,
)
from scripts.v3.route_adapter.credentials import CredentialRef
from scripts.v3.route_adapter.session import SessionKey
from scripts.v3.route_contract.model import ExecutionRequest, canonical_json, sha256_digest

LABEL = "direct-cli-fallback"
NEUTRAL_WORKLOAD_ID = "direct-cli"
NEUTRAL_PROCESS_REQUIREMENT_ID = "proc-req-direct-cli"
TENANT = "tenant-direct"


def run_direct(engine: str, prompt: str, *, script: tuple[str, ...] = ()) -> dict[str, Any]:
    stack = build_fake_stack(scripts={engine: script} if script else None)
    stack.issue("direct-cli", TENANT, {SCOPE_OBSERVE})
    adapters = stack.service.adapters
    if engine not in adapters:
        raise ValueError("unknown_engine")
    adapter = adapters[engine]
    request = ExecutionRequest(
        request_id="req-1",
        route_id=f"route-{engine}",
        engine_id=engine,
        route_descriptor_digest=sha256_digest(canonical_json(adapter.support())),
        process_requirement_id=NEUTRAL_PROCESS_REQUIREMENT_ID,
        workload_id=NEUTRAL_WORKLOAD_ID,
        session=SessionKey(TENANT, f"route-{engine}", "direct"),
        credential=CredentialRef(tenant_id=TENANT, provider=FAKE_PROVIDER, name=FAKE_CREDENTIAL),
        cli_model=FAKE_MODEL,
        prompt=prompt,
        output_schema=VERDICT_SCHEMA,
        timeout_s=30.0,
    )
    outcome = adapter.execute(request)
    return {
        "path": LABEL,
        "satisfies_x2": False,
        "notice": "Direct fake-driver run. Bypasses the Control API. Not X2.",
        "live_calls": False,
        "request": request.to_record(),
        "result": outcome.to_record(request),
    }
