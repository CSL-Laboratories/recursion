"""CSL-839: the ``*`` NO_PROXY matcher gives the old regex's results, in bounded time (Devin rt343, MEDIUM-1).

Run from the repository root:
``PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/child_env/tests -t core -p 'test_no_proxy_glob.py' -v``.
No engine, provider, network or real credential is used; every secret-shaped value is fake.
"""

from __future__ import annotations

import itertools
import os
import random
import re
import shutil
import time
import unittest
from unittest import mock

from recursion_core.child_env import ENGINE_PROFILES, ChildRoute, EnvRefused, build_child_env, policy
from recursion_core.child_env.tests import cells

PROXY = {"HTTPS_PROXY": "http://127.0.0.1:3128"}
USER_KEY = "fake-user-entered-key-not-a-credential"
ROUTE_HOSTS = ("gateway.example", "llm.gateway.example", "10.1.2.3", "2001:db8::1", "localhost", "127.0.0.1")
ENGINE_HOSTS = tuple(sorted({host for profile in ENGINE_PROFILES.values() for host in profile.hosts}))
# The oracle backtracks, so it only ever sees inputs this small: at most len(host) ** stars steps.
ORACLE_MAX_STARS, ORACLE_MAX_HOST = 4, 40
SECONDS = 0.5  # per input; the matcher needs well under a millisecond, the old regex 8 s to forever


def _oracle(entry: str, host: str) -> bool:
    """The matcher ``_no_proxy_hit`` had at 8682b37a, kept here only to compare with."""

    assert entry.count("*") <= ORACLE_MAX_STARS and len(host) <= ORACLE_MAX_HOST, "too large for the regex oracle"
    pattern = re.compile(".*".join(re.escape(part) for part in entry.split("*")) + r"\Z")
    return pattern.match(host) is not None


def _words(rng: random.Random, alphabet: str, longest: int, count: int) -> list[str]:
    return ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, longest))) for _ in range(count)]


def _starred(rng: random.Random, host: str) -> str:
    """A real host with up to three of its spans replaced by ``*``, and sometimes one character changed."""

    text = host
    for _ in range(rng.randint(1, 3)):
        start = rng.randint(0, len(text))
        text = text[:start] + "*" + text[rng.randint(start, len(text)):]
    if text.count("*") > 3:
        return host[:1] + "*"
    if rng.random() < 0.3 and text:
        at = rng.randrange(len(text))
        text = text[:at] + rng.choice("ax.-*1") + text[at + 1:]
    return text if text.count("*") <= ORACLE_MAX_STARS else host[:1] + "*"


class StarMatcherEquivalenceTests(unittest.TestCase):
    """Every pair gets the result the regex gave; the corpus is generated from fixed seeds."""

    def _assert_same(self, pairs) -> int:
        checked = hits = 0
        for entry, host in pairs:
            expected = _oracle(entry, host)
            if policy._star_match(entry, host) != expected:
                self.fail(f"_star_match({entry!r}, {host!r}) is {not expected}; the regex gave {expected}")
            if "*" in entry and policy._no_proxy_hit(entry, (host,)) != expected:
                self.fail(f"_no_proxy_hit({entry!r}, ({host!r},)) is {not expected}; the regex gave {expected}")
            checked += 1
            hits += expected
        return hits if checked else -1

    def test_every_short_pattern_and_host_over_a_tiny_alphabet(self) -> None:
        entries = ["".join(item) for size in range(6) for item in itertools.product("a*.", repeat=size)]
        entries = [entry for entry in entries if entry.count("*") <= ORACLE_MAX_STARS]
        hosts = ["".join(item) for size in range(6) for item in itertools.product("a.", repeat=size)]
        hits = self._assert_same(itertools.product(entries, hosts))
        self.assertGreater(hits, 1000)  # the corpus holds matches as well as misses

    def test_random_patterns_and_hosts(self) -> None:
        rng = random.Random(839)
        entries = [entry for entry in _words(rng, "ab*.-1*", 9, 4000) if entry.count("*") <= ORACLE_MAX_STARS]
        hosts = _words(rng, "ab.-1", 12, 300) + _words(rng, "ab.-1*", 8, 50)
        pairs = [(rng.choice(entries), rng.choice(hosts)) for _ in range(60000)]
        self.assertGreater(self._assert_same(pairs), 1000)

    def test_patterns_cut_from_the_real_hosts(self) -> None:
        rng = random.Random(343)
        hosts = ROUTE_HOSTS + ENGINE_HOSTS
        self.assertTrue(all(len(host) <= ORACLE_MAX_HOST for host in hosts))
        entries = [_starred(rng, host) for host in hosts for _ in range(300)]
        entries += [f"*.{host}" for host in hosts] + [f"*{host}" for host in hosts] + [f"{host}*" for host in hosts]
        self.assertGreater(self._assert_same(itertools.product(entries, hosts)), 1000)

    def test_a_star_never_matches_a_newline(self) -> None:
        """The regex had no DOTALL. No entry or host holds a newline in ``build_child_env``; the rule is kept anyway."""

        rng = random.Random(11)
        entries = [entry for entry in _words(rng, "a*.\n*", 7, 3000) if entry.count("*") <= ORACLE_MAX_STARS]
        hosts = _words(rng, "a.\n", 8, 300)
        self.assertGreater(self._assert_same(itertools.product(entries, hosts)), 1000)
        for entry, host, expected in (("*", "a\nb", False), ("*", "a\n", False), ("a*", "a\n", False), ("*\n*", "a\nb", True),
                                      ("*\n", "a\n", True), ("*", "", True), ("*", "a\r.b", True), ("a*b", "a\nb", False)):
            with self.subTest(entry=entry, host=host):
                self.assertEqual(expected, _oracle(entry, host))
                self.assertEqual(expected, policy._star_match(entry, host))

    def test_named_cases(self) -> None:
        for entry, host, expected in (
            ("*.gateway.example", "llm.gateway.example", True), ("*.gateway.example", "gateway.example", False),
            ("*gateway.example", "gateway.example", True), ("gate*.example", "gateway.example", True),
            ("gateway.*", "gateway.example", True), ("g*y.e*e", "gateway.example", True), ("*way*", "gateway.example", True),
            ("gateway*example", "gateway.example", True), ("*.example", "gateway.example.org", False),
            ("gateway.example*", "gateway.example", True), ("**gateway*example*", "gateway.example", True),
            ("*a*b", "ab.ab.a", False), ("*a*b", "ab.ab.ab", True), ("*ab", "aab", True), ("a*a", "a", False),
            ("*.EXAMPLE", "gateway.example", False), ("10.*.3", "10.1.2.3", True), ("2001:*::1", "2001:db8::1", True),
            ("*.", "gateway.", True), ("*+", "a+", True), ("*[a]", "x[a]", True), ("*[a]", "xa", False), ("*?", "a?", True),
        ):
            with self.subTest(entry=entry, host=host):
                self.assertEqual(expected, _oracle(entry, host))
                self.assertEqual(expected, policy._star_match(entry, host))
                self.assertEqual(expected, policy._no_proxy_hit(entry, (host,)))

    def test_the_module_compiles_no_regex_for_a_star_entry(self) -> None:
        with mock.patch.object(policy.re, "compile", side_effect=AssertionError("a regex was compiled")):
            self.assertTrue(policy._no_proxy_hit("*.gateway.example", ("llm.gateway.example",)))
            self.assertFalse(policy._no_proxy_hit("*a*a*a*q", ("a" * 30 + ".example",)))


