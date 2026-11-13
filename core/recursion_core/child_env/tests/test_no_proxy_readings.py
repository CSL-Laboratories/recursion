"""CSL-839, Devin rt343 MEDIUM-2: NO_PROXY entries read as loosely as real client matchers read them.

``policy._no_proxy_readings`` reads each entry the extra ways undici, Node's built-in matcher, requests and Go
read it, and any reading refuses (PRODUCT.md d84cdd26 / CSLR-D-012, section 11.5, N5i's ``NO_PROXY`` rule).
Each class below covers one reading: entries that some client reads as the route host, an engine host or
every host refuse with an existing cause and show no entry text, and nearby harmless entries still launch.
Loopback and ``localhost`` entries stay allowed as before. Tests only; no engine, network or credential.
"""

from __future__ import annotations

import os
import shutil
import time
import traceback
import unicodedata
import unittest

from recursion_core.child_env import ChildRoute, EnvRefused, build_child_env, policy
from recursion_core.child_env.tests import cells

PROXY = {"HTTPS_PROXY": "http://127.0.0.1:3128"}
GATEWAY = "https://gateway.example/api"
V4_GATEWAY = "http://10.1.2.3:4000"
V6_GATEWAY = "https://[2001:db8::1]:8443/v1"
IDN_GATEWAY = "https://xn--bcher-kva.example/api"  # the punycode of "bucher.example" with a u-umlaut
LOOPBACK = "http://127.0.0.1:4000"
SECONDS = 10.0  # backstop against blow-ups; growth below is the real check (CI runners are slower than a dev box)
GROWTH = 3.0  # full input vs half: linear is about 2, quadratic about 4
GROWTH_FLOOR_S = 0.05  # below this, timer noise dominates the ratio


def _home(test: unittest.TestCase) -> str:
    home = cells.owned_tmpdir("csl839-readings-")
    test.addCleanup(shutil.rmtree, home, True)
    return home


def _outcome(home: str, parent: dict, base_url: str | None = GATEWAY, engine: str = "claude_code"):
    route = ChildRoute(api_key="sk-test-FAKE", base_url=base_url)
    try:
        build_child_env(engine, home=home, auth_mode="api_key", route=route, parent={**PROXY, **parent})
    except EnvRefused as refused:
        texts = [str(refused), repr(refused), *map(str, refused.args), str(refused.detail),
                 "".join(traceback.format_exception(refused))]
        for value in parent.values():
            for entry in policy._no_proxy_entries(value):
                for fragment in {entry, entry.strip("*.\ufeff "), policy._no_proxy_name(entry), *entry.split("\ufeff")}:
                    if len(fragment) >= 4:
                        assert not any(fragment in text.lower() for text in texts), "a refusal shows entry text"
        return refused.name, refused.cause, refused.position
    return "launched"


@unittest.skipIf(os.name == "nt", "POSIX rules")
class StarDotTests(unittest.TestCase):
    """undici strips a leading ``*.``: ``*.host`` names ``host`` itself, as ``.host`` already did."""

    def test_a_star_dot_entry_names_the_host_itself(self) -> None:
        home = _home(self)
        for value, base_url, expected in (
            ("*.gateway.example", GATEWAY, ("NO_PROXY", "no_proxy_route_host", 1)),
            ("localhost,*.gateway.example:8443", GATEWAY, ("NO_PROXY", "no_proxy_route_host", 2)),
            ("*.*.gateway.example", GATEWAY, ("NO_PROXY", "no_proxy_route_host", 1)),
            ("*.example", GATEWAY, ("NO_PROXY", "no_proxy_route_host", 1)),  # a parent domain, as before
            ("*.api.anthropic.com", None, ("NO_PROXY", "no_proxy_engine_host", 1)),
            ("*.claude.ai", GATEWAY, ("NO_PROXY", "no_proxy_engine_host", 1)),
        ):
            with self.subTest(value=value, base_url=base_url):
                self.assertEqual(expected, _outcome(home, {"NO_PROXY": value}, base_url))

    def test_star_dot_entries_for_other_hosts_still_launch(self) -> None:
        home = _home(self)
        for value in ("*.llm.gateway.example", "*.gateway.example.evil", "*.notgateway.example", "*.corp.example",
                      "*.localhost", "api.gateway.example,*.other.example"):
            with self.subTest(value=value):
                self.assertEqual("launched", _outcome(home, {"NO_PROXY": value}))


