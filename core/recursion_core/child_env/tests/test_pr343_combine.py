"""Refusals ported onto the CSL-839 NO_PROXY design (PR 343).

Checked locally against CPython 3.12 urllib.request and pip's vendored requests 2.31.0
(``python3 -c 'import pip._vendor.requests.utils'``). No network. Secrets here are fake.
"""

from __future__ import annotations

import os
import shutil
import stat
import unittest
from unittest import mock

from recursion_core.child_env import EnvRefused, build_child_env, spawn
from recursion_core.child_env.policy import ChildRoute
from recursion_core.child_env.tests import cells

PROXY = "http://127.0.0.1:3128"
KEY = "fake-user-entered-key-not-a-credential"
OAUTH = "CLAUDE_CODE_OAUTH_TOKEN"
_PROXY = {"HTTPS_PROXY": PROXY}


def _home(test: unittest.TestCase) -> str:
    path = cells.owned_tmpdir("csl839-combine-")
    test.addCleanup(shutil.rmtree, path, True)
    return path


def _build(home: str, parent: dict, route: ChildRoute | None = None, auth_mode: str = "api_key",
           overrides: dict | None = None):
    if route is None:
        route = ChildRoute() if auth_mode == "subscription" else ChildRoute(api_key=KEY)
    return build_child_env(
        "claude_code", home=home, auth_mode=auth_mode, route=route,
        parent={"PATH": "/usr/bin", **parent}, overrides=overrides,
    )


class MalformedValueTests(unittest.TestCase):
    """A null or a non-string must be EnvRefused before stat or Popen."""

    def test_nul_in_home_refuses_before_any_stat(self) -> None:
        def boom(*_args, **_kwargs):
            raise AssertionError("stat")

        with mock.patch("os.lstat", side_effect=boom), mock.patch("os.stat", side_effect=boom):
            with self.assertRaises(EnvRefused) as caught:
                build_child_env("claude_code", home="/tmp/csl839-\0home", auth_mode="subscription", parent={})
        self.assertEqual(("HOME", "home_unsafe"), (caught.exception.name, caught.exception.cause))
        self.assertNotIn("csl839", str(caught.exception))

    def test_nul_in_kept_values_passthrough_and_no_proxy_refuses(self) -> None:
        home = _home(self)
        cases = (
            ("LANG", "C\0", "subscription", None),
            ("NO_PROXY", "local\0host", "subscription", None),
            (OAUTH, "fake\0token", "subscription", None),
            ("SSL_CERT_FILE", "/tmp/cert\0.pem", "subscription", None),
        )
        called = []
        for name, value, mode, route in cases:
            with self.subTest(name=name):
                with self.assertRaises(EnvRefused) as caught:
                    env = _build(home, {name: value}, route, mode)
                    spawn(["/bin/true"], env, popen=lambda *args, **kwargs: called.append(args))
                self.assertEqual((name, "value_malformed"), (caught.exception.name, caught.exception.cause))
                self.assertNotIn("\0", str(caught.exception))
                self.assertNotIn("fake", str(caught.exception))
                self.assertNotIn("local", str(caught.exception))
        self.assertEqual([], called)

    def test_non_string_parent_name_or_value_refuses(self) -> None:
        home = _home(self)
        cases = (
            {"PATH": "/usr/bin", "LANG": 1},
            {"PATH": "/usr/bin", "LANG": b"en"},
            {"PATH": "/usr/bin", "NOT_A_NAME": 1},
            {"PATH": "/usr/bin", 1: "x"},
        )
        for parent in cases:
            with self.subTest(parent=sorted(parent, key=repr)):
                with self.assertRaises(EnvRefused) as caught:
                    build_child_env("claude_code", home=home, auth_mode="subscription", parent=parent)
                self.assertEqual("value_malformed", caught.exception.cause)
                if all(isinstance(name, str) for name in parent):
                    bad = next(name for name, value in parent.items() if not isinstance(value, str))
                    self.assertEqual(bad, caught.exception.name)
                else:
                    self.assertIsNone(caught.exception.name)
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("claude_code", home=home, auth_mode="subscription", parent=["PATH", "/usr/bin"])
        self.assertEqual((None, "value_malformed"), (caught.exception.name, caught.exception.cause))

    def test_nul_or_non_string_argv_and_cwd_never_reach_popen(self) -> None:
        home = _home(self)
        env = _build(home, {}, auth_mode="subscription", route=ChildRoute())
        called = []

        def popen(*args, **kwargs):
            called.append(args)
            raise AssertionError("popen")

        with self.assertRaises(EnvRefused) as caught:
            spawn(["/bin/true\0x"], env, popen=popen)
        self.assertEqual((None, "argv_malformed"), (caught.exception.name, caught.exception.cause))
        with self.assertRaises(EnvRefused) as caught:
            spawn(["/bin/true", 1], env, popen=popen)
        self.assertEqual((None, "argv_malformed"), (caught.exception.name, caught.exception.cause))
        with self.assertRaises(EnvRefused) as caught:
            spawn(["/bin/true"], env, cwd="/tmp/csl\0", popen=popen)
        self.assertEqual((None, "value_malformed"), (caught.exception.name, caught.exception.cause))
        with self.assertRaises(EnvRefused) as caught:
            spawn(["/bin/true"], env, cwd=1, popen=popen)
        self.assertEqual((None, "value_malformed"), (caught.exception.name, caught.exception.cause))
        self.assertEqual([], called)


