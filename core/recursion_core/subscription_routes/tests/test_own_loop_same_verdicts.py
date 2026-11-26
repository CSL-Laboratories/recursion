"""The Nous own-loop route accepts and refuses exactly the proxy addresses it took and refused before.

Only the words changed: a refusal shows the address in its shown form (recursion_core.shown_address), and
a user name or a password in the address's authority is refused as ``recursion_holds_no_token`` before
anything else is read. An at sign in the path, the query or the fragment, and an empty user, are not a
sign-in: such an address is taken as it was taken before.

Made-up values only: ``SECRETSTANDIN`` stands where a password or a token would. No engine, login store,
provider, DNS or network is touched.
"""

from __future__ import annotations

import ipaddress
import itertools
import unittest
from urllib.parse import urlsplit

from recursion_core.shown_address import holds_sign_in
from recursion_core.subscription_routes import OwnLoopRouteRefused, check_nous_own_loop, nous_proxy_doctor

S = "SECRETSTANDIN"
BASE = {"PATH": "/usr/bin:/bin", "HOME": "/home/u", "LANG": "C.UTF-8"}
SIGN_IN_WORDS = ("credentials in proxy URL: a user name or password stands before the host; give the proxy's "
                 "address with no user name or password, since the proxy holds the sign-in")


def _verdict_before(base_url: object) -> str:
    """The decision of ``_loopback_url`` in core/recursion_core/subscription_routes/own_loop.py at 7a9d71af,
    statement for statement, with each refusal reduced to its cause and "" for an address it took."""

    if not isinstance(base_url, str) or not base_url.strip():
        return "proxy_address_missing"
    try:
        parts = urlsplit(base_url.strip())
        host = parts.hostname
        parts.port  # noqa: B018
    except ValueError:
        return "proxy_address_malformed"
    if parts.scheme not in ("http", "https") or not host:
        return "proxy_address_malformed"
    if parts.username or parts.password:
        return "recursion_holds_no_token"
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return "proxy_not_loopback"
    if getattr(addr, "ipv4_mapped", None) is not None:
        addr = addr.ipv4_mapped
    if not addr.is_loopback:
        return "proxy_not_loopback"
    return ""


# Characters the URL library deletes (tab, CR, LF), strips from the front or the scheme (C0 controls and
# space), or that cannot be printed or are white space by Python's own rule.
ODD = tuple(chr(i) for i in range(0x21)) + ("\x7f", "\x85", "\xa0", "\u2028", "\u2029", "\u3000", "\u200b",
                                            "\ufeff")

