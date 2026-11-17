"""Pairing-state models (lane REMOTE): scopes, one-time offers, deadlines, single use, locks and rate limits.

The clock is moved by hand (``support.FakeClock``); nothing waits on the wall clock.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import unittest
from unittest import mock

from recursion_core.remote import pairing
from recursion_core.remote.pairing import (CODE_BYTES, EXPIRED, LOCKED, OFFER_TTL_S, OPEN, REDEEMED, AttemptLimiter,
                                           OfferBook, PairingGrant, PairingRefused, device_label, device_scopes,
                                           pairing_link)
from recursion_core.remote.tests import support


class ScopeTests(unittest.TestCase):
    def test_pairing_never_grants_manage(self) -> None:
        for scopes in (["manage"], ["read", "manage"], ["approve", "run", "manage"]):
            with self.subTest(scopes=scopes), self.assertRaises(PairingRefused) as caught:
                device_scopes(scopes)
            self.assertEqual(caught.exception.code, "scope_refused")

    def test_unknown_empty_repeated_and_string_scopes_are_refused(self) -> None:
        for scopes in (["admin"], [], ["read", "read"], "read", ["Read"], None, 7, [["read"]], [7]):
            with self.subTest(scopes=scopes), self.assertRaises(PairingRefused) as caught:
                device_scopes(scopes)
            self.assertEqual(caught.exception.code, "scope_invalid")

    def test_scopes_come_back_in_one_order(self) -> None:
        self.assertEqual(device_scopes(["stop", "read"]), ("read", "stop"))
        self.assertEqual(device_scopes(("read", "stop")), ("read", "stop"))

    def test_a_label_is_printable_ascii_with_no_control_or_direction_characters(self) -> None:
        self.assertEqual(device_label("Owner's phone"), "Owner's phone")
        for label in ("phone\n", "phone\r\n","", " phone", "phone ", "ph\none", "ph\u202eone", "x" * 65, None):
            with self.subTest(label=label), self.assertRaises(PairingRefused):
                device_label(label)


class OfferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = support.FakeClock()
        self.book = OfferBook(clock=self.clock)

    def refused(self, offer_id, code, *, source="phone") -> str:
        with self.assertRaises(PairingRefused) as caught:
            self.book.redeem(offer_id, code, source=source)
        return caught.exception.code

    def test_an_offer_redeems_once_with_its_own_scopes_and_label(self) -> None:
        offer = self.book.issue(scopes=["read", "stop"], label="phone")
        grant = self.book.redeem(offer.offer_id, offer.code, source="phone")
        self.assertIsInstance(grant, PairingGrant)
        self.assertEqual((grant.scopes, grant.label, grant.offer_id), (("read", "stop"), "phone", offer.offer_id))
        self.assertTrue(grant.device_id.startswith("dev_"))
        self.assertEqual(self.refused(offer.offer_id, offer.code), "pairing_code_used")
        self.assertEqual(self.book.state(offer.offer_id), REDEEMED)

    def test_the_code_is_128_random_bits_and_only_its_hash_is_kept(self) -> None:
        offer = self.book.issue(scopes=["read"], label="phone")
        self.assertEqual(CODE_BYTES, 16)
        self.assertEqual(len(offer.code), 22)  # 16 bytes, URL-safe base64, unpadded
        kept = self.book._offers[offer.offer_id]
        self.assertNotIn(offer.code, repr(vars(kept)))
        self.assertNotIn(offer.code.encode(), b"".join(v for v in vars(kept).values() if isinstance(v, bytes)))

    def test_an_offer_is_expired_at_its_two_minute_deadline_and_not_before(self) -> None:
        self.assertEqual(OFFER_TTL_S, 120.0)
        early, late = self.book.issue(scopes=["read"], label="a"), self.book.issue(scopes=["read"], label="b")
        self.clock.now += OFFER_TTL_S - 0.5  # binary fractions: the sums below are exact
        self.book.redeem(early.offer_id, early.code, source="phone")
        self.clock.now += 0.5
        self.assertEqual(self.book.state(late.offer_id), EXPIRED)
        self.assertEqual(self.refused(late.offer_id, late.code), "pairing_code_expired")

    def test_wrong_tries_do_not_extend_the_deadline(self) -> None:
        offer = self.book.issue(scopes=["read"], label="phone")
        for _ in range(3):
            self.clock.now += 30
            self.assertEqual(self.refused(offer.offer_id, "A" * 22, source=f"s{self.clock.now}"), "pairing_code_invalid")
        self.clock.now += 30
        self.assertEqual(self.refused(offer.offer_id, offer.code), "pairing_code_expired")

    def test_five_wrong_codes_lock_the_offer_for_good(self) -> None:
        offer = self.book.issue(scopes=["read"], label="phone")
        for n in range(5):
            self.assertEqual(self.refused(offer.offer_id, f"wrong{n}", source=f"s{n}"), "pairing_code_invalid")
        self.assertEqual(self.book.state(offer.offer_id), LOCKED)
        self.assertEqual(self.refused(offer.offer_id, offer.code, source="fresh"), "pairing_offer_locked")

    def test_an_unknown_offer_and_a_wrong_code_get_the_same_word(self) -> None:
        offer = self.book.issue(scopes=["read"], label="phone")
        self.assertEqual(self.refused("no-such-offer", offer.code), "pairing_code_invalid")
        self.assertEqual(self.refused(offer.offer_id, offer.code[:-1] + "A" if offer.code[-1] != "A" else "B"),
                         "pairing_code_invalid")
        for code in (None, 7, "", "\u00e9" * 4):
            with self.subTest(code=code):
                self.assertEqual(self.refused(offer.offer_id, code, source=f"s{code!r}"), "pairing_code_invalid")
        self.assertEqual(self.book.state(offer.offer_id), LOCKED)  # five wrong codes, whatever their form
        fresh = self.book.issue(scopes=["read"], label="phone")
        self.assertEqual(self.refused(fresh.offer_id, fresh.code + "x" * 43, source="long"), "pairing_code_invalid")

    def test_a_cancelled_offer_redeems_nothing(self) -> None:
        offer = self.book.issue(scopes=["read"], label="phone")
        self.assertTrue(self.book.cancel(offer.offer_id))
        self.assertFalse(self.book.cancel(offer.offer_id))
        self.assertEqual(self.refused(offer.offer_id, offer.code), "pairing_offer_cancelled")

    def test_an_offer_for_manage_is_never_issued_and_open_offers_are_bounded(self) -> None:
        with self.assertRaises(PairingRefused) as caught:
            self.book.issue(scopes=["read", "manage"], label="phone")
        self.assertEqual(caught.exception.code, "scope_refused")
        for n in range(8):
            self.book.issue(scopes=["read"], label=f"p{n}")
        with self.assertRaises(PairingRefused) as caught:
            self.book.issue(scopes=["read"], label="ninth")
        self.assertEqual(caught.exception.code, "pairing_busy")
        self.clock.now += OFFER_TTL_S  # expired offers no longer count as open
        self.assertEqual(self.book.issue(scopes=["read"], label="later").scopes, ("read",))

    def test_a_redemption_in_the_middle_of_another_waits_and_is_refused(self) -> None:
        done, outcome = threading.Event(), []

        def second() -> None:
            try:
                outcome.append(book.redeem(offer.offer_id, offer.code, source="second"))
            except PairingRefused as exc:
                outcome.append(exc.code)
            finally:
                done.set()

        worker = threading.Thread(target=second)
        armed = [True]

        def random_bytes(n: int) -> bytes:
            if n == 12 and armed[0]:  # the first redemption, after its checks, before it marks the offer
                armed[0] = False
                worker.start()
                self.assertFalse(done.wait(0.2), "a second redemption finished inside the first")  # not yet
            return secrets.token_bytes(n)

        book = OfferBook(clock=self.clock, random_bytes=random_bytes)
        offer = book.issue(scopes=["read"], label="phone")
        grant = book.redeem(offer.offer_id, offer.code, source="first")
        worker.join(support.WATCHDOG_S)
        self.assertIsInstance(grant, PairingGrant)
        self.assertEqual(outcome, ["pairing_code_used"])

    def test_racing_redemptions_of_one_offer_give_exactly_one_grant(self) -> None:
        offer = self.book.issue(scopes=["read", "stop"], label="phone")
        workers = 16
        barrier = threading.Barrier(workers)
        grants, refusals, lock = [], [], threading.Lock()

        def redeem(n: int) -> None:
            barrier.wait(support.WATCHDOG_S)
            try:
                grant = self.book.redeem(offer.offer_id, offer.code, source=f"s{n}")
            except PairingRefused as exc:
                with lock:
                    refusals.append(exc.code)
            else:
                with lock:
                    grants.append(grant)

        threads = [threading.Thread(target=redeem, args=(n,)) for n in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(support.WATCHDOG_S)
        self.assertEqual(len(grants), 1)
        self.assertEqual(refusals, ["pairing_code_used"] * (workers - 1))

    def test_redeeming_carries_no_scope_from_the_redeemer(self) -> None:
        offer = self.book.issue(scopes=["read"], label="phone")
        with self.assertRaises(TypeError):
            self.book.redeem(offer.offer_id, offer.code, source="phone", scopes=["read", "manage"])  # type: ignore[call-arg]
        self.assertEqual(self.book.state(offer.offer_id), OPEN)

    def test_wrong_arguments_get_typed_refusals(self) -> None:
        with self.assertRaises(PairingRefused) as caught:
            self.book.issue(scopes=None, label="phone")  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, "scope_invalid")
        offer = self.book.issue(scopes=["read"], label="phone")
        for source in ([], None, 7):
            with self.subTest(source=source):
                self.assertEqual(self.refused(offer.offer_id, offer.code, source=source), "source_invalid")
        self.assertEqual(self.book.state(offer.offer_id), OPEN)

    def test_the_code_is_compared_in_constant_time(self) -> None:
        offer = self.book.issue(scopes=["read"], label="phone")
        with mock.patch.object(pairing.hmac, "compare_digest", wraps=hmac.compare_digest) as compare:
            self.refused(offer.offer_id, "W" * 22)
            self.book.redeem(offer.offer_id, offer.code, source="phone")
        self.assertEqual(compare.call_count, 2)


class LimiterTests(unittest.TestCase):
    def test_failed_tries_from_one_source_are_limited_in_a_sliding_window(self) -> None:
        clock = support.FakeClock()
        book = OfferBook(clock=clock, limiter=AttemptLimiter(per_source=3, overall=100, window_s=60.0))
        offer = book.issue(scopes=["read"], label="phone")
        for n in range(3):
            with self.assertRaises(PairingRefused):
                book.redeem("missing", "x", source="attacker")
            clock.now += 1
        with self.assertRaises(PairingRefused) as caught:
            book.redeem(offer.offer_id, offer.code, source="attacker")
        self.assertEqual(caught.exception.code, "pairing_rate_limited")
        self.assertEqual(book.state(offer.offer_id), OPEN)  # a refused try leaves the offer as it was
        clock.now += 58  # the oldest failures leave the window; a try is allowed again
        self.assertEqual(book.redeem(offer.offer_id, offer.code, source="attacker").scopes, ("read",))

    def test_all_sources_together_are_limited_too(self) -> None:
        clock = support.FakeClock()
        book = OfferBook(clock=clock, limiter=AttemptLimiter(per_source=5, overall=4, window_s=60.0))
        offer = book.issue(scopes=["read"], label="phone")
        for n in range(4):
            with self.assertRaises(PairingRefused):
                book.redeem("missing", "x", source=f"s{n}")
        with self.assertRaises(PairingRefused) as caught:
            book.redeem(offer.offer_id, offer.code, source="the-real-phone")
        self.assertEqual(caught.exception.code, "pairing_rate_limited")

    def test_the_number_of_sources_tracked_is_bounded(self) -> None:
        limiter = AttemptLimiter(per_source=1, overall=10_000, window_s=60.0, max_sources=3)
        for n in range(10):
            limiter.record_failure(f"s{n}", 1.0)
        self.assertEqual(len(limiter._by_source), 3)
        self.assertFalse(limiter.allow("s9", 1.0))
        self.assertTrue(limiter.allow("s0", 1.0))  # dropped as least recently seen


class PairingLinkTests(unittest.TestCase):
    def test_the_code_rides_in_the_fragment_of_an_https_origin(self) -> None:
        self.assertEqual(pairing_link("https://host.tailnet.example", "off1", "c0de_-"),
                         "https://host.tailnet.example/pair#offer=off1&code=c0de_-")
        self.assertEqual(pairing_link("https://host.tailnet.example:8443/", "off1", "code"),
                         "https://host.tailnet.example:8443/pair#offer=off1&code=code")
        self.assertEqual(pairing_link("https://[2001:db8::1]:8443", "off1", "code"),
                         "https://[2001:db8::1]:8443/pair#offer=off1&code=code")

    def test_anything_but_a_bare_https_origin_is_refused(self) -> None:
        for origin in ("http://host.example", "https://user@host.example", "https://host.example/path",
                       "https://host.example?x=1", "https://host.example#f", "https://", "ftp://host.example",
                       "https://host.example:99999", "https:\\\\host.example", "https://bad host.example",
                       "https://host.exa\nmple", "https://host.example\n", "https://host\t.example",
                       "https://host.example\x7f", "https://[broken", "https://[::g]", "https://[1:2:3]",
                       "https://[::1::2]", "https://host:",
                       "https://host.example:0/", "https://host.example:65536", "https://-host.example",
                       "https://host..example", " https://host.example", None, 7,
                       "https://" + ".".join(["a" * 63] * 4) + ".example"):  # a host longer than 255 characters
            with self.subTest(origin=origin), self.assertRaises(PairingRefused) as caught:
                pairing_link(origin, "off1", "code")
            self.assertEqual(caught.exception.code, "origin_invalid")
        for offer_id, code in (("off 1", "code"), ("off1", "co&de"), ("off1", "")):
            with self.subTest(offer_id=offer_id, code=code), self.assertRaises(PairingRefused):
                pairing_link("https://host.example", offer_id, code)


if __name__ == "__main__":
    unittest.main()
