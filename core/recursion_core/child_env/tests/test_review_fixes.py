"""Review of bfa68d87: '..' ownership, route host in NO_PROXY, NO_PROXY CIDR, bracket host. Each fails on bfa68d87."""

from __future__ import annotations

import unittest
from pathlib import Path

import recursion_core.child_env.policy as policy
from recursion_core.child_env.policy import ChildRoute, EnvRefused, build_child_env
from recursion_core.child_env.tests import cells


def _home() -> str:
    return cells.owned_tmpdir("csl785-proposal-")


class DotdotWritableDirectoryTests(unittest.TestCase):
    """A '..' component must not drop a writable directory out of the ownership check."""

    def test_dotdot_through_a_world_writable_directory_is_refused(self) -> None:
        root = Path(_home())
        safe = root / "safe"
        safe.mkdir(mode=0o755)
        safe.chmod(0o755)
        pub = safe / "pub"
        pub.mkdir()
        pub.chmod(0o777)
        hop = pub / "hop"
        hop.mkdir(mode=0o755)
        hop.chmod(0o755)
        ca = safe / "ca.pem"
        ca.write_text("operator-ca\n", encoding="ascii")
        ca.chmod(0o644)
        priv = safe / "priv"
        priv.mkdir(mode=0o700)
        priv.chmod(0o700)
        dotted = f"{pub}/hop/../../ca.pem"
        home_dotted = f"{pub}/hop/../../priv"
        self.assertFalse(policy._owned_chain(str(pub)))
        self.assertEqual("value_chain_unsafe", policy._unsafe_file(dotted))
        self.assertFalse(policy._private_dir(home_dotted))
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("claude_code", home=home_dotted, auth_mode="subscription", parent={"PATH": "/usr/bin"})
        self.assertEqual(("HOME", "home_unsafe"), (caught.exception.name, caught.exception.cause))
        # Same shape the delta's hosted cell refuses when named directly, admitted when spelled with '..'.
        hosted = root / "hosted"
        local_bin = hosted / "usr" / "local" / "bin"
        usr_bin = hosted / "usr" / "bin"
        local_bin.mkdir(parents=True)
        usr_bin.mkdir(parents=True)
        (local_bin / "hop").mkdir(mode=0o755)
        local_bin.chmod(0o777)
        script = usr_bin / "bash"
        script.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        script.chmod(0o755)
        direct = str(local_bin)
        via_dotdot = f"{local_bin}/hop/../../usr/bin"
        self.assertEqual("value_chain_unsafe", policy._unsafe_dir(direct))
        self.assertEqual("value_chain_unsafe", policy._unsafe_path(via_dotdot))

    def test_dotdot_through_an_owned_directory_stays_allowed(self) -> None:
        root = Path(_home())
        op = root / "op"
        op.mkdir(mode=0o700)
        op.chmod(0o700)
        (op / "d").mkdir(mode=0o755)
        ca = op / "ca.pem"
        ca.write_text("operator-ca\n", encoding="ascii")
        ca.chmod(0o644)
        self.assertIsNone(policy._unsafe_file(f"{op}/d/../ca.pem"))