TABLE = (
    # taken before, taken now
    "http://127.0.0.1:8645/v1", "https://[::1]/v1", "http://[::ffff:127.0.0.1]:1/", " http://127.0.0.1:8645 ",
    "HTTP://127.0.0.1/", "http://127.0.0.1:8645/v1?q=1", "http://127.0.0.1/%40", "http://127.0.0.1:8645/v1?x=a@b",
    "http://127.0.0.1:8645/v1#a@b", "http://127.0.0.1:8645/a@b/v1", "http://@127.0.0.1/", "http://:@127.0.0.1/",
    f"http://@127.0.0.1/v1?x={S}", f"http://127.0.0.1/v1/a@{S}", f"http://127.0.0.1/v1?q=a@{S}",
    f"http://127.0.0.1/v1#a@{S}", f"http://127.0.0.1:8645/v1;token={S}", f"http://127.0.0.1:8645/{S}/v1",
    f"http://127.0.0.1:8645/v1%3Ftoken={S}", f"http://[::1%25{S}]:8645/v1", f"http://127.0.0.1/v1\ntoken={S}",
    f"http://127.0.0.1/v1\t?token={S}", f"http://127.0.0.1:86\n45/v1?{S}", "http://\t@127.0.0.1/",
    "http://127.0.0.1/v1 x", f"http://127.0.0.1/v1\x00{S}",
    # refused before, refused now
    "", "   ", None, 5, "127.0.0.1:8645", "//127.0.0.1/", "http://localhost:8645/v1", "http://127.0.0.1:99999/",
    "ftp://127.0.0.1/", "http:///v1", "http://10.0.0.1/", "http://user@127.0.0.1/", "http://127.0.0.1\uff03x/",
    f"http://user:{S}@127.0.0.1/v1", f"http://{S}@127.0.0.1/v1", f"http://:{S}@127.0.0.1/v1",
    f"http://user%3A{S}@127.0.0.1/v1", f"http://user:{S}%40x@127.0.0.1/v1", f"http://user:{S}@@127.0.0.1/v1",
    f"http://user@{S}@127.0.0.1/v1", f"http://user:pa/{S}@127.0.0.1/v1", f"http://user:pa?{S}@127.0.0.1/v1",
    f"http://user:pa#{S}@127.0.0.1/v1", f"http://user:{S}\uff20127.0.0.1/v1", f"http://user\uff1a{S}@127.0.0.1/v1",
    f"http://user:pa\uff03{S}@127.0.0.1/v1", f"http://user:{S}\u2100@127.0.0.1/v1", f"http://127.0.0.1\uff03{S}/v1",
    f"http://[user:{S}@::1]/v1", f"http://[::1]{S}:80/v1", f"http://[::1:{S}/v1", f"user:{S}@127.0.0.1:8645",
    f"//user:{S}@127.0.0.1/v1", f"socks5h://user:{S}@127.0.0.1:1080", f"HTTP://USER:{S}@127.0.0.1/",
    f"http:/user:{S}@127.0.0.1/", f"http:user:{S}@127.0.0.1/", f"http://user:{S}\n@127.0.0.1/",
    f"\x01http://user:{S}@127.0.0.1/", f"ht\ttp://user:{S}@127.0.0.1/", f"http://us\ter:{S}@127.0.0.1/",
    f"http://127.0.0.1:{S}/v1", f"http://127.0.0.1 {S}/v1", f"http://{S}.127.0.0.1/v1", "http://user:@127.0.0.1/",
    f"http://admin:12?{S}@127.0.0.1", f"http://user:{S}@/v1",
)


# A secret written in plain letters where a host or a plain path stands is shown: nothing tells it from a
# name. These rows hold the verdicts to the same rule and pin that the miss is still the one named.
PLAIN_PARTS = frozenset({f"http://127.0.0.1:8645/{S}/v1", f"http://{S}.127.0.0.1/v1"})


def _generated() -> list[str]:
    """Addresses put together from parts, the made-up secret in a part that is never shown."""

    fronts = ("http://", "HTTPS://", "ftp://", "//", "")
    sign_ins = ("", "@", ":@", f"u:{S}@", f"{S}@", f":{S}@", f"u:pa/{S}@", f"u:pa?{S}@", f"u%40x:{S}@", "\t@")
    hosts = ("127.0.0.1", "[::1]", "[::ffff:127.0.0.1]", "10.0.0.1", "localhost", "")
    ports = ("", ":8645", f":{S}", ":65536")
    tails = ("", "/v1", f"/v1?token={S}", f"/v1#{S}", f"/a@{S}", f"/v1\n{S}", f"/v1\t?{S}", f"/v1;k={S}")
    return [a + b + c + d + e for a, b, c, d, e in itertools.product(fronts, sign_ins, hosts, ports, tails)]