@unittest.skipIf(os.name == "nt", "POSIX rules")
class WhitespaceTests(unittest.TestCase):
    """Every character JavaScript's trim and ``\\s`` take (U+FEFF too), and Go's and requests' whitespace."""

    def test_the_space_set_is_python_whitespace_and_javascript_whitespace(self) -> None:
        python = {chr(code) for code in range(0x110000) if chr(code).isspace()}
        self.assertEqual(python | {"\ufeff"}, set(policy._SPACES))
        self.assertEqual(len(policy._SPACES), len(set(policy._SPACES)))

    def test_any_space_around_or_inside_the_host_is_stripped_or_split(self) -> None:
        home = _home(self)
        for space in policy._SPACES:
            for value in (f"{space}gateway.example", f"gateway.example{space}", f"x{space}gateway.example",
                          f"localhost,{space}gateway.example"):
                with self.subTest(value=ascii(value)):
                    self.assertEqual("no_proxy_route_host", _outcome(home, {"NO_PROXY": value})[1])
            with self.subTest(value=ascii(f"{space}*")):
                self.assertEqual("no_proxy_wildcard", _outcome(home, {"no_proxy": f"{space}*"}, None)[1])
            with self.subTest(value=ascii(f"x{space}claude.ai")):
                self.assertEqual("no_proxy_engine_host", _outcome(home, {"NO_PROXY": f"x{space}claude.ai"}, None)[1])

    def test_a_bom_keeps_the_position_of_its_entry(self) -> None:
        home = _home(self)
        self.assertEqual(("NO_PROXY", "no_proxy_route_host", 2), _outcome(home, {"NO_PROXY": "a,x\ufeffgateway.example"}))
        self.assertEqual(("NO_PROXY", "no_proxy_wildcard", 3), _outcome(home, {"NO_PROXY": "a b,\ufeff*"}))

    def test_a_bom_beside_a_harmless_entry_still_launches(self) -> None:
        home = _home(self)
        for value in ("\ufefflocalhost", "x\ufeffother.example", "corp.example\ufeff,127.0.0.1", "\ufeff", "a\ufeff\ufeffb"):
            with self.subTest(value=ascii(value)):
                self.assertEqual("launched", _outcome(home, {"NO_PROXY": value}))


