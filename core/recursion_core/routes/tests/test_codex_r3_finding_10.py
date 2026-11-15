"""R3 finding 10, the Codex half: an exhausted vendor bucket fences the connection (lane DRIVER-CX).

The first test is Astra's red test from the R3 review (Codex GPT-6 Astra, ``fix-10.patch``, reviewed base
1abd2f77), copied unchanged apart from the class name and the host pin; the rest are this lane's. The
brief's step 1, under Lucas's D0, with the option the brief recommends and Lucas has not yet chosen (lane
questions Q2): "an unknown bucket fences the connection until a bucket-to-model mapping is recorded".
Before, a bucket other than ``codex`` was taken as a breaker model key, so the route model that had just
hit it was admitted again. The profile half of finding 10 is ``routes/profile_route.py``, not this lane's.
Fakes only: no real ``codex``, no credential, no network.
"""

import unittest
from unittest.mock import patch

from recursion_core.routes import CodexAdapter, ConnectionBreaker, RouteService
from recursion_core.routes import codex as codex_module
from recursion_core.routes import contract as c
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, codex_script, request

# These tests drive the adapter through a fake launcher only; #376's R3-7 refuses native Windows at the
# shared adapter boundary, so the host test is pinned (the form the two reviews of #376 proposed, R4).
on_any_host = patch("recursion_core.routes.base.platform_supported", lambda: True, create=True)


@on_any_host
class AstraCodexBucketBindingTest(unittest.TestCase):
    def test_exhausted_bucket_fences_the_model_that_just_hit_it(self):
        clock = FakeClock()
        note = {"method":"account/rateLimits/updated","params":{"rateLimits":{"limitId":"codex_gpt5_weekly","primary":{"usedPercent":100,"resetsAt":clock.now+900}}}}
        turn = {"status":"failed","error":{"codexErrorInfo":"usageLimitExceeded"}}
        launcher = FakeLauncher([codex_script(notes=[note],turn=turn),codex_script()],clock)
        service = RouteService({"codex":CodexAdapter(launcher,clock,lambda *_:"",{},wall_clock=clock)},ConnectionBreaker(clock),clock)
        self.assertEqual(service.handle(request(engine="codex",model="gpt-5"))["code"], c.PLAN_LIMIT)
        self.assertEqual(service.handle(request(rid="r2",engine="codex",model="gpt-5"))["code"], c.BREAKER_OPEN)
        self.assertEqual(len(launcher.calls), 1)


def _exhausted(bucket, clock, reset_in=900):
    return {"limitId": bucket, "primary": {"usedPercent": 100, "resetsAt": clock.now + reset_in}}


@on_any_host
class ConnectionFenceTest(unittest.TestCase):
    """This lane's tests: which scope each exhausted bucket fences, at the read and mid-turn."""

    def setUp(self):
        self.clock = FakeClock()

    def service(self, scripts):
        clock = self.clock
        self.launcher = FakeLauncher(list(scripts), clock)
        self.breaker = ConnectionBreaker(clock)
        return RouteService({"codex": CodexAdapter(self.launcher, clock, lambda *_: "", {}, wall_clock=clock)},
                            self.breaker, clock)

    def test_an_unknown_bucket_at_the_read_fences_every_model_of_the_connection(self):
        clock = self.clock
        service = self.service([codex_script(limits=_exhausted("codex_gpt5_weekly", clock)), codex_script()])
        first = service.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual((first["code"], first["limit"]["scope"], first["limit"]["model"]),
                         (c.PLAN_LIMIT, "connection", None))
        self.assertEqual([m.get("method") for m in self.launcher.processes[0].sent],
                         ["initialize", "initialized", "account/rateLimits/read"])
        for rid, model in (("r2", "gpt-5"), ("r3", "o3")):
            with self.subTest(model=model):
                self.assertEqual(service.handle(request(rid=rid, engine="codex", model=model))["code"], c.BREAKER_OPEN)
        self.assertEqual(self.breaker.state("subscription:login-A"), "open")
        self.assertEqual(len(self.launcher.calls), 1)

    def test_an_unknown_bucket_mid_turn_fences_the_connection_with_its_reset(self):
        clock = self.clock
        note = {"method": "account/rateLimits/updated", "params": {"rateLimits": _exhausted("gpt-5-bucket", clock)}}
        turn = {"id": "tu-1", "status": "failed", "error": {"message": "x", "codexErrorInfo": "usageLimitExceeded"}}
        service = self.service([codex_script(notes=[note], turn=turn)])
        res = service.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual((res["code"], res["limit"]["scope"]), (c.PLAN_LIMIT, "connection"))
        self.assertEqual(res["limit"]["reset_at"], clock.now + 900)
        self.assertEqual(self.breaker.state("subscription:login-A"), "open")
        usage = [e["data"] for e in res["events"] if e["source"] == "codex.account/rateLimits/updated"]
        self.assertEqual(usage[0]["limitId"], "gpt-5-bucket")  # the bucket is still recorded, as a usage event

    def test_the_default_bucket_still_fences_the_connection(self):
        clock = self.clock
        service = self.service([codex_script(limits=_exhausted("codex", clock))])
        res = service.handle(request(engine="codex", model="gpt-5"))
        self.assertEqual((res["code"], res["limit"]["scope"]), (c.PLAN_LIMIT, "connection"))

    def test_a_recorded_bucket_fences_its_model_only(self):
        clock = self.clock
        with patch.dict(codex_module.BUCKET_MODELS, {"codex_gpt5_weekly": "gpt-5"}):
            service = self.service([codex_script(limits=_exhausted("codex_gpt5_weekly", clock)), codex_script()])
            first = service.handle(request(engine="codex", model="gpt-5"))
            self.assertEqual((first["code"], first["limit"]["scope"], first["limit"]["model"]),
                             (c.PLAN_LIMIT, "model", "gpt-5"))
            self.assertEqual(service.handle(request(rid="r2", engine="codex", model="gpt-5"))["code"],
                             c.BREAKER_OPEN)
            other = service.handle(request(rid="r3", engine="codex", model="o3"))
            self.assertEqual(other["status"], "ok")
        self.assertEqual(self.breaker.state("subscription:login-A"), "closed")

    def test_no_bucket_is_recorded_yet(self):
        self.assertEqual(codex_module.BUCKET_MODELS, {})

    def test_a_window_below_100_fences_nothing(self):
        clock = self.clock
        limits = {"limitId": "codex_gpt5_weekly", "primary": {"usedPercent": 99.9, "resetsAt": clock.now + 900}}
        service = self.service([codex_script(limits=limits)])
        self.assertEqual(service.handle(request(engine="codex", model="gpt-5"))["status"], "ok")
        self.assertEqual(self.breaker.state("subscription:login-A"), "closed")


if __name__ == "__main__":
    unittest.main()
