#!/usr/bin/env python3
"""Adversarial source-only acceptance for the MVP control surface and watchdog."""
from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("mvp_control_surface", SCRIPTS / "mvp_control_surface.py")
assert spec and spec.loader
surface = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = surface
spec.loader.exec_module(surface)

NOW = "2026-08-31T20:00:00Z"
SOURCE_SHA = "a" * 40
EVIDENCE_DIGEST = "b" * 64
AUTH_DIGEST = "c" * 64


def seal(record, field):
    return surface._seal(record, field)


def evidence():
    return [{"id": "evidence-one", "sha256": EVIDENCE_DIGEST,
             "pointer": "git:origin/main@" + SOURCE_SHA}]


def work(ident="task-one", status="running", summary="Bounded source task"):
    return {"id": ident, "project_id": "recursion", "status": status, "summary": summary,
            "reason": "Serves the authorized MVP", "due_at": "2026-08-31T23:00:00Z",
            "assignment_digest": "d" * 64, "evidence_refs": ["evidence-one"]}


def snapshot(*, paused=False):
    body = {
        "schema_version": 1, "kind": "control_snapshot", "source_sha": SOURCE_SHA,
        "observed_at": "2026-08-31T19:59:30Z", "expires_at": "2026-08-31T20:05:00Z",
        "pause_observed": paused, "today": [work()], "active_work": [work()],
        "reasons": ["The MVP is the current authorized task"],
        "next_work": [work("task-two", "queued", "Independent verification")],
        "budget": {"quota_status": "available", "active_workers": 1, "worker_capacity": 3,
                   "review_queue": 1, "review_capacity": 2, "month_spend_cents": 0,
                   "month_ceiling_cents": 0, "automatic_metered_spend_allowed": False,
                   "observations": ["Subscription capacity available"]},
        "evidence_refs": evidence(), "snapshot_digest": "",
    }
    return seal(body, "snapshot_digest")


def request(command, snap, *, arguments=None, principal="anonymous", ident="request-one"):
    body = {
        "schema_version": 1, "kind": "command_request", "id": ident, "command": command,
        "arguments": list(arguments or []), "principal": principal, "source_sha": snap["source_sha"],
        "snapshot_digest": snap["snapshot_digest"], "created_at": NOW,
        "expires_at": "2026-08-31T20:00:30Z", "request_digest": "",
    }
    return seal(body, "request_digest")


def auth(principal="lucas"):
    return surface.AuthenticatedPrincipal(principal, AUTH_DIGEST, NOW)


def heartbeat(sequence, sent_at, *, key=b"0123456789abcdef0123456789abcdef", signer="box-two",
              controller="hermes-controller"):
    body = {
        "schema_version": 1, "kind": "controller_heartbeat", "controller_id": controller,
        "sequence": sequence, "source_sha": SOURCE_SHA, "sent_at": sent_at, "status": "healthy",
        "signer_id": signer, "evidence_refs": ["evidence-one"],
        "heartbeat_digest": "", "authentication_tag": "",
    }
    body["heartbeat_digest"] = surface.heartbeat_digest(body)
    body["authentication_tag"] = hmac.new(key, body["heartbeat_digest"].encode("ascii"), hashlib.sha256).hexdigest()
    return body