@unittest.skipIf(os.name == "nt", "POSIX rules")
class RangeTests(unittest.TestCase):
    """Node's built-in matcher reads ``a-b`` as an IPv4 range, trimming each end."""

    def test_a_range_holding_the_route_host_refuses(self) -> None:
        home = _home(self)
        for value, position in (("10.0.0.0-10.255.255.255", 1), ("10.1.2.3-10.1.2.3", 1), ("10.1.2.0-10.1.2.3-junk", 1),
                                ("localhost,10.0.0.0 - 10.255.255.255", 2), ("localhost,10.0.0.0-\ufeff10.1.2.3", 2),
                                ("0.0.0.0-10.1.2.3", 1)):
            for base_url in (V4_GATEWAY, "http://[::ffff:10.1.2.3]:4000"):
                with self.subTest(value=ascii(value), base_url=base_url):
                    self.assertEqual(("NO_PROXY", "no_proxy_route_host", position),
                                     _outcome(home, {"NO_PROXY": value}, base_url))

    def test_ranges_that_cover_every_address_are_a_wildcard(self) -> None:
        home = _home(self)
        for parent, expected in (
            ({"NO_PROXY": "0.0.0.0-255.255.255.255"}, ("NO_PROXY", "no_proxy_wildcard", 1)),
            ({"NO_PROXY": "0.0.0.0 - 255.255.255.255"}, ("NO_PROXY", "no_proxy_wildcard", 1)),
            ({"NO_PROXY": "0.0.0.0-127.255.255.255,128.0.0.0/1"}, ("NO_PROXY", "no_proxy_wildcard", 2)),
            ({"NO_PROXY": "128.0.0.0/1", "no_proxy": "x,0.0.0.0-127.255.255.255"}, ("no_proxy", "no_proxy_wildcard", 2)),
            ({"NO_PROXY": "0.0.0.0-100.0.0.0,100.0.0.1-255.255.255.255"}, ("NO_PROXY", "no_proxy_wildcard", 2)),
        ):
            for base_url in (None, GATEWAY, LOOPBACK):
                with self.subTest(parent=parent, base_url=base_url):
                    self.assertEqual(expected, _outcome(home, parent, base_url))
        refused = self._refusal({"NO_PROXY": "0.0.0.0-255.255.255.255"})
        self.assertEqual("NO_PROXY: no_proxy_wildcard: entry 1 of NO_PROXY completes a cover of every IPv4 address; "
                         "the entry is not shown", str(refused))

    def _refusal(self, parent: dict) -> EnvRefused:
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("claude_code", home=_home(self), auth_mode="api_key",
                            route=ChildRoute(api_key="sk-test-FAKE"), parent={**PROXY, **parent})
        return caught.exception

    def test_other_ranges_still_launch(self) -> None:
        home = _home(self)
        for value, base_url in (("192.168.0.0-192.168.255.255", V4_GATEWAY), ("10.255.255.255-10.0.0.0", V4_GATEWAY),
                                ("10.0.0.0-10.255.255.255", GATEWAY), ("0.0.0.0-255.255.255.254", GATEWAY),
                                ("010.0.0.0-10.255.255.255", V4_GATEWAY), ("10.0.0.0-", V4_GATEWAY),
                                ("my-host.example,a-b", GATEWAY), ("127.0.0.0-127.255.255.255", LOOPBACK)):
            with self.subTest(value=value, base_url=base_url):
                self.assertEqual("launched", _outcome(home, {"NO_PROXY": value}, base_url))

    def test_the_range_parser(self) -> None:
        self.assertEqual((167772160, 184549375), policy._ipv4_range("10.0.0.0-10.255.255.255"))
        self.assertEqual((0, 2 ** 32 - 1), policy._ipv4_range(" 0.0.0.0\ufeff -\u3000255.255.255.255 "))
        for text in ("10.0.0.0", "10.0.0.0-10.0.0", "10.0.0.0-::1", "a-b", "-", "10.0.0.0/8-10.0.0.1", "10.0.0.2-10.0.0.1"):
            with self.subTest(text=text):
                self.assertIsNone(policy._ipv4_range(text))


