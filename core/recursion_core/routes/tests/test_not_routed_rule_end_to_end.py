"""A routing rule naming pi ends in ``engine_not_routed`` with nothing started.

The shared routing fixture still names pi in a rule (``search-to-pi``), a login and a route, and it still
loads. A search task picks that rule. The routed decision is turned into the route request a run start builds
from it and routes.json (the start path that does this in the daemon is a later slice, so the test stands in
for it) and is handed to the route service with the profile adapters, whose launcher records every spawn.
The answer is ``engine_not_routed`` and the launcher was never called.

The fixture puts pi on an xAI key, which the launch checks would refuse for pi on their own; a second test
moves that one login and route, in a copy, onto an OpenAI key that ``check_route`` accepts for pi, so the
route check is the only thing between the rule and a spawn. The same path with the fixture's rule naming
opencode starts one process, so an empty launcher is the refusal's doing.
"""

from __future__ import annotations

import copy
import os
import pathlib
import tempfile
import types
import unittest
from unittest import mock

from recursion_core.routes import contract as c
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.profile_route import profile_adapters
from recursion_core.routes.service import RouteService
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher
from recursion_core.routing import router
from recursion_core.routing.tests.fixtures import CONFIG, ROUTES, config, detection
from recursion_core.subscription_routes import WrapperInstall, check_route, measure_package_pin, policy

NOT_POSIX = os.name == "nt" or not hasattr(os, "geteuid")
OPERATOR_HOME = "/home/operator"
NOT_ROUTED = ("error", "engine_not_routed", "detected, not routed in this release")
PROFILE = {"provider": "openai", "auth": "api_key", "credential_env": "OPENAI_API_KEY"}


def pi_on_an_openai_key() -> tuple[dict, dict]:
    """Copies of the shared fixture with pi's login and route on an OpenAI key; the fixture is untouched."""
    raw, routes = copy.deepcopy(CONFIG), copy.deepcopy(ROUTES)
    for login in raw["logins"]:
        if login["engines"] == ["pi"]:
            login.update(vendor="openai", connection_id="openai-api")
    routes["pi"]["route"].update(model="openai/model-e", profile=dict(PROFILE), connection={
        "kind": "api_key", "id": "openai-api", "credential_env": "OPENAI_API_KEY"})
    return raw, routes


