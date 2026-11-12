"""Reproducible defects in the CSL-785 child environment (PR 320).

Each test names a behavior the allowlist claims and that the code at af393599 got wrong.
Secrets here are fake. Nothing is spawned and no network is used.
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
_PROXY = {"HTTPS_PROXY": PROXY}


def _home(test: unittest.TestCase) -> str:
    path = cells.owned_tmpdir("csl785-defect-")
    test.addCleanup(shutil.rmtree, path, True)
    return path


def _refused(test: unittest.TestCase, parent: dict, route: ChildRoute | None = None,
             auth_mode: str = "api_key") -> EnvRefused:
    with test.assertRaises(EnvRefused) as caught:
        build_child_env(
            "claude_code",
            home=_home(test),
            auth_mode=auth_mode,
            route=route if route is not None else ChildRoute(api_key=KEY),
            parent={"PATH": "/usr/bin", **parent},
        )
    text = str(caught.exception)
    test.assertNotIn(KEY, text)
    test.assertNotIn(PROXY, text)
    return caught.exception


class NoProxyBypassTests(unittest.TestCase):
    """A proxy is useless if NO_PROXY exempts the host that receives the credential.

    The matcher claims the union of the curl, requests, urllib, hyper-util and undici readings.
    urllib.request.proxy_bypass_environment and requests' space-deletion and CIDR readings
    exempt hosts the code at af393599 still launches with.
    """

    def test_route_host_is_not_exempted_while_a_proxy_is_set(self) -> None:
        route = ChildRoute(api_key=KEY, base_url="https://gateway.invalid/v1")
        for entry in ("gateway.invalid", "invalid", "gate way.invalid", ".gateway.invalid"):
            with self.subTest(entry=entry):
                refused = _refused(self, {**_PROXY, "NO_PROXY": entry}, route)
                self.assertEqual(("NO_PROXY", "no_proxy_engine_host"), (refused.name, refused.cause))

    def test_ipv6_route_host_entries_urllib_treats_as_bypass_are_refused(self) -> None:
        route = ChildRoute(api_key=KEY, base_url="http://[::1]:4000")
        # urllib _splitport('::1') is (':', '1'), so a NO_PROXY of ':' bypasses ::1.
        for entry in ("::1", "[::1]", "[::1]:4000", "0:0:0:0:0:0:0:1", ":"):
            with self.subTest(entry=entry):
                refused = _refused(self, {**_PROXY, "NO_PROXY": entry}, route)
                self.assertEqual(("NO_PROXY", "no_proxy_engine_host"), (refused.name, refused.cause))
        longer = ChildRoute(api_key=KEY, base_url="http://[2001:db8::1]:443")
        refused = _refused(self, {**_PROXY, "NO_PROXY": "2001:db8:"}, longer)
        self.assertEqual("no_proxy_engine_host", refused.cause)

    def test_cidr_and_address_suffix_that_cover_a_route_ip_are_refused(self) -> None:
        route = ChildRoute(api_key=KEY, base_url="http://10.1.2.3:4000")
        # '3' is urllib: 10.1.2.3 endswith '.3'. '10.0/8' is requests' inet_aton shorthand.
        for entry in ("10.1.2.3", "10.0.0.0/8", "10.0/8", "3", "::ffff:10.1.2.3"):
            with self.subTest(entry=entry):
                refused = _refused(self, {**_PROXY, "NO_PROXY": entry}, route)
                self.assertEqual(("NO_PROXY", "no_proxy_engine_host"), (refused.name, refused.cause))
        mapped = ChildRoute(api_key=KEY, base_url="http://[::ffff:10.1.2.3]:4000")
        refused = _refused(self, {**_PROXY, "NO_PROXY": "10.0.0.0/8"}, mapped)
        self.assertEqual("no_proxy_engine_host", refused.cause)

    def test_requests_deletes_spaces_before_matching_an_engine_host(self) -> None:
        refused = _refused(
            self, {**_PROXY, "NO_PROXY": "a pi.anthropic.com"}, ChildRoute(), auth_mode="subscription",
        )
        self.assertEqual(("NO_PROXY", "no_proxy_engine_host"), (refused.name, refused.cause))

    def test_readings_that_do_not_exempt_a_protected_host_still_launch(self) -> None:
        home = _home(self)
        gateway = ChildRoute(api_key=KEY, base_url="https://gateway.invalid/v1")
        cases = (
            ({**_PROXY, "NO_PROXY": "notanthropic.com,anthropic.com.example"}, ChildRoute(), "subscription"),
            ({**_PROXY, "NO_PROXY": "0.0.0.0/0,::/0"}, gateway, "api_key"),
            ({**_PROXY, "NO_PROXY": "10.0.0.0/8"}, gateway, "api_key"),
            ({**_PROXY, "NO_PROXY": "3"}, ChildRoute(), "subscription"),
            ({**_PROXY, "NO_PROXY": "localhost,127.0.0.1"}, gateway, "api_key"),
            ({"NO_PROXY": "gateway.invalid"}, gateway, "api_key"),  # no proxy: the bypass cannot happen
        )
        for parent, route, mode in cases:
            with self.subTest(no_proxy=parent.get("NO_PROXY"), mode=mode):
                env = build_child_env(
                    "claude_code", home=home, auth_mode=mode, route=route,
                    parent={"PATH": "/usr/bin", **parent},
                )
                self.assertEqual(parent["NO_PROXY"], env["NO_PROXY"])
                self.assertNotIn(KEY, repr(env))


class MalformedInputTests(unittest.TestCase):
    """Values Popen cannot accept must be EnvRefused, not ValueError or TypeError."""

    def test_nul_in_home_is_a_refusal(self) -> None:
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("claude_code", home="/tmp/csl785-\0home", auth_mode="subscription", parent={})
        self.assertEqual(("HOME", "home_unsafe"), (caught.exception.name, caught.exception.cause))
        self.assertNotIn("csl785", str(caught.exception))

    def test_nul_in_the_oauth_token_is_refused_and_not_launched(self) -> None:
        home = _home(self)
        called = []
        with self.assertRaises(EnvRefused) as caught:
            env = build_child_env(
                "claude_code", home=home, auth_mode="subscription",
                parent={"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": "fake\0token"},
            )
            spawn(["/bin/true"], env, popen=lambda *args, **kwargs: called.append(args))
        self.assertEqual(("CLAUDE_CODE_OAUTH_TOKEN", "value_malformed"), (caught.exception.name, caught.exception.cause))
        self.assertNotIn("fake", str(caught.exception))
        self.assertEqual([], called)

    def test_nul_in_no_proxy_is_refused_even_without_a_proxy(self) -> None:
        with self.assertRaises(EnvRefused) as caught:
            build_child_env(
                "claude_code", home=_home(self), auth_mode="subscription",
                parent={"PATH": "/usr/bin", "NO_PROXY": "local\0host"},
            )
        self.assertEqual(("NO_PROXY", "value_malformed"), (caught.exception.name, caught.exception.cause))

    def test_non_string_parent_is_a_refusal(self) -> None:
        home = _home(self)
        cases = (
            {"PATH": "/usr/bin", "LANG": 1},
            {"PATH": "/usr/bin", "LANG": b"en"},
        )
        for parent in cases:
            with self.subTest(parent=parent), self.assertRaises(EnvRefused) as caught:
                build_child_env("claude_code", home=home, auth_mode="subscription", parent=parent)
            self.assertEqual(("LANG", "value_malformed"), (caught.exception.name, caught.exception.cause))
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("claude_code", home=home, auth_mode="subscription", parent=["PATH", "/usr/bin"])
        self.assertEqual((None, "value_malformed"), (caught.exception.name, caught.exception.cause))

    def test_nul_in_argv_or_cwd_never_reaches_popen(self) -> None:
        home = _home(self)
        env = build_child_env("claude_code", home=home, auth_mode="subscription", parent={"PATH": "/usr/bin"})
        called = []

        def popen(*args, **kwargs):
            called.append(args)
            return mock.Mock()

        with self.assertRaises(EnvRefused) as caught:
            spawn(["/bin/true\0x"], env, popen=popen)
        self.assertEqual((None, "argv_malformed"), (caught.exception.name, caught.exception.cause))
        with self.assertRaises(EnvRefused) as caught:
            spawn(["/bin/true"], env, cwd="/tmp/csl\0", popen=popen)
        self.assertEqual((None, "value_malformed"), (caught.exception.name, caught.exception.cause))
        self.assertEqual([], called)


class RouteTempTests(unittest.TestCase):
    def test_an_unsafe_cached_route_temp_is_not_left_behind(self) -> None:
        from recursion_core.child_env import policy

        policy._TMP.clear()
        self.addCleanup(policy._TMP.clear)
        home = _home(self)
        parent = {"PATH": "/usr/bin", "TMPDIR": "/tmp"}
        first = build_child_env("claude_code", home=home, auth_mode="subscription", parent=parent)
        old = first["TMPDIR"]
        self.assertTrue(old.startswith("/tmp/recursion-child-tmp-") or old.startswith(os.path.realpath("/tmp")))
        os.chmod(old, 0o777)
        self.addCleanup(lambda: os.path.isdir(old) and (os.chmod(old, 0o700) or os.rmdir(old)))
        second = build_child_env("claude_code", home=home, auth_mode="subscription", parent=parent)
        self.assertNotEqual(old, second["TMPDIR"])
        self.assertFalse(os.path.exists(old))
        mode = os.lstat(second["TMPDIR"]).st_mode
        self.assertTrue(stat.S_ISDIR(mode))
        self.assertEqual(0, mode & 0o077)


if __name__ == "__main__":
    unittest.main()