class ControlSurfaceTests(unittest.TestCase):
    def test_all_read_commands_are_fresh_evidenced_and_available_under_pause(self):
        snap = snapshot(paused=True)
        for index, command in enumerate(sorted(surface.READ_COMMANDS)):
            result = surface.handle_command(request(command, snap, ident=f"request-{index}"), snap,
                                            reference_time=NOW)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["source_sha"], SOURCE_SHA)
            self.assertEqual(result["snapshot_digest"], snap["snapshot_digest"])
            self.assertEqual(result["evidence_refs"], ["evidence-one"])
            self.assertEqual(result["result_digest"], surface._digest(result, "result_digest"))
            self.assertIsNone(result["intent"])

    def test_stale_tampered_unknown_and_unbound_requests_fail_closed(self):
        snap = snapshot()
        stale = copy.deepcopy(snap); stale["expires_at"] = "2026-08-31T19:59:59Z"; stale = seal(stale, "snapshot_digest")
        with self.assertRaises(surface.ControlSurfaceError):
            surface.handle_command(request("/status", stale), stale, reference_time=NOW)
        tampered = request("/status", snap); tampered["command"] = "/today"
        with self.assertRaisesRegex(surface.ControlSurfaceError, "digest mismatch"):
            surface.handle_command(tampered, snap, reference_time=NOW)
        unknown = request("/status", snap); unknown["extra"] = 1; unknown = seal(unknown, "request_digest")
        with self.assertRaisesRegex(surface.ControlSurfaceError, "fields are not strict"):
            surface.handle_command(unknown, snap, reference_time=NOW)
        other = snapshot(); other["source_sha"] = "f" * 40; other = seal(other, "snapshot_digest")
        with self.assertRaisesRegex(surface.ControlSurfaceError, "current snapshot"):
            surface.handle_command(request("/status", snap), other, reference_time=NOW)

    def test_secret_private_reasoning_and_cyclic_inputs_are_typed(self):
        snap = snapshot(); bad = copy.deepcopy(snap); bad["reasons"] = ["api_key=bad-value"]
        with self.assertRaises(surface.ControlSurfaceError):
            surface.validate_snapshot(bad, reference_time=NOW)
        bad = snapshot(); bad["reasons"] = ["private reasoning: hidden"]
        with self.assertRaises(surface.ControlSurfaceError):
            surface.validate_snapshot(bad, reference_time=NOW)
        cyclic = {}; cyclic["self"] = cyclic
        with self.assertRaises(surface.ControlSurfaceError):
            surface.validate_snapshot(cyclic, reference_time=NOW)

    def test_mutations_require_exact_authenticated_lucas(self):
        snap = snapshot()
        command = request("/pause", snap, principal="lucas")
        for identity in (None, auth("mallory")):
            with self.assertRaises(surface.ControlSurfaceError):
                surface.handle_command(command, snap, reference_time=NOW, authentication=identity)
        result = surface.handle_command(command, snap, reference_time=NOW, authentication=auth())
        self.assertEqual(result["intent"]["action"], "pause-request")
        self.assertEqual(result["intent"]["disposition"], "ready-for-canonical-outbox")
        self.assertNotIn("destination", result["intent"])

    def test_pause_is_idempotent_and_resume_never_removes_pause(self):
        snap = snapshot(paused=True)
        paused = surface.handle_command(request("/pause", snap, principal="lucas"), snap,
                                        reference_time=NOW, authentication=auth())
        self.assertIsNone(paused["intent"])
        resumed = surface.handle_command(request("/resume", snap, principal="lucas"), snap,
                                         reference_time=NOW, authentication=auth())
        self.assertEqual(resumed["status"], "held-paused")
        self.assertTrue(resumed["intent"]["approval_required"])
        self.assertEqual(resumed["intent"]["disposition"], "held-paused")
        self.assertTrue(snap["pause_observed"])

    def test_approval_exact_one_shot_bound_and_held_under_pause(self):
        snap = snapshot(paused=True)
        pending = surface.handle_command(request("/resume", snap, principal="lucas"), snap,
                                         reference_time=NOW, authentication=auth())["intent"]
        approve = request("/approve", snap, principal="lucas", ident="request-approve",
                          arguments=[pending["id"], pending["intent_digest"]])
        result = surface.handle_command(approve, snap, reference_time=NOW, authentication=auth(),
                                        pending_intents=[pending])
        self.assertEqual(result["intent"]["approved_intent_digest"], pending["intent_digest"])
        self.assertEqual(result["intent"]["disposition"], "held-paused")
        with self.assertRaisesRegex(surface.ControlSurfaceError, "already consumed"):
            surface.handle_command(approve, snap, reference_time=NOW, authentication=auth(),
                                   pending_intents=[pending],
                                   consumed_approval_digests=[pending["intent_digest"]])
        wrong = request("/approve", snap, principal="lucas", ident="request-wrong",
                        arguments=[pending["id"], "f" * 64])
        with self.assertRaisesRegex(surface.ControlSurfaceError, "exactly match"):
            surface.handle_command(wrong, snap, reference_time=NOW, authentication=auth(),
                                   pending_intents=[pending])

    def test_resealed_intents_preserve_action_approval_invariants(self):
        snap = snapshot()
        pause = surface.handle_command(request("/pause", snap, principal="lucas"), snap,
                                       reference_time=NOW, authentication=auth())["intent"]
        paused_snap = snapshot(paused=True)
        resume = surface.handle_command(request("/resume", paused_snap, principal="lucas", ident="resume"), paused_snap,
                                        reference_time=NOW, authentication=auth())["intent"]
        approval_request = request("/approve", paused_snap, principal="lucas", ident="approve",
                                   arguments=[resume["id"], resume["intent_digest"]])
        approval = surface.handle_command(approval_request, paused_snap, reference_time=NOW,
                                          authentication=auth(), pending_intents=[resume])["intent"]
        cases = ((pause, {"approval_required": True}),
                 (resume, {"approval_required": False}),
                 (approval, {"approval_required": True}),)
        for original, changes in cases:
            forged = copy.deepcopy(original)
            forged.update(changes)
            forged = seal(forged, "intent_digest")
            with self.assertRaises(surface.ControlSurfaceError):
                surface.validate_intent(forged)
        for original in (pause, resume):
            forged = copy.deepcopy(original)
            forged["approved_intent_id"] = "intent-forged"
            forged["approved_intent_digest"] = "f" * 64
            forged = seal(forged, "intent_digest")
            with self.assertRaises(surface.ControlSurfaceError):
                surface.validate_intent(forged)
        forged = copy.deepcopy(approval)
        forged["approved_intent_id"] = None
        forged["approved_intent_digest"] = None
        forged = seal(forged, "intent_digest")
        with self.assertRaises(surface.ControlSurfaceError):
            surface.validate_intent(forged)

    def test_stale_authentication_and_boolean_impersonation_fail(self):
        snap = snapshot(); command = request("/pause", snap, principal="lucas")
        stale = surface.AuthenticatedPrincipal("lucas", AUTH_DIGEST, "2026-08-31T19:00:00Z")
        with self.assertRaises(surface.ControlSurfaceError):
            surface.handle_command(command, snap, reference_time=NOW, authentication=stale)
        fake = surface.AuthenticatedPrincipal("lucas", AUTH_DIGEST, NOW, authenticated=1)
        with self.assertRaises(surface.ControlSurfaceError):
            surface.handle_command(command, snap, reference_time=NOW, authentication=fake)

    def test_budget_cannot_authorize_automatic_metered_spend(self):
        snap = snapshot(); snap["budget"]["automatic_metered_spend_allowed"] = True
        snap["budget"]["month_ceiling_cents"] = 2_000; snap = seal(snap, "snapshot_digest")
        with self.assertRaisesRegex(surface.ControlSurfaceError, "cannot authorize"):
            surface.validate_snapshot(snap, reference_time=NOW)

    def test_public_boundaries_never_leak_raw_python_exceptions(self):
        snap = snapshot()
        calls = (
            lambda value: surface.validate_snapshot(value, reference_time=NOW),
            lambda value: surface.validate_request(value, snapshot=snap, reference_time=NOW),
            lambda value: surface.validate_intent(value),
            lambda value: surface.heartbeat_digest(value),
        )
        for call in calls:
            for malformed in (None, object(), [], "x", 1):
                with self.assertRaises(surface.ControlSurfaceError):
                    call(malformed)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.key = b"0123456789abcdef0123456789abcdef"
        self.verifier = surface.HeartbeatVerifier({"box-two": self.key})
        self.state = surface.WatchdogState("hermes-controller")

    def test_authentication_unknown_signer_tamper_and_wrong_controller_fail(self):
        beat = heartbeat(1, "2026-08-31T19:59:50Z")
        bad = copy.deepcopy(beat); bad["authentication_tag"] = "0" * 64
        with self.assertRaises(surface.ControlSurfaceError):
            surface.evaluate_watchdog(self.state, self.verifier, reference_time=NOW,
                                      threshold_seconds=30, heartbeat=bad)
        unknown = heartbeat(1, "2026-08-31T19:59:50Z", signer="box-three")
        with self.assertRaises(surface.ControlSurfaceError):
            surface.evaluate_watchdog(self.state, self.verifier, reference_time=NOW,
                                      threshold_seconds=30, heartbeat=unknown)
        wrong = heartbeat(1, "2026-08-31T19:59:50Z", controller="other-controller")
        with self.assertRaises(surface.ControlSurfaceError):
            surface.evaluate_watchdog(self.state, self.verifier, reference_time=NOW,
                                      threshold_seconds=30, heartbeat=wrong)

    def test_one_death_intent_per_episode_duplicate_is_harmless_and_newer_rearms(self):
        beat = heartbeat(1, "2026-08-31T19:58:00Z")
        state, first = surface.evaluate_watchdog(self.state, self.verifier, reference_time=NOW,
                                                 threshold_seconds=30, heartbeat=beat)
        self.assertEqual(first["event_type"], "lucas-attention-required")
        self.assertEqual(first["action"], "controller-death-notification")
        state, duplicate = surface.evaluate_watchdog(state, self.verifier, reference_time=NOW,
                                                     threshold_seconds=30, heartbeat=beat)
        self.assertIsNone(duplicate)
        newer = heartbeat(2, "2026-08-31T19:59:50Z")
        state, healthy = surface.evaluate_watchdog(state, self.verifier, reference_time=NOW,
                                                   threshold_seconds=30, heartbeat=newer)
        self.assertIsNone(healthy)
        state, second = surface.evaluate_watchdog(state, self.verifier,
                                                  reference_time="2026-08-31T20:01:00Z",
                                                  threshold_seconds=30)
        self.assertIsNotNone(second)
        self.assertNotEqual(first["intent_digest"], second["intent_digest"])

    def test_pause_holds_watchdog_intent_and_never_sends(self):
        state, intent = surface.evaluate_watchdog(
            self.state, self.verifier, reference_time=NOW, threshold_seconds=30,
            heartbeat=heartbeat(1, "2026-08-31T19:58:00Z"), pause_observed=True)
        self.assertEqual(intent["disposition"], "held-paused")
        self.assertNotIn("destination", intent)
        self.assertEqual(state.notified_heartbeat_digest, state.last_heartbeat_digest)

    def test_sequence_regression_conflict_future_and_bad_state_fail(self):
        state, _ = surface.evaluate_watchdog(self.state, self.verifier, reference_time=NOW,
                                             threshold_seconds=30,
                                             heartbeat=heartbeat(2, "2026-08-31T19:59:50Z"))
        with self.assertRaisesRegex(surface.ControlSurfaceError, "regressed"):
            surface.evaluate_watchdog(state, self.verifier, reference_time=NOW,
                                      threshold_seconds=30,
                                      heartbeat=heartbeat(1, "2026-08-31T19:59:51Z"))
        conflict = heartbeat(2, "2026-08-31T19:59:51Z")
        with self.assertRaisesRegex(surface.ControlSurfaceError, "reused"):
            surface.evaluate_watchdog(state, self.verifier, reference_time=NOW,
                                      threshold_seconds=30, heartbeat=conflict)
        with self.assertRaisesRegex(surface.ControlSurfaceError, "future"):
            surface.evaluate_watchdog(self.state, self.verifier, reference_time=NOW,
                                      threshold_seconds=30,
                                      heartbeat=heartbeat(1, "2026-08-31T20:01:00Z"))
        with self.assertRaises(surface.ControlSurfaceError):
            surface.WatchdogState("hermes-controller", last_sequence=0)

    def test_increasing_sequence_requires_strictly_newer_sent_at(self):
        state, _ = surface.evaluate_watchdog(
            self.state, self.verifier, reference_time=NOW, threshold_seconds=30,
            heartbeat=heartbeat(1, "2026-08-31T19:59:50Z"))
        for sent_at in ("2026-08-31T19:59:49Z", "2026-08-31T19:59:50Z"):
            with self.assertRaisesRegex(surface.ControlSurfaceError, "did not advance"):
                surface.evaluate_watchdog(
                    state, self.verifier, reference_time=NOW, threshold_seconds=30,
                    heartbeat=heartbeat(2, sent_at))

    def test_import_is_inert_and_has_no_transport_or_filesystem_symbols(self):
        self.assertTrue(hasattr(surface, "handle_command"))
        for forbidden in ("requests", "subprocess", "socket", "Path"):
            self.assertFalse(hasattr(surface, forbidden))


if __name__ == "__main__":
    unittest.main()
