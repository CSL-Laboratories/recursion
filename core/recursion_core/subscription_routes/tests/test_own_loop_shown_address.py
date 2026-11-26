"""The Nous own-loop route shows a proxy address only in its shown form (recursion_core.shown_address).

Made-up values only: ``SECRETSTANDIN`` stands where a password or a token would. No engine, login store,
provider, DNS or network is touched.
"""

from __future__ import annotations

import unittest

from recursion_core.subscription_routes import (
    OwnLoopRouteRefused, check_copilot_own_loop, check_nous_own_loop, nous_proxy_doctor,
)

S = "SECRETSTANDIN"
BASE = {"PATH": "/usr/bin:/bin", "HOME": "/home/u", "LANG": "C.UTF-8"}
SIGN_INS = (f"ftp://user:{S}@127.0.0.1/v1", f"http://user:{S}@/v1", f"http://user:{S}@127.0.0.1/v1")


def nous(base_url: object) -> dict:
    return {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy", "base_url": base_url}


class RefusalTests(unittest.TestCase):
    def refusal(self, base_url: object) -> OwnLoopRouteRefused:
        with self.assertRaises(OwnLoopRouteRefused) as ctx:
            check_nous_own_loop(nous(base_url), BASE)
        return ctx.exception

    def assertHidden(self, exc: OwnLoopRouteRefused) -> None:
        for text in (str(exc), exc.detail, repr(exc), repr(exc.args)):
            self.assertNotIn(S, text)

    def test_a_sign_in_is_looked_for_first_and_never_shown(self) -> None:
        # An at sign after the host is no sign-in: such an address is taken as before (test_own_loop_same_verdicts).
        for url in SIGN_INS + (f"http://user:pa\uff03{S}@127.0.0.1/v1", f"http://user:{S}@10.1.2.3/v1"):
            with self.subTest(url=url):
                exc = self.refusal(url)
                self.assertEqual(exc.cause, "recursion_holds_no_token")
                self.assertHidden(exc)
                self.assertIn("no user name or password", exc.detail)

    def test_a_malformed_address_is_described_not_shown(self) -> None:
        cases = {
            f"ftp://127.0.0.1/{S}": "a scheme that is not http or https",
            f"http:///{S}": "no host",
            f"http://127.0.0.1\uff03{S}/v1": "NFKC normalization",
            f"http://127.0.0.1:{S}/v1": "a port that is not a number",
            f"//127.0.0.1/{S}": "no scheme, which must be http or https",
        }
        for url, words in cases.items():
            with self.subTest(url=url):
                exc = self.refusal(url)
                self.assertEqual(exc.cause, "proxy_address_malformed")
                self.assertHidden(exc)
                self.assertIn(words, exc.detail)
                self.assertIn("the address is not shown", str(exc))

    def test_a_host_that_is_not_loopback_is_shown_without_its_query(self) -> None:
        for url, host in ((f"http://example.invalid:8645/v1?token={S}", "example.invalid:8645"),
                          (f"http://10.1.2.3:8645/v1#{S}", "10.1.2.3:8645")):
            with self.subTest(url=url):
                exc = self.refusal(url)
                self.assertEqual(exc.cause, "proxy_not_loopback")
                self.assertHidden(exc)
                self.assertIn(host, exc.detail)


class DoctorTests(unittest.TestCase):
    def test_a_refused_address_is_reported_in_its_shown_form(self) -> None:
        want = {
            SIGN_INS[0]: "<an address with a scheme that is not http or https; the address is not shown>",
            SIGN_INS[1]: "<an address with no host; the address is not shown>",
            SIGN_INS[2]: "http://127.0.0.1 <sign-in hidden> <path not shown>",
        }
        for url, address in want.items():
            with self.subTest(url=url):
                record = nous_proxy_doctor(url, current_user="u", interactive_users=["u"])
                self.assertNotIn(S, repr(record))
                self.assertEqual((record["ok"], record["address"], record["cause"]),
                                 (False, address, "recursion_holds_no_token"))

    def test_a_query_is_not_shown_on_success(self) -> None:
        record = nous_proxy_doctor(f"http://127.0.0.1:8645/v1?token={S}", current_user="u", interactive_users=["u"])
        self.assertNotIn(S, repr(record))
        self.assertEqual(record, {"ok": True, "address": "http://127.0.0.1:8645/v1 <query not shown>", "cause": "",
                                  "warnings": ()})

    def test_a_taken_address_is_shown_built_from_harmless_parts(self) -> None:
        odd = "<an address with white space or a character that cannot be printed inside it; the address is not shown>"
        want = {
            f"http://127.0.0.1:8645/v1\ntoken={S}": odd,
            f"http://127.0.0.1:8645/v1\ttoken={S}": odd,
            f"http://127.0.0.1:8645/v1\r{S}": odd,
            f"http://127.0.0.1:8645/v1;token={S}": "http://127.0.0.1:8645/v1 <path parameters not shown>",
            f"http://127.0.0.1:8645/v1%3Ftoken={S}": "http://127.0.0.1:8645 <path not shown>",
            f"http://[::1%25{S}]:8645/v1": "http://[::1]:8645/v1 <zone id not shown>",
            f"http://127.0.0.1:8645/v1?x=a@{S}":
                "<an address with an at sign after its host, which may end a sign-in; the address is not shown>",
        }
        for url, address in want.items():
            with self.subTest(url=url):
                record = nous_proxy_doctor(url, current_user="u", interactive_users=["u"])
                self.assertNotIn(S, repr(record))
                self.assertEqual((record["ok"], record["address"]), (True, address))
                decision = check_nous_own_loop(nous(url), BASE)
                self.assertEqual(decision.base_url, url)  # the value worked with, never shown
                self.assertNotIn(S, repr(decision))
                self.assertIn("base_url=", repr(decision))
                self.assertNotIn("/v1", repr(decision))  # a decision's repr shows no path

    def test_no_text_and_white_space_report_no_address(self) -> None:
        self.assertEqual(nous_proxy_doctor(None)["address"], "")
        self.assertEqual(nous_proxy_doctor("  ")["address"], "")


class DecisionReprTests(unittest.TestCase):
    def test_the_address_is_shown_and_the_value_kept(self) -> None:
        url = f"http://127.0.0.1:8645/v1?token={S}"
        decision = check_nous_own_loop(nous(url), BASE, current_user="u", interactive_users=["u"])
        self.assertEqual(decision.base_url, url)  # the route still reaches the address as given
        self.assertNotIn(S, repr(decision))
        self.assertIn("http://127.0.0.1:8645 <path not shown> <query not shown>", repr(decision))

    def test_the_child_env_is_shown_by_its_names(self) -> None:
        env = dict(BASE, PATH=f"/opt/{S}/bin")
        for decision in (check_nous_own_loop(nous("http://127.0.0.1:8645/v1"), env),
                         check_copilot_own_loop({"engine": "copilot", "auth": "subscription",
                                                 "method": "copilot_cli_sign_in"}, env,
                                                has_copilot_login=lambda: True)):
            with self.subTest(engine=decision.engine):
                self.assertEqual(decision.child_env["PATH"], f"/opt/{S}/bin")
                self.assertNotIn(S, repr(decision))
                self.assertIn("PATH", repr(decision))


if __name__ == "__main__":
    unittest.main()
