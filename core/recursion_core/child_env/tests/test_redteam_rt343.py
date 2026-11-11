"""Red-team tests for PR #343 (CSL-839) at 8682b37a: the NO_PROXY route-host, wildcard and unparseable rules.

Run from the repository root:
``PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/child_env/tests -p 'test_redteam_rt343.py' -v``.

Tests only; no engine, network or real credential is used. ``policy._no_proxy_bypass`` says its matcher is "the
union of curl, requests, hyper-util, undici and Codex matchers", and ``_no_proxy_route_bypass`` uses the same
matcher for the route host. The ``*MatcherGap*`` classes give entries that a matcher shipped with a runtime the
child may use (Node 24.22.1's bundled undici ``EnvHttpProxyAgent`` and built-in ``NODE_USE_ENV_PROXY`` matcher,
Go 1.27.3 ``net/http.ProxyFromEnvironment``, requests 2.34.2 ``should_bypass_proxies``) reads as the route host or
as "every host", checked offline against that shipped code, while ``build_child_env`` launches. Each expects a
NO_PROXY refusal of any of the four causes (the fix may pick the cause) that shows no entry text.

Devin's partial rt343 file, brought in with the ``*`` matcher fix. The ``*MatcherGap*`` tests are the MEDIUM-2 items:
the head launches every one of their cases, and whether to refuse them is the owner's call, so each is an expected
failure with ``MEDIUM_2`` as its reason, not a skip. ``StarPatternTimeTests`` (MEDIUM-1) and
``HardeningThatHoldsTests`` pass.

No parent here sets PATH, so the host's /usr layout decides nothing; only the private home needs an owned chain.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import sys
import time
import traceback
import unittest
from pathlib import Path

from recursion_core.child_env import ENGINE_PROFILES, ChildRoute, EnvRefused, build_child_env, policy
from recursion_core.child_env.tests import cells

PROXY = {"HTTPS_PROXY": "http://127.0.0.1:3128"}
GATEWAY = "https://gateway.example/api"
V4_GATEWAY = "http://10.1.2.3:4000"
V6_GATEWAY = "https://[2001:db8::1]:8443/v1"
USER_KEY = "sk-test-FAKE"
NO_PROXY_CAUSES = ("no_proxy_wildcard", "no_proxy_engine_host", "no_proxy_route_host", "no_proxy_unparseable")
WILDCARD_CAUSES = ("no_proxy_wildcard", "no_proxy_unparseable")
MEDIUM_2 = "MEDIUM-2 client-normalization parity: waiting on owner call (Devin rt343)"


def _waiting(reason: str):
    """``unittest.expectedFailure`` with its reason kept on the test (``expected_failure_reason``)."""

    def mark(test):
        test.expected_failure_reason = reason
        return unittest.expectedFailure(test)

    return mark


def _home(test: unittest.TestCase) -> str:
    home = cells.owned_tmpdir("rt343-home-")
    test.addCleanup(shutil.rmtree, home, True)
    return home


def _build(home: str, parent: dict, base_url: str | None = GATEWAY, engine: str = "claude_code"):
    route = ChildRoute(api_key=USER_KEY, base_url=base_url)
    return build_child_env(engine, home=home, auth_mode="api_key", route=route, parent=parent)


def _outcome(home: str, parent: dict, base_url: str | None = GATEWAY, engine: str = "claude_code"):
    try:
        _build(home, parent, base_url, engine)
    except EnvRefused as refused:
        return refused.name, refused.cause, refused.position
    return "launched"


class _Base(unittest.TestCase):
    def assert_refused_without_entry_text(self, home: str, parent: dict, causes: tuple[str, ...], base_url: str | None = GATEWAY,
                                          engine: str = "claude_code") -> EnvRefused:
        with self.assertRaises(EnvRefused, msg=f"launched: {ascii(parent)} base_url={base_url} engine={engine}") as caught:
            _build(home, parent, base_url, engine)
        refused = caught.exception
        self.assertIn(refused.name, policy.NO_PROXY_NAMES, ascii(parent))
        self.assertIn(refused.cause, causes, ascii(parent))
        texts = [str(refused), repr(refused), *map(str, refused.args), str(refused.detail),
                 "".join(traceback.format_exception(refused))]
        for name in policy.NO_PROXY_NAMES:
            for entry in policy._no_proxy_entries(parent.get(name, "")):
                for fragment in {entry, entry.strip("*.\ufeff "), policy._no_proxy_name(entry)}:
                    if len(fragment) >= 4:
                        for text in texts:
                            self.assertNotIn(fragment, text.lower(), "a refusal shows entry text")
        return refused


@unittest.skipIf(os.name == "nt", "POSIX rules")
class NodeUndiciStarDotMatcherGapTests(_Base):
    """Node 24.22.1 bundled undici ``EnvHttpProxyAgent#parseNoProxy`` strips a leading ``*.`` or ``.``.

    ``hostname: (parsed ? parsed[1] : entry).replace(/^\\*?\\./, "").toLowerCase()``, then ``hostname === entry.hostname``:
    so ``*.gateway.example`` (and ``*.gateway.example:443``) skips the proxy for ``gateway.example`` itself. The
    checker compiles ``*.gateway.example`` to ``.*\\.gateway\\.example\\Z``, which needs a label before the dot.
    """

    @_waiting(MEDIUM_2)
    def test_a_star_dot_entry_naming_the_route_host_itself_is_refused(self) -> None:
        home = _home(self)
        for base_url, value in ((GATEWAY, "*.gateway.example"), (GATEWAY, "*.gateway.example:443"), (GATEWAY, "*.GATEWAY.Example"),
                                ("https://llm.gateway.example:8443", "*.llm.gateway.example"),
                                ("https://llm.gateway.example:8443", "localhost,*.llm.gateway.example:8443")):
            for name in policy.NO_PROXY_NAMES:
                with self.subTest(base_url=base_url, value=value, name=name):
                    self.assert_refused_without_entry_text(home, {**PROXY, name: value}, NO_PROXY_CAUSES, base_url)

    @_waiting(MEDIUM_2)
    def test_a_star_dot_entry_naming_an_engine_host_itself_is_refused(self) -> None:
        """Claude Code engine hosts: the same undici rule. Pre-existing on master a70e9e40 (same ``*`` pattern code)."""

        home = _home(self)
        for host in ENGINE_PROFILES["claude_code"].hosts:
            with self.subTest(host=host):
                self.assert_refused_without_entry_text(home, {**PROXY, "NO_PROXY": f"*.{host}"}, NO_PROXY_CAUSES, None)


@unittest.skipIf(os.name == "nt", "POSIX rules")
class NodeFeffSeparatorMatcherGapTests(_Base):
    """U+FEFF is whitespace to JavaScript (``/\\s/`` and ``String.prototype.trim``) and not to Python's ``\\s``.

    It is the only such code point. undici splits NO_PROXY on ``/[,\\s]/``; Node's built-in env-proxy matcher does
    ``split(',').map(e => e.trim().toLowerCase())`` and returns "bypass" for an entry equal to ``*``. So
    ``\\ufeffgateway.example`` or ``x\\ufeffgateway.example`` skip the proxy for the gateway in undici, and
    ``\\ufeff*`` (a BOM before ``*``) skips it for every host in Node's built-in matcher.
    """

    @_waiting(MEDIUM_2)
    def test_a_feff_split_entry_naming_the_route_host_is_refused(self) -> None:
        home = _home(self)
        for value in ("\ufeffgateway.example", "x\ufeffgateway.example", "gateway.example\ufeff", "localhost,\ufeffgateway.example"):
            with self.subTest(value=ascii(value)):
                self.assert_refused_without_entry_text(home, {**PROXY, "NO_PROXY": value}, NO_PROXY_CAUSES)

    @_waiting(MEDIUM_2)
    def test_a_feff_star_is_a_wildcard_on_any_route(self) -> None:
        home = _home(self)
        for value in ("\ufeff*", "*\ufeff", "localhost,\ufeff*"):
            for engine, base_url in (("claude_code", None), ("claude_code", GATEWAY), ("claude_code", "http://127.0.0.1:4000"),
                                     ("codex", None)):
                with self.subTest(value=ascii(value), engine=engine, base_url=base_url):
                    self.assert_refused_without_entry_text(home, {**PROXY, "no_proxy": value}, WILDCARD_CAUSES, base_url, engine)

    @_waiting(MEDIUM_2)
    def test_a_feff_split_entry_naming_an_engine_host_is_refused(self) -> None:
        """Claude Code engine hosts: the same undici split. Pre-existing on master a70e9e40 (same ``[,\\s]+`` split)."""

        home = _home(self)
        for value in ("x\ufeffapi.anthropic.com", "\ufeffclaude.ai"):
            with self.subTest(value=ascii(value)):
                self.assert_refused_without_entry_text(home, {**PROXY, "NO_PROXY": value}, NO_PROXY_CAUSES, None)


@unittest.skipIf(os.name == "nt", "POSIX rules")
class RequestsSpaceRemovalMatcherGapTests(_Base):
    """requests 2.34.2 ``should_bypass_proxies``: ``no_proxy.replace(" ", "").split(",")``.

    A space inside an entry is deleted, so ``g ateway.example`` is ``gateway.example``, ``1 0.1.2.3`` is
    ``10.1.2.3`` and ``0.0.0.0/1,1 28.0.0.0/1`` is a cover of every IPv4 address (requests honours ``/1``). The
    checker splits at the space instead, and no fragment matches.
    """

    @_waiting(MEDIUM_2)
    def test_a_space_split_entry_naming_the_route_host_is_refused(self) -> None:
        home = _home(self)
        for base_url, value in ((GATEWAY, "g ateway.example"), (GATEWAY, "localhost,gatew ay.example"),
                                (V4_GATEWAY, "1 0.1.2.3"), (V4_GATEWAY, "1 0.0.0.0/8")):
            with self.subTest(base_url=base_url, value=value):
                self.assert_refused_without_entry_text(home, {**PROXY, "NO_PROXY": value}, NO_PROXY_CAUSES, base_url)

    @_waiting(MEDIUM_2)
    def test_a_space_split_union_that_covers_every_ipv4_address_is_a_wildcard(self) -> None:
        """requests reads one name only (``no_proxy`` first), so each value here is a cover on its own."""

        home = _home(self)
        for parent in ({"NO_PROXY": "0.0.0.0/1,1 28.0.0.0/1"}, {"no_proxy": "0.0.0.0/1 ,12 8.0.0.0/1"}):
            for base_url in (None, "http://127.0.0.1:4000"):
                with self.subTest(parent=parent, base_url=base_url):
                    self.assert_refused_without_entry_text(home, {**PROXY, **parent}, WILDCARD_CAUSES, base_url)


@unittest.skipIf(os.name == "nt", "POSIX rules")
class GoIdnaMatcherGapTests(_Base):
    """Go 1.27.3 ``net/http.ProxyFromEnvironment`` maps each NO_PROXY name through ``idna.Lookup.ToASCII``.

    UTS 46 maps fullwidth letters and dots, U+3002 and U+FF0E to ASCII and drops U+00AD, so these entries skip the
    proxy for ``gateway.example`` in Go (the PR cites Go as a matcher it follows for CIDR). Python ``lower()`` keeps them
    non-ASCII and the checker matches nothing.
    """

    @_waiting(MEDIUM_2)
    def test_an_idna_mapped_entry_naming_the_route_host_is_refused(self) -> None:
        home = _home(self)
        for value in ("\uff47\uff41\uff54\uff45\uff57\uff41\uff59.example", "gateway\u3002example", "gateway\uff0eexample",
                      "GATEWAY\uff0eEXAMPLE", "gate\u00adway.example"):
            with self.subTest(value=ascii(value)):
                self.assert_refused_without_entry_text(home, {**PROXY, "NO_PROXY": value}, NO_PROXY_CAUSES)


@unittest.skipIf(os.name == "nt", "POSIX rules")
class NodeBuiltinRangeMatcherGapTests(_Base):
    """Node 24.22.1 built-in env-proxy matcher (``internal/http`` ``ProxyConfig.shouldUseProxy``) reads IPv4 ranges.

    ``192.168.1.0-192.168.1.255`` style: ``10.0.0.0-10.255.255.255`` skips the proxy for ``10.1.2.3`` and
    ``0.0.0.0-255.255.255.255`` for every IPv4 host, like ``0.0.0.0/0``. The checker has no '/' or '%' to refuse on
    and reads the entry as a name.
    """

    @_waiting(MEDIUM_2)
    def test_an_ipv4_range_holding_the_route_host_is_refused(self) -> None:
        home = _home(self)
        for value in ("10.0.0.0-10.255.255.255", "10.1.2.3-10.1.2.3", "localhost,10.0.0.0 - 10.255.255.255"):
            with self.subTest(value=value):
                self.assert_refused_without_entry_text(home, {**PROXY, "NO_PROXY": value}, NO_PROXY_CAUSES, V4_GATEWAY)

    @_waiting(MEDIUM_2)
    def test_an_ipv4_range_of_every_address_is_a_wildcard(self) -> None:
        home = _home(self)
        for base_url in (None, "http://127.0.0.1:4000"):
            with self.subTest(base_url=base_url):
                self.assert_refused_without_entry_text(home, {**PROXY, "NO_PROXY": "0.0.0.0-255.255.255.255"}, WILDCARD_CAUSES, base_url)


@unittest.skipIf(os.name == "nt", "POSIX rules")
class StarPatternTimeTests(unittest.TestCase):
    """``_no_proxy_hit`` compiles a ``*`` entry to ``.*a.*a...`` and runs it on the route host with backtracking.

    The route host's length is the caller's (``_BASE_URL`` sets no limit) and the entry is the parent's NO_PROXY. 22
    characters of entry against a 38-character host took about 1 s here, growing about 5x per extra ``*a``; this
    case (30 characters against 56) must finish well inside 10 s. Run in a child process so a stall cannot hang the
    suite.
    """

    def test_a_star_entry_against_a_long_route_host_finishes_in_bounded_time(self) -> None:
        home = _home(self)
        core = str(Path(policy.__file__).resolve().parents[2])
        code = ("import sys\n"
                "from recursion_core.child_env.policy import ChildRoute, EnvRefused, build_child_env\n"
                "route = ChildRoute(api_key='sk-test-FAKE', base_url='https://' + 'a' * 48 + '.example')\n"
                "try:\n"
                "    build_child_env('claude_code', home=sys.argv[1], auth_mode='api_key', route=route,\n"
                "                    parent={'HTTPS_PROXY': 'http://127.0.0.1:3128', 'NO_PROXY': '*a' * 14 + '*q'})\n"
                "except EnvRefused:\n"
                "    pass\n")
        started = time.monotonic()
        try:
            subprocess.run([sys.executable, "-B", "-c", code, home], env={"PYTHONPATH": core}, timeout=10,
                           capture_output=True, check=True)
        except subprocess.TimeoutExpired:
            self.fail(f"build_child_env still running after {time.monotonic() - started:.1f}s on one 30-character entry")


@unittest.skipIf(os.name == "nt", "POSIX rules")
class HardeningThatHoldsTests(unittest.TestCase):
    """Shapes from the red-team list that the head already gets right (expected to pass on 8682b37a)."""

    def test_positions_count_empty_entries_and_restart_in_no_proxy(self) -> None:
        home = _home(self)
        for parent, expected in (
            ({"NO_PROXY": ",,a,gateway.example"}, ("NO_PROXY", "no_proxy_route_host", 3)),
            ({"NO_PROXY": "a,b", "no_proxy": " ,gateway.example"}, ("no_proxy", "no_proxy_route_host", 2)),
            ({"NO_PROXY": "a b\tc", "no_proxy": "d,0.0.0.0/0"}, ("no_proxy", "no_proxy_wildcard", 2)),
            ({"NO_PROXY": "0.0.0.0/1,,", "no_proxy": ",,128.0.0.0/1"}, ("no_proxy", "no_proxy_wildcard", 2)),
            ({"NO_PROXY": "0.0.0.0/1,128.0.0.0/1,0.0.0.0/0"}, ("NO_PROXY", "no_proxy_wildcard", 2)),
            ({"NO_PROXY": "0.0.0.0/1,0.0.0.0/1", "no_proxy": "128.0.0.0/1,128.0.0.0/1"}, ("no_proxy", "no_proxy_wildcard", 1)),
            ({"NO_PROXY": "a\u3000gateway.example"}, ("NO_PROXY", "no_proxy_route_host", 2)),
            ({"NO_PROXY": "a\x1cgateway.example"}, ("NO_PROXY", "no_proxy_route_host", 2)),
        ):
            with self.subTest(parent=ascii(parent)):
                self.assertEqual(expected, _outcome(home, {**PROXY, **parent}))

    def test_the_reported_cause_is_deterministic_and_ignores_parent_key_order(self) -> None:
        home = _home(self)
        for parent, expected in (
            ({"no_proxy": "gateway.example", **PROXY, "NO_PROXY": "a,gateway.example"}, ("NO_PROXY", "no_proxy_route_host", 2)),
            ({"no_proxy": "*", **PROXY, "NO_PROXY": "gateway.example"}, ("no_proxy", "no_proxy_wildcard", 1)),
            ({**PROXY, "NO_PROXY": "gateway.example,x%y"}, ("NO_PROXY", "no_proxy_unparseable", 2)),
            ({**PROXY, "NO_PROXY": "gateway.example", "no_proxy": "api.anthropic.com"}, ("no_proxy", "no_proxy_engine_host", 1)),
            ({**PROXY, "NO_PROXY": "128.0.0.0/1,api.anthropic.com", "no_proxy": "0.0.0.0/1"}, ("NO_PROXY", "no_proxy_engine_host", 2)),
            ({**PROXY, "NO_PROXY": "0.0.0.0/1", "no_proxy": "x%y,128.0.0.0/1"}, ("no_proxy", "no_proxy_unparseable", 1)),
        ):
            with self.subTest(parent=parent):
                self.assertEqual({expected}, {_outcome(home, parent) for _ in range(5)})

    def test_a_proxy_in_only_one_name_triggers_every_rule(self) -> None:
        home = _home(self)
        for proxy in ({"http_proxy": "http://127.0.0.1:3128"}, {"https_proxy": "127.0.0.1:3128"}, {"HTTP_PROXY": "http://[::1]:3128"},
                      {"all_proxy": "socks5h://127.0.0.1:1080"}, {"ALL_PROXY": "socks4a://127.0.0.1:1080"},
                      {"HTTPS_PROXY": "", "https_proxy": "http://127.0.0.1:3128"}):
            for parent, cause in (({"no_proxy": "gateway.example"}, "no_proxy_route_host"), ({"NO_PROXY": "0.0.0.0/0"}, "no_proxy_wildcard"),
                                  ({"no_proxy": "a/b/c"}, "no_proxy_unparseable"), ({"NO_PROXY": "claude.ai"}, "no_proxy_engine_host")):
                with self.subTest(proxy=proxy, parent=parent):
                    self.assertEqual(cause, _outcome(home, {**proxy, **parent})[1])

    def test_with_no_proxy_set_nothing_new_refuses_and_values_pass_unchanged(self) -> None:
        home = _home(self)
        lists = {"NO_PROXY": "*,x%y,a/b/c,gateway.example,0.0.0.0/0,api.anthropic.com,*.gateway.example",
                 "no_proxy": "0.0.0.0/1 128.0.0.0/1 ::ffff:0:0/96 10.0.0.0/33"}
        for proxy in ({}, {"HTTPS_PROXY": "", "http_proxy": "", "ALL_PROXY": ""}):
            for engine, base_url in (("claude_code", GATEWAY), ("claude_code", V4_GATEWAY), ("claude_code", None), ("codex", None)):
                with self.subTest(proxy=proxy, engine=engine, base_url=base_url):
                    env = _build(home, {**proxy, **lists}, base_url, engine)
                    self.assertEqual(lists, {name: env[name] for name in lists})

    def test_address_spellings_go_matches_are_refused_for_an_ip_route_host(self) -> None:
        home = _home(self)
        for base_url, value in (
            (V6_GATEWAY, "2001:db8::0.0.0.1"), (V6_GATEWAY, "2001:0db8:0000::0001"), (V6_GATEWAY, "2001:db8:0:0:0:0:0:1"),
            (V6_GATEWAY, "[2001:db8::1]:8443"), (V6_GATEWAY, "2001:db8::1/128"), (V6_GATEWAY, "2001:DB8::1"),
            (V4_GATEWAY, "::ffff:10.1.2.3"), (V4_GATEWAY, "::FFFF:A01:203"), (V4_GATEWAY, "[::ffff:10.1.2.3]"),
            (V4_GATEWAY, "0:0:0:0:0:ffff:10.1.2.3"), (V4_GATEWAY, "HTTP://10.1.2.3:8080"), ("http://[::ffff:10.1.2.3]:4000", "10.1.2.3"),
        ):
            with self.subTest(base_url=base_url, value=value):
                self.assertEqual(("NO_PROXY", "no_proxy_route_host", 1), _outcome(home, {**PROXY, "NO_PROXY": value}, base_url))

    def test_odd_cidr_spellings_refuse_on_every_route(self) -> None:
        home = _home(self)
        for value in ("0x0/0", "00.0.0.0/0", "0.0.0.0/00", "0.0.0.0/+0", "0.0.0.0/ 0", "\uff10.0.0.0/0", "10.0.0.0/\u0668",
                      "0.0.0.0/\uff10", "[::]/0", "[0.0.0.0]/0", "::/0", "0:0:0:0:0:0:0:0/0", "::FFFF:0.0.0.0/96",
                      "http://0.0.0.0:80/0", "user:pw@10.0.0.0/8", "user@0.0.0.0/0"):
            for base_url in (None, GATEWAY, "http://127.0.0.1:4000"):
                with self.subTest(value=ascii(value), base_url=base_url):
                    outcome = _outcome(home, {**PROXY, "NO_PROXY": value}, base_url)
                    self.assertIn(outcome[1] if outcome != "launched" else outcome, WILDCARD_CAUSES)

    def test_loopback_is_exempt_and_lookalikes_are_not(self) -> None:
        home = _home(self)
        for base_url, value in (("http://127.1:4000", "127.1"), ("http://0x7f.1:4000", "127.0.0.1"), ("http://[::1]:4000", "::1"),
                                ("http://[::ffff:127.0.0.1]:4000", "127.0.0.0/8"), ("http://localhost.:4000", "localhost"),
                                ("http://LocalHost:4000", "localhost:4000")):
            with self.subTest(base_url=base_url, value=value):
                self.assertEqual("launched", _outcome(home, {**PROXY, "NO_PROXY": value}, base_url))
        for base_url, value in (("http://localhost.example.com:4000", "localhost.example.com"),
                                ("http://127.0.0.1.example.com", ".example.com"), ("https://[::2]", "::2"),
                                ("http://128.0.0.1", "128.0.0.0/8"), ("https://[::127.0.0.1]", "::127.0.0.1"),
                                ("https://[64:ff9b::7f00:1]", "64:ff9b::7f00:1"), ("http://localhost..:4000", "localhost")):
            with self.subTest(base_url=base_url, value=value):
                self.assertEqual(("NO_PROXY", "no_proxy_route_host", 1), _outcome(home, {**PROXY, "NO_PROXY": value}, base_url))

    def test_a_large_list_reports_the_entry_that_completes_the_cover(self) -> None:
        home = _home(self)
        many = ",".join(f"{address}/32" for address in ipaddress.IPv4Network("10.0.0.0/18"))
        rest = ipaddress.IPv4Network("0.0.0.0/0").address_exclude(ipaddress.IPv4Network("255.255.255.255/32"))
        cover = ",".join(map(str, rest)) + ",255.255.255.255"
        started = time.monotonic()
        self.assertEqual(("no_proxy", "no_proxy_wildcard", 33), _outcome(home, {**PROXY, "NO_PROXY": many, "no_proxy": cover}))
        self.assertEqual(("NO_PROXY", "no_proxy_wildcard", 16384 + 33), _outcome(home, {**PROXY, "NO_PROXY": many + "," + cover}))
        self.assertLess(time.monotonic() - started, 60)


if __name__ == "__main__":
    unittest.main()
