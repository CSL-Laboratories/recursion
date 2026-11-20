"""Second answer round on #376: one test for each item R1 to R8.

Each test is written to fail on the head it was added to, for the reason that item names.
No process, engine or network is used. Every path comes from a temporary directory.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.routes import contract as c
from recursion_core.routes import driver as d
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.driver import Declaration, DriverInputError, DriverStateError, Flags, check_draft, one_turn
from recursion_core.routes.host import PLATFORM_UNSUPPORTED
from recursion_core.routes.profile_route import profile_adapters
from recursion_core.routes.service import RouteService
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, request, runs_adapter
from recursion_core.routes.tests.test_driver import StandIn, msg, session
from recursion_core.routes.tests.test_routes_claude import Harness

_REPO = Path(d.__file__).resolve().parents[3]
_MARKER = "S7-TRIAGE-MARKER\nFORGED-LINE"


class SecondRoundTest(unittest.TestCase):
    def test_r1_interrupt_after_the_input_has_ended_writes_nothing(self) -> None:
        driver = StandIn(Flags(interrupt=True))
        with tempfile.TemporaryDirectory() as temp:
            handle = driver.start(session(), None, {"PATH": str(Path(temp) / "bin")})
        driver.send(handle, msg())
        driver.send(handle, {"kind": "close"})
        written = list(driver.written)
        with self.assertRaises(DriverStateError):
            driver.interrupt(handle, "turn-1")
        self.assertEqual(driver.written, written)

    def test_r2_a_bad_registered_name_is_a_typed_result(self) -> None:
        class Growing:
            def __init__(self) -> None:
                self.calls = 0

            def names(self) -> list[str]:
                self.calls += 1
                if self.calls == 1:
                    return ["claude_code"]
                return ["claude_code", "Bad-Name"]

        class Idle:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, req, deadline_at):
                self.calls += 1
                return c.result(req["request_id"], None)

        clock = FakeClock()
        idle = Idle()
        service = RouteService({"claude_code": idle}, ConnectionBreaker(clock), clock, engines=Growing())
        res = service.handle(request())
        self.assertIsInstance(res, dict)
        self.assertEqual(res["code"], c.REQUEST_INVALID)
        self.assertEqual(idle.calls, 0)

    def test_r3_a_profile_route_does_not_read_a_credential_before_the_host_refusal(self) -> None:
        asked: list[tuple[str, str]] = []

        def credentials(conn_id: str, name: str) -> str:
            asked.append((conn_id, name))
            return "sk-test"

        profiles = {
            "pi": {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY"},
            "opencode": {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY"},
            "hermes": {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY",
                       "auth.adopt_external_logins": False},
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir()
            clock = FakeClock()
            launcher = FakeLauncher(lambda argv, env: [{"ok": True}], clock)
            drive = lambda process, req, deadline_at, events: c.result(req["request_id"], None, text="driven")
            adapters = profile_adapters(
                launcher, clock, credentials, {"PATH": str(root / "bin"), "HOME": str(home)},
                argv=lambda req: [req["route"]["engine"]], driver=drive, operator_home=str(home))
            service = RouteService(adapters, ConnectionBreaker(clock), clock)
            with mock.patch.object(os, "name", "nt"):
                for engine, profile in profiles.items():
                    with self.subTest(engine=engine):
                        before = list(asked)
                        res = service.handle(request(
                            engine=engine, model="m", kind="api_key", conn_id="k1",
                            credential_env="ANTHROPIC_API_KEY", profile=profile, home=str(home)))
                        self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, PLATFORM_UNSUPPORTED))
                        self.assertEqual(asked, before)
            self.assertEqual(launcher.calls, [])

    def test_r4_an_unhashable_draft_type_is_a_driver_input_error(self) -> None:
        with self.assertRaises(DriverInputError) as caught:
            check_draft({"type": ["nope"], "payload": {}})
        self.assertEqual(caught.exception.field, "draft.type")

    def test_r5_a_count_the_log_cannot_write_is_refused(self) -> None:
        draft = {"type": "engine.output", "payload": {
            "stream": "stdout", "text": "x", "offset_start": 10 ** 4300, "offset_end": 1}}
        with self.assertRaises(DriverInputError) as caught:
            check_draft(draft)
        self.assertEqual(caught.exception.field, "draft.engine.output.offset_start")

    @runs_adapter
    def test_r6_a_reported_mode_marker_is_not_copied(self) -> None:
        marked = Harness([[{"type": "system", "subtype": "init", "permissionMode": _MARKER}]])
        res = marked.handle(request())
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertIn("unknown", res["detail"])
        blob = json.dumps(res)
        self.assertNotIn("S7-TRIAGE-MARKER", blob)
        self.assertNotIn("FORGED-LINE", blob)
        for event in res["events"]:
            self.assertNotIn("S7-TRIAGE-MARKER", json.dumps(event))
            self.assertNotIn("FORGED-LINE", json.dumps(event))
        known = Harness([[{"type": "system", "subtype": "init", "permissionMode": "plan"}]])
        named = known.handle(request())
        self.assertEqual(named["code"], c.LAUNCH_REFUSED)
        self.assertIn("plan", named["detail"])
        self.assertNotIn("unknown", named["detail"])

    def test_r7_a_schema_record_loads_and_the_emitted_record_stays_inside_it(self) -> None:
        schema = json.loads((_REPO / "schemas/control/v1/driver.v2.json").read_text(encoding="utf-8"))
        flags_schema = schema["$defs"]["flags"]
        required = flags_schema["required"]
        properties = set(flags_schema["properties"])
        record = {"engine": "claude_code", "driver": 2, "input_ack": False,
                  "flags": {name: False for name in required}}
        loaded = Declaration.from_record(record)
        self.assertEqual(set(loaded.as_record()["flags"]), set(required))
        emitted = one_turn("claude_code").as_record()["flags"]
        self.assertLessEqual(set(emitted), properties)
        self.assertFalse(flags_schema["additionalProperties"])

    @runs_adapter
    def test_r8_an_unhashable_stream_field_is_a_typed_result(self) -> None:
        h = Harness([[{"type": ["assistant"], "subtype": {"a": 1}}]])
        res = h.handle(request())
        self.assertIsInstance(res, dict)
        self.assertEqual(res["code"], c.PROTOCOL_ERROR)
        self.assertEqual(h.launcher.processes[0].fenced, ["protocol_error"])


if __name__ == "__main__":
    unittest.main()
