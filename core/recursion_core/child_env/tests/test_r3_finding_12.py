"""R3 regression: dummy data only; no provider, network or operator credential use."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

class PrivateHome(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="r3-regression-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.root.chmod(0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.operator = self.root / "operator"
        self.operator.mkdir(mode=0o700)

from recursion_core.child_env import EnvRefused, build_child_env
from recursion_core.subscription_routes import SubscriptionRouteRefused, check_route
from recursion_core.routes import ConnectionBreaker, RouteService, profile_adapters
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, request

@unittest.skipIf(os.name == "nt", "POSIX input validation")
class MalformedEnvironmentTest(PrivateHome):
    def test_all_carried_values_reject_nul_before_launch(self):
        for name in ("NO_PROXY","no_proxy","CLAUDE_CODE_OAUTH_TOKEN"):
            for proxy in ({},{"HTTPS_PROXY":"http://127.0.0.1:3128"}):
                with self.subTest(name=name,proxy=bool(proxy)), self.assertRaises(EnvRefused):
                    build_child_env("claude_code",home=str(self.home),auth_mode="subscription",parent={name:"FAKE\0value",**proxy})

    def test_nul_home_is_a_typed_refusal(self):
        with self.assertRaises(EnvRefused):
            build_child_env("codex",home=str(self.home)+"\0",auth_mode="subscription",parent={})
        with self.assertRaises(SubscriptionRouteRefused):
            check_route("opencode",{"provider":"openai","auth":"api_key","credential_env":"OPENAI_API_KEY"},
                {"HOME":str(self.home)+"\0","OPENAI_API_KEY":"FAKE-key"},route_home=str(self.home)+"\0")

    def test_route_service_returns_a_contract_error_for_nul_home(self):
        clock = FakeClock()
        launcher = FakeLauncher()
        adapters = profile_adapters(launcher,clock,lambda *_:"FAKE-key",{"PATH":"/usr/bin"},argv=lambda _:["opencode"],driver=lambda *_:{})
        result = RouteService(adapters,ConnectionBreaker(clock),clock).handle(request(engine="opencode",kind="api_key",credential_env="OPENAI_API_KEY",
            home=str(self.home)+"\0",profile={"provider":"openai","auth":"api_key","credential_env":"OPENAI_API_KEY"}))
        self.assertEqual(result["status"],"error")
        self.assertEqual(launcher.calls,[])

    def test_valid_opaque_login_value_is_preserved(self):
        env = build_child_env("claude_code",home=str(self.home),auth_mode="subscription",parent={"CLAUDE_CODE_OAUTH_TOKEN":"FAKE-opaque-value"})
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"],"FAKE-opaque-value")

    def test_nul_proxy_refusal_keeps_position_and_never_echoes_entry(self):
        for proxy in ({}, {"HTTPS_PROXY":"http://127.0.0.1:3128"}):
            with self.subTest(proxy=bool(proxy)), self.assertRaises(EnvRefused) as caught:
                build_child_env("codex",home=str(self.home),auth_mode="subscription",
                    parent={"NO_PROXY":"localhost,FAKE-marker\0",**proxy})
            self.assertEqual((caught.exception.name,caught.exception.cause,caught.exception.position),
                             ("NO_PROXY","no_proxy_unparseable",2))
            self.assertNotIn("FAKE-marker",str(caught.exception))

    def test_existing_slash_refusal_takes_precedence_over_nul_serialization(self):
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("codex",home=str(self.home),auth_mode="subscription",
                parent={"HTTPS_PROXY":"http://127.0.0.1:3128","no_proxy":"localhost,x/\0"})
        self.assertEqual((caught.exception.cause,caught.exception.position),("no_proxy_unparseable",2))
        self.assertIn("a path, a second '/'",caught.exception.detail)