class RouteTempTests(unittest.TestCase):
    def test_unsafe_cached_route_temp_is_removed_and_remade(self) -> None:
        from recursion_core.child_env import policy

        policy._TMP.clear()
        self.addCleanup(policy._TMP.clear)
        home = _home(self)
        parent = {"PATH": "/usr/bin", "TMPDIR": "/tmp"}
        first = _build(home, parent, auth_mode="subscription", route=ChildRoute())
        old = first["TMPDIR"]
        self.assertTrue(stat.S_ISDIR(os.lstat(old).st_mode))
        os.chmod(old, 0o777)
        self.addCleanup(lambda: os.path.isdir(old) and (os.chmod(old, 0o700) or os.rmdir(old)))
        second = _build(home, parent, auth_mode="subscription", route=ChildRoute())
        self.assertNotEqual(old, second["TMPDIR"])
        self.assertFalse(os.path.exists(old))
        mode = os.lstat(second["TMPDIR"]).st_mode
        self.assertTrue(stat.S_ISDIR(mode))
        self.assertEqual(0, mode & 0o077)
        self.assertEqual(os.geteuid(), os.lstat(second["TMPDIR"]).st_uid)


class NoProxyClientTests(unittest.TestCase):
    """urllib's last-colon split and requests' endswith, on top of PR 343's causes."""

    V6 = "https://[2001:db8::1]:8443/v1"

    def _refused(self, entry: str, base_url: str | None = None, auth_mode: str = "api_key") -> EnvRefused:
        route = ChildRoute(api_key=KEY, base_url=base_url) if base_url else ChildRoute()
        if auth_mode == "subscription":
            route = ChildRoute()
        with self.assertRaises(EnvRefused) as caught:
            _build(_home(self), {**_PROXY, "NO_PROXY": entry}, route, auth_mode)
        text = str(caught.exception) + (caught.exception.detail or "")
        # Position-only: a short entry such as ":" is also the separator in "NAME: cause".
        shown = entry.replace(" ", "").strip().lstrip(".").lower()
        if len(shown) > 3:
            self.assertNotIn(shown, text.lower())
        self.assertNotIn(PROXY, text)
        self.assertNotIn("10.1.2.3", text)
        return caught.exception

    def test_urllib_last_colon_split_refuses_a_non_loopback_host(self) -> None:
        # proxy_bypass_environment('2001:db8::1', {'no': '2001:db8:'}) is True; ':' is the hostonly of '::1'.
        refused = self._refused("2001:db8:", self.V6)
        self.assertEqual(("NO_PROXY", "no_proxy_route_host"), (refused.name, refused.cause))
        # Loopback stays exempt: ':' bypasses ::1, and #343 does not refuse a loopback route host.
        home = _home(self)
        env = _build(home, {**_PROXY, "NO_PROXY": ":"}, ChildRoute(api_key=KEY, base_url="http://[::1]:4000"))
        self.assertEqual(":", env["NO_PROXY"])
        # ::2 is not loopback. urllib's hostonly of '::2' is ':', so the entry refuses.
        refused = self._refused(":", "http://[::2]:4000")
        self.assertEqual("no_proxy_route_host", refused.cause)

    def test_leading_dot_ipv6_entries_and_host_with_port(self) -> None:
        for entry in (".2001:db8::1", "..2001:db8::1", ".2001:DB8::1", ".2001:db8::1:8443", "2001:db8::1:8443",
                      ".[2001:db8::1]", "db8::1", "::1"):
            with self.subTest(entry=entry):
                refused = self._refused(entry, self.V6)
                self.assertEqual(("NO_PROXY", "no_proxy_route_host"), (refused.name, refused.cause))
        # A different port is not requests' host_with_port, so it may launch.
        home = _home(self)
        for entry in (".2001:db8::1:9", "2001:db8::1:9", "2001:db8::1:0"):
            with self.subTest(kept=entry):
                env = _build(home, {**_PROXY, "NO_PROXY": entry}, ChildRoute(api_key=KEY, base_url=self.V6))
                self.assertEqual(entry, env["NO_PROXY"])

    def test_requests_suffix_without_a_dot_boundary(self) -> None:
        # requests 2.31 should_bypass_proxies: hostname.endswith(entry) after deleting spaces.
        for entry in ("pi.anthropic.com", "p i.anthropic.com"):
            with self.subTest(entry=entry):
                refused = self._refused(entry, auth_mode="subscription")
                self.assertEqual(("NO_PROXY", "no_proxy_engine_host"), (refused.name, refused.cause))
        # '443' bypasses an explicit :443 on the engine host and on a route URL.
        refused = self._refused("443", auth_mode="subscription")
        self.assertEqual("no_proxy_engine_host", refused.cause)
        refused = self._refused("443", "https://gateway.invalid:443/v1")
        self.assertEqual("no_proxy_engine_host", refused.cause)
        # The route's own host:port, which is not a suffix of an engine host.
        refused = self._refused("invalid:443", "https://gateway.invalid:443/v1")
        self.assertEqual("no_proxy_route_host", refused.cause)
        # No explicit port in the URL: requests does not append :443, and this entry is not an engine suffix.
        home = _home(self)
        env = _build(home, {**_PROXY, "NO_PROXY": "llm.gateway.example:443"},
                     ChildRoute(api_key=KEY, base_url="https://gateway.example/api"))
        self.assertEqual("llm.gateway.example:443", env["NO_PROXY"])

    def test_inet_aton_shorthand_stays_unparseable(self) -> None:
        # requests treats '10.0/8' as 10.0.0.0/8. #343 already refuses it as unparseable whenever a proxy is set.
        refused = self._refused("10.0/8", "http://10.1.2.3:4000")
        self.assertEqual(("NO_PROXY", "no_proxy_unparseable", 1), (refused.name, refused.cause, refused.position))
        self.assertNotIn("10.0", str(refused) + (refused.detail or ""))
        home = _home(self)
        env = _build(home, {"NO_PROXY": "10.0/8"}, ChildRoute(api_key=KEY, base_url="http://10.1.2.3:4000"))
        self.assertEqual("10.0/8", env["NO_PROXY"])


