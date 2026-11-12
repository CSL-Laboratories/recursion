"""Half-open leases, limit-signal merges and deadlines.

The cases that failed on cc1f1579: a different request's limit drops a live probe
lease; a refusal reports a shorter window and hides a longer one; a later model
signal narrows a connection window; a recovered bucket stays tripped; a launch
that has already passed the deadline still writes to the child.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from recursion_core.routes import contract as c
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.claude_code import ClaudeCodeAdapter
from recursion_core.routes.profile_route import profile_adapters
from recursion_core.routes.service import RouteService
from recursion_core.routes.tests.fakes import FakeClock, claude_ok, codex_script, request
from recursion_core.routes.tests.test_routes import PARENT_ENV, Harness

LIMIT = {"code": c.PLAN_LIMIT, "limit": c.limit_signal("connection", "test.typed")}


class BreakerTransitionTest(unittest.TestCase):
    def test_a_different_requests_limit_does_not_drop_a_live_probe_lease(self) -> None:
        # The probe is inside its deadline. A limit from a request that is not that probe
        # must not free the lease, or the next admit starts a second probe.
        clock = FakeClock()
        breaker = ConnectionBreaker(clock, cooldown_s=300, max_cooldown_s=3600, probe_grace_s=30)
        breaker.record("subscription:A", "m", "t1", LIMIT)
        clock.advance(300)
        first = breaker.admit("subscription:A", "m", "t1", deadline_s=600)
        self.assertIsNotNone(first["probe"])
        late_until = clock.now + 5
        late = {"code": c.PLAN_LIMIT, "limit": c.limit_signal("connection", "late", reset_at=late_until)}
        breaker.record("subscription:A", "m", "t2", late)
        clock.advance(5)
        second = breaker.admit("subscription:A", "m", "t2", deadline_s=600)
        self.assertEqual(second["code"], c.PROBE_IN_FLIGHT)
        self.assertIsNone(second["probe"])
        self.assertNotEqual(second.get("probe"), first["probe"])

    def test_probe_success_keeps_a_limit_opened_while_the_lease_holds(self) -> None:
        clock = FakeClock()
        breaker = ConnectionBreaker(clock, cooldown_s=300, probe_grace_s=30)
        breaker.record("subscription:A", "m", "t1", LIMIT)
        clock.advance(300)
        first = breaker.admit("subscription:A", "m", "t1", deadline_s=600)
        late_until = clock.now + 1000
        late = {"code": c.PLAN_LIMIT, "limit": c.limit_signal("connection", "late", reset_at=late_until)}
        breaker.record("subscription:A", "m", "t2", late)
        effect = breaker.record("subscription:A", "m", "t1", {"code": None}, first["probe"])
        self.assertNotEqual(effect, "closed")
        refused = breaker.admit("subscription:A", "m", "t3")
        self.assertEqual((refused["code"], refused["retry_at"]), (c.BREAKER_OPEN, late_until))

    def test_a_probe_lease_does_not_hide_a_longer_model_window(self) -> None:
        clock = FakeClock()
        breaker = ConnectionBreaker(clock, cooldown_s=300, probe_grace_s=30)
        breaker.record("subscription:A", "m1", "t1", LIMIT)
        clock.advance(300)
        probe = breaker.admit("subscription:A", "m1", "t1", deadline_s=30)
        self.assertEqual(probe["key"], "subscription:A")
        model_until = clock.now + 5000
        model_limit = {"code": c.PLAN_LIMIT, "limit": c.limit_signal(
            "model", "bucket", model="m1", reset_at=model_until)}
        breaker.record("subscription:A", "m1", "t2", model_limit)
        refused = breaker.admit("subscription:A", "m1", "t3")
        self.assertEqual(refused["code"], c.BREAKER_OPEN)
        self.assertEqual(refused["retry_at"], model_until)
        self.assertIn("#model:", refused["key"])

    def test_refusal_waits_out_the_latest_open_window(self) -> None:
        clock = FakeClock()
        breaker = ConnectionBreaker(clock, cooldown_s=300)
        conn_until = clock.now + 100
        model_until = clock.now + 5000
        breaker.record("subscription:A", "m", "t1", {"code": c.PLAN_LIMIT, "limit": c.limit_signal(
            "connection", "c", reset_at=conn_until)})
        breaker.record("subscription:A", "m", "t1", {"code": c.PLAN_LIMIT, "limit": c.limit_signal(
            "model", "m", model="m", reset_at=model_until)})
        refused = breaker.admit("subscription:A", "m", "t2")
        self.assertEqual(refused["code"], c.BREAKER_OPEN)
        self.assertEqual(refused["retry_at"], model_until)


class LimitSignalAccountingTest(unittest.TestCase):
    def test_a_later_connection_limit_is_not_hidden_by_an_earlier_model_event(self) -> None:
        script = [
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "seven_day_opus", "resetsAt": 1_000_400.0}},
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1_005_000.0}},
            {"type": "result", "subtype": "error", "is_error": True, "result": "limit"},
        ]
        h = Harness([script, claude_ok("sonnet")])
        res = h.handle(request(model="claude-opus-4"))
        self.assertEqual(res["limit"]["scope"], "connection")
        self.assertEqual(res["limit"]["reset_at"], 1_005_000.0)
        self.assertEqual(h.handle(request(rid="s", model="claude-sonnet-4"))["code"], c.BREAKER_OPEN)

    def test_a_later_reset_on_the_same_window_replaces_the_earlier_one(self) -> None:
        script = [
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1_000_400.0}},
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1_005_000.0}},
            {"type": "result", "subtype": "error", "is_error": True, "result": "limit"},
        ]
        h = Harness([script])
        res = h.handle(request())
        self.assertEqual(res["limit"]["reset_at"], 1_005_000.0)

    def test_a_typed_connection_error_widens_an_earlier_model_window(self) -> None:
        script = [
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "seven_day_opus", "resetsAt": 1_000_400.0}},
            {"type": "result", "subtype": "error", "is_error": True, "error": "rate_limit", "result": "limit"},
        ]
        h = Harness([script, claude_ok("sonnet")])
        res = h.handle(request(model="claude-opus-4"))
        self.assertEqual(res["limit"]["scope"], "connection")
        self.assertEqual(h.handle(request(rid="s", model="claude-sonnet-4"))["code"], c.BREAKER_OPEN)

    def test_an_earlier_connection_window_is_not_narrowed_by_a_later_model_event(self) -> None:
        script = [
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1_005_000.0}},
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "seven_day_opus", "resetsAt": 1_000_400.0}},
            {"type": "result", "subtype": "error", "is_error": True, "result": "limit"},
        ]
        h = Harness([script, claude_ok("sonnet")])
        res = h.handle(request(model="claude-opus-4"))
        self.assertEqual(res["limit"]["scope"], "connection")
        self.assertEqual(h.handle(request(rid="s", model="claude-sonnet-4"))["code"], c.BREAKER_OPEN)

    def test_a_later_model_snapshot_does_not_narrow_an_earlier_connection_window(self) -> None:
        notes = [
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": 1_002_000}}}},
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "gpt-5-bucket", "primary": {"usedPercent": 100, "resetsAt": 1_000_900}}}},
        ]
        turn = {"id": "tu-1", "status": "failed", "error": {"message": "upstream error"}}
        h = Harness([codex_script(notes=notes, turn=turn), codex_script("next")])
        res = h.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual(res["limit"]["scope"], "connection")
        self.assertEqual(h.breaker.state("codex:subscription:login-A"), "open")
        self.assertEqual(h.handle(request(rid="other", engine="codex", model="gpt-5"))["code"], c.BREAKER_OPEN)

    def test_a_later_connection_snapshot_replaces_an_earlier_model_bucket(self) -> None:
        notes = [
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "gpt-5-bucket", "primary": {"usedPercent": 100, "resetsAt": 1_000_900}}}},
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": 1_002_000}}}},
        ]
        turn = {"id": "tu-1", "status": "failed", "error": {"message": "upstream error"}}
        h = Harness([codex_script(notes=notes, turn=turn)])
        res = h.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"]["scope"], "connection")
        self.assertEqual(res["limit"]["reset_at"], 1_002_000.0)
        self.assertEqual(h.breaker.state("codex:subscription:login-A"), "open")

    def test_a_later_reset_on_the_same_codex_bucket_replaces_the_earlier_one(self) -> None:
        notes = [
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": 1_000_400}}}},
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": 1_005_000}}}},
        ]
        turn = {"id": "tu-1", "status": "failed", "error": {"message": "upstream error"}}
        h = Harness([codex_script(notes=notes, turn=turn)])
        res = h.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual(res["limit"]["reset_at"], 1_005_000.0)

    def test_a_later_healthy_snapshot_clears_that_bucket(self) -> None:
        notes = [
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "gpt-5-bucket", "primary": {"usedPercent": 100, "resetsAt": 1_000_900}}}},
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "gpt-5-bucket", "primary": {"usedPercent": 10}}}},
        ]
        turn = {"id": "tu-1", "status": "failed", "error": {"message": "upstream error"}}
        h = Harness([codex_script(notes=notes, turn=turn)])
        res = h.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual(res["code"], c.ENGINE_ERROR)
        self.assertEqual(h.breaker.state("codex:subscription:login-A", "gpt-5-bucket"), "closed")

    def test_a_different_buckets_healthy_snapshot_does_not_clear_the_exhausted_one(self) -> None:
        notes = [
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "gpt-5-bucket", "primary": {"usedPercent": 100, "resetsAt": 1_000_900}}}},
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "other-bucket", "primary": {"usedPercent": 10}}}},
        ]
        turn = {"id": "tu-1", "status": "failed", "error": {"message": "upstream error"}}
        h = Harness([codex_script(notes=notes, turn=turn)])
        res = h.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"]["model"], "gpt-5-bucket")

    def test_an_in_flight_exhausted_update_beats_the_read_result(self) -> None:
        # This script's update is read while account/rateLimits/read is in flight, so it is
        # newer than that read. The test used to expect the healthy result to clear the update
        # and the turn to run; that expectation was the regression. An update from before the
        # read request is covered by test_a_healthy_read_clears_an_update_from_before_the_request.
        script = [
            {"id": 1, "result": {"userAgent": "fake"}},
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": 1_000_800}}}},
            {"id": 2, "result": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 10}}}},
            {"id": 3, "result": {"thread": {"id": "th-1"}}},
            {"id": 4, "result": {"turn": {"id": "tu-1"}}},
            {"method": "turn/completed", "params": {"turn": {
                "id": "tu-1", "status": "failed", "error": {"message": "upstream error"}}}},
        ]
        h = Harness([script])
        res = h.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"]["reset_at"], 1_000_800.0)
        methods = [m.get("method") for m in h.launcher.processes[0].sent]
        self.assertNotIn("turn/start", methods)
        self.assertNotIn("thread/start", methods)

    def test_a_longer_model_window_is_not_erased_by_a_shorter_connection_signal(self) -> None:
        # five_hour ends at +400s. seven_day_opus ends at +9000s. Booking only the connection
        # signal admits Opus once that shorter window is over.
        script = [
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1_000_400.0}},
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "seven_day_opus", "resetsAt": 1_009_000.0}},
            {"type": "result", "subtype": "error", "is_error": True, "result": "limit"},
        ]
        h = Harness([script, claude_ok("sonnet")])
        res = h.handle(request(model="claude-opus-4"))
        self.assertEqual(res["limit"]["scope"], "connection")
        self.assertEqual(res["limit"]["reset_at"], 1_000_400.0)
        h.clock.advance(401)
        self.assertIsNone(h.handle(request(rid="s", model="claude-sonnet-4"))["code"])
        self.assertEqual(h.handle(request(rid="o", model="claude-opus-4"))["code"], c.BREAKER_OPEN)

    def test_a_longer_connection_window_is_not_erased_by_a_later_model_signal(self) -> None:
        script = [
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "seven_day_opus", "resetsAt": 1_009_000.0}},
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1_000_400.0}},
            {"type": "result", "subtype": "error", "is_error": True, "result": "limit"},
        ]
        h = Harness([script, claude_ok("sonnet")])
        res = h.handle(request(model="claude-opus-4"))
        self.assertEqual(res["limit"]["scope"], "connection")
        h.clock.advance(401)
        self.assertIsNone(h.handle(request(rid="s", model="claude-sonnet-4"))["code"])
        self.assertEqual(h.handle(request(rid="o", model="claude-opus-4"))["code"], c.BREAKER_OPEN)

    def test_a_longer_codex_model_window_survives_a_shorter_connection_snapshot(self) -> None:
        notes = [
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": 1_000_400}}}},
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "gpt-5-bucket", "primary": {"usedPercent": 100, "resetsAt": 1_009_000}}}},
        ]
        turn = {"id": "tu-1", "status": "failed", "error": {"message": "upstream error"}}
        h = Harness([codex_script(notes=notes, turn=turn)])
        h.handle(request(engine="codex", model="gpt-5"))
        h.clock.advance(401)
        conn = "codex:subscription:login-A"
        self.assertNotEqual(h.breaker.state(conn), "open")
        self.assertEqual(h.breaker.state(conn, "gpt-5-bucket"), "open")

    def test_a_later_connection_snapshot_does_not_erase_a_longer_model_window(self) -> None:
        notes = [
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "gpt-5-bucket", "primary": {"usedPercent": 100, "resetsAt": 1_009_000}}}},
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": 1_000_400}}}},
        ]
        turn = {"id": "tu-1", "status": "failed", "error": {"message": "upstream error"}}
        h = Harness([codex_script(notes=notes, turn=turn)])
        res = h.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual(res["limit"]["scope"], "connection")
        h.clock.advance(401)
        conn = "codex:subscription:login-A"
        self.assertNotEqual(h.breaker.state(conn), "open")
        self.assertEqual(h.breaker.state(conn, "gpt-5-bucket"), "open")

    def test_a_typed_rate_limit_does_not_copy_the_model_reset_onto_the_connection(self) -> None:
        # Opus is exhausted for 10s. The typed error names no connection reset. Copying the
        # model reset onto the connection fence lets Sonnet in at +11s.
        script = [
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "seven_day_opus", "resetsAt": 1_000_010.0}},
            {"type": "result", "subtype": "error", "is_error": True, "error": "rate_limit", "result": "limit"},
        ]
        h = Harness([script, claude_ok("sonnet")])
        res = h.handle(request(model="claude-opus-4"))
        self.assertEqual(res["limit"]["scope"], "connection")
        self.assertIsNone(res["limit"]["reset_at"])
        h.clock.advance(11)
        self.assertEqual(h.handle(request(rid="s", model="claude-sonnet-4"))["code"], c.BREAKER_OPEN)

    def test_widening_to_connection_keeps_the_longer_model_window(self) -> None:
        script = [
            {"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "seven_day_opus", "resetsAt": 1_001_000.0}},
            {"type": "result", "subtype": "error", "is_error": True, "error": "rate_limit", "result": "limit"},
        ]
        h = Harness([script, claude_ok("sonnet")])
        res = h.handle(request(model="claude-opus-4"))
        self.assertEqual(res["limit"]["scope"], "connection")
        h.clock.advance(301)  # past the breaker cooldown the connection fence uses
        self.assertIsNone(h.handle(request(rid="s", model="claude-sonnet-4"))["code"])
        self.assertEqual(h.handle(request(rid="o", model="claude-opus-4"))["code"], c.BREAKER_OPEN)

    def test_an_omitted_window_stays_exhausted_until_it_is_reported_healthy(self) -> None:
        notes = [
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "gpt-5-bucket", "secondary": {"usedPercent": 100, "resetsAt": 1_000_900}}}},
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "gpt-5-bucket", "primary": {"usedPercent": 10}}}},
        ]
        turn = {"id": "tu-1", "status": "failed", "error": {"message": "upstream error"}}
        h = Harness([codex_script(notes=notes, turn=turn)])
        res = h.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"]["reset_at"], 1_000_900.0)
        self.assertEqual(h.breaker.state("codex:subscription:login-A", "gpt-5-bucket"), "open")

    def test_an_unreadable_used_percent_does_not_clear_an_exhausted_window(self) -> None:
        notes = [
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "gpt-5-bucket", "primary": {"usedPercent": 100, "resetsAt": 1_000_900}}}},
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "gpt-5-bucket", "primary": {"usedPercent": "100"}}}},
        ]
        turn = {"id": "tu-1", "status": "failed", "error": {"message": "upstream error"}}
        h = Harness([codex_script(notes=notes, turn=turn)])
        res = h.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"]["reset_at"], 1_000_900.0)

    def test_a_healthy_read_clears_an_update_from_before_the_request(self) -> None:
        script = [
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": 1_000_800}}}},
            {"id": 1, "result": {"userAgent": "fake"}},
            {"id": 2, "result": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 10}}}},
            {"id": 3, "result": {"thread": {"id": "th-1"}}},
            {"id": 4, "result": {"turn": {"id": "tu-1"}}},
            {"method": "turn/completed", "params": {"turn": {
                "id": "tu-1", "status": "failed", "error": {"message": "upstream error"}}}},
        ]
        h = Harness([script])
        res = h.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual(res["code"], c.ENGINE_ERROR)
        self.assertEqual(h.breaker.state("codex:subscription:login-A"), "closed")

    def test_a_newer_snapshot_generation_beats_arrival_order(self) -> None:
        # The update is delivered before the read request, which would otherwise let the
        # healthy read win. Its generation is newer, so the exhausted window stays.
        script = [
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "codex", "generation": 5,
                "primary": {"usedPercent": 100, "resetsAt": 1_000_800}}}},
            {"id": 1, "result": {"userAgent": "fake"}},
            {"id": 2, "result": {"rateLimits": {
                "limitId": "codex", "generation": 1, "primary": {"usedPercent": 10}}}},
            {"id": 3, "result": {"thread": {"id": "th-1"}}},
            {"id": 4, "result": {"turn": {"id": "tu-1"}}},
            {"method": "turn/completed", "params": {"turn": {"id": "tu-1", "status": "completed"}}},
        ]
        h = Harness([script])
        res = h.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"]["reset_at"], 1_000_800.0)
        methods = [m.get("method") for m in h.launcher.processes[0].sent]
        self.assertNotIn("turn/start", methods)

    def test_a_newer_read_generation_clears_an_older_in_flight_update(self) -> None:
        # Arrival would keep the update, because it was received after the read was sent.
        # The read's generation is newer, so the healthy reading wins.
        script = [
            {"id": 1, "result": {"userAgent": "fake"}},
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "codex", "generation": 1,
                "primary": {"usedPercent": 100, "resetsAt": 1_000_800}}}},
            {"id": 2, "result": {"rateLimits": {
                "limitId": "codex", "generation": 5, "primary": {"usedPercent": 10}}}},
            {"id": 3, "result": {"thread": {"id": "th-1"}}},
            {"id": 4, "result": {"turn": {"id": "tu-1"}}},
            {"method": "turn/completed", "params": {"turn": {
                "id": "tu-1", "status": "failed", "error": {"message": "upstream error"}}}},
        ]
        h = Harness([script])
        res = h.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual(res["code"], c.ENGINE_ERROR)
        self.assertEqual(h.breaker.state("codex:subscription:login-A"), "closed")

    def test_an_exhausted_update_before_turn_start_does_not_start_the_turn(self) -> None:
        script = [
            {"id": 1, "result": {"userAgent": "fake"}},
            {"id": 2, "result": {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 10}}}},
            {"method": "account/rateLimits/updated", "params": {"rateLimits": {
                "limitId": "codex", "primary": {"usedPercent": 100, "resetsAt": 1_000_800}}}},
            {"id": 3, "result": {"thread": {"id": "th-1"}}},
            {"id": 4, "result": {"turn": {"id": "tu-1"}}},
            {"method": "turn/completed", "params": {"turn": {"id": "tu-1", "status": "completed"}}},
        ]
        h = Harness([script])
        res = h.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual(res["code"], c.PLAN_LIMIT)
        self.assertEqual(res["limit"]["reset_at"], 1_000_800.0)
        methods = [m.get("method") for m in h.launcher.processes[0].sent]
        self.assertNotIn("turn/start", methods)
        self.assertIn("thread/start", methods)


class DeadlineLaunchTest(unittest.TestCase):
    def test_a_launch_that_passes_the_deadline_does_not_write_stdin(self) -> None:
        clock = FakeClock()
        proc = _Proc()

        def launch(argv, **kwargs):
            clock.advance(120)
            return proc

        adapter = ClaudeCodeAdapter(launch, clock, lambda *_: "", PARENT_ENV, wall_clock=clock)
        service = RouteService({"claude_code": adapter}, ConnectionBreaker(clock), clock)
        res = service.handle(request(deadline_s=60))
        self.assertEqual(res["code"], c.DEADLINE_EXCEEDED)
        self.assertEqual(proc.sent, [])
        self.assertEqual(proc.fenced, ["deadline"])
        self.assertEqual(res["breaker_effect"], "none")
        self.assertEqual(service._breaker.state("claude_code:subscription:login-A"), "closed")
        self.assertEqual(service._live, {})

    def test_a_profile_launch_that_passes_the_deadline_does_not_drive(self) -> None:
        clock = FakeClock()
        home = tempfile.mkdtemp(prefix="route-deadline-")
        self.addCleanup(shutil.rmtree, home)
        os.chmod(home, 0o700)
        proc = _Proc()
        driven = []

        def launch(argv, **kwargs):
            clock.advance(120)
            return proc

        def drive(process, req, deadline_at, events):
            driven.append(req["request_id"])
            return c.result(req["request_id"], None, text="driven")

        adapters = profile_adapters(
            launch, clock, lambda *_: "sk-test", {"PATH": "/usr/bin", "HOME": "/home/operator"},
            argv=lambda req: ["opencode"], driver=drive, operator_home="/home/operator")
        service = RouteService(adapters, ConnectionBreaker(clock), clock)
        profile = {"provider": "openai", "auth": "api_key", "credential_env": "OPENAI_API_KEY"}
        res = service.handle(request(
            engine="opencode", model="m", kind="api_key", conn_id="k1", credential_env="OPENAI_API_KEY",
            profile=profile, home=home, deadline_s=60))
        self.assertEqual(res["code"], c.DEADLINE_EXCEEDED)
        self.assertEqual(driven, [])
        self.assertEqual(proc.fenced, ["deadline"])
        self.assertEqual(service._live, {})

    def test_a_deadline_fence_failure_stays_tracked(self) -> None:
        clock = FakeClock()
        proc = _Proc()

        def fence(reason: str) -> None:
            proc.fenced.append(reason)
            raise RuntimeError("fence failed")

        proc.fence = fence

        def launch(argv, **kwargs):
            clock.advance(120)
            return proc

        adapter = ClaudeCodeAdapter(launch, clock, lambda *_: "", PARENT_ENV, wall_clock=clock)
        service = RouteService({"claude_code": adapter}, ConnectionBreaker(clock), clock)
        with self.assertRaises(RuntimeError):
            service.handle(request(deadline_s=60))
        self.assertEqual(proc.sent, [])
        self.assertEqual(proc.fenced, ["deadline"])
        self.assertIs(service._live.get(id(proc)), proc)
        self.assertIn(id(proc), service._retained)


class _Proc:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fenced: list[str] = []

    def send(self, line: str) -> None:
        self.sent.append(line)

    def close_stdin(self) -> None:
        return None

    def read_line(self, timeout_s: float):
        return None

    def fence(self, reason: str) -> None:
        self.fenced.append(reason)


if __name__ == "__main__":
    unittest.main()
