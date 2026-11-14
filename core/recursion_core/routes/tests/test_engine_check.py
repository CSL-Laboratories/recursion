"""The call-time engine check (seam S3): today's five names, plus what the composition registered.

Before: ``contract.ENGINES``, a tuple of five names fixed at import, refused every other engine
``engine_unknown`` before dispatch (recursion_loop and every new engine). SPINE v3, S3: "its first slice
makes the check at 121-123 call a function that returns today's five names plus the registered ones at the
time of the call, not a tuple fixed at import." SPINE's engine table (``routes.engines.EngineTable``, in its
seams pull request) belongs to one composition, so it reaches the check through the route service. Until
that module is on this base the tests use a stand-in with the same ``names()``; one test runs against the
real table when it is importable. Fake doubles only.
"""

from __future__ import annotations

import importlib.util
import unittest

from recursion_core.routes import contract as c
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.limits import validate_limits
from recursion_core.routes.service import RouteService
from recursion_core.routes.tests.fakes import FakeClock, request

# The base's answer, written out: what master fb578e31 checked against.
BASE_FIVE = ("claude_code", "codex", "pi", "hermes", "opencode")
# S3's catalogue as SPINE's draft has it: names only, no factory for the last eight until a lane registers one.
CATALOGUE = ("claude_code", "codex", "opencode", "grok", "antigravity", "cursor", "gemini_cli", "pi", "hermes",
             "recursion_loop")


class StandInTable:
    """The part of S3's EngineTable the check reads: ``names()``, the engines with an active factory."""

    def __init__(self, *registered: str) -> None:
        self._names = ["claude_code", "codex", *registered]  # seeded with today's two, in today's order

    def register(self, name: str) -> None:
        self._names.append(name)

    def names(self) -> tuple:
        return tuple(self._names)


class EchoAdapter:
    """An adapter for an engine with no adapter today: answers its own engine name and launches nothing."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, req, deadline_at):
        self.calls.append(req["route"]["engine"])
        return c.result(req["request_id"], None, text=req["route"]["engine"])


def _route(engine: str, model: str = "m") -> dict:
    return {"engine": engine, "model": model, "connection": {"kind": "subscription", "id": "login-A"},
            **({"profile": {"provider": "x"}} if engine in c.PROFILE_ENGINES else {})}


def _engine_code(engine: str, engines=None) -> str | None:
    req = request(engine=engine)
    if engine in c.PROFILE_ENGINES:
        req["route"]["profile"] = {"provider": "x"}
    try:
        c.validate_request(req, engines=engines)
    except c.RouteError as err:
        return err.code
    return None


class NothingRegisteredTest(unittest.TestCase):
    def test_with_nothing_registered_the_check_answers_as_at_the_base(self) -> None:
        self.assertEqual(c.engine_names(), BASE_FIVE)
        for engine in (*CATALOGUE, "nope", "", None, 7):
            expected = None if engine in BASE_FIVE else c.ENGINE_UNKNOWN
            with self.subTest(engine=engine):
                self.assertEqual(_engine_code(engine), expected)
                self.assertEqual(_engine_code(engine, StandInTable()), expected)  # the seeded table alone

    def test_a_service_with_todays_adapters_answers_as_at_the_base(self) -> None:
        clock = FakeClock()
        service = RouteService({"claude_code": EchoAdapter(), "codex": EchoAdapter()}, ConnectionBreaker(clock), clock)
        self.assertEqual(service.handle(request(engine="recursion_loop"))["code"], c.ENGINE_UNKNOWN)
        self.assertEqual(service.handle(request(engine="claude_code"))["status"], "ok")
        # One of the five with no adapter passes the check and is refused at dispatch, as before.
        res = service.handle(request(engine="opencode", profile={"provider": "x"}))
        self.assertEqual((res["code"], res["detail"]), (c.ENGINE_UNKNOWN, "no adapter for opencode"))


class RegisteredTest(unittest.TestCase):
    def test_an_engine_registered_at_composition_passes_validate_request(self) -> None:
        table = StandInTable("recursion_loop")
        self.assertIsNone(_engine_code("recursion_loop", table))
        self.assertEqual(c.engine_names(table), (*BASE_FIVE, "recursion_loop"))

    def test_an_unregistered_catalogue_name_is_still_engine_unknown(self) -> None:
        table = StandInTable("recursion_loop")
        for engine in CATALOGUE:
            if engine not in BASE_FIVE and engine != "recursion_loop":
                with self.subTest(engine=engine):
                    self.assertEqual(_engine_code(engine, table), c.ENGINE_UNKNOWN)

    def test_the_names_are_read_at_each_call(self) -> None:
        table = StandInTable()
        self.assertEqual(_engine_code("grok", table), c.ENGINE_UNKNOWN)
        table.register("grok")  # a real table is frozen after composition; this shows the read is not cached
        self.assertIsNone(_engine_code("grok", table))

    def test_the_service_checks_against_its_composition(self) -> None:
        clock = FakeClock()
        loop = EchoAdapter()
        service = RouteService({"claude_code": EchoAdapter(), "recursion_loop": loop}, ConnectionBreaker(clock), clock)
        res = service.handle(request(engine="recursion_loop"))
        self.assertEqual((res["status"], res["text"], loop.calls), ("ok", "recursion_loop", ["recursion_loop"]))
        # An explicit table is what counts when given: an engine it does not name is refused before dispatch.
        service = RouteService({"recursion_loop": loop}, ConnectionBreaker(clock), clock, engines=StandInTable())
        self.assertEqual(service.handle(request(engine="recursion_loop"))["code"], c.ENGINE_UNKNOWN)
        self.assertEqual(loop.calls, ["recursion_loop"])

    def test_a_fallback_may_name_a_registered_engine(self) -> None:
        limits = {"on_limit": "fallback", "fallback": [_route("recursion_loop")]}
        with self.assertRaises(c.RouteError) as caught:
            validate_limits(limits)
        self.assertEqual(caught.exception.code, c.ENGINE_UNKNOWN)
        self.assertEqual(validate_limits(limits, engines=StandInTable("recursion_loop"))["fallback"][0]["engine"],
                         "recursion_loop")
        clock = FakeClock()
        service = RouteService({"claude_code": EchoAdapter(), "recursion_loop": EchoAdapter()},
                               ConnectionBreaker(clock), clock, limits=limits)  # the service default, too
        self.assertEqual(service.handle(request(engine="claude_code"))["status"], "ok")

    def test_a_registered_name_that_is_not_an_engine_name_fails_composition(self) -> None:
        clock = FakeClock()
        for bad in ("Recursion", "recursion-loop", "", "x" * 33, 7):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                RouteService({bad: EchoAdapter()}, ConnectionBreaker(clock), clock)


@unittest.skipUnless(importlib.util.find_spec("recursion_core.routes.engines"),
                     "S3's engine table (routes/engines) is not on this base: SPINE's seams pull request brings it")
class SpineTableTest(unittest.TestCase):
    def test_against_spines_engine_table(self) -> None:
        from recursion_core.routes.engines import EngineTable

        table = EngineTable()
        self.assertEqual(c.engine_names(table), BASE_FIVE)
        table.register("recursion_loop", lambda *args, **kwargs: EchoAdapter())
        table.freeze()
        self.assertIsNone(_engine_code("recursion_loop", table))
        self.assertEqual(_engine_code("grok", table), c.ENGINE_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
