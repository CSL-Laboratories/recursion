#!/usr/bin/env python3
"""Control: the Phase 6 Cursor Cloud client contract, driven entirely by a fake transport.

Frozen contract: scripts/cursor-cloud-adapter.py and docs/plans/remote-execution-adapters.md;
program context docs/plans/recursion-v2-execution-fabric.md Phase 6.

Every network call is a ``FakeTransport``. The production transport is never constructed here
(scripts/tests/remote-backend.test.py proves it cannot be, without RECURSION_REMOTE_LIVE=1),
so this suite makes no external call by construction rather than by promise.

The four gate properties from the plan, each with a check:
  * a default-branch write is impossible by request construction AND by post-check;
  * host loss, timeout, cancellation, malformed response and API rate limit are typed;
  * a vendor self-report cannot substitute for independent verification;
  * quota exhaustion defers and cannot activate on-demand spend.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / "scripts"))

import remote_assignment as ra  # noqa: E402
import remote_backend as rb  # noqa: E402
from remote_assignment import RemoteError  # noqa: E402

ADAPTER_PATH = SOURCE / "scripts" / "cursor-cloud-adapter.py"
_spec = importlib.util.spec_from_file_location("recursion_cursor_cloud_adapter", ADAPTER_PATH)
cc = importlib.util.module_from_spec(_spec)
sys.modules["recursion_cursor_cloud_adapter"] = cc
_spec.loader.exec_module(cc)

failures: list[str] = []
TEMPDIRS: list[str] = []

ENDPOINT = "https://api.cursor.com"
SHA = "0123456789abcdef0123456789abcdef01234567"
RESULT_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
DISPATCH = "dispatch-0001"

MANIFEST_SOURCE = {
    "schema_version": 1,
    "name": "cursor-cloud",
    "kind": "cursor-cloud",
    "description": "control fixture",
    "endpoint": ENDPOINT,
    "endpoint_ref": None,
    "credential_ref": "secret:CURSOR_CLOUD_API_KEY",
    "signing_key_ref": None,
    "capacity_pool": "cursor-cloud-pool",
    "host_id": "cursor-cloud",
    "billing_basis": "included",
    "on_demand": False,
    "timeout_seconds": 60,
    "max_attempts": 3,
    "concurrency_lease": 2,
    "publication": {
        "default_branch": "main",
        "integration_branch": "recursion/integration",
        "task_branch_pattern": "recursion/extern/<task-id>",
        "allowed_result_branch_patterns": ["recursion/extern/*", "cursor/*"],
    },
}

ASSIGNMENT = {
    "task_id": "t-0042",
    "project_id": "example-ops",
    "source_repository": "https://github.com/NotSkittlz/example-ops",
    "source_branch": "main",
    "source_sha": SHA,
    "engine": "cursor",
    "provider": "cursor",
    "capacity_pool": "cursor-cloud-pool",
    "host": "cursor-cloud",
    "sandbox_profile": "vendor-cloud",
    "publication_policy": "verified-integration-only",
}


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"ok - {label}")
    else:
        print(f"FAIL - {label}")
        failures.append(label)


def raises(code: str, fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
    except RemoteError as exc:
        if exc.code == code:
            return True
        print(f"     (got {exc.code}, wanted {code}: {exc})")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"     (got {type(exc).__name__}: {exc})")
        return False
    return False


def tempdir() -> Path:
    path = tempfile.mkdtemp(prefix="recursion-cursor-test-")
    TEMPDIRS.append(path)
    return Path(path)


def assignment(**overrides) -> dict:
    out = dict(ASSIGNMENT)
    out["created_at"] = "2026-08-28T12:00:00Z"
    out.update(overrides)
    return out


def manifest(**overrides) -> dict:
    payload = json.loads(json.dumps(MANIFEST_SOURCE))
    payload.update(overrides)
    return rb.validate_manifest(payload, "cursor-cloud")


class FakeTransport(rb.Transport):
    """Records every call and answers from a queued route table. Opens nothing."""

    def __init__(self) -> None:
        self.routes: dict[tuple, list] = {}
        self.calls: list[dict] = []
        self.stream_payload: list[str] = []

    def route(self, method: str, path: str, *, status: int = 200, payload: object = None,
              body: bytes | None = None, error: Exception | None = None) -> "FakeTransport":
        self.routes.setdefault((method, path), []).append((status, payload, body, error))
        return self

    def request(self, method, url, headers, body, timeout):
        if not url.startswith(ENDPOINT):
            raise AssertionError(f"call escaped the configured endpoint: {url}")
        path = url[len(ENDPOINT):]
        self.calls.append({"method": method, "path": path, "headers": dict(headers),
                           "body": body, "timeout": timeout})
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"unrouted call: {method} {path}")
        status, payload, raw, error = queue.pop(0) if len(queue) > 1 else queue[0]
        if error is not None:
            raise error
        data = raw if raw is not None else json.dumps(payload if payload is not None
                                                      else {}).encode("utf-8")
        return rb.TransportResponse(status, {}, data)

    def stream_lines(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "path": url[len(ENDPOINT):], "stream": True})
        for line in self.stream_payload:
            yield line

    def posts(self, path: str) -> int:
        return sum(1 for call in self.calls
                   if call.get("method") == "POST" and call.get("path") == path)


def fresh(**manifest_overrides):
    base = tempdir()
    (base / "state").mkdir(parents=True, exist_ok=True)
    transport = FakeTransport()
    adapter = cc.CursorCloudAdapter(base, manifest(**manifest_overrides), transport)
    return base, transport, adapter


def with_session(**manifest_overrides):
    """A fresh adapter with one created run, so each scenario starts from clean routes."""
    base, transport, adapter = fresh(**manifest_overrides)
    transport.route("POST", "/v0/agents", payload={"id": "agent-1", "status": "RUNNING"})
    adapter.create_run(assignment(), "x", DISPATCH)
    return base, transport, adapter


os.environ["CURSOR_CLOUD_API_KEY"] = "cursor-control-fixture-value"

# --- request construction: exact sha and isolated branch -------------------------------------

_, _, adapter = fresh()
body = adapter.build_create_request(assignment(), "do the bounded thing")
check(body["source"]["startingRef"] == SHA,
      "the run is bound to the exact 40-hex source sha")
check(body["workOnCurrentBranch"] is False,
      "workOnCurrentBranch is forced false -- a vendor run never writes the branch it "
      "started from")
check(body["target"]["autoCreatePr"] is False,
      "autoCreatePr is forced false -- Recursion is the sole publisher")
check(body["target"]["branchName"] == "recursion/extern/t-0042",
      "the target branch is derived from the project's task branch pattern")
check("model" not in body, "no model key is sent when the assignment names none")
check(adapter.build_create_request(assignment(model="grok-4.6"), "x")["model"] == "grok-4.6",
      "a named model is passed through")

check(raises("assignment-invalid", adapter.build_create_request,
             assignment(source_sha="main"), "x"),
      "a branch name in source_sha is refused before a request can be built")
check(raises("assignment-invalid", adapter.build_create_request,
             assignment(source_sha=SHA[:12]), "x"),
      "an abbreviated sha is refused")
check(raises("request-invalid", adapter.build_create_request, assignment(), "   "),
      "an empty prompt is refused")
check(raises("redaction-violation", adapter.build_create_request, assignment(),
             "here is my key ghp_" + "a" * 36),
      "a credential in the prompt is refused before it can leave the box")

publication = manifest()["publication"]
for label, mutation in (
    ("workOnCurrentBranch true", {"workOnCurrentBranch": True}),
    ("workOnCurrentBranch truthy-but-not-false", {"workOnCurrentBranch": 0}),
):
    mutated_body = json.loads(json.dumps(body))
    mutated_body.update(mutation)
    check(raises("request-invalid", cc.assert_isolated_branch_request, mutated_body,
                 publication),
          f"the post-check refuses a body with {label}")
for label, branch in (("the default branch", "main"),
                      ("the integration branch", "recursion/integration"),
                      ("an unlisted branch", "wild/whatever")):
    mutated_body = json.loads(json.dumps(body))
    mutated_body["target"]["branchName"] = branch
    check(raises("request-invalid", cc.assert_isolated_branch_request, mutated_body,
                 publication),
          f"the post-check refuses a target of {label}")
mutated_body = json.loads(json.dumps(body))
mutated_body["target"]["autoCreatePr"] = True
check(raises("request-invalid", cc.assert_isolated_branch_request, mutated_body, publication),
      "the post-check refuses a vendor-opened pull request")
mutated_body = json.loads(json.dumps(body))
mutated_body["source"]["startingRef"] = "main"
check(raises("request-invalid", cc.assert_isolated_branch_request, mutated_body, publication),
      "the post-check refuses a moving startingRef")
check(cc.branch_is_allowed("main", publication) is False
      and cc.branch_is_allowed("recursion/integration", publication) is False
      and cc.branch_is_allowed("cursor/abc", publication) is True,
      "default and integration branches are rejected before patterns are consulted")

# --- create: idempotency, PAUSE, and typed failures ---------------------------------------------

base, transport, adapter = fresh()
transport.route("POST", "/v0/agents", payload={"id": "agent-1", "status": "RUNNING"})
session = adapter.create_run(assignment(), "do the bounded thing", DISPATCH)
check(session["run_id"] == "agent-1" and session["state"] == "created",
      "a successful create writes a durable session record")
check(session["source_sha"] == SHA and len(session["assignment_digest"]) == 64,
      "the session records the exact source and the assignment digest")
check(transport.posts("/v0/agents") == 1, "exactly one POST was made")
check(not rb.open_attempts(base, "cursor-cloud"), "the attempt closed")

repeat = adapter.create_run(assignment(), "do the bounded thing", DISPATCH)
check(transport.posts("/v0/agents") == 1,
      "an identical create REPLAYS the stored session -- a second POST would be a second "
      "vendor run and a second branch")
check(repeat["run_id"] == "agent-1", "the replay returns the original session")

recorded = json.dumps(session)
check("cursor-control-fixture-value" not in recorded and "Authorization" not in recorded,
      "no credential and no request header reaches the durable session record")

base, transport, adapter = fresh()
(base / "state" / "PAUSE").write_text("held\n", encoding="utf-8")
transport.route("POST", "/v0/agents", payload={"id": "agent-x"})
check(raises("paused", adapter.create_run, assignment(), "x", DISPATCH),
      "PAUSE refuses a create")
check(not transport.calls, "PAUSE is checked BEFORE anything leaves the box")
check(not rb.read_attempts(base, "cursor-cloud"),
      "a paused create commits no attempt at all")

base, transport, adapter = fresh()
transport.route("POST", "/v0/agents", status=402, payload={"error": "weekly pool exhausted"})
check(raises("quota-exhausted", adapter.create_run, assignment(), "x", DISPATCH),
      "an exhausted pool is a typed quota-exhausted")
check(RemoteError("quota-exhausted", "x").exit_code == 75,
      "quota exhaustion DEFERS; it never activates on-demand spend")
deferred = rb.read_attempts(base, "cursor-cloud")[-1]
check(deferred["state"] == "deferred" and deferred["outcome"] == "quota-exhausted",
      "the attempt is recorded as deferred, not failed")
check(adapter.manifest["on_demand"] is False,
      "no handler in the create path can set on_demand")

base, transport, adapter = fresh()
transport.route("POST", "/v0/agents", status=429, payload={"error": "slow down"})
check(raises("rate-limited", adapter.create_run, assignment(), "x", DISPATCH),
      "an API rate limit is typed and retryable")
check(rb.read_attempts(base, "cursor-cloud")[-1]["state"] == "open",
      "a rate-limited create remains open because the provider may have accepted it")

base, transport, adapter = fresh()
transport.route("POST", "/v0/agents", status=503, payload={"error": "temporarily unavailable"})
check(raises("provider-unavailable", adapter.create_run, assignment(), "x", DISPATCH),
      "an unavailable provider is typed")
check(rb.read_attempts(base, "cursor-cloud")[-1]["state"] == "open",
      "an HTTP 503 after create remains open despite retryable exit code 75")

base, transport, adapter = fresh()
transport.route("POST", "/v0/agents",
                error=RemoteError("transport-error", "connection reset"))
check(raises("transport-error", adapter.create_run, assignment(), "x", DISPATCH),
      "host loss is a typed retryable transport error")
check(rb.read_attempts(base, "cursor-cloud")[-1]["state"] == "open",
      "an ambiguous transport failure leaves the create open for reconciliation")

base, transport, adapter = fresh()
transport.route("POST", "/v0/agents", error=RemoteError("transport-timeout", "timed out"))
check(raises("transport-timeout", adapter.create_run, assignment(), "x", DISPATCH),
      "a timeout is typed")

base, transport, adapter = fresh()
transport.route("POST", "/v0/agents", payload={"status": "RUNNING"})
check(raises("malformed-response", adapter.create_run, assignment(), "x", DISPATCH),
      "a create response with no run id is a malformed-response, not a silent success")

base, transport, adapter = fresh()
transport.route("POST", "/v0/agents", body=b"<html>gateway</html>")
check(raises("malformed-response", adapter.create_run, assignment(), "x", DISPATCH),
      "a non-JSON body is a malformed-response")

# --- reads do not consume attempts ----------------------------------------------------------------

_, _, adapter = fresh()
check(raises("request-invalid", adapter._begin, DISPATCH, "status", "d" * 64, "r" * 64),
      "a read operation cannot open a durable attempt -- attempts answer 'did a MUTATION land'")

# --- status, backoff, and unknown vendor states ------------------------------------------------------

base, transport, adapter = fresh()
transport.route("POST", "/v0/agents", payload={"id": "agent-1", "status": "RUNNING"})
adapter.create_run(assignment(), "x", DISPATCH)
transport.route("GET", "/v0/agents/agent-1", payload={"status": "RUNNING"})
transport.route("GET", "/v0/agents/agent-1", payload={"status": "RUNNING"})
transport.route("GET", "/v0/agents/agent-1", payload={"status": "FINISHED"})
sleeps: list[int] = []
final = adapter.poll_status(DISPATCH, max_polls=5, sleeper=sleeps.append)
check(final["state"] == "completed", "polling stops at a terminal state")
check(sleeps == [2, 4], "the poll schedule backs off exponentially, bounded")

check(cc.map_vendor_state("QUEUED") == "created"
      and cc.map_vendor_state("in_progress") == "running"
      and cc.map_vendor_state("CANCELED") == "cancelled",
      "known vendor states map onto Recursion states")
check(raises("malformed-response", cc.map_vendor_state, "SOMETHING_NEW"),
      "an unrecognised vendor state is refused -- guessing 'finished' imports a half-built "
      "candidate")
hostile_status = "Bearer ghp_" + "z" * 36
try:
    cc.map_vendor_state(hostile_status)
except RemoteError as exc:
    hostile_status_error = str(exc)
else:
    hostile_status_error = ""
check(hostile_status not in hostile_status_error,
      "an unrecognised vendor state never echoes credential-shaped material")
check(raises("malformed-response", cc.map_vendor_state, None),
      "a missing vendor state is refused")

# --- streaming ------------------------------------------------------------------------------------------

base, transport, adapter = fresh()
transport.route("POST", "/v0/agents", payload={"id": "agent-1", "status": "RUNNING"})
adapter.create_run(assignment(), "x", DISPATCH)
transport.stream_payload = [": keep-alive", "", 'data: {"event":"start"}',
                            '{"event":"edit"}', "data: [DONE]", '{"never":"reached"}']
events = list(adapter.stream_events(DISPATCH))
check([item.get("event") for item in events] == ["start", "edit"],
      "the stream skips comments and blanks, unwraps SSE framing, and stops at [DONE]")
transport.stream_payload = ["data: {oops"]
check(raises("malformed-response", lambda: list(adapter.stream_events(DISPATCH))),
      "a malformed stream line is a typed error, never a silently skipped line")
try:
    list(adapter.stream_events(DISPATCH))
except RemoteError as exc:
    stream_error = str(exc)
else:
    stream_error = ""
check("oops" not in stream_error and "JSONDecodeError" not in stream_error,
      "a malformed provider stream hides parser text")

# --- cancel ------------------------------------------------------------------------------------------------

base, transport, adapter = fresh()
transport.route("POST", "/v0/agents", payload={"id": "agent-1", "status": "RUNNING"})
adapter.create_run(assignment(), "x", DISPATCH)
transport.route("POST", "/v0/agents/agent-1/cancel", payload={"status": "CANCELLED"})
cancelled = adapter.cancel(DISPATCH)
check(cancelled["state"] == "cancelled",
      "cancel reconciles the state the provider actually reports")
adapter.cancel(DISPATCH)
check(transport.posts("/v0/agents/agent-1/cancel") == 1,
      "a repeated cancel replays rather than issuing a second request")

for label, response in (("missing status", {}), ("RUNNING status", {"status": "RUNNING"}),
                        ("CREATED status", {"status": "CREATING"})):
    base, transport, adapter = with_session()
    transport.route("POST", "/v0/agents/agent-1/cancel", payload=response)
    expected = "malformed-response" if not response else "unreconciled-attempt"
    check(raises(expected, adapter.cancel, DISPATCH),
          f"a cancel with {label} does not report success")
    check(bool(rb.open_attempts(base, "cursor-cloud")),
          f"a cancel with {label} leaves its attempt open for reconciliation")

# --- artifacts -----------------------------------------------------------------------------------------------

base, transport, adapter = fresh()
transport.route("POST", "/v0/agents", payload={"id": "agent-1", "status": "RUNNING"})
adapter.create_run(assignment(), "x", DISPATCH)
transport.route("GET", "/v0/agents/agent-1/artifacts",
                payload={"artifacts": [{"id": "a-1", "name": "test.log", "bytes": 12}]})
listed = adapter.list_artifacts(DISPATCH)
check(listed == [{"id": "a-1", "name": "test.log", "bytes": 12}], "artifacts enumerate")
transport.route("GET", "/v0/agents/agent-1/artifacts/a-1", body=b"all tests passed")
payload, row = adapter.download_artifact(DISPATCH, "a-1")
check(payload == b"all tests passed", "an artifact downloads byte-exact")
check(row["evidence_class"] == "advisory" and len(row["sha256"]) == 64,
      "a vendor artifact is ADVISORY evidence with a recorded digest, not proof")
base, transport, adapter = with_session()
transport.route("GET", "/v0/agents/agent-1/artifacts", payload={"artifacts": [{"name": "x"}]})
check(raises("malformed-response", adapter.list_artifacts, DISPATCH),
      "an artifact entry with no id is a malformed-response")

for hostile_id in ("../secret", "a/b", "a?token=1", "a#fragment", ".", "..", "a%2Fb"):
    base, transport, adapter = with_session()
    transport.route("GET", "/v0/agents/agent-1/artifacts",
                    payload={"artifacts": [{"id": hostile_id, "bytes": 1}]})
    check(raises("malformed-response", adapter.list_artifacts, DISPATCH),
          f"a provider artifact id {hostile_id!r} is one safe URL path segment")

for byte_count in (True, 1.0, "1", -1):
    base, transport, adapter = with_session()
    transport.route("GET", "/v0/agents/agent-1/artifacts",
                    payload={"artifacts": [{"id": "a-1", "bytes": byte_count}]})
    check(raises("malformed-response", adapter.list_artifacts, DISPATCH),
          f"artifact byte count {byte_count!r} is rejected unless it is an exact non-negative int")

for hostile_id in ("../secret", "a/b", "a?token=1", "a#fragment", ".", ".."):
    base, transport, adapter = with_session()
    check(raises("request-invalid", adapter.download_artifact, DISPATCH, hostile_id),
          f"download refuses artifact id {hostile_id!r} before constructing a provider URL")

# --- usage and pool attribution ----------------------------------------------------------------------------------

base, transport, adapter = fresh()
transport.route("POST", "/v0/agents", payload={"id": "agent-1", "status": "RUNNING"})
adapter.create_run(assignment(), "x", DISPATCH)
transport.route("GET", "/v0/agents/agent-1/usage",
                payload={"usage": {"input_tokens": 10, "output_tokens": 20,
                                   "total_tokens": 30}, "billing": "included"})
usage = adapter.get_usage(DISPATCH)
check(usage["capacity_pool"] == "cursor-cloud-pool" and usage["host_id"] == "cursor-cloud"
      and usage["provider"] == "cursor",
      "usage is attributed to the pool and host that paid for it")
check(usage["tokens"]["total_tokens"] == 30 and usage["spend_state"] == "included",
      "an included pool reports included spend")
check(usage["on_demand"] is False, "usage never reports on-demand spend")
check(rb.read_usage(base, "cursor-cloud", DISPATCH)["spend_state"] == "included",
      "usage is durably persisted under the remote backend state")

# Rotate the live manifest after admission. Historical usage must remain bound to the
# authenticated snapshot captured with the session, not to the new pool/billing policy.
adapter.manifest.update({"capacity_pool": "rotated-pool", "host_id": "rotated-host",
                         "billing_basis": "metered", "on_demand": True})
transport.route("GET", "/v0/agents/agent-1/usage",
                payload={"usage": {"total_tokens": 31}, "billing": "metered"})
rotated_usage = adapter.get_usage(DISPATCH)
check(rotated_usage["capacity_pool"] == "cursor-cloud-pool"
      and rotated_usage["host_id"] == "cursor-cloud"
      and rotated_usage["billing_basis"] == "included"
      and rotated_usage["on_demand"] is False,
      "usage after manifest rotation retains the admitted pool/host/billing snapshot")

tampered = rb.read_session(base, "cursor-cloud", DISPATCH)
tampered["admission_snapshot"]["capacity_pool"] = "forged-pool"
rb.write_session(base, "cursor-cloud", DISPATCH, tampered)
transport.route("GET", "/v0/agents/agent-1/usage", payload={"usage": {"total_tokens": 32}})
check(raises("malformed-response", adapter.get_usage, DISPATCH),
      "usage rejects a missing or mismatched admission snapshot authentication")

base, transport, adapter = fresh(billing_basis="metered")
transport.route("POST", "/v0/agents", payload={"id": "agent-1", "status": "RUNNING"})
adapter.create_run(assignment(), "x", DISPATCH)
transport.route("GET", "/v0/agents/agent-1/usage",
                payload={"usage": {"total_tokens": 5}, "billing": "included",
                         "included": True})
metered_usage = adapter.get_usage(DISPATCH)
check(metered_usage["spend_state"] == "metered-unreconciled",
      "a METERED run cannot report itself as included usage, whatever the vendor payload says")

base, transport, adapter = fresh(on_demand=True)
transport.route("POST", "/v0/agents", payload={"id": "agent-1", "status": "RUNNING"})
adapter.create_run(assignment(), "x", DISPATCH)
transport.route("GET", "/v0/agents/agent-1/usage",
                payload={"usage": {"total_tokens": 2}, "billing": "included"})
on_demand_usage = adapter.get_usage(DISPATCH)
check(on_demand_usage["on_demand"] is True
      and on_demand_usage["spend_state"] == "metered-unreconciled",
      "on-demand attribution comes from the authorized manifest, not provider billing claims")

base, transport, adapter = fresh()
transport.route("POST", "/v0/agents", payload={"id": "agent-1", "status": "RUNNING"})
adapter.create_run(assignment(), "x", DISPATCH)
transport.route("GET", "/v0/agents/agent-1/usage",
                payload={"usage": {"total_tokens": -1}})
check(raises("malformed-response", adapter.get_usage, DISPATCH),
      "a negative token count is a malformed-response")

# --- returned ref / commit reconciliation and the verification boundary -----------------------------------------------

base, transport, adapter = with_session()
vendor = {
    "status": "FINISHED",
    "target": {"branchName": "cursor/agent-1", "commit": RESULT_COMMIT},
    "tests_passed": True,
    "verified": True,
    "summary": "I ran the tests and they all pass.",
}
record = adapter.reconciliation_input(DISPATCH, vendor)
check(record["verification_state"] == "requires-independent-verification",
      "the reconciliation input ALWAYS requires independent verification")
check(record["vendor_self_report_is_evidence"] is False and record["gate_evidence"] is None,
      "a vendor claiming tests_passed and verified carries no gate evidence")
check(record["repositories"][0]["result_commit"] == RESULT_COMMIT
      and record["repositories"][0]["result_branch"] == "cursor/agent-1"
      and record["repositories"][0]["source_sha"] == SHA,
      "the returned branch and commit are recorded against the exact source sha")
check(record["advisory_only"]["vendor_reported_state"] == "FINISHED"
      and len(record["advisory_only"]["vendor_report_digest"]) == 64
      and "tests_passed" in record["advisory_only"]["vendor_claim_keys"],
      "the vendor's claims are kept as an advisory digest and key inventory, not ingested")
check("I ran the tests" not in json.dumps(record),
      "unvetted vendor prose does not enter canonical evidence verbatim")
check("verified" not in json.dumps(record["repositories"][0]).replace(
          "requires-independent-verification", ""),
      "no repository row can be marked verified")
check(cc.VERIFICATION_STATE == "requires-independent-verification",
      "there is exactly one verification_state constant, and it is not 'verified'")

for label, branch in (("the default branch", "main"),
                      ("the integration branch", "recursion/integration"),
                      ("an unlisted branch", "wild/anything")):
    base, transport, adapter = with_session()
    check(raises("unauthorized-result", adapter.reconciliation_input, DISPATCH,
                 {"status": "FINISHED",
                  "target": {"branchName": branch, "commit": RESULT_COMMIT}}),
          f"a result returned on {label} is unauthorized")

base, transport, adapter = with_session()
check(raises("malformed-response", adapter.reconciliation_input, DISPATCH,
             {"status": "FINISHED",
              "target": {"branchName": "cursor/x", "commit": "abc1234"}}),
      "a returned commit that is not a 40-hex sha is a malformed-response")

base, transport, adapter = with_session()
check(raises("unauthorized-result", adapter.reconciliation_input, DISPATCH,
             {"status": "FINISHED",
              "target": {"branchName": "cursor/x", "commit": SHA}}),
      "a returned commit equal to source_sha produced no candidate to verify")

base, transport, adapter = with_session()
check(raises("unauthorized-result", adapter.reconciliation_input, DISPATCH,
             {"status": "FINISHED", "repositories": [
                 {"repository": "https://github.com/NotSkittlz/example-project",
                  "branch": "cursor/x", "commit": RESULT_COMMIT}]}),
      "a result for a repository the assignment did not authorise is unauthorized")

multi_assignment = assignment(authorized_repositories=[
    {"repository": "https://github.com/NotSkittlz/example-project",
     "source_branch": "main", "source_sha": "1" * 40},
])
base, transport, adapter = fresh()
transport.route("POST", "/v0/agents", payload={"id": "agent-1", "status": "RUNNING"})
adapter.create_run(multi_assignment, "x", DISPATCH)
multi = adapter.reconciliation_input(
    DISPATCH,
    {"status": "FINISHED", "repositories": [
        {"repository": "https://github.com/NotSkittlz/example-ops",
         "branch": "cursor/one", "commit": RESULT_COMMIT},
        {"repository": "https://github.com/NotSkittlz/example-project",
         "branch": "cursor/two", "commit": "2" * 40}]},
    authorized_repositories=["https://github.com/NotSkittlz/example-ops",
                             "https://github.com/NotSkittlz/example-project"])
check(len(multi["repositories"]) == 2
      and {row["result_branch"] for row in multi["repositories"]} == {"cursor/one", "cursor/two"},
      "a multi-repository run records source, branch and commit separately per repository")
check(all(row["verification_state"] == "requires-independent-verification"
          for row in multi["repositories"]),
      "each repository candidate needs its own independent verification")
check(len(cc.REQUIRED_INDEPENDENT_CHECKS) >= 4
      and any("descends from source_sha" in item.replace("-", " ")
              for item in cc.REQUIRED_INDEPENDENT_CHECKS),
      "the record names the independent checks that must still run, including ancestry")

base, transport, adapter = with_session()
check(raises("malformed-response", adapter.reconciliation_input, DISPATCH,
             {"status": "FINISHED"}),
      "a vendor result with neither a repositories list nor a target block is malformed")

base, transport, adapter = with_session()
check(raises("unauthorized-result", adapter.reconciliation_input, DISPATCH,
             {"status": "FINISHED", "repositories": [
                 {"repository": "https://github.com/NotSkittlz/example-ops",
                  "branch": "../cursor/x", "commit": RESULT_COMMIT}]},
             authorized_repositories=["https://github.com/NotSkittlz/example-ops"]),
      "a returned branch is validated before fnmatch and traversal-shaped refs are refused")

base, transport, adapter = with_session()
check(raises("unauthorized-result", adapter.reconciliation_input, DISPATCH,
             {"status": "FINISHED", "repositories": [
                 {"repository": "https://github.com/NotSkittlz/example-project",
                  "branch": "cursor/x", "commit": RESULT_COMMIT}]},
             authorized_repositories=["https://github.com/NotSkittlz/example-project"]),
      "a caller cannot widen the immutable authorized repository scope")

# Two concurrent, different reconciliation inputs have one durable winner. Identify the winner
# from the record returned by the winning call rather than assuming thread/input order. Repeating
# that input is idempotent, while replaying the loser remains a typed conflict.
def reconciliation_race(adapter, dispatch_id, inputs):
    barrier = threading.Barrier(len(inputs) + 1)
    outcomes = [None] * len(inputs)

    def reconcile_probe(index, payload):
        try:
            barrier.wait(timeout=10)
            outcomes[index] = ("winner", adapter.reconciliation_input(dispatch_id, payload))
        except RemoteError as exc:
            outcomes[index] = ("error", exc.code)
        except Exception as exc:  # noqa: BLE001
            outcomes[index] = ("exception", type(exc).__name__)

    threads = [threading.Thread(target=reconcile_probe, args=(index, payload))
               for index, payload in enumerate(inputs)]
    for thread in threads:
        thread.start()
    try:
        barrier.wait(timeout=10)
    except threading.BrokenBarrierError:
        pass
    for thread in threads:
        thread.join(10)
    return outcomes, all(not thread.is_alive() for thread in threads)


base, transport, adapter = with_session()
winner_inputs = [
    {"status": "FINISHED", "target": {"branchName": "cursor/one", "commit": RESULT_COMMIT}},
    {"status": "FINISHED", "target": {"branchName": "cursor/two", "commit": "a" * 40}},
]
outcomes, joined = reconciliation_race(adapter, DISPATCH, winner_inputs)
winners = [(index, outcome[1]) for index, outcome in enumerate(outcomes)
           if outcome and outcome[0] == "winner"]
conflicts = [outcome[1] for outcome in outcomes
             if outcome and outcome[0] == "error"]
check(joined and len(winners) == 1 and conflicts == ["request-invalid"]
      and all(outcome and outcome[0] != "exception" for outcome in outcomes),
      "concurrent reconciliation inputs have one durable winner and one typed conflict")

if len(winners) == 1:
    winning_index, winning_record = winners[0]
    winning_branch = winning_record["repositories"][0]["result_branch"]
    identified_winner = next((index for index, payload in enumerate(winner_inputs)
                              if payload["target"]["branchName"] == winning_branch), None)
else:
    winning_index = identified_winner = None
    winning_record = None
check(identified_winner == winning_index,
      "the durable reconciliation record identifies which concurrent payload won")
if identified_winner is not None:
    check(adapter.reconciliation_input(DISPATCH, winner_inputs[identified_winner])
          == winning_record,
          "repeating the actual winning reconciliation input is idempotent")
    loser_index = 1 - identified_winner
    check(raises("request-invalid", adapter.reconciliation_input, DISPATCH,
                 winner_inputs[loser_index]),
          "replaying the losing reconciliation input remains a typed conflict")
else:
    check(False, "repeating the actual winning reconciliation input is idempotent")
    check(False, "replaying the losing reconciliation input remains a typed conflict")

# Repeat the full race a bounded number of times to exercise lock acquisition order and ensure
# every round preserves winner identity, idempotent winner replay, and loser refusal.
stress_rounds = 8
stress_ok = True
for iteration in range(stress_rounds):
    stress_base, _, stress_adapter = with_session()
    stress_inputs = [
        {"status": "FINISHED", "target": {
            "branchName": f"cursor/stress-{iteration:02d}-one", "commit": RESULT_COMMIT}},
        {"status": "FINISHED", "target": {
            "branchName": f"cursor/stress-{iteration:02d}-two", "commit": "b" * 40}},
    ]
    stress_outcomes, stress_joined = reconciliation_race(
        stress_adapter, DISPATCH, stress_inputs)
    stress_winners = [(index, outcome[1])
                      for index, outcome in enumerate(stress_outcomes)
                      if outcome and outcome[0] == "winner"]
    stress_conflicts = [outcome[1] for outcome in stress_outcomes
                        if outcome and outcome[0] == "error"]
    if not (stress_joined and len(stress_winners) == 1
            and stress_conflicts == ["request-invalid"]):
        stress_ok = False
        continue
    stress_winner_index, stress_record = stress_winners[0]
    stress_branch = stress_record["repositories"][0]["result_branch"]
    stress_identified = next(
        (index for index, payload in enumerate(stress_inputs)
         if payload["target"]["branchName"] == stress_branch), None)
    if stress_identified != stress_winner_index:
        stress_ok = False
        continue
    try:
        if stress_adapter.reconciliation_input(
                DISPATCH, stress_inputs[stress_identified]) != stress_record:
            stress_ok = False
    except RemoteError as exc:
        stress_ok = False
    except Exception:  # noqa: BLE001
        stress_ok = False
    loser_refused = False
    try:
        stress_loser = 1 - stress_identified
        stress_adapter.reconciliation_input(DISPATCH, stress_inputs[stress_loser])
    except RemoteError as exc:
        loser_refused = exc.code == "request-invalid"
    except Exception:  # noqa: BLE001
        pass
    if not loser_refused:
        stress_ok = False
check(stress_ok, f"bounded {stress_rounds}-round reconciliation concurrency stress preserves "
      "winner identity, idempotent replay, and loser refusal")

# --- restart recovery -----------------------------------------------------------------------------------------------------

base, transport, adapter = fresh()
(base / "state" / "PAUSE").write_text("held\n", encoding="utf-8")
rb.begin_attempt(base, "cursor-cloud", dispatch_id=DISPATCH, operation="create",
                 assignment_digest="d" * 64, request_digest="r" * 64, max_attempts=3)
report = adapter.recover()
check(report["paused"] is True and report["provider_queried"] is False,
      "under PAUSE, recovery reports local awareness and does NOT query the provider")
check(report["open_attempts"] and report["unreconciled"] == report["open_attempts"],
      "the open attempt is listed and explicitly left unreconciled")
check(not transport.calls, "recovery under PAUSE makes no external call")
(base / "state" / "PAUSE").unlink()

base, transport, adapter = fresh()
transport.route("POST", "/v0/agents", payload={"id": "agent-1", "status": "RUNNING"})
adapter.create_run(assignment(), "x", DISPATCH)
session_digest = rb.read_session(base, "cursor-cloud", DISPATCH)["assignment_digest"]
crashed = rb.begin_attempt(base, "cursor-cloud", dispatch_id=DISPATCH, operation="cancel",
                           assignment_digest=session_digest, request_digest="r" * 64,
                           max_attempts=3)
transport.route("GET", "/v0/agents/agent-1", payload={"status": "CANCELLED"})
report = adapter.recover()
check(report["reconciled"] == [crashed.attempt_id],
      "with the provider reachable, an open attempt is reconciled against observed state")
check(not rb.open_attempts(base, "cursor-cloud"),
      "controller restart leaves no attempt open, so nothing is re-sent by default")

base, transport, adapter = fresh()
orphan = rb.begin_attempt(base, "cursor-cloud", dispatch_id="dispatch-orph",
                          operation="create", assignment_digest="d" * 64,
                          request_digest="r" * 64, max_attempts=3)
report = adapter.recover()
check(report["unreconciled"] == [orphan.attempt_id],
      "an attempt whose outcome cannot be established stays a question for a human")
check(report["ambiguous_create_attempts"] == [orphan.attempt_id],
      "an ambiguous create crash is explicitly classified and never blindly recreated")
check(not transport.calls,
      "recovering an attempt with no local session queries nothing and invents nothing")

# --- an open attempt blocks a re-send -----------------------------------------------------------------------------------------

base, transport, adapter = fresh()
request_body = adapter.build_create_request(assignment(), "do the bounded thing")
rb.begin_attempt(base, "cursor-cloud", dispatch_id=DISPATCH, operation="create",
                 assignment_digest=ra.assignment_digest(assignment()),
                 request_digest=ra.canonical_digest(request_body),
                 max_attempts=3)
transport.route("POST", "/v0/agents", payload={"id": "agent-2"})
check(raises("unreconciled-attempt", adapter.create_run, assignment(),
             "do the bounded thing", DISPATCH),
      "a create whose earlier attempt was left OPEN refuses to re-send")
check(not transport.calls, "nothing was sent while the earlier attempt remains unreconciled")

# --- the CLI is inert without the human-owned flags -------------------------------------------------------------------------------

cli_dir = tempdir()
cli_base = tempdir()
(cli_base / "state").mkdir(parents=True, exist_ok=True)
env = dict(os.environ)
for name in list(env):
    if name.startswith("RECURSION_REMOTE"):
        env.pop(name, None)
env["RECURSION_REMOTE_DIR"] = str(cli_dir)
(cli_dir / "cursor-cloud.json").write_text(json.dumps(MANIFEST_SOURCE), encoding="utf-8")

completed = subprocess.run(
    [sys.executable, str(ADAPTER_PATH), "--base", str(cli_base), "models"],
    env=env, capture_output=True, text=True)
check(completed.returncode == 3,
      "the CLI exits 3 (disabled) with the enablement flags unset")

env["RECURSION_REMOTE_ADAPTERS"] = "1"
env["RECURSION_REMOTE_CURSOR_CLOUD_ENABLED"] = "1"
completed = subprocess.run(
    [sys.executable, str(ADAPTER_PATH), "--base", str(cli_base), "models"],
    env=env, capture_output=True, text=True)
check(completed.returncode == 4 and "live-transport-disabled" in completed.stderr,
      "with the backend enabled but RECURSION_REMOTE_LIVE unset, the CLI refuses (4) rather "
      "than reaching the network")

completed = subprocess.run(
    [sys.executable, str(ADAPTER_PATH), "validate"], env=env, capture_output=True, text=True)
check(completed.returncode == 0, "the CLI validates the manifest directory")

# --- cleanup ------------------------------------------------------------------------------------------------------------------------

os.environ.pop("CURSOR_CLOUD_API_KEY", None)
for path in TEMPDIRS:
    shutil.rmtree(path, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for item in failures:
        print(f"  - {item}")
    raise SystemExit(1)
print("all checks passed")