@unittest.skipIf(os.name == "nt", "POSIX rules")
class SpaceJoinTests(unittest.TestCase):
    """requests deletes the spaces inside each comma item: ``g ateway.example`` is ``gateway.example``."""

    def test_a_space_split_name_or_address_refuses_at_the_entry_it_starts_in(self) -> None:
        home = _home(self)
        for value, base_url, expected in (
            ("g ateway.example", GATEWAY, ("NO_PROXY", "no_proxy_route_host", 1)),
            ("localhost,gatew ay.exam ple", GATEWAY, ("NO_PROXY", "no_proxy_route_host", 2)),
            ("1 0.1.2.3", V4_GATEWAY, ("NO_PROXY", "no_proxy_route_host", 1)),
            ("x,1 0.0.0.0/8", V4_GATEWAY, ("NO_PROXY", "no_proxy_route_host", 2)),
            ("2001:db8: :1", V6_GATEWAY, ("NO_PROXY", "no_proxy_route_host", 1)),
            ("api.anth ropic.com", None, ("NO_PROXY", "no_proxy_engine_host", 1)),
        ):
            with self.subTest(value=value, base_url=base_url):
                self.assertEqual(expected, _outcome(home, {"NO_PROXY": value}, base_url))

    def test_a_space_split_cover_is_a_wildcard_and_ipv6_joins_are_not_a_cover(self) -> None:
        home = _home(self)
        self.assertEqual(("NO_PROXY", "no_proxy_wildcard", 2), _outcome(home, {"NO_PROXY": "0.0.0.0/1,1 28.0.0.0/1"}, None))
        self.assertEqual(("no_proxy", "no_proxy_wildcard", 2), _outcome(home, {"no_proxy": "0.0.0.0/1 ,12 8.0.0.0/1"}, None))
        # requests reads CIDR for IPv4 only: "::1 0.0.0.0/0" joined is no IPv6 cover; the /0 entry still is one.
        self.assertEqual(("NO_PROXY", "no_proxy_wildcard", 3), _outcome(home, {"NO_PROXY": "localhost,::1 0.0.0.0/0"}, None))
        self.assertEqual("launched", _outcome(home, {"NO_PROXY": "::1 0.0.0.0/1"}, None))  # not "::10.0.0.0/1"

    def test_space_separated_harmless_lists_still_launch(self) -> None:
        home = _home(self)
        for value, base_url in (("localhost 127.0.0.1 ::1 .corp.example", GATEWAY), ("10.0.0.0/8 192.168.0.0/16", GATEWAY),
                                ("192.168.0.0/16 x", V4_GATEWAY), ("gate way.other", GATEWAY), ("1 0.1.2.4", V4_GATEWAY),
                                ("*a *b", GATEWAY), ("0.0.0.0/1 x", None)):
            with self.subTest(value=value, base_url=base_url):
                self.assertEqual("launched", _outcome(home, {"NO_PROXY": value}, base_url))


