#!/usr/bin/env python3
"""Control: the Phase 7 Grok Bot outbound signed-routine dispatcher, driven by a fake transport.

Frozen contract: scripts/grok-bot-dispatcher.py and docs/plans/remote-execution-adapters.md;
program context docs/plans/recursion-v2-execution-fabric.md Phase 7.

Every network call is a ``FakeTransport``. The production transport is never constructed here.

The Phase 7 gate properties, each with a check:
  * a duplicate dispatch cannot create duplicate work;
  * PAUSE prevents dispatch, and is checked before anything else;
  * the Bot receives no controller or deploy credential -- structurally, not by convention;
  * results arrive on a proposal/review-only channel and are advisory;
  * the account-shared lease bounds concurrency at the ACCOUNT, because every Bot on the
    account shares one computer;
  * weekly exhaustion defers instead of spending.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / "scripts"))

import remote_assignment as ra  # noqa: E402
import remote_backend as rb  # noqa: E402
from remote_assignment import RemoteError  # noqa: E402

DISPATCHER_PATH = SOURCE / "scripts" / "grok-bot-dispatcher.py"
_spec = importlib.util.spec_from_file_location("recursion_grok_bot_dispatcher",
                                               DISPATCHER_PATH)
gb = importlib.util.module_from_spec(_spec)
sys.modules["recursion_grok_bot_dispatcher"] = gb
_spec.loader.exec_module(gb)

failures: list[str] = []
TEMPDIRS: list[str] = []

WEBHOOK = "https://hooks.example.com/routines/abc?v=1"
SIGNING_SECRET = "control-fixture-signing-secret"
SENDER_ID = "control-fixture-sender"
SHA = "0123456789abcdef0123456789abcdef01234567"
DISPATCH = "dispatch-0001"

MANIFEST_SOURCE = {
    "schema_version": 1,
    "name": "grok-bot",
    "kind": "grok-routine",
    "description": "control fixture",
    "endpoint": None,
    "endpoint_ref": "secret:GROK_ROUTINE_WEBHOOK_URL",
    "credential_ref": "secret:GROK_ROUTINE_SENDER_ID",
    "signing_key_ref": "secret:GROK_ROUTINE_SIGNING_SECRET",
    "capacity_pool": "grok-bot-weekly",
    "host_id": "grok-bot-account-cloud",
    "billing_basis": "included",
    "on_demand": False,
    "timeout_seconds": 30,
    "max_attempts": 3,
    "concurrency_lease": 1,
    "publication": {
        "default_branch": "main",
        "integration_branch": "recursion/integration",
        "task_branch_pattern": "recursion/extern/grok/<task-id>",
        "allowed_result_branch_patterns": ["recursion/extern/grok/*"],
    },
}

ASSIGNMENT = {
    "task_id": "t-0042",
    "project_id": "example-ops",
    "source_repository": "https://github.com/NotSkittlz/example-ops",
    "source_branch": "main",
    "source_sha": SHA,
    "engine": "grok-bot",
    "provider": "cursor",
    "capacity_pool": "grok-bot-weekly",
    "host": "grok-bot-account-cloud",
    "sandbox_profile": "account-shared-supervisor",
    "network_profile": "provider-only",
    "allowed_paths": ["docs/plans/x.md"],
    "publication_policy": "review-branch-only",
    "created_at": "2026-08-28T12:00:00Z",
}

INSTRUCTIONS = "Review the linked candidate and report findings. Do not change code."
NON_GOALS = ["do not merge", "do not deploy", "do not contact anyone outside this task"]


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
    path = tempfile.mkdtemp(prefix="recursion-grok-test-")
    TEMPDIRS.append(path)
    return Path(path)


def assignment(**overrides) -> dict:
    out = dict(ASSIGNMENT)
    out.update(overrides)
    return out


def manifest(**overrides) -> dict:
    payload = json.loads(json.dumps(MANIFEST_SOURCE))
    payload.update(overrides)
    return rb.validate_manifest(payload, "grok-bot")


class FakeTransport(rb.Transport):
    """Records every call and answers from a queued route list. Opens nothing."""

    def __init__(self) -> None:
        self.queue: list = []
        self.calls: list[dict] = []

    def route(self, *, status: int = 200, payload: object = None,
              error: Exception | None = None) -> "FakeTransport":
        self.queue.append((status, payload, error))
        return self

    def request(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": body, "timeout": timeout})
        if not self.queue:
            raise AssertionError("unrouted call")
        status, payload, error = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if error is not None:
            raise error
        return rb.TransportResponse(status, {},
                                    json.dumps(payload if payload is not None
                                               else {}).encode("utf-8"))


def fresh(**manifest_overrides):
    base = tempdir()
    (base / "state").mkdir(parents=True, exist_ok=True)
    transport = FakeTransport()
    dispatcher = gb.GrokBotDispatcher(base, manifest(**manifest_overrides), transport)
    return base, transport, dispatcher


os.environ["GROK_ROUTINE_WEBHOOK_URL"] = WEBHOOK
os.environ["GROK_ROUTINE_SENDER_ID"] = SENDER_ID
os.environ["GROK_ROUTINE_SIGNING_SECRET"] = SIGNING_SECRET

# --- the envelope -------------------------------------------------------------------------------

envelope = gb.build_envelope(assignment(), role="reviewer", instructions=INSTRUCTIONS,
                             non_goals=NON_GOALS, dispatch_id=DISPATCH,
                             issued_at="2026-08-28T12:00:00Z")
check(envelope["envelope_kind"] == gb.ENVELOPE_KIND
      and envelope["result_channel"] == "proposal-review-only",
      "the envelope declares its kind and its proposal/review-only result channel")
check(envelope["assignment"]["source_sha"] == SHA,
      "the envelope carries the exact source sha")
check("sandbox_profile" not in envelope["assignment"]
      and "network_profile" not in envelope["assignment"]
      and "allowed_paths" not in envelope["assignment"],
      "the containment shape is withheld from the executor")
check(envelope["assignment_digest"] == ra.assignment_digest(assignment()),
      "the envelope carries the full assignment digest even though it carries a subset")
check("controller-credentials" in envelope["capabilities_withheld"]
      and "deploy-keys" in envelope["capabilities_withheld"]
      and "production-deployment" in envelope["capabilities_withheld"]
      and "pause-control" in envelope["capabilities_withheld"],
      "the withheld-capability boundary travels WITH the assignment, not only in a doc")
check("advisory" in envelope["verification_note"],
      "the envelope states that Bot self-reports are advisory")

check(raises("request-invalid", gb.build_envelope, assignment(), role="implementer",
             instructions=INSTRUCTIONS, non_goals=NON_GOALS, dispatch_id=DISPATCH,
             issued_at="2026-08-28T12:00:00Z"),
      "'implementer' is not a role this dispatcher can request -- implementation on the "
      "account-shared computer stays exceptional")
check("implementer" not in gb.ROLES, "the role vocabulary contains no implementer")
check(raises("request-invalid", gb.build_envelope, assignment(), role="reviewer",
             instructions="  ", non_goals=NON_GOALS, dispatch_id=DISPATCH,
             issued_at="2026-08-28T12:00:00Z"),
      "empty instructions are refused")
check(raises("redaction-violation", gb.build_envelope, assignment(), role="reviewer",
             instructions="use ghp_" + "a" * 36, non_goals=NON_GOALS, dispatch_id=DISPATCH,
             issued_at="2026-08-28T12:00:00Z"),
      "a credential in the instructions is REFUSED, not stripped and sent anyway")
check(raises("assignment-invalid", gb.build_envelope, assignment(source_sha="main"),
             role="reviewer", instructions=INSTRUCTIONS, non_goals=NON_GOALS,
             dispatch_id=DISPATCH, issued_at="2026-08-28T12:00:00Z"),
      "an assignment bound to a moving ref cannot be enveloped")

other = gb.build_envelope(assignment(), role="reviewer", instructions=INSTRUCTIONS + "!",
                          non_goals=NON_GOALS, dispatch_id=DISPATCH,
                          issued_at="2026-08-28T12:00:00Z")
check(gb.envelope_digest(envelope) != gb.envelope_digest(other),
      "changing one character of the instructions changes the envelope digest")
check(gb.envelope_digest(envelope) == gb.envelope_digest(dict(reversed(list(envelope.items())))),
      "the envelope digest is independent of key order")

# --- the signature -------------------------------------------------------------------------------

body = ra.canonical_bytes(envelope)
signature = gb.sign_envelope(body, "2026-08-28T12:00:00Z", SIGNING_SECRET)
expected = "v1=" + hmac.new(SIGNING_SECRET.encode("utf-8"),
                            b"v1:2026-08-28T12:00:00Z:" + body,
                            hashlib.sha256).hexdigest()
check(signature == expected,
      "the signature is HMAC-SHA256 over version, timestamp and the exact body")
check(gb.sign_envelope(body, "2026-08-28T12:00:01Z", SIGNING_SECRET) != signature,
      "the timestamp is inside the signed material, so a captured body cannot be replayed "
      "with a fresh header")
check(gb.sign_envelope(body, "2026-08-28T12:00:00Z", "other") != signature,
      "a different key produces a different signature")
check(raises("credential-absent", gb.sign_envelope, body, "2026-08-28T12:00:00Z", ""),
      "an empty signing key is refused")

# --- PAUSE is checked first -----------------------------------------------------------------------

base, transport, dispatcher = fresh()
(base / "state" / "PAUSE").write_text("held\n", encoding="utf-8")
transport.route(payload={"accepted": True})
check(raises("paused", dispatcher.dispatch, assignment(), role="reviewer",
             instructions=INSTRUCTIONS, non_goals=NON_GOALS, dispatch_id=DISPATCH),
      "PAUSE refuses a dispatch")
check(not transport.calls, "nothing left the box under PAUSE")
check(not rb.read_attempts(base, "grok-bot"), "a paused dispatch commits no attempt")

# --- a clean dispatch ------------------------------------------------------------------------------

base, transport, dispatcher = fresh()
transport.route(payload={"accepted": True, "routine_run_id": "run-9"})
record = dispatcher.dispatch(assignment(), role="reviewer", instructions=INSTRUCTIONS,
                             non_goals=NON_GOALS, dispatch_id=DISPATCH)
check(record["state"] == "dispatched" and record["routine_run_id"] == "run-9",
      "a successful dispatch writes a durable record")
check(len(transport.calls) == 1 and transport.calls[0]["method"] == "POST"
      and transport.calls[0]["url"] == WEBHOOK,
      "exactly one POST went to the resolved webhook")
check(transport.calls[0]["body"] == ra.canonical_bytes(record["envelope"]),
      "the bytes on the wire are exactly the canonical envelope that was recorded")

headers = transport.calls[0]["headers"]
check(set(headers) == {"Content-Type", "X-Recursion-Dispatch-Id",
                       "X-Recursion-Assignment-Digest", "X-Recursion-Envelope-Digest",
                       "X-Recursion-Timestamp", "X-Recursion-Signature",
                       "X-Recursion-Sender", "Idempotency-Key"},
      "the request carries exactly the declared headers -- no environment passthrough, no "
      "controller token rides along")
check(headers["X-Recursion-Signature"].startswith("v1="),
      "the request is signed")
check(headers["X-Recursion-Envelope-Digest"] == gb.envelope_digest(record["envelope"]),
      "the envelope digest header matches the recorded envelope")

serialised = json.dumps(record)
check(WEBHOOK not in serialised and SIGNING_SECRET not in serialised
      and SENDER_ID not in serialised,
      "neither the bearer-grade webhook URL nor either key reaches the durable record")
check(record["endpoint_ref"] == "secret:GROK_ROUTINE_WEBHOOK_URL"
      and record["signing_key_ref"] == "secret:GROK_ROUTINE_SIGNING_SECRET",
      "the durable record carries the REFERENCES instead")

# --- idempotency ---------------------------------------------------------------------------------------

again = dispatcher.dispatch(assignment(), role="reviewer", instructions=INSTRUCTIONS,
                            non_goals=NON_GOALS, dispatch_id=DISPATCH)
check(len(transport.calls) == 1,
      "an identical dispatch REPLAYS -- a duplicate POST would be a duplicate routine run "
      "and, if the routine writes code, a duplicate branch")
check(again["envelope_digest"] == record["envelope_digest"], "the replay returns the original")

transport.route(payload={"accepted": True, "routine_run_id": "run-10"})
changed = dispatcher.dispatch(assignment(), role="reviewer",
                              instructions=INSTRUCTIONS + " Also check the tests.",
                              non_goals=NON_GOALS, dispatch_id=DISPATCH)
check(len(transport.calls) == 2 and changed["envelope_digest"] != record["envelope_digest"],
      "a DIFFERENT envelope under the same dispatch id is a different idempotency key and "
      "is sent -- deduplication is on content, not on the id alone")

# --- an attempt left open blocks a re-send -----------------------------------------------------------------

base, transport, dispatcher = fresh()
# The identity digest deliberately excludes issued_at, so this reconstruction does not depend
# on the wall clock -- which is the same property that makes a one-second-later retry a
# replay rather than a second dispatch.
envelope_now = gb.build_envelope(assignment(), role="reviewer", instructions=INSTRUCTIONS,
                                 non_goals=NON_GOALS, dispatch_id=DISPATCH,
                                 issued_at="2026-08-28T12:00:00Z")
later = gb.build_envelope(assignment(), role="reviewer", instructions=INSTRUCTIONS,
                          non_goals=NON_GOALS, dispatch_id=DISPATCH,
                          issued_at="2026-08-28T12:00:01Z")
check(gb.envelope_identity_digest(envelope_now) == gb.envelope_identity_digest(later)
      and gb.envelope_digest(envelope_now) != gb.envelope_digest(later),
      "the idempotency identity ignores issued_at while the signed envelope does not")
rb.begin_attempt(base, "grok-bot", dispatch_id=DISPATCH, operation="dispatch",
                 assignment_digest=ra.assignment_digest(assignment()),
                 request_digest=gb.envelope_identity_digest(envelope_now),
                 max_attempts=3)
check(raises("unreconciled-attempt", dispatcher.dispatch, assignment(), role="reviewer",
             instructions=INSTRUCTIONS, non_goals=NON_GOALS, dispatch_id=DISPATCH),
      "a dispatch whose earlier attempt was left OPEN refuses to re-send")
check(not transport.calls,
      "nothing is re-sent while an earlier attempt remains unreconciled")

recovery = dispatcher.recover()
check(len(recovery["open_attempts"]) == 1 and "reconcile" in recovery["next_action"],
      "restart recovery lists the open attempt and names the next action")

open_id = recovery["open_attempts"][0]["attempt_id"]
dispatcher.reconcile(open_id, observed="not-landed", observer="lucas:grok-bot-app")
transport.route(payload={"accepted": True, "routine_run_id": "run-11"})
resent = dispatcher.dispatch(assignment(), role="reviewer", instructions=INSTRUCTIONS,
                             non_goals=NON_GOALS, dispatch_id=DISPATCH)
check(len(transport.calls) == 1 and resent["routine_run_id"] == "run-11",
      "once a human establishes the routine did NOT run, the dispatch proceeds")
check(raises("redaction-violation", dispatcher.reconcile, open_id, observed="landed",
             observer="lucas", evidence={"note": "ghp_" + "b" * 36}),
      "reconciliation evidence is screened for credentials like everything else")

# --- recovery is available under PAUSE ---------------------------------------------------------------------

base, transport, dispatcher = fresh()
(base / "state" / "PAUSE").write_text("held\n", encoding="utf-8")
rb.begin_attempt(base, "grok-bot", dispatch_id=DISPATCH, operation="dispatch",
                 assignment_digest="d" * 64, request_digest="r" * 64, max_attempts=3)
paused_recovery = dispatcher.recover()
check(paused_recovery["paused"] is True and len(paused_recovery["open_attempts"]) == 1,
      "a paused box can still say what it was doing -- awareness survives PAUSE")
check(not transport.calls, "recovery under PAUSE makes no external call")

# --- the account-shared lease -----------------------------------------------------------------------------------

base, transport, dispatcher = fresh()
transport.route(payload={"accepted": True})
with rb.AccountLease(base, "grok-bot-weekly", 1):
    check(raises("lease-unavailable", dispatcher.dispatch, assignment(), role="reviewer",
                 instructions=INSTRUCTIONS, non_goals=NON_GOALS, dispatch_id=DISPATCH),
          "with the single account slot held, a dispatch DEFERS -- two Bots on one account "
          "computer are one writer with two screens")
    check(not transport.calls, "the deferred dispatch sent nothing")
deferred = rb.read_attempts(base, "grok-bot")[-1]
check(deferred["state"] == "deferred" and deferred["outcome"] == "lease-unavailable",
      "the deferred attempt is recorded as deferred, freeing the key for a later run")
check(RemoteError("lease-unavailable", "x").exit_code == 75,
      "lease exhaustion is a defer (75), not a failure and not an escalation")

# --- typed provider outcomes -------------------------------------------------------------------------------------

for status, code in ((402, "quota-exhausted"), (429, "rate-limited"), (401, "auth-failed"),
                     (503, "provider-unavailable"), (500, "provider-error")):
    base, transport, dispatcher = fresh()
    transport.route(status=status, payload={"error": "no"})
    check(raises(code, dispatcher.dispatch, assignment(), role="reviewer",
                 instructions=INSTRUCTIONS, non_goals=NON_GOALS, dispatch_id=DISPATCH),
          f"HTTP {status} from the routine is a typed {code}")
    if status in (429, 503, 500):
        check(rb.read_attempts(base, "grok-bot")[-1]["state"] == "open",
              f"HTTP {status} leaves the mutation open for reconciliation")

base, transport, dispatcher = fresh()
transport.route(payload={"accepted": False, "reason": "routine rejected duplicate"})
check(raises("malformed-response", dispatcher.dispatch, assignment(), role="reviewer",
             instructions=INSTRUCTIONS, non_goals=NON_GOALS, dispatch_id=DISPATCH),
      "a routine that does not acknowledge with accepted=true is not a successful dispatch")
check(rb.read_attempts(base, "grok-bot")[-1]["state"] == "failed",
      "an unacknowledged dispatch closes the attempt as failed, not open")

base, transport, dispatcher = fresh()
transport.route(error=RemoteError("transport-timeout", "timed out"))
check(raises("transport-timeout", dispatcher.dispatch, assignment(), role="reviewer",
             instructions=INSTRUCTIONS, non_goals=NON_GOALS, dispatch_id=DISPATCH),
      "a timeout is typed and retryable")
check(rb.open_attempts(base, "grok-bot"),
      "an ambiguous fire-and-forget timeout remains open for external reconciliation")

base, transport, dispatcher = fresh()
os.environ.pop("GROK_ROUTINE_SIGNING_SECRET", None)
transport.route(payload={"accepted": True})
check(raises("credential-absent", dispatcher.dispatch, assignment(), role="reviewer",
             instructions=INSTRUCTIONS, non_goals=NON_GOALS, dispatch_id=DISPATCH),
      "an absent signing key is a clean refusal")
check(not rb.read_attempts(base, "grok-bot"),
      "a configuration refusal consumes no attempt -- credentials resolve BEFORE the commit")
os.environ["GROK_ROUTINE_SIGNING_SECRET"] = SIGNING_SECRET

# --- no controller or deploy credential is reachable ---------------------------------------------------------------

for forbidden in ("secret:GITHUB_TOKEN", "secret:SSH_AUTH_SOCK", "secret:RECURSION_ROOT",
                  "secret:TELEGRAM_BOT_TOKEN", "secret:HERMES_HOME"):
    check(raises("manifest-invalid", rb.validate_manifest,
                 {**json.loads(json.dumps(MANIFEST_SOURCE)), "credential_ref": forbidden},
                 "grok-bot"),
          f"a manifest referencing {forbidden} cannot validate")
check("env_passthrough" not in rb.MANIFEST_KEYS,
      "a remote backend manifest has no env_passthrough field at all -- there is no channel "
      "for arbitrary controller environment to reach the Bot")

# --- the proposal/review-only result channel --------------------------------------------------------------------------

base, transport, dispatcher = fresh()
transport.route(payload={"accepted": True, "routine_run_id": "run-9"})
dispatcher.dispatch(assignment(), role="reviewer", instructions=INSTRUCTIONS,
                    non_goals=NON_GOALS, dispatch_id=DISPATCH)
proposal = dispatcher.record_proposal(DISPATCH, {
    "findings": [{"file": "docs/plans/x.md", "note": "the gate is not machine-checkable"}],
    "recommendation": "split the task",
})
check(proposal["authority"] == "advisory" and proposal["requires_human_review"] is True
      and proposal["result_channel"] == "proposal-review-only",
      "a Bot result is recorded as an advisory proposal requiring human review")
check(proposal["self_reported_evidence_is_advisory"] is True,
      "the proposal record restates that self-reported evidence is advisory")
check((base / "state" / "remote" / "grok-bot" / "proposals" / f"{DISPATCH}.json").is_file(),
      "the proposal lands under state/remote/, and nowhere else")
check(not (base / "state" / "tasks").exists() and not (base / "state" / "DECISIONS.md").exists(),
      "recording a proposal touches no task, board or decision state")

check(raises("result-channel-violation", dispatcher.record_proposal, DISPATCH,
             {"merge": "loop/work/t-0042"}),
      "a proposal carrying an imperative key is refused whole")
check(raises("result-channel-violation", dispatcher.record_proposal, DISPATCH,
             {"recommendations": [{"deploy": "production"}]}),
      "an imperative key NESTED inside advice is still refused")
check(raises("result-channel-violation", dispatcher.record_proposal, DISPATCH,
             {"force_push": True}),
      "force-push is not expressible through the result channel")
check(raises("redaction-violation", dispatcher.record_proposal, DISPATCH,
             {"note": "the key is ghp_" + "c" * 36}),
      "a credential in a proposal is refused")
check(raises("request-invalid", dispatcher.record_proposal, DISPATCH, {}),
      "an empty proposal is refused")
check(raises("not-found", dispatcher.record_proposal, "dispatch-nope", {"findings": []}),
      "a proposal for a dispatch that never happened is not-found")

# --- the CLI is inert without the human-owned flags ---------------------------------------------------------------------

cli_dir = tempdir()
cli_base = tempdir()
(cli_base / "state").mkdir(parents=True, exist_ok=True)
env = dict(os.environ)
for name in list(env):
    if name.startswith("RECURSION_REMOTE"):
        env.pop(name, None)
env["RECURSION_REMOTE_DIR"] = str(cli_dir)
(cli_dir / "grok-bot.json").write_text(json.dumps(MANIFEST_SOURCE), encoding="utf-8")

completed = subprocess.run(
    [sys.executable, str(DISPATCHER_PATH), "--base", str(cli_base), "recover"],
    env=env, capture_output=True, text=True)
check(completed.returncode == 3,
      "the CLI exits 3 (disabled) with the enablement flags unset")

env["RECURSION_REMOTE_ADAPTERS"] = "1"
env["RECURSION_REMOTE_GROK_BOT_ENABLED"] = "1"
completed = subprocess.run(
    [sys.executable, str(DISPATCHER_PATH), "--base", str(cli_base), "recover"],
    env=env, capture_output=True, text=True)
check(completed.returncode == 0 and '"open_attempts": []' in completed.stdout,
      "with the backend enabled, local recovery works without any network capability")

completed = subprocess.run(
    [sys.executable, str(DISPATCHER_PATH), "validate"], env=env, capture_output=True,
    text=True)
check(completed.returncode == 0, "the CLI validates the manifest directory")

completed = subprocess.run(
    [sys.executable, str(DISPATCHER_PATH), "--base", str(cli_base), "show", DISPATCH],
    env=env, capture_output=True, text=True)
check(completed.returncode == 2, "showing an unknown dispatch exits 2 (not-found)")

# --- cleanup ---------------------------------------------------------------------------------------------------------------

for name in ("GROK_ROUTINE_WEBHOOK_URL", "GROK_ROUTINE_SENDER_ID",
             "GROK_ROUTINE_SIGNING_SECRET"):
    os.environ.pop(name, None)
for path in TEMPDIRS:
    shutil.rmtree(path, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for item in failures:
        print(f"  - {item}")
    raise SystemExit(1)
print("all checks passed")