@unittest.skipIf(os.name == "nt", "POSIX rules")
class StarMatcherTimeTests(unittest.TestCase):
    """Devin rt343 MEDIUM-1: at 8682b37a the 18-character entry took 8.6 s here and the 22-character one over 130 s."""

    HOST = "a" * 48 + ".example"
    LONG_HOST = "a" * 242 + ".example"  # 250 characters; _BASE_URL sets no length limit
    # (entry, route host, whether the entry matches the host)
    INPUTS = (
        ("*a" * 8 + "*q", HOST, False),  # Devin's 18 characters
        ("*a" * 10 + "*q", HOST, False),  # Devin's 22 characters
        ("*a" * 14 + "*q", HOST, False),  # the 30 characters of Devin's own test
        ("*a" * 100 + "*q", LONG_HOST, False),
        ("*a" * 100 + "*e", LONG_HOST, True),
        ("*a" * 8 + "*e", HOST, True),
        ("*" + "a" * 100 + "b", LONG_HOST, False),  # the two-pointer walk's own worst shape
        ("*a" * 100 + "*", "a" * 99 + "b" * 151, False),
    )

    def _timed(self, call):
        """The call's result and its fastest time: it is run again, twice at most, only when it was too slow."""

        for _ in range(3):
            started = time.perf_counter()
            result = call()
            elapsed = time.perf_counter() - started
            if elapsed < SECONDS:
                break
        return result, elapsed

    def _decided(self, home: str, entry: str, host: str):
        route = ChildRoute(api_key=USER_KEY, base_url=f"https://{host}/api")
        try:
            build_child_env("claude_code", home=home, auth_mode="api_key", route=route, parent={**PROXY, "NO_PROXY": entry})
        except EnvRefused as refused:
            return refused.name, refused.cause, refused.position, str(refused)
        return "launched"

    def test_build_child_env_is_fast_and_decides_as_before(self) -> None:
        home = cells.owned_tmpdir("csl839-home-")
        self.addCleanup(shutil.rmtree, home, True)
        refusal = ("NO_PROXY", "no_proxy_route_host", 1,
                   "NO_PROXY: no_proxy_route_host: entry 1 of NO_PROXY matches the route's base URL host; the entry is not shown")
        for entry, host, matches in self.INPUTS:
            with self.subTest(entry=len(entry), host=len(host), matches=matches):
                outcome, elapsed = self._timed(lambda: self._decided(home, entry, host))
                self.assertLess(elapsed, SECONDS)
                if matches:
                    self.assertEqual(refusal[:3], outcome[:3])
                    self.assertEqual(refusal[3], outcome[3])
                else:
                    self.assertEqual("launched", outcome)

    def test_no_proxy_bypass_is_fast_and_decides_as_before(self) -> None:
        for entry, host, matches in self.INPUTS:
            for name in policy.NO_PROXY_NAMES:
                with self.subTest(entry=len(entry), host=len(host), matches=matches, name=name):
                    lists = [(name, f"localhost,{entry}")]
                    refused, elapsed = self._timed(lambda: policy._no_proxy_bypass(lists, ENGINE_HOSTS + (host,)))
                    self.assertLess(elapsed, SECONDS)
                    if matches:
                        self.assertEqual((name, "no_proxy_engine_host", 2), (refused.name, refused.cause, refused.position))
                        self.assertNotIn(entry[:8], str(refused))
                    else:
                        self.assertIsNone(refused)

    def test_many_star_entries_in_one_value_are_fast(self) -> None:
        value = ",".join("*a" * 100 + f"*q{index}" for index in range(200))
        refused, elapsed = self._timed(lambda: policy._no_proxy_route_bypass(value, self.LONG_HOST))
        self.assertLess(elapsed, 10 * SECONDS)
        self.assertIsNone(refused)


if __name__ == "__main__":
    unittest.main()