@unittest.skipIf(NOT_POSIX, "POSIX-only: the route home owner check needs os.geteuid")
class RuleNamingPiEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "route-home"
        self.home.mkdir(mode=0o700)
        # The route check also refuses a route home inside the passwd home of the user running the test. The
        # fixture's operator home is fake, so the passwd lookup answers that one for the length of each test:
        # a scratch folder below the real home (a TMPDIR there) is then no operator home.
        stand_in = types.SimpleNamespace(getpwuid=lambda uid: types.SimpleNamespace(pw_dir=OPERATOR_HOME))
        patcher = mock.patch.object(policy, "pwd", stand_in)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = FakeClock()
        self.launcher = FakeLauncher(lambda argv, env: [{"ok": True}], self.clock)
        self.parent = {"PATH": "/usr/bin", "HOME": OPERATOR_HOME}
        drive = lambda process, req, deadline_at, events: c.result(req["request_id"], None, text="driven")
        argv = lambda req: [req["route"]["wrapper"].launcher_path if req["route"].get("wrapper") is not None
                            else req["route"]["engine"]]
        adapters = profile_adapters(self.launcher, self.clock, lambda cid, name: "test-key-value", self.parent,
                                    argv=argv, driver=drive, operator_home=OPERATOR_HOME)
        self.service = RouteService(adapters, ConnectionBreaker(self.clock), self.clock)
        self.cfg = config()   # the shared fixture, pi and all, loads as before
        self.detected = detection({"claude_code", "codex", "opencode", "pi"})

    def pin(self) -> WrapperInstall:
        pkg = self.root / "pin"
        pkg.mkdir()
        os.chmod(pkg, 0o700)   # the process umask does not decide the mode of the package
        for name, text in (("launcher.js", "launcher\n"), ("lib.js", "lib\n")):
            pkg.joinpath(name).write_text(text, encoding="ascii")
            os.chmod(pkg / name, 0o600)
        launcher = str(pkg / "launcher.js")
        return WrapperInstall(launcher, str(pkg), measure_package_pin(launcher, str(pkg)))

    def start(self, decision: dict, wrapper: WrapperInstall | None, routes: dict = ROUTES) -> dict:
        """The route request a run start builds from a routed decision and the engine's routes.json entry."""
        route = copy.deepcopy(routes[decision["engine"]]["route"])
        self.assertEqual(route["connection"]["id"], decision["connection"]["id"])
        route.update(model=decision["model"], home=str(self.home))
        if wrapper is not None:
            route["wrapper"] = wrapper
        return self.service.handle({"contract": c.CONTRACT_VERSION, "request_id": "r1", "tenant": "t1",
                                    "prompt": "hello", "deadline_s": 60.0, "route": route})

    def test_a_rule_naming_pi_ends_in_engine_not_routed_with_nothing_started(self) -> None:
        decision = router.decide(self.cfg, self.detected, {"task_kind": "search"})
        self.assertEqual((decision["ok"], decision["reason"], decision["rule"], decision["engine"]),
                         (True, "config", "search-to-pi", "pi"))
        res = self.start(decision, self.pin())
        self.assertEqual((res["status"], res["code"], res["detail"]), NOT_ROUTED)
        self.assertEqual((self.launcher.calls, self.launcher.processes), ([], []))   # no process was spawned

    def test_with_pi_on_a_key_check_route_accepts_the_rule_still_starts_nothing(self) -> None:
        raw, routes = pi_on_an_openai_key()
        decision = router.decide(config(raw, routes), self.detected, {"task_kind": "search"})
        self.assertEqual((decision["ok"], decision["rule"], decision["engine"], decision["connection"]),
                         (True, "search-to-pi", "pi", {"kind": "api_key", "id": "openai-api"}))
        wrapper = self.pin()
        child = {"PATH": "/usr/bin", "HOME": str(self.home), "OPENAI_API_KEY": "test-key-value"}
        check_route("pi", dict(PROFILE), child, route_home=str(self.home), parent_env=self.parent,
                    operator_home=OPERATOR_HOME, wrapper=wrapper)   # accepted: no refusal raised
        res = self.start(decision, wrapper, routes)
        self.assertEqual((res["status"], res["code"], res["detail"]), NOT_ROUTED)
        self.assertEqual((self.launcher.calls, self.launcher.processes), ([], []))   # no process was spawned

    def test_the_same_path_with_the_rule_naming_opencode_starts_one_process(self) -> None:
        decision = router.decide(self.cfg, self.detected, {"task_kind": "chat", "tags": ["cheap"]})
        self.assertEqual((decision["ok"], decision["rule"], decision["engine"]), (True, "cheap-to-opencode", "opencode"))
        res = self.start(decision, None)
        self.assertEqual((res["status"], res.get("code")), ("ok", None))
        self.assertEqual(len(self.launcher.calls), 1)


class RuleNamingPiUnderUmask002Test(RuleNamingPiEndToEndTest):
    """The same cases with a group-writable default: the package the test pins is still private."""

    def setUp(self) -> None:
        self.addCleanup(os.umask, os.umask(0o002))
        super().setUp()


class RuleNamingPiUnderUmask077Test(RuleNamingPiEndToEndTest):
    def setUp(self) -> None:
        self.addCleanup(os.umask, os.umask(0o077))
        super().setUp()


class RuleNamingPiScratchInThePasswdHomeTest(RuleNamingPiEndToEndTest):
    """The same cases with the scratch folder (TMPDIR) inside the passwd home of the user running them.

    The test makes its own stand-in for that home and points the passwd lookup at it before the fixture runs;
    the machine's real home is never written.
    """

    def setUp(self) -> None:
        outer = tempfile.TemporaryDirectory()
        self.addCleanup(outer.cleanup)
        passwd_home = os.path.join(os.path.realpath(outer.name), "passwd-home")
        scratch = os.path.join(passwd_home, "tmp")
        os.makedirs(scratch, mode=0o700)
        stand_in = types.SimpleNamespace(getpwuid=lambda uid: types.SimpleNamespace(pw_dir=passwd_home))
        for patcher in (mock.patch.object(policy, "pwd", stand_in), mock.patch.object(tempfile, "tempdir", scratch)):
            patcher.start()
            self.addCleanup(patcher.stop)
        super().setUp()
        # The layout is the one under test: the route home lies inside the passwd home of this stand-in.
        self.assertTrue(os.path.realpath(self.home).startswith(passwd_home + os.sep), self.home)


if __name__ == "__main__":
    unittest.main()