class RouteHostNoProxyTests(unittest.TestCase):
    """A NO_PROXY entry must not exempt the route base host while a proxy is set."""

    def _refused(self, **kwargs: object) -> EnvRefused:
        home = _home()
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("claude_code", home=home, parent={"PATH": "/usr/bin", "HTTPS_PROXY": "http://127.0.0.1:3128",
                                                              "NO_PROXY": "gateway.invalid"}, **kwargs)
        return caught.exception

    def test_route_base_host_cannot_be_in_no_proxy(self) -> None:
        gateway = "https://gateway.invalid/api"
        cases = (
            ("api_key", ChildRoute(api_key="fake-user-key", base_url=gateway)),
            ("cloud_provider", ChildRoute(base_url=gateway, provider_switches=("CLAUDE_CODE_USE_BEDROCK",))),
            ("subscription", ChildRoute(base_url=gateway, trusted_base_urls=("https://gateway.invalid",))),
            ("subscription", ChildRoute(base_url=gateway, full_access_acknowledged=True)),
        )
        for mode, route in cases:
            with self.subTest(mode=mode, full=route.full_access_acknowledged):
                refused = self._refused(auth_mode=mode, route=route)
                self.assertEqual(("NO_PROXY", "no_proxy_engine_host"), (refused.name, refused.cause))

    def test_a_different_no_proxy_host_still_launches(self) -> None:
        home = _home()
        env = build_child_env(
            "claude_code",
            home=home,
            auth_mode="api_key",
            route=ChildRoute(api_key="fake-user-key", base_url="https://gateway.invalid/api"),
            parent={"PATH": "/usr/bin", "HTTPS_PROXY": "http://127.0.0.1:3128", "NO_PROXY": "localhost"},
        )
        self.assertEqual("https://gateway.invalid/api", env["ANTHROPIC_BASE_URL"])
        self.assertEqual("localhost", env["NO_PROXY"])


class NoProxyCidrTests(unittest.TestCase):
    """A CIDR in NO_PROXY is curl's bypass for a literal-IP host; it refuses when it covers the route host."""

    def _build(self, base_url: str, no_proxy: str) -> dict[str, str]:
        return build_child_env(
            "claude_code",
            home=_home(),
            auth_mode="api_key",
            route=ChildRoute(api_key="fake-user-key", base_url=base_url),
            parent={"PATH": "/usr/bin", "HTTPS_PROXY": "http://127.0.0.1:3128", "NO_PROXY": no_proxy},
        )

    def test_cidr_covering_a_literal_ip_route_host_is_refused(self) -> None:
        for base, no_proxy in (("https://10.1.2.3/v1", "10.0.0.0/8"), ("https://10.1.2.3/v1", "0.0.0.0/0"),
                               ("https://[fd00::5]/v1", "fd00::/8")):
            with self.subTest(base=base, no_proxy=no_proxy):
                with self.assertRaises(EnvRefused) as caught:
                    self._build(base, no_proxy)
                self.assertEqual(("NO_PROXY", "no_proxy_cidr"), (caught.exception.name, caught.exception.cause))

    def test_cidr_that_covers_no_route_or_engine_host_still_launches(self) -> None:
        env = self._build("https://gateway.invalid/api", "10.0.0.0/8,localhost,127.0.0.1")
        self.assertEqual("10.0.0.0/8,localhost,127.0.0.1", env["NO_PROXY"])
        env = self._build("https://10.1.2.3/v1", "192.168.0.0/16")
        self.assertEqual("192.168.0.0/16", env["NO_PROXY"])


class BracketIpv6Tests(unittest.TestCase):
    """A bracket host that is not an IPv6 address is a malformed base URL, not an origin."""

    def test_triple_colon_is_malformed(self) -> None:
        home = _home()
        with self.assertRaises(EnvRefused) as caught:
            build_child_env(
                "claude_code",
                home=home,
                auth_mode="api_key",
                route=ChildRoute(api_key="fake-user-key", base_url="https://[:::]"),
                parent={"PATH": "/usr/bin"},
            )
        self.assertEqual(("ANTHROPIC_BASE_URL", "base_url_malformed"), (caught.exception.name, caught.exception.cause))

    def test_a_real_ipv6_literal_still_launches_on_an_api_key_route(self) -> None:
        home = _home()
        env = build_child_env(
            "claude_code",
            home=home,
            auth_mode="api_key",
            route=ChildRoute(api_key="fake-user-key", base_url="https://[::1]:8443/v1"),
            parent={"PATH": "/usr/bin"},
        )
        self.assertEqual("https://[::1]:8443/v1", env["ANTHROPIC_BASE_URL"])


if __name__ == "__main__":
    unittest.main()
