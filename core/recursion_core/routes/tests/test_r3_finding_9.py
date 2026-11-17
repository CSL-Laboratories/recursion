"""R3-9: malformed JSON-RPC responses must reach the existing protocol-error path."""
import os
import unittest
from unittest import mock

from recursion_core.routes import CodexAdapter, ConnectionBreaker, RouteService
from recursion_core.routes import contract as c
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, codex_script, request, runs_adapter


def _nonobject_error(error, response_id):
    clock = FakeClock()
    script = codex_script()
    for index, message in enumerate(script):
        if message.get("id") == response_id:
            script[index] = {"id": response_id, "error": error}
    launcher = FakeLauncher([script], clock)
    service = RouteService(
        {"codex": CodexAdapter(launcher, clock, lambda *_: "", {})}, ConnectionBreaker(clock), clock)
    result = service.handle(request(engine="codex", model="gpt-5"))
    return result, launcher


@runs_adapter
class MalformedRpcTest(unittest.TestCase):
    # Fake launcher only. R3-7 refuses a Windows-reported host in RouteAdapter.run before any launch,
    # which would turn every subtest into launch_refused. runs_adapter pins that host check so the
    # protocol assertions keep running (the two reviews of #376, R4). The real refusal stays in
    # test_r3_finding_7, which does not carry this marker.
    def test_nonobject_error_cannot_be_interpreted_as_a_result(self):
        for error in ("FAKE-error", [], 23, None):
            for response_id in (1, 2, 4):
                with self.subTest(error=error, response_id=response_id):
                    result, launcher = _nonobject_error(error, response_id)
                    self.assertEqual(result["code"], c.PROTOCOL_ERROR)
                    self.assertEqual(launcher.processes[0].fenced, ["protocol_error"])

    def test_a_windows_reported_host_keeps_the_protocol_error_and_fences_once(self):
        # No simulation harness. os.name is nt in this process, which is enough for the host check
        # to refuse. Without runs_adapter the code is launch_refused and no child starts.
        with mock.patch.object(os, "name", "nt"):
            for error in ("FAKE-error", [], 23, None):
                for response_id in (1, 2, 4):
                    with self.subTest(error=error, response_id=response_id):
                        result, launcher = _nonobject_error(error, response_id)
                        self.assertEqual(result["code"], c.PROTOCOL_ERROR)
                        self.assertEqual(len(launcher.calls), 1)
                        self.assertEqual(launcher.processes[0].fenced, ["protocol_error"])
