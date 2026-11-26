"""R3-9: malformed JSON-RPC responses must reach the existing protocol-error path."""
import unittest
from unittest.mock import patch

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
    # The test drives the adapter through a fake launcher only. DRIVER-CC's R3-7 (#376) refuses native
    # Windows at the shared adapter boundary before the launcher, which would turn every subtest into
    # launch_refused there; pinning the host test keeps these protocol assertions running on Windows (the
    # two reviews of #376, R4). create=True: the boundary's name exists only once #376 has merged.
    @patch("recursion_core.routes.base.platform_supported", lambda: True, create=True)
    def test_nonobject_error_cannot_be_interpreted_as_a_result(self):
        for error in ("FAKE-error", [], 23, None):
            for response_id in (1, 2, 4):
                with self.subTest(error=error, response_id=response_id):
                    result, launcher = _nonobject_error(error, response_id)
                    self.assertEqual(result["code"], c.PROTOCOL_ERROR)
                    self.assertEqual(launcher.processes[0].fenced, ["protocol_error"])

    def test_a_pinned_host_check_keeps_the_protocol_error_and_one_fence(self):
        # The class marker pins the host check so a launch is allowed. This holds the protocol
        # path and the one fence under that pin. It is not the host refusal: with the real check
        # a Windows-reported host is launch_refused before any child, which test_r3_finding_7 holds.
        for error in ("FAKE-error", [], 23, None):
            for response_id in (1, 2, 4):
                with self.subTest(error=error, response_id=response_id):
                    result, launcher = _nonobject_error(error, response_id)
                    self.assertEqual(result["code"], c.PROTOCOL_ERROR)
                    self.assertEqual(
                        (len(launcher.calls), launcher.processes[0].fenced), (1, ["protocol_error"]))