def _route(base_url: object) -> tuple[str, object]:
    try:
        decision = check_nous_own_loop(
            {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy", "base_url": base_url},
            BASE, current_user="u", interactive_users=["u"])
    except OwnLoopRouteRefused as exc:
        return exc.cause, exc
    return "", decision


class SameVerdictsTests(unittest.TestCase):
    def assertSame(self, value: object) -> None:
        before = _verdict_before(value)
        now, what = _route(value)
        with self.subTest(value=value):
            self.assertEqual(now == "", before == "", f"taken before: {before == ''}; now: {now!r}")
            if before == "":
                self.assertEqual(what.base_url, value.strip())  # the route still works with the address as given
            elif before == "recursion_holds_no_token":
                self.assertEqual(now, before)
            record = nous_proxy_doctor(value, current_user="u", interactive_users=["u"])
            self.assertEqual(record["ok"], before == "")
            if now:
                self.assertIsNone(what.__context__)  # no parse error with the address in its text rides along
            if value in PLAIN_PARTS:  # the known miss, named in shown_address's docstring
                self.assertIn(S, record["address"])
                return
            if now:
                self.assertNotIn(S, str(what))
                self.assertNotIn(S, repr(what.args))
            self.assertNotIn(S, repr(record))

    def test_the_table(self) -> None:
        taken = [value for value in TABLE if _verdict_before(value) == ""]
        self.assertGreaterEqual(len(taken), 20)
        self.assertGreaterEqual(len(TABLE) - len(taken), 40)
        for value in TABLE:
            self.assertSame(value)

    def test_addresses_put_together_from_parts(self) -> None:
        values = _generated()
        self.assertGreater(len(values), 2000)
        self.assertGreater(sum(_verdict_before(v) == "" for v in values), 100)
        for value in values:
            self.assertSame(value)

    def test_every_odd_character_in_every_part(self) -> None:
        for ch in ODD:
            for value in (f"http://127.0.0.1:8645/v1{ch}token={S}", f"http://us{ch}er:{S}@127.0.0.1/v1",
                          f"http://127.0.0.1{ch}{S}:8645/v1", f"ht{ch}tp://127.0.0.1:8645/{S}",
                          f"{ch}http://127.0.0.1:8645/v1{ch}{S}", f"http://{ch}@127.0.0.1:8645/v1"):
                self.assertSame(value)

    def test_an_at_sign_after_the_host_or_an_empty_user_is_taken(self) -> None:
        for value in ("http://127.0.0.1:8645/v1?x=a@b", "http://127.0.0.1:8645/v1#a@b",
                      "http://127.0.0.1:8645/a@b/v1", "http://@127.0.0.1/", "http://:@127.0.0.1/"):
            with self.subTest(value=value):
                self.assertEqual(_route(value)[0], "")
                self.assertFalse(holds_sign_in(value))
                self.assertTrue(nous_proxy_doctor(value)["ok"])

    def test_a_sign_in_in_the_authority_is_refused_for_that_in_true_words(self) -> None:
        for value in (f"http://user:{S}@127.0.0.1/v1", f"ftp://user:{S}@127.0.0.1/v1", f"http://user:{S}@/v1",
                      f"http://{S}@10.1.2.3/v1", f"http://:{S}@[::1]/v1", f"http://user:pa\uff03{S}@127.0.0.1/v1",
                      f"socks5h://user:{S}@127.0.0.1:1080", f"//user:{S}@127.0.0.1/v1",
                      f"http://user:{S}\n@127.0.0.1/"):
            with self.subTest(value=value):
                self.assertTrue(holds_sign_in(value))
                cause, exc = _route(value)
                self.assertEqual(cause, "recursion_holds_no_token")
                self.assertEqual(exc.detail, SIGN_IN_WORDS)
                self.assertEqual(str(exc), f"nous: recursion_holds_no_token ({SIGN_IN_WORDS})")

    def test_a_password_that_holds_a_question_mark_is_still_not_shown(self) -> None:
        # By the address's own grammar the authority ends at the ?, so this is no sign-in to the route; it
        # was refused before (its port does not read) and is refused now, with nothing of it shown.
        value = f"http://user:pa?{S}@127.0.0.1/v1"
        self.assertFalse(holds_sign_in(value))
        cause, exc = _route(value)
        self.assertEqual(cause, "proxy_address_malformed")
        self.assertNotIn("user", str(exc))
        self.assertNotIn(S, str(exc))


if __name__ == "__main__":
    unittest.main()
