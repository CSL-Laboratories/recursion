"""2026-11-08 own-loop routes: Copilot on the user's own copilot CLI sign-in, Nous via Hermes's loopback proxy.

Fakes only: no engine, login store, provider, DNS or network is touched.
"""

from __future__ import annotations

import threading
import time
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

    def test_unrelated_provider_credentials_do_not_reach_the_copilot_child(self) -> None:
        # Devin r4505395174: the stored Copilot sign-in is the only credential this run uses.
        env = {**BASE, "ANTHROPIC_API_KEY": FAKE, "OPENAI_API_KEY": FAKE, "XAI_API_KEY": FAKE,
               "CLAUDE_CODE_OAUTH_TOKEN": FAKE}
        d = check_copilot_own_loop(copilot(), env, has_copilot_login=lambda: True)
        self.assertEqual(dict(d.child_env), BASE)
        self.assertEqual(set(d.removed_env), set(env) - set(BASE))
        self.assertNotIn(FAKE, repr(d.child_env))

    def test_copilot_model_is_checked_and_carried(self) -> None:
        d = check_copilot_own_loop(copilot(model="gpt-5"), BASE, has_copilot_login=lambda: True)
        self.assertEqual(d.model, "gpt-5")
        self.assertEqual(check_copilot_own_loop(copilot(), BASE, has_copilot_login=lambda: True).model, "")
        for bad in ("", "has space", 7, "x" * 129, "line\nbreak"):
            with self.subTest(model=bad):
                self.refused("model_malformed", copilot(model=bad))

    def test_no_stored_login_refuses_and_does_not_fall_back_to_gh(self) -> None:
        self.refused("no_stored_copilot_login", copilot(), {**BASE, "GH_TOKEN": FAKE}, probe=lambda: False)

    def test_missing_or_failing_probe_refuses(self) -> None:
        self.refused("copilot_login_unknown", copilot(), probe=None)

        def boom() -> bool:
            raise OSError("keychain locked")
        self.refused("copilot_login_unknown", copilot(), probe=boom)
        self.refused("no_stored_copilot_login", copilot(), probe=lambda: "yes")

    def test_login_probe_systemexit_refuses(self) -> None:
        # SystemExit is a BaseException, so `except Exception` lets a probe sys.exit escape.
        def exits() -> bool:
            raise SystemExit("do-not-leak-this")

        try:
            check_copilot_own_loop(copilot(), BASE, has_copilot_login=exits)
        except OwnLoopRouteRefused as exc:
            self.assertEqual(exc.cause, "copilot_login_unknown")
            self.assertEqual(exc.detail, "SystemExit")
            self.assertNotIn("do-not-leak-this", str(exc))
        except SystemExit:
            self.fail("SystemExit escaped the login probe")
        else:
            self.fail("probe SystemExit was ignored")

    def test_login_probe_deadline_refuses(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def hang() -> bool:
            release.wait(3)
            return True

        started = time.monotonic()
        with self.assertRaises(OwnLoopRouteRefused) as caught:
            check_copilot_own_loop(copilot(), BASE, has_copilot_login=hang, probe_deadline=0.2)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(caught.exception.cause, "copilot_login_unknown")
        self.assertIn("deadline", caught.exception.detail)
        kept = check_copilot_own_loop(copilot(), BASE, has_copilot_login=lambda: True, probe_deadline=0.5)
        self.assertEqual(kept.method, "copilot_cli_sign_in")

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
                d = check_nous_own_loop(nous(base_url=url, adapters=["nous"]), BASE, current_user="u",
                                        interactive_users=["u"])
                self.assertEqual((d.base_url, d.adapters, d.warnings), (url, ("nous",), ()))
        d = check_nous_own_loop(nous(), BASE)
        self.assertEqual(d.adapters, ("nous",))

    def test_child_env_carries_no_provider_credential(self) -> None:
        env = {**BASE, "NOUS_API_KEY": FAKE, "XAI_API_KEY": FAKE, "OPENAI_API_KEY": FAKE,
               "ANTHROPIC_AUTH_TOKEN": FAKE, "HERMES_HOME": "/h", "OPENAI_BASE_URL": "http://evil"}
        d = check_nous_own_loop(nous(), env)
        self.assertEqual(dict(d.child_env), BASE)
        self.assertNotIn(FAKE, repr(d.child_env))

    def test_credential_aliases_and_git_config_bearer_never_reach_the_child(self) -> None:
        # Names the *_API_KEY / *_TOKEN matcher misses, and git's config-injection channel.
        secret = "bearer-not-a-real-token"
        names = (
            "GITHUB_PASSWORD", "github_pat", "AWS_SECRET_ACCESS_KEY", "OPENAI_API_BASE",
            "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_VALUE",
            "GIT_CONFIG_PARAMETERS",
        )
        env = {**BASE, **{name: secret for name in names}}
        for check in (
            lambda: check_copilot_own_loop(copilot(), env, has_copilot_login=lambda: True),
            lambda: check_nous_own_loop(nous(), env),
        ):
            with self.subTest(check=check):
                decision = check()
                self.assertEqual(dict(decision.child_env), BASE)
                self.assertNotIn(secret, repr(decision.child_env))
                self.assertTrue(set(names) <= set(decision.removed_env))

    def test_proxy_settings_never_reach_the_nous_child(self) -> None:
        # Devin r4505395127: a proxy-aware client would send loopback-bound traffic to the proxy host.
        proxies = {"HTTP_PROXY": "http://proxy.example:3128", "https_proxy": "http://proxy.example:3128",
                   "ALL_PROXY": "socks5://proxy.example:1080", "all_proxy": "socks5://p:1"}
        d = check_nous_own_loop(nous(), {**BASE, **proxies})
        self.assertEqual(dict(d.child_env), BASE)
        self.assertEqual(set(d.removed_env), set(proxies))

    def test_nous_model_must_stay_on_the_nous_adapter(self) -> None:
        # Devin r4505395083: the model is checked and carried, not left to the caller.
        self.refused("adapter_must_stay_off", nous(model="xai/grok-3"))
        self.refused("nous_adapter_only", nous(model="openai/gpt-5"))
        self.refused("model_malformed", nous(model="has space"))
        for model in ("Hermes-4-405B", "nous/Hermes-4-70B"):
            with self.subTest(model=model):
                self.assertEqual(check_nous_own_loop(nous(model=model), BASE).model, model)
        self.assertEqual(check_nous_own_loop(nous(), BASE).model, "")

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

    def test_unknown_host_users_warn(self) -> None:
        # red-team #321 F3: "not checked" must not read as "nobody else is here".
        d = check_nous_own_loop(nous(), BASE)
        self.assertEqual(len(d.warnings), 1)
        self.assertTrue(d.warnings[0].startswith("host users unknown"))
        d = check_nous_own_loop(nous(), BASE, current_user="u", interactive_users=[])
        self.assertEqual(d.warnings, ())


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_address_and_never_raises(self) -> None:
        self.assertEqual(nous_proxy_doctor("http://127.0.0.1:8645/v1", current_user="u", interactive_users=["u"]),
                         {"ok": True, "address": "http://127.0.0.1:8645/v1", "cause": "", "warnings": ()})
        bad = nous_proxy_doctor("http://10.1.2.3:8645")
        self.assertEqual((bad["ok"], bad["address"], bad["cause"]), (False, "http://10.1.2.3:8645", "proxy_not_loopback"))
        self.assertEqual(nous_proxy_doctor(None)["cause"], "proxy_address_missing")
        shared = nous_proxy_doctor("http://127.0.0.1:1", current_user="u", interactive_users=["u", "eve"])
        self.assertTrue(shared["ok"])
        self.assertIn("eve", shared["warnings"][0])

    def test_doctor_does_not_echo_proxy_userinfo(self) -> None:
        secret = "userinfo-secret"
        leaked = (
            f"http://user:{secret}@127.0.0.1:8645/v1",
            f"http://user:{secret}@[::1]:8645/v1",
            f"ftp://user:{secret}@127.0.0.1/x",
        )
        for url in leaked:
            with self.subTest(url=url.split("@", 1)[0]):
                report = nous_proxy_doctor(url)
                self.assertFalse(report["ok"])
                self.assertNotIn(secret, str(report))
                self.assertNotIn("@", report["address"].split("://", 1)[-1])
                try:
                    check_nous_own_loop(nous(base_url=url), BASE)
                except OwnLoopRouteRefused as exc:
                    self.assertNotIn(secret, str(exc))
                    self.assertNotIn(secret, exc.detail)
                    if url.startswith("ftp://"):
                        self.assertEqual(exc.cause, report["cause"])
                        self.assertEqual("recursion_holds_no_token", exc.cause)
                else:
                    self.fail("credential URL was accepted")

    def test_doctor_warns_when_host_users_unknown(self) -> None:
        for url in ("http://127.0.0.1:8645/v1", "http://10.1.2.3:8645"):
            got = nous_proxy_doctor(url)
            self.assertEqual(len(got["warnings"]), 1)
            self.assertTrue(got["warnings"][0].startswith("host users unknown"))


if __name__ == "__main__":
    unittest.main()