@unittest.skipIf(os.name == "nt", "POSIX rules")
class IdnaFoldTests(unittest.TestCase):
    """Go maps names through ``idna.Lookup``: NFKC, case folding, ignorables dropped, ideographic full stops."""

    def test_the_fold(self) -> None:
        for text, folded in (("\uff47\uff41\uff54\uff45\uff57\uff41\uff59.example", "gateway.example"),
                             ("gateway\u3002example", "gateway.example"), ("gateway\uff61example", "gateway.example"),
                             ("GATEWAY\uff0eEXAMPLE".lower(), "gateway.example"), ("gate\xadway\u200b.ex\ufeffample", "gateway.example"),
                             ("\u212aelvin", "kelvin"), ("stra\xdfe", "strasse"), ("\uff0a", "*"), ("b\xfccher", "b\xfccher")):
            with self.subTest(text=ascii(text)):
                self.assertEqual(folded, policy._idna_fold(text))
        self.assertEqual("stra\xdfe", policy._idna_fold("stra\xdfe", str.lower))
        self.assertEqual("cn", unicodedata.category("\u0378").lower())
        self.assertIsNone(policy._idna_fold("a\u0378b"))

    def test_a_folded_entry_naming_a_host_refuses(self) -> None:
        home = _home(self)
        for value, base_url, cause in (
            ("\uff47\uff41\uff54\uff45\uff57\uff41\uff59.example", GATEWAY, "no_proxy_route_host"),
            ("gateway\u3002example", GATEWAY, "no_proxy_route_host"), ("gateway\uff61example", GATEWAY, "no_proxy_route_host"),
            ("gate\xadway.example", GATEWAY, "no_proxy_route_host"), ("gateway\u200b.example", GATEWAY, "no_proxy_route_host"),
            ("\uff11\uff10.1.2.3", V4_GATEWAY, "no_proxy_route_host"), ("[2001:db8::\uff11]", V6_GATEWAY, "no_proxy_route_host"),
            ("*\uff0egateway.example", GATEWAY, "no_proxy_route_host"),
            ("\uff41\uff50\uff49.anthropic.com", None, "no_proxy_engine_host"), ("claude\u3002ai", None, "no_proxy_engine_host"),
            ("\uff0a", None, "no_proxy_wildcard"), ("\uff0a\uff0e", None, "no_proxy_wildcard"),
            ("x\uff05y", None, "no_proxy_unparseable"), ("a\uff0fb\uff0fc", GATEWAY, "no_proxy_unparseable"),
            ("gateway\uff0cexample", None, "launched"),
        ):
            with self.subTest(value=ascii(value), base_url=base_url):
                outcome = _outcome(home, {"NO_PROXY": value}, base_url)
                self.assertEqual(cause, outcome if outcome == "launched" else outcome[1])

    def test_a_non_ascii_name_refuses_only_beside_a_punycode_host(self) -> None:
        home = _home(self)
        for value in ("b\xfccher.example", "B\xdcCHER.example", "stra\xdfe.example", "\xe9.example"):
            with self.subTest(value=ascii(value)):
                self.assertEqual("launched", _outcome(home, {"NO_PROXY": value}, GATEWAY))
                self.assertEqual("launched", _outcome(home, {"NO_PROXY": value}, None))
                self.assertEqual(("NO_PROXY", "no_proxy_unparseable", 1), _outcome(home, {"NO_PROXY": value}, IDN_GATEWAY))
        with self.assertRaises(EnvRefused) as caught:
            build_child_env("claude_code", home=_home(self), auth_mode="api_key", parent={**PROXY, "NO_PROXY": "x,b\xfccher.example"},
                            route=ChildRoute(api_key="sk-test-FAKE", base_url=IDN_GATEWAY))
        self.assertEqual("NO_PROXY: no_proxy_unparseable: entry 2 of NO_PROXY is unparseable: a name that is not ASCII after "
                         "folding can't be compared with a punycode (xn--) host; the entry is not shown", str(caught.exception))
        self.assertEqual("launched", _outcome(home, {"NO_PROXY": "xn--other-kva.example,corp.example"}, IDN_GATEWAY))

    def test_an_unassigned_code_point_is_unparseable_on_any_route(self) -> None:
        home = _home(self)
        for base_url in (None, GATEWAY, LOOPBACK):
            with self.subTest(base_url=base_url):
                self.assertEqual(("NO_PROXY", "no_proxy_unparseable", 2),
                                 _outcome(home, {"NO_PROXY": "localhost,corp\u0378.example"}, base_url))

    def test_harmless_non_ascii_entries_still_launch(self) -> None:
        home = _home(self)
        for value in ("\uff4c\uff4f\uff43\uff41\uff4c\uff48\uff4f\uff53\uff54", "corp\u3002example", "\xe9cole.example",
                      "\uff47\uff41\uff54\uff45\uff57\uff41\uff59.example.evil", "\u0433ateway.example"):
            with self.subTest(value=ascii(value)):
                self.assertEqual("launched", _outcome(home, {"NO_PROXY": value}))


@unittest.skipIf(os.name == "nt", "POSIX rules")
class LoopbackStaysAllowedTests(unittest.TestCase):
    def test_the_usual_loopback_list_launches_on_every_route(self) -> None:
        home = _home(self)
        for value in ("localhost,127.0.0.1,::1", "localhost 127.0.0.1 ::1", ".localhost,127.0.0.0/8", "localhost\ufeff,\ufeff127.0.0.1",
                      "127.0.0.1-127.0.0.255"):
            for base_url in (None, GATEWAY, V4_GATEWAY, V6_GATEWAY, LOOPBACK, "http://localhost:4000", "http://[::1]:4000"):
                for engine in ("claude_code", "codex"):
                    if engine == "codex" and base_url is not None:
                        continue
                    with self.subTest(value=ascii(value), base_url=base_url, engine=engine):
                        self.assertEqual("launched", _outcome(home, {"NO_PROXY": value}, base_url, engine))

    def test_a_loopback_route_may_still_be_named_in_any_reading(self) -> None:
        home = _home(self)
        for value in ("127.0.0.0-127.255.255.255", "*.localhost", "1 27.0.0.1", "\uff4c\uff4f\uff43\uff41\uff4c\uff48\uff4f\uff53\uff54"):
            with self.subTest(value=ascii(value)):
                self.assertEqual("launched", _outcome(home, {"NO_PROXY": value}, "http://localhost:4000"))
                self.assertEqual("launched", _outcome(home, {"NO_PROXY": value}, LOOPBACK))


