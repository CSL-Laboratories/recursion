"""Nov 8 own-loop routes: Copilot on the user's own copilot CLI sign-in, Nous via Hermes's loopback proxy.

Fakes only: no engine, login store, provider, DNS or network is touched.
"""

from __future__ import annotations

import unittest

from recursion_core.subscription_routes import (
    COPILOT_STRIP_ENV, OwnLoopRouteRefused, check_copilot_own_loop, check_nous_own_loop, nous_proxy_doctor,
)

FAKE = "fake-not-a-real-token"
BASE = {"PATH": "/usr/bin:/bin", "HOME": "/home/u", "LANG": "C.UTF-8"}


def copilot(**extra: object) -> dict:
    return {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in", **extra}


def nous(**extra: object) -> dict:
    return {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy",
            "base_url": "http://127.0.0.1:8645/v1", **extra}


class CopilotTests(unittest.TestCase):
    def refused(self, cause: str, route: object, env: dict | None = None, probe=lambda: True) -> None:
        with self.assertRaises(OwnLoopRouteRefused) as ctx:
            check_copilot_own_loop(route, BASE if env is None else env, has_copilot_login=probe)
        self.assertEqual(ctx.exception.cause, cause)
        self.assertNotIn(FAKE, str(ctx.exception))

    def test_stored_login_runs_with_every_token_variable_removed(self) -> None:
        env = {**BASE, **{n: FAKE for n in COPILOT_STRIP_ENV}, "GH_HOST": "github.com", "GH_CONFIG_DIR": "/x"}
        d = check_copilot_own_loop(copilot(model="gpt-5"), env, has_copilot_login=lambda: True)
        self.assertEqual(dict(d.child_env), BASE)
        self.assertEqual(set(d.removed_env), set(COPILOT_STRIP_ENV) | {"GH_HOST", "GH_CONFIG_DIR"})
        self.assertIn("COPILOT_API_URL", d.removed_env)
        self.assertNotIn(FAKE, repr(d))

    def test_no_stored_login_refuses_and_does_not_fall_back_to_gh(self) -> None:
        self.refused("no_stored_copilot_login", copilot(), {**BASE, "GH_TOKEN": FAKE}, probe=lambda: False)

    def test_missing_or_failing_probe_refuses(self) -> None:
        self.refused("copilot_login_unknown", copilot(), probe=None)

        def boom() -> bool:
            raise OSError("keychain locked")
        self.refused("copilot_login_unknown", copilot(), probe=boom)
        self.refused("no_stored_copilot_login", copilot(), probe=lambda: "yes")

    def test_route_may_not_pass_a_token(self) -> None:
        for name in ("token", "github_token", "COPILOT_GITHUB_TOKEN", "api_key", "bearer"):
            with self.subTest(name=name):
                self.refused("recursion_holds_no_token", copilot(**{name: FAKE}))

    def test_other_methods_and_fields_refuse(self) -> None:
        for method in ("gh_cli", "gh", "oauth_app", "github_oauth_app", "", None):
            with self.subTest(method=method):
                self.refused("method_not_allowed", copilot(method=method))
        self.refused("route_unknown_field", copilot(fallback="gh"))
        self.refused("auth_not_subscription", copilot(auth="api_key"))
        self.refused("engine_mismatch", copilot(engine="codex"))
        self.refused("route_malformed", ["copilot"])

    def test_spelling_only_normalisation(self) -> None:
        d = check_copilot_own_loop(copilot(method="Copilot CLI sign-in"), BASE, has_copilot_login=lambda: True)
        self.assertEqual(d.method, "copilot_cli_sign_in")


class NousTests(unittest.TestCase):
    def refused(self, cause: str, route: object, env: dict | None = None) -> None:
        with self.assertRaises(OwnLoopRouteRefused) as ctx:
            check_nous_own_loop(route, BASE if env is None else env)
        self.assertEqual(ctx.exception.cause, cause)
        self.assertNotIn(FAKE, str(ctx.exception))

    def test_loopback_proxy_with_nous_adapter_only_runs(self) -> None:
        for url in ("http://127.0.0.1:8645/v1", "http://127.8.9.1:1/", "http://[::1]:8645", "https://127.0.0.1"):
            with self.subTest(url=url):
                d = check_nous_own_loop(nous(base_url=url, adapters=["nous"]), BASE)
                self.assertEqual((d.base_url, d.adapters, d.warnings), (url, ("nous",), ()))
        d = check_nous_own_loop(nous(), BASE)
        self.assertEqual(d.adapters, ("nous",))

    def test_child_env_carries_no_provider_credential(self) -> None:
        env = {**BASE, "NOUS_API_KEY": FAKE, "XAI_API_KEY": FAKE, "OPENAI_API_KEY": FAKE,
               "ANTHROPIC_AUTH_TOKEN": FAKE, "HERMES_HOME": "/h", "OPENAI_BASE_URL": "http://evil"}
        d = check_nous_own_loop(nous(), env)
        self.assertEqual(dict(d.child_env), BASE)
        self.assertNotIn(FAKE, repr(d.child_env))

    def test_non_loopback_and_names_refuse(self) -> None:
        for url in ("http://10.0.0.5:8645", "http://192.168.1.2", "http://0.0.0.0:8645", "http://[::]:1",
                    "https://portal.nousresearch.com/v1", "http://localhost:8645", "http://127.0.0.1.nip.io"):
            with self.subTest(url=url):
                self.refused("proxy_not_loopback", nous(base_url=url))

    def test_ipv4_mapped_loopback_passes_and_mapped_remote_refuses(self) -> None:
        check_nous_own_loop(nous(base_url="http://[::ffff:127.0.0.1]:8645"), BASE)
        self.refused("proxy_not_loopback", nous(base_url="http://[::ffff:10.0.0.1]:8645"))

    def test_malformed_addresses_refuse(self) -> None:
        self.refused("proxy_address_missing", nous(base_url=""))
        self.refused("proxy_address_missing", nous(base_url=None))
        for url in ("ftp://127.0.0.1", "127.0.0.1:8645", "http://127.0.0.1:99999", "unix:///tmp/s"):
            with self.subTest(url=url):
                self.refused("proxy_address_malformed", nous(base_url=url))
        self.refused("recursion_holds_no_token", nous(base_url=f"http://u:{FAKE}@127.0.0.1:8645"))

    def test_xai_adapter_stays_off_and_only_nous_runs(self) -> None:
        self.refused("adapter_must_stay_off", nous(adapters=["nous", "xai"]))
        self.refused("adapter_must_stay_off", nous(adapters=["XAI"]))
        self.refused("nous_adapter_only", nous(adapters=["openai"]))
        self.refused("nous_adapter_only", nous(adapters=[]))
        self.refused("nous_adapter_only", nous(adapters=["nous", "nous"]))
        self.refused("adapters_malformed", nous(adapters="nous"))

    def test_route_holds_no_nous_token(self) -> None:
        for name in ("api_key", "token", "bearer", "authorization", "portal_credential"):
            with self.subTest(name=name):
                self.refused("recursion_holds_no_token", nous(**{name: FAKE}))
        self.refused("method_not_allowed", nous(method="portal_api_key"))
        self.refused("route_unknown_field", nous(host="127.0.0.1"))

    def test_shared_host_warns(self) -> None:
        d = check_nous_own_loop(nous(), BASE, current_user="u", interactive_users=["u", "bob", "", "alice"])
        self.assertEqual(len(d.warnings), 1)
        self.assertIn("alice, bob", d.warnings[0])


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_address_and_never_raises(self) -> None:
        self.assertEqual(nous_proxy_doctor("http://127.0.0.1:8645/v1"),
                         {"ok": True, "address": "http://127.0.0.1:8645/v1", "cause": "", "warnings": ()})
        bad = nous_proxy_doctor("http://10.1.2.3:8645")
        self.assertEqual((bad["ok"], bad["address"], bad["cause"]), (False, "http://10.1.2.3:8645", "proxy_not_loopback"))
        self.assertEqual(nous_proxy_doctor(None)["cause"], "proxy_address_missing")
        shared = nous_proxy_doctor("http://127.0.0.1:1", current_user="u", interactive_users=["u", "eve"])
        self.assertTrue(shared["ok"])
        self.assertIn("eve", shared["warnings"][0])


if __name__ == "__main__":
    unittest.main()
