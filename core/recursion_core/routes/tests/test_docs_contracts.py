"""core-a F05, core-a F18, and core-b F17.

F05 is the scope of a breaker refusal. F18 and F17 are the documented contract.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from recursion_core.child_env.policy import ChildRoute
from recursion_core.routes import ConnectionBreaker, RouteService
from recursion_core.routes import contract as c
from recursion_core.routes.tests.fakes import FakeClock, request

_CORE = Path(__file__).resolve().parents[2]
_PHRASE = "no driver does this yet"


class DocsContractTest(unittest.TestCase):
    def test_f05_connection_scope_does_not_depend_on_display_text(self):
        clock = FakeClock()
        breaker = ConnectionBreaker(clock)
        req = request(conn_id="team#model:shared")
        breaker.record(c.connection_key(req["route"]), req["route"]["model"], "t1",
                       c.result("r", c.PLAN_LIMIT, limit=c.limit_signal("connection", "fixture")))
        service = RouteService({"claude_code": object()}, breaker, clock)
        self.assertEqual(service.handle(req)["limit_notice"]["scope"], "connection")

    def test_f05_a_model_bucket_is_still_model_scope(self):
        clock = FakeClock()
        breaker = ConnectionBreaker(clock)
        req = request(conn_id="login-A", model="claude-opus")
        breaker.record(c.connection_key(req["route"]), req["route"]["model"], "t1",
                       c.result("r", c.PLAN_LIMIT, limit=c.limit_signal("model", "fixture", model="claude-opus")))
        service = RouteService({"claude_code": object()}, breaker, clock)
        self.assertEqual(service.handle(req)["limit_notice"]["scope"], "model")

    def test_f18_validate_request_documents_engine_unknown(self):
        doc = c.validate_request.__doc__ or ""
        self.assertIn("ENGINE_UNKNOWN", doc)
        self.assertIn("REQUEST_INVALID", doc)

    def test_f17_no_driver_passes_trusted_base_urls_yet(self):
        readme = (_CORE / "child_env" / "README.md").read_text()
        doc = ChildRoute.__doc__ or ""
        self.assertIn(_PHRASE, readme)
        self.assertIn(_PHRASE, doc)


if __name__ == "__main__":
    unittest.main()