@unittest.skipIf(os.name == "nt", "POSIX rules")
class ReadingTimeTests(unittest.TestCase):
    """The extra readings stay linear-ish: the worst new paths on input about as long as an env value may be."""

    LONG_HOST = "a" * 242 + ".example"

    def _timed(self, call):
        for _ in range(3):  # a slow run (a busy host) is retried twice at most
            started = time.perf_counter()
            result = call()
            elapsed = time.perf_counter() - started
            if elapsed < SECONDS:
                break
        return result, elapsed

    def _decide(self, value: str):
        lists = [("NO_PROXY", value)]
        refused = policy._no_proxy_bypass(lists, policy.ENGINE_PROFILES["claude_code"].hosts + (self.LONG_HOST,))
        return refused if refused is not None else policy._no_proxy_route_bypass(value, self.LONG_HOST)

    def test_hostile_values_are_decided_in_bounded_time(self) -> None:
        # Each value is built for a size n; it is timed at n and at n / 2. Growth, not wall time, is what proves
        # "linear-ish": a CI runner may be several times slower than a dev box, but halving the input must still
        # roughly halve the time (quadratic gives 4x). SECONDS is only a backstop against blow-ups.
        unit = 100_000
        for label, make in (
            # A 50,000-character requests join with star-dot tails. Most of its time is the "*" matcher on 12,500
            # entries, as before MEDIUM-2 (1.65 s at 100,000 characters at abb3d75d; 1.88 s now).
            ("one long spaced item", lambda n: "*.a " * (n // 8) + "q"),
            ("one long space run", lambda n: "a" + " " * n + "b"),
            ("one long BOM run", lambda n: "a" + "\ufeff" * n + "b"),  # undici pieces
            ("BOM and star", lambda n: "\ufeff*a" * (n // 3) + "q"),
            ("fullwidth", lambda n: "\uff41" * n + "\uff0eexample"),  # one long IDNA fold
            ("fullwidth spaced", lambda n: "\uff41 " * (n // 2)),
            ("dashes", lambda n: "-" * n), ("dashes in a range", lambda n: "10.0.0.0-" + "-" * n + "10.0.0.1"),
            ("spaced dashes", lambda n: "1.0.0.0 - " * (n // 10)),
            ("many ranges", lambda n: ",".join(f"10.{i // 256}.{i % 256}.1-10.{i // 256}.{i % 256}.254"
                                               for i in range(n // 25))),
            ("mixed", lambda n: "\ufeff \uff0e*.\xad-1 0.0.0.0/1 ," * (n // 20)),
        ):
            value, half = make(unit), make(unit // 2)
            with self.subTest(label=label, length=len(value)):
                for _ in range(3):  # a noisy pair is measured again, twice at most
                    _, elapsed = self._timed(lambda: self._decide(value))
                    _, half_elapsed = self._timed(lambda: self._decide(half))
                    if elapsed < GROWTH * max(half_elapsed, GROWTH_FLOOR_S):
                        break
                self.assertLess(elapsed, SECONDS)
                self.assertLess(elapsed, GROWTH * max(half_elapsed, GROWTH_FLOOR_S))

    def test_ranges_cost_one_interval_each(self) -> None:
        # 4,000 distinct ranges of 62 CIDR blocks each would be 248,000 blocks; as intervals they sweep at once.
        value = ",".join(f"{i // 256}.{i % 256}.0.1-{i // 256}.{i % 256}.255.254" for i in range(4000)) + ",0.0.0.0-255.255.255.255"
        refused, elapsed = self._timed(lambda: policy._no_proxy_bypass([("NO_PROXY", value)], ()))
        self.assertLess(elapsed, SECONDS)
        self.assertEqual(("no_proxy_wildcard", 4001), (refused.cause, refused.position))


if __name__ == "__main__":
    unittest.main()
