"""R3-9: malformed JSON-RPC responses must reach the existing protocol-error path."""
import unittest
from recursion_core.routes import CodexAdapter, ConnectionBreaker, RouteService
from recursion_core.routes import contract as c
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, codex_script, request

class MalformedRpcTest(unittest.TestCase):
    def test_nonobject_error_cannot_be_interpreted_as_a_result(self):
        for error in ("FAKE-error", [], 23, None):
            for response_id in (1,2,4):
                with self.subTest(error=error,response_id=response_id):
                    clock = FakeClock()
                    script = codex_script()
                    for index, message in enumerate(script):
                        if message.get("id") == response_id:
                            script[index] = {"id":response_id,"error":error}
                    launcher = FakeLauncher([script],clock)
                    service = RouteService({"codex":CodexAdapter(launcher,clock,lambda *_:"",{})},ConnectionBreaker(clock),clock)
                    result = service.handle(request(engine="codex",model="gpt-5"))
                    self.assertEqual(result["code"], c.PROTOCOL_ERROR)
                    self.assertEqual(launcher.processes[0].fenced, ["protocol_error"])