class ProxyClearedTests(unittest.TestCase):
    def test_empty_lowercase_proxy_clears_uppercase(self) -> None:
        home = _home(self)
        pairs = (
            ("https_proxy", "HTTPS_PROXY", PROXY),
            ("http_proxy", "HTTP_PROXY", PROXY),
            ("all_proxy", "ALL_PROXY", "socks5://127.0.0.1:1080"),
        )
        for lower, upper, value in pairs:
            with self.subTest(lower=lower):
                with self.assertRaises(EnvRefused) as caught:
                    _build(home, {upper: value, lower: ""})
                self.assertEqual((lower, "proxy_cleared"), (caught.exception.name, caught.exception.cause))
                self.assertIsNone(caught.exception.detail)
                self.assertNotIn(value, str(caught.exception))
                self.assertNotIn("127.0.0.1", str(caught.exception))
            with self.subTest(lower=lower, via="override"):
                with self.assertRaises(EnvRefused) as caught:
                    _build(home, {upper: value}, overrides={lower: ""})
                self.assertEqual((lower, "proxy_cleared"), (caught.exception.name, caught.exception.cause))
        # The other way around still launches, and so do both empty.
        env = _build(home, {"https_proxy": PROXY, "HTTPS_PROXY": ""})
        self.assertEqual(PROXY, env["https_proxy"])
        self.assertEqual("", env["HTTPS_PROXY"])
        env = _build(home, {"https_proxy": "", "HTTPS_PROXY": ""}, auth_mode="subscription", route=ChildRoute())
        self.assertEqual("", env["https_proxy"])
        self.assertEqual("", env["HTTPS_PROXY"])


if __name__ == "__main__":
    unittest.main()
