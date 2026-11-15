"""Lane QUOTA, slice 1: the dollar reservation at the breaker's admission (F9; grill Q44, O59; P10).

Every store here is lane SPINE's one shared fake of the event store's transaction helper (``FakeStore`` in
``control/tests/test_seams.py``), with QUOTA's tables declared as composition declares them. KEYS' spend policy
and the ledger's reads are fakes with the published signatures. No key, keychain or engine is touched: every
key id and cap is made up.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from recursion_core.control.tests.test_seams import FakeStore
from recursion_core.quota import reservation as R
from recursion_core.quota import tables
from recursion_core.routes import contract

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO = Path(__file__).resolve().parents[4]
BOUND_S = 30.0  # only ends a wait that would otherwise never end; it never decides a pass
CODE_WORD = re.compile(r"[a-z][a-z0-9_]{0,63}")
RUN_A = "run-" + "a1" * 16
RUN_B = "run-" + "b2" * 16
RUN_C = "run-" + "c3" * 16
SUB = "subscription:login-A"
KEY = "api_key:key-A"
KEY_B = "api_key:key-B"


def route(kind: str = "subscription", conn_id: str = "login-A", engine: str = "claude_code",
          model: str = "claude-sonnet-5-5", **extra) -> dict:
    conn = {"kind": kind, "id": conn_id}
    if kind == "api_key":
        conn["credential_env"] = "ANTHROPIC_API_KEY"
    if kind == "passthrough":
        conn["method"] = "bedrock"
    out = {"engine": engine, "model": model, "connection": conn}
    if engine == "claude_code" and kind == "subscription":
        out["api_key_helper"] = "absent"
    out.update(extra)
    return out


def key_route(conn_id: str = "key-A", **extra) -> dict:
    return route("api_key", conn_id, **extra)


class Policies:
    """KEYS' ``spend_policy(connection_id)``, faked. An id it keeps no key for raises KeyError, as KEYS does. Each
    call records how many transactions were open on the store at that moment."""

    def __init__(self, store: FakeStore, **policies) -> None:
        self.store, self.policies, self.calls = store, dict(policies), []

    def __call__(self, connection_id: str):
        self.calls.append((connection_id, self.store.open_transactions))
        return self.policies[connection_id]


def opted_in(cap="5") -> dict:
    return {"metered_spend_allowed": True, "cap_usd": cap if not isinstance(cap, str) else Decimal(cap)}


class Reads:
    """The ledger's two reads, faked: a spend and a paid-fallback state per connection."""

    def __init__(self, spent: dict | None = None, paid: dict | None = None) -> None:
        self.spent_by, self.paid_by, self.calls = dict(spent or {}), dict(paid or {}), []

    def spent(self, tx, connection_id):
        self.calls.append(("spent", connection_id, tx))
        return self.spent_by.get(connection_id, R.UNKNOWN_SPEND)

    def paid_fallback_active(self, tx, connection_id):
        self.calls.append(("paid", connection_id, tx))
        return self.paid_by.get(connection_id)


def declared(**options) -> FakeStore:
    store = FakeStore(**options)
    tables.declare(store)
    return store


def rows(store: FakeStore) -> list[dict]:
    """Every QUOTA row in the order written, read outside every transaction."""
    return [dict(value, row_id=key[1]) for key, value in sorted(store.rows.items()) if key[0] == "quota_reservation"]


def held(store: FakeStore) -> list[dict]:
    released = {row["reservation_id"] for row in rows(store) if row["state"] == "released"}
    return [row for row in rows(store) if row["state"] == "held" and row["reservation_id"] not in released]


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.store = declared()
        self.keys = Policies(self.store, **{"key-A": opted_in("5"), "key-B": opted_in("5")})
        self.reads = Reads(spent={KEY: R.known_spend(2), KEY_B: R.known_spend(0)})
        self.res = R.Reservations(self.store, self.keys, reads=self.reads)

    def assertRefused(self, answer: dict, reason: str) -> None:
        self.assertEqual((answer["allowed"], answer["code"], answer["reservation_id"], answer["reason"]),
                         (False, "reservation_refused", None, reason))


class AdmissionTest(Case):
    def test_a_subscription_route_is_admitted_with_a_zero_hold(self):
        answer = self.res.admit(SUB, route(), RUN_A)
        self.assertEqual((answer["allowed"], answer["code"], answer["reason"], answer["metered"]),
                         (True, None, "no_spend", False))
        self.assertRegex(answer["reservation_id"], r"^rsv-[0-9a-f]{32}$")
        [row] = rows(self.store)
        self.assertEqual({k: row[k] for k in ("reservation_id", "run_id", "connection_id", "state", "metered", "basis",
                                              "amount_usd", "cap_usd", "spent_usd")},
                         {"reservation_id": answer["reservation_id"], "run_id": RUN_A, "connection_id": SUB,
                          "state": "held", "metered": 0, "basis": None, "amount_usd": "0", "cap_usd": None,
                          "spent_usd": None})
        self.assertNotIn("spent", [call[0] for call in self.reads.calls])  # a free route reads no spend

    def test_a_key_route_with_no_opt_in_is_refused(self):
        self.keys.policies["key-A"] = {"metered_spend_allowed": False, "cap_usd": 5}
        self.assertRefused(self.res.admit(KEY, key_route(), RUN_A), "no_opt_in")
        del self.keys.policies["key-A"]  # KEYS keeps no key by that id: nobody opted in
        self.assertRefused(self.res.admit(KEY, key_route(), RUN_B), "no_opt_in")
        self.keys.policies["key-A"] = {"cap_usd": 5}  # no opt-in field at all
        self.assertRefused(self.res.admit(KEY, key_route(), RUN_C), "no_opt_in")
        self.assertEqual(held(self.store), [])
        self.assertEqual([row["state"] for row in rows(self.store)], ["refused"] * 3)

    def test_an_absent_cap_is_zero(self):
        for run_id, policy in ((RUN_A, {"metered_spend_allowed": True}),
                               (RUN_B, {"metered_spend_allowed": True, "cap_usd": None}),
                               (RUN_C, {"metered_spend_allowed": True, "cap_usd": 0})):
            with self.subTest(policy=policy):
                self.keys.policies["key-A"] = policy
                self.assertRefused(self.res.admit(KEY, key_route(), run_id), "cap_zero")
        self.assertEqual(held(self.store), [])

    def test_a_key_route_under_its_cap_holds_the_headroom(self):
        answer = self.res.admit(KEY, key_route(), RUN_A)
        self.assertEqual((answer["allowed"], answer["reason"], answer["metered"]), (True, "within_cap", True))
        [row] = rows(self.store)
        self.assertEqual({k: row[k] for k in ("state", "metered", "basis", "amount_usd", "cap_usd", "spent_usd",
                                              "spent_quality", "spent_provenance")},
                         {"state": "held", "metered": 1, "basis": "api_key", "amount_usd": "3", "cap_usd": "5",
                          "spent_usd": "2", "spent_quality": "known", "spent_provenance": "observed"})

    def test_a_key_route_at_or_past_its_cap_is_refused(self):
        for run_id, spent in ((RUN_A, R.known_spend(5)), (RUN_B, R.known_spend(Decimal("6.5"))),
                              (RUN_C, R.lower_bound_spend(5))):
            with self.subTest(spent=spent):
                self.reads.spent_by[KEY] = spent
                self.assertRefused(self.res.admit(KEY, key_route(), run_id), "cap_reached")
        self.assertEqual(held(self.store), [])

    def test_unknown_spend_refuses_and_is_kept_null_not_zero(self):
        self.reads.spent_by[KEY] = R.UNKNOWN_SPEND
        self.assertRefused(self.res.admit(KEY, key_route(), RUN_A), "spend_unknown")
        self.reads.spent_by[KEY] = R.lower_bound_spend(2, "modelled")  # at least 2: under 5 is not shown
        self.assertRefused(self.res.admit(KEY, key_route(), RUN_B), "spend_unknown")
        first, second = rows(self.store)
        self.assertEqual((first["spent_usd"], first["spent_quality"], first["spent_provenance"]),
                         (None, "unknown", "unavailable"))
        self.assertEqual((second["spent_usd"], second["spent_quality"], second["spent_provenance"]),
                         ("2", "lower_bound", "modelled"))

    def test_before_the_ledger_every_spend_is_unknown(self):
        res = R.Reservations(self.store, self.keys)  # no reads: what slice 1 has until the ledger lands
        self.assertRefused(res.admit(KEY, key_route(), RUN_A), "spend_unknown")
        self.assertTrue(res.admit(SUB, route(), RUN_B)["allowed"])

    def test_a_modelled_cost_that_is_known_counts(self):
        self.reads.spent_by[KEY] = R.known_spend(Decimal("4.25"), "modelled")
        answer = self.res.admit(KEY, key_route(), RUN_A)
        self.assertTrue(answer["allowed"])
        self.assertEqual((rows(self.store)[0]["amount_usd"], rows(self.store)[0]["spent_provenance"]),
                         ("0.75", "modelled"))

    def test_two_runs_on_one_paid_connection_count_against_one_meter(self):
        first = self.res.admit(KEY, key_route(), RUN_A)
        self.assertTrue(first["allowed"])  # holds the 3 left under the cap of 5
        self.assertRefused(self.res.admit(KEY, key_route(), RUN_B), "cap_held")
        other = self.res.admit(KEY_B, key_route("key-B"), RUN_C)  # another key is another meter
        self.assertTrue(other["allowed"])
        self.assertEqual(self.res.release(RUN_A), [first["reservation_id"]])
        again = self.res.admit(KEY, key_route(), RUN_B)
        self.assertTrue(again["allowed"])
        self.assertEqual([row["amount_usd"] for row in held(self.store)], ["5", "3"])

    def test_a_keys_consent_covers_no_other_kind_with_the_same_id(self):
        # Review R4 (gpt-6.1-sol) / R3 (gpt-6-astra): key-A's opt-in is a stored key's, never a subscription's or a
        # cloud's that happens to share the id; those meters bill other funding sources.
        self.reads.spent_by["subscription:key-A"] = R.known_spend(0)
        self.reads.spent_by["passthrough:key-A"] = R.known_spend(0)
        self.reads.paid_by["subscription:key-A"] = True
        self.assertRefused(self.res.admit("subscription:key-A", route(conn_id="key-A"), RUN_A),
                           "consent_unsupported")
        self.assertRefused(self.res.admit("passthrough:key-A", route("passthrough", "key-A"), RUN_B),
                           "consent_unsupported")
        self.assertEqual(self.keys.calls, [])  # KEYS is asked about its own keys only
        self.assertTrue(self.res.admit(KEY, key_route(), RUN_C)["allowed"])
        self.assertEqual(self.keys.calls, [("key-A", 0)])

    def test_a_retried_start_reuses_its_reservation(self):
        for connection, made, run_id in ((SUB, route(), RUN_A), (KEY, key_route(), RUN_B)):
            with self.subTest(connection=connection):
                first = self.res.admit(connection, made, run_id)
                count = len(rows(self.store))
                again = self.res.admit(connection, made, run_id)
                self.assertTrue(again["allowed"])
                self.assertEqual(again["reservation_id"], first["reservation_id"])
                self.assertEqual(len(rows(self.store)), count)  # no second reservation for one run

    def test_a_retried_start_is_checked_again(self):
        first = self.res.admit(KEY, key_route(), RUN_A)
        self.keys.policies["key-A"] = {"metered_spend_allowed": False, "cap_usd": 5}  # the user took the opt-in back
        self.assertRefused(self.res.admit(KEY, key_route(), RUN_A), "no_opt_in")
        self.assertEqual(held(self.store), [])
        self.assertEqual([(row["reservation_id"] == first["reservation_id"], row["state"], row["reason"])
                          for row in rows(self.store)],
                         [(True, "held", "within_cap"), (True, "released", "refused_on_retry"),
                          (False, "refused", "no_opt_in")])

    def test_a_run_admitted_on_another_connection_lets_the_first_go(self):
        first = self.res.admit(SUB, route(), RUN_A)
        second = self.res.admit(KEY, key_route(), RUN_A)
        self.assertTrue(second["allowed"])
        self.assertNotEqual(first["reservation_id"], second["reservation_id"])
        self.assertEqual([(row["reservation_id"], row["state"], row["reason"]) for row in rows(self.store)],
                         [(first["reservation_id"], "held", "no_spend"),
                          (first["reservation_id"], "released", "superseded"),
                          (second["reservation_id"], "held", "within_cap")])

    def test_release_ends_the_hold_once(self):
        answer = self.res.admit(KEY, key_route(), RUN_A)
        self.assertEqual(self.res.release(RUN_A), [answer["reservation_id"]])
        count = len(rows(self.store))
        self.assertEqual(self.res.release(RUN_A), [])
        self.assertEqual(self.res.release(RUN_B), [])  # a run that never held anything
        self.assertEqual(self.res.release("not a run id"), [])
        self.assertEqual(len(rows(self.store)), count)
        last = rows(self.store)[-1]
        self.assertEqual((last["state"], last["reason"], last["amount_usd"]), ("released", "run_ended", "3"))

    def test_rows_are_only_appended(self):
        seen = []
        steps = (lambda: self.res.admit(SUB, route(), RUN_A), lambda: self.res.admit(KEY, key_route(), RUN_B),
                 lambda: self.res.admit(KEY, key_route(), RUN_C), lambda: self.res.release(RUN_B),
                 lambda: self.res.admit(KEY, key_route(), RUN_A), lambda: self.res.release(RUN_A))
        for step in steps:
            step()
            now = rows(self.store)
            self.assertEqual(now[:len(seen)], seen)  # nothing written before is changed or gone
            self.assertGreater(len(now), len(seen))
            seen = now

    def test_keys_is_read_outside_the_transaction_and_spend_inside(self):
        self.res.admit(KEY, key_route(), RUN_A)
        self.assertEqual(self.keys.calls, [("key-A", 0)])
        spent_calls = [call for call in self.reads.calls if call[0] == "spent"]
        self.assertEqual(len(spent_calls), 1)
        self.assertIsNotNone(spent_calls[0][2])  # the admission's own transaction object

    def test_keys_failing_refuses_a_paid_route_only(self):
        def broken(connection_id):
            raise RuntimeError("keyring locked")

        res = R.Reservations(self.store, broken, reads=self.reads)
        with self.assertLogs("recursion_core.quota.reservation", "ERROR"):
            self.assertRefused(res.admit(KEY, key_route(), RUN_A), "policy_unavailable")
        self.assertTrue(res.admit(SUB, route(), RUN_B)["allowed"])  # a subscription never asks KEYS
        no_keys = R.Reservations(self.store, None, reads=self.reads)
        self.assertRefused(no_keys.admit(KEY, key_route(), RUN_C), "policy_unavailable")

    def test_a_policy_that_is_not_typed_refuses(self):
        cases = (None, [], "yes", {"metered_spend_allowed": "yes", "cap_usd": 5},
                 {"metered_spend_allowed": 1, "cap_usd": 5}, {"metered_spend_allowed": True, "cap_usd": -1},
                 {"metered_spend_allowed": True, "cap_usd": float("nan")},
                 {"metered_spend_allowed": True, "cap_usd": float("inf")},
                 {"metered_spend_allowed": True, "cap_usd": True}, {"metered_spend_allowed": True, "cap_usd": "5"})
        for number, policy in enumerate(cases):
            with self.subTest(policy=policy):
                self.keys.policies["key-A"] = policy
                self.assertRefused(self.res.admit(KEY, key_route(), f"run-p{number}"), "policy_invalid")

    def test_what_can_spend_money(self):
        self.assertRefused(self.res.admit("passthrough:cloud-A", route("passthrough", "cloud-A"), RUN_A),
                           "consent_unsupported")
        billing = route(api_key_helper="present", billing_ack="api_key_helper")
        self.assertRefused(self.res.admit(SUB, billing, RUN_B), "consent_unsupported")
        self.assertEqual(rows(self.store)[-1]["basis"], "api_key_helper_billing")
        self.assertEqual(self.keys.calls, [])
        asks = route(api_key_helper="present")  # not acknowledged: the adapter asks, nothing is billed
        self.assertEqual(self.res.admit(SUB, asks, RUN_C)["reason"], "no_spend")
        ack_without_helper = route(billing_ack="api_key_helper")
        self.assertEqual(self.res.admit(SUB, ack_without_helper, "run-d4")["reason"], "no_spend")

    def test_a_subscription_on_its_plans_paid_fallback_is_refused_until_a_consent_exists(self):
        self.keys.policies["login-A"] = opted_in("10")  # a key policy by the same id does not count (R4/R3)
        self.reads.spent_by[SUB] = R.known_spend(1)
        self.reads.paid_by[SUB] = True
        self.assertRefused(self.res.admit(SUB, route(), RUN_A), "consent_unsupported")
        last = rows(self.store)[-1]
        self.assertEqual((last["basis"], last["paid_fallback"]), ("plan_paid_fallback", "active"))
        self.reads.paid_by[SUB] = False  # a typed reading of free billing: the way out
        self.assertEqual(self.res.admit(SUB, route(), RUN_B)["reason"], "no_spend")
        self.assertEqual(rows(self.store)[-1]["paid_fallback"], "inactive")
        self.assertEqual(self.keys.calls, [])

    def test_no_reading_keeps_the_last_typed_one(self):
        # Review R2 (both): a missing reading must not erase a known paid fallback.
        self.assertEqual(self.res.admit(SUB, route(), RUN_A)["reason"], "no_spend")  # cold: free, as documented
        self.reads.paid_by[SUB] = True
        self.assertRefused(self.res.admit(SUB, route(), RUN_B), "consent_unsupported")
        self.reads.paid_by[SUB] = None
        self.assertRefused(self.res.admit(SUB, route(), RUN_C), "consent_unsupported")
        self.reads.paid_by[SUB] = False
        self.assertEqual(self.res.admit(SUB, route(), "run-d4")["reason"], "no_spend")
        self.reads.paid_by[SUB] = None
        self.assertEqual(self.res.admit(SUB, route(), "run-e5")["reason"], "no_spend")
        self.assertEqual(self.res.admit("subscription:login-B", route(conn_id="login-B"), "run-f6")["reason"],
                         "no_spend")  # another login's reading is not this one's

    def test_a_failed_or_untyped_reading_refuses(self):
        # Review R2 (both): a failure, or an answer that is neither True, False nor None, is not free billing.
        class Failing(Reads):
            def paid_fallback_active(self, tx, connection_id):
                raise RuntimeError("meters down")

        failing = R.Reservations(self.store, self.keys, reads=Failing())
        with self.assertLogs("recursion_core.quota.reservation", "ERROR"):
            self.assertRefused(failing.admit(SUB, route(), RUN_A), "paid_fallback_unknown")
        for number, odd in enumerate(("true", 1, "false", 0)):
            with self.subTest(reading=odd):
                self.reads.paid_by[SUB] = odd
                self.assertRefused(self.res.admit(SUB, route(), f"run-u{number}"), "paid_fallback_unknown")
        self.assertEqual(held(self.store), [])

    def test_a_paid_retry_whose_reading_is_lost_keeps_its_policy(self):
        # Review R2 (gpt-6-astra): a run held as metered on a subscription keeps being checked as paid.
        self.hold_directly(RUN_A, SUB, metered=True, amount="4", basis="plan_paid_fallback", paid="active")
        self.reads.paid_by[SUB] = None
        self.assertRefused(self.res.admit(SUB, route(), RUN_A), "consent_unsupported")
        self.assertEqual(held(self.store), [])
        self.assertEqual(rows(self.store)[-2]["reason"], "refused_on_retry")

    def test_a_paid_retry_whose_meter_fails_is_refused_and_lets_its_hold_go(self):
        # Review R2 (gpt-6-astra), its second case: a failing meter on a held paid run.
        self.hold_directly(RUN_A, SUB, metered=True, amount="4", basis="plan_paid_fallback", paid="active")

        class Failing(Reads):
            def paid_fallback_active(self, tx, connection_id):
                raise RuntimeError("meters down")

        with self.assertLogs("recursion_core.quota.reservation", "ERROR"):
            self.assertRefused(R.Reservations(self.store, self.keys, reads=Failing()).admit(SUB, route(), RUN_A),
                               "paid_fallback_unknown")
        self.assertEqual(held(self.store), [])

    def test_a_paid_retry_that_is_now_free_lets_its_money_go(self):
        # Review R3 (gpt-6.1-sol): a retry that spends nothing must not keep a money hold.
        for run_id, basis, paid, reading in ((RUN_A, "plan_paid_fallback", "active", False),
                                             (RUN_B, "api_key_helper_billing", None, None)):
            with self.subTest(basis=basis):
                old = self.hold_directly(run_id, SUB, metered=True, amount="4", basis=basis, paid=paid)
                self.reads.paid_by[SUB] = reading
                answer = self.res.admit(SUB, route(), run_id)
                self.assertEqual((answer["allowed"], answer["reason"], answer["metered"]), (True, "no_spend", False))
                self.assertNotEqual(answer["reservation_id"], old)
                [now] = [row for row in held(self.store) if row["run_id"] == run_id]
                self.assertEqual((now["metered"], now["amount_usd"]), (0, "0"))
                self.assertIn((old, "released", "superseded"),
                              [(row["reservation_id"], row["state"], row["reason"]) for row in rows(self.store)])

    def test_a_free_retry_that_now_spends_replaces_its_zero_hold(self):
        # Review M07 (gpt-6-astra) / M08 (gpt-6.1-sol): one current hold, never a zero hold kept for a paid attempt.
        old = self.hold_directly(RUN_A, KEY, metered=False, amount="0", basis=None, paid=None)
        answer = self.res.admit(KEY, key_route(), RUN_A)
        self.assertEqual((answer["allowed"], answer["metered"]), (True, True))
        self.assertNotEqual(answer["reservation_id"], old)
        [now] = held(self.store)
        self.assertEqual((now["metered"], now["amount_usd"]), (1, "3"))

    def test_a_retry_after_the_cap_was_cut_holds_only_what_is_left(self):
        # Review R4 (gpt-6-astra): an old hold larger than the headroom the cap now leaves is not reused.
        first = self.res.admit(KEY, key_route(), RUN_A)  # cap 5, spent 2: holds 3
        self.keys.policies["key-A"] = opted_in("3")
        again = self.res.admit(KEY, key_route(), RUN_A)
        self.assertTrue(again["allowed"])
        self.assertNotEqual(again["reservation_id"], first["reservation_id"])
        [now] = held(self.store)
        self.assertEqual((now["amount_usd"], now["cap_usd"]), ("1", "3"))
        same = self.res.admit(KEY, key_route(), RUN_A)  # nothing changed: the same hold
        self.assertEqual(same["reservation_id"], again["reservation_id"])
        self.keys.policies["key-A"] = opted_in("2")  # spent 2 reaches a cap of 2: refused, and the hold goes
        self.assertRefused(self.res.admit(KEY, key_route(), RUN_A), "cap_reached")
        self.assertEqual(held(self.store), [])

    def test_a_raised_cap_opens_headroom_for_another_run(self):
        first = self.res.admit(KEY, key_route(), RUN_A)  # cap 5, spent 2: holds 3
        self.keys.policies["key-A"] = opted_in("10")
        self.assertEqual(self.res.admit(KEY, key_route(), RUN_A)["reservation_id"], first["reservation_id"])
        second = self.res.admit(KEY, key_route(), RUN_B)  # 10 - 2 - 3 = 5 left
        self.assertTrue(second["allowed"])
        self.assertEqual(sorted(row["amount_usd"] for row in held(self.store)), ["3", "5"])

    def hold_directly(self, run_id, connection, *, metered, amount, basis, paid):
        """A held row as an earlier admission would have written it (for the retry transitions)."""
        reservation_id = "rsv-" + "f" * 24 + run_id[-8:]
        with self.store.transaction() as tx:
            tx.execute("INSERT INTO quota_reservation (reservation_id, run_id, connection_id, engine, model, state, "
                       "metered, basis, reason, amount_usd, cap_usd, paid_fallback) "
                       "VALUES (?, ?, ?, 'claude_code', 'claude-sonnet-5-5', 'held', ?, ?, ?, ?, ?, ?)",
                       (reservation_id, run_id, connection, 1 if metered else 0, basis,
                        "within_cap" if metered else "no_spend", amount, "5" if metered else None, paid))
        return reservation_id

    def test_a_reader_that_fails_knows_nothing(self):
        class Broken(Reads):
            def spent(self, tx, connection_id):
                raise RuntimeError("ledger down")

            def paid_fallback_active(self, tx, connection_id):
                raise RuntimeError("meters down")

        res = R.Reservations(self.store, self.keys, reads=Broken())
        with self.assertLogs("recursion_core.quota.reservation", "ERROR"):
            self.assertRefused(res.admit(KEY, key_route(), RUN_A), "spend_unknown")
        with self.assertLogs("recursion_core.quota.reservation", "ERROR"):
            self.assertRefused(res.admit(SUB, route(), RUN_B), "paid_fallback_unknown")
        odd = Reads(spent={KEY: 2.0})  # not a Spend: unknown
        self.assertRefused(R.Reservations(self.store, self.keys, reads=odd).admit(KEY, key_route(), RUN_C),
                           "spend_unknown")

    def test_a_store_that_cannot_commit_admits_nothing(self):
        self.store.fail_commit = True
        with self.assertLogs("recursion_core.quota.reservation", "ERROR"):
            self.assertRefused(self.res.admit(SUB, route(), RUN_A), "store_unavailable")
        with self.assertLogs("recursion_core.quota.reservation", "ERROR"):
            self.assertRefused(self.res.admit(KEY, key_route(), RUN_B), "store_unavailable")
        self.store.fail_commit = False
        self.assertEqual(rows(self.store), [])
        self.res.admit(KEY, key_route(), RUN_B)
        self.store.fail_commit = True
        with self.assertLogs("recursion_core.quota.reservation", "ERROR"):
            self.assertIsNone(self.res.release(RUN_B))
        self.store.fail_commit = False
        self.assertEqual(len(held(self.store)), 1)  # the hold stays, and still counts

    def test_a_malformed_call_is_refused_with_no_row(self):
        cases = ((KEY, route(), RUN_A),                       # the connection is not the route's
                 (SUB, route(), "run id with spaces"),
                 (SUB, route(), 7),
                 (SUB, route(), "r" * 65),                    # a run id is 1 to 64 characters (review R6/M09)
                 ("subscription:", route(conn_id=""), RUN_A),  # an empty connection id (R6/M11)
                 (SUB, dict(route(), engine=""), RUN_A),      # an empty engine or model (R6/M10)
                 (SUB, dict(route(), model=""), RUN_A),
                 (SUB, {k: v for k, v in route().items() if k != "model"}, RUN_A),
                 ("other:x", dict(route(), connection={"kind": "other", "id": "x"}), RUN_A),
                 (SUB, "not a route", RUN_A))
        for connection, made, run_id in cases:
            with self.subTest(connection=connection, run_id=run_id):
                self.assertRefused(self.res.admit(connection, made, run_id), "route_invalid")
        self.assertEqual(rows(self.store), [])
        self.assertEqual(self.keys.calls, [])

    def test_a_run_id_of_sixty_four_characters_is_a_run_id(self):
        self.assertTrue(self.res.admit(SUB, route(), "r" * 64)["allowed"])

    def test_the_connection_is_the_route_services_meter(self):
        for made in (route(), key_route(), route("passthrough", "cloud-A")):
            self.assertEqual(R.connection_key(made), contract.connection_key(made))

    def test_every_reason_is_a_code_word(self):
        for reason in sorted(R.ALLOWED_REASONS | R.REFUSAL_REASONS):
            self.assertTrue(CODE_WORD.fullmatch(reason), reason)
        self.assertEqual(R.RESERVATION_REFUSED, "reservation_refused")


class ConcurrencyTest(unittest.TestCase):
    """Two admissions on one paid connection at once never both take the headroom."""

    def run_both(self, store: FakeStore, reads, barrier: threading.Barrier | None = None) -> list[dict]:
        keys = Policies(store, **{"key-A": opted_in("5")})
        res = R.Reservations(store, keys, reads=reads)
        answers: list = []

        def admit(run_id: str) -> None:
            try:
                answers.append(res.admit(KEY, key_route(), run_id))
            finally:
                if barrier is not None:
                    barrier.abort()  # a worker that is done never leaves the other waiting at the barrier

        threads = [threading.Thread(target=admit, args=(run_id,)) for run_id in (RUN_A, RUN_B)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(BOUND_S)
            self.assertFalse(thread.is_alive())
        return answers

    def test_a_store_that_waits(self):
        store = declared()
        answers = self.run_both(store, Reads(spent={KEY: R.known_spend(1)}))
        self.assertEqual(sorted(answer["reason"] for answer in answers), ["cap_held", "within_cap"])
        self.assertEqual(len(held(store)), 1)

    def test_a_store_that_refuses_a_conflict(self):
        store = declared(serial=False)
        barrier = threading.Barrier(2, timeout=BOUND_S)
        arrived: list = []

        class BothOpen(Reads):
            def spent(self, tx, connection_id):
                arrived.append(connection_id)
                barrier.wait()  # both transactions have read before either writes
                return R.known_spend(1)

        # A reader held open by this thread for the whole test keeps the store's write-ahead log in place. With no
        # connection open, the log is removed, and two connections that open it again at the same moment can be
        # refused at their first read by the fake's zero busy timeout, before either reaches the barrier.
        with store.transaction() as reader, self.assertLogs("recursion_core.quota.reservation", "ERROR"):
            reader.execute("SELECT count(*) FROM quota_reservation")
            answers = self.run_both(store, BothOpen(), barrier)
        self.assertEqual(arrived, [KEY, KEY])  # both read the spend inside an open transaction
        self.assertEqual(sorted(answer["allowed"] for answer in answers), [False, True])
        self.assertEqual(len(held(store)), 1)


class ModuleTest(unittest.TestCase):
    """``reservation.admit`` and ``release``, which the route service calls."""

    def test_before_composition_only_a_route_that_cannot_spend_is_admitted(self):
        # Review R1 (both): a present module that is not composed owes the zero-dollar rule; only the module's
        # ABSENCE is the caller's base behaviour.
        with mock.patch.object(R, "_installed", None):
            self.assertEqual(R.admit(SUB, route(), RUN_A),
                             {"allowed": True, "code": None, "reservation_id": None, "reason": "no_spend",
                              "metered": False})
            for connection, made in ((KEY, key_route()), ("passthrough:cloud-A", route("passthrough", "cloud-A")),
                                     (SUB, route(api_key_helper="present", billing_ack="api_key_helper"))):
                with self.subTest(connection=connection):
                    self.assertEqual(R.admit(connection, made, RUN_A),
                                     {"allowed": False, "code": "reservation_refused", "reservation_id": None,
                                      "reason": "quota_not_composed", "metered": True})
            self.assertEqual(R.admit(KEY, route(), RUN_A)["reason"], "route_invalid")
            self.assertEqual(R.release(RUN_A), [])

    def test_composition_installs_once(self):
        store = declared()
        res = R.Reservations(store, Policies(store))
        with mock.patch.object(R, "_installed", None):
            with self.assertRaises(TypeError):
                R.install(object())
            R.install(res)
            with self.assertRaises(RuntimeError):
                R.install(res)
            answer = R.admit(SUB, route(), RUN_A)
            self.assertEqual(answer["reason"], "no_spend")
            self.assertEqual(held(store)[0]["reservation_id"], answer["reservation_id"])
            self.assertEqual(R.release(RUN_A), [answer["reservation_id"]])

    def test_the_tables_are_declared_once(self):
        store = declared()
        with self.assertRaises(RuntimeError):
            tables.declare(store)


class TableConstraintTest(unittest.TestCase):
    """Review R6 (gpt-6-astra) / M18-M22 (gpt-6.1-sol): each constraint the table declares refuses a bad row."""

    GOOD = {"reservation_id": "rsv-" + "1" * 32, "run_id": RUN_A, "connection_id": KEY, "engine": "claude_code",
            "model": "m", "state": "held", "metered": 1, "basis": "api_key", "reason": "within_cap",
            "amount_usd": "1", "cap_usd": "5", "spent_usd": "2", "spent_quality": "known",
            "spent_provenance": "observed", "paid_fallback": None}

    def insert(self, store, **changes):
        row = dict(self.GOOD, **changes)
        with store.transaction() as tx:
            tx.execute(f"INSERT INTO quota_reservation ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                       tuple(row.values()))

    def test_a_good_row_is_kept(self):
        store = declared()
        self.insert(store)
        self.assertEqual(len(rows(store)), 1)

    def test_each_constraint_refuses(self):
        for change in ({"state": "open"}, {"metered": 2}, {"spent_quality": "exact"},
                       {"spent_provenance": "guessed"}, {"paid_fallback": "maybe"}, {"reason": None}):
            with self.subTest(change=change):
                store = declared()
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert(store, **change)
                self.assertEqual(rows(store), [])

    def test_a_reservation_has_one_row_per_state(self):
        store = declared()
        self.insert(store)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert(store, run_id=RUN_B)  # a second held row for the same reservation
        self.insert(store, state="released", reason="run_ended")
        self.assertEqual([row["state"] for row in rows(store)], ["held", "released"])


class SpendTypeTest(unittest.TestCase):
    def test_unknown_is_null_never_zero(self):
        self.assertEqual(R.UNKNOWN_SPEND, R.Spend(None, "unknown", "unavailable"))
        for bad in ((Decimal(0), "unknown", "unavailable"), (None, "unknown", "observed"),
                    (None, "known", "observed"), (Decimal(1), "known", "unavailable"),
                    (Decimal(-1), "known", "observed"), (Decimal("NaN"), "known", "observed"),
                    (1, "known", "observed"), (Decimal(1), "exact", "observed"),
                    (Decimal(1), "known", "guessed")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                R.Spend(*bad)

    def test_dollars_are_numbers(self):
        self.assertEqual(R.dollars(0.4127), Decimal("0.4127"))
        self.assertEqual(R.dollars(3), Decimal(3))
        for bad in (True, "5", None, float("nan"), float("inf"), -0.01, Decimal("Infinity")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                R.dollars(bad)


class FixtureTest(Case):
    """The fixtures sent to SPINE are the shapes this lane reads, and the admission rules hold on them."""

    USAGE = ("q01-claude-key-turn-usage-tokens-observed-cost-modelled.json",
             "q02-codex-key-session-total-cost-unknown.json",
             "q03-codex-key-session-total-later-would-double-if-summed.json")
    MEASURES = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens",
                "cost_usd")

    @staticmethod
    def spend_of(event: dict) -> R.Spend:
        cost = event["payload"]["cost_usd"]
        if cost["quality"] == "unknown":
            return R.UNKNOWN_SPEND
        return R.Spend(R.dollars(cost["value"]), cost["quality"], cost["provenance"])

    def test_the_index_names_every_fixture(self):
        index = load("index.json")
        named = [entry["file"] for entry in index["fixtures"]]
        self.assertEqual(sorted(named), sorted(path.name for path in FIXTURES.glob("q*.json")))

    def test_usage_fixtures_keep_unknown_null(self):
        for name in self.USAGE:
            with self.subTest(fixture=name):
                event = load(name)
                self.assertEqual(event["type"], "usage")
                payload = event["payload"]
                self.assertIn(payload["span"], ("call", "turn", "session_total"))
                self.assertRegex(payload["connection_id"], r"^api_key:.+$")
                for measure in self.MEASURES:
                    value = payload[measure]
                    if value["quality"] == "unknown":
                        self.assertEqual((value["value"], value["provenance"]), (None, "unavailable"))
                    else:
                        self.assertNotEqual(value["provenance"], "unavailable")
                        self.assertIsInstance(value["value"], (int, float))

    def test_the_claude_key_turn_against_a_cap(self):
        event = load(self.USAGE[0])
        self.reads.spent_by[KEY] = self.spend_of(event)
        self.keys.policies["key-A"] = opted_in("0.40")
        self.assertRefused(self.res.admit(KEY, key_route(), RUN_A), "cap_reached")
        self.keys.policies["key-A"] = opted_in("1")
        self.assertTrue(self.res.admit(KEY, key_route(), RUN_B)["allowed"])
        self.assertEqual(held(self.store)[0]["amount_usd"], "0.5873")  # 1 - 0.4127, by hand
        self.assertEqual(held(self.store)[0]["spent_provenance"], "modelled")

    def test_a_codex_key_with_no_reported_cost_is_refused(self):
        for name in self.USAGE[1:]:
            with self.subTest(fixture=name):
                self.reads.spent_by[KEY] = self.spend_of(load(name))
                self.assertRefused(self.res.admit(KEY, key_route(), RUN_A), "spend_unknown")

    def test_the_later_running_total_replaces_the_earlier(self):
        earlier, later = load(self.USAGE[1]), load(self.USAGE[2])
        self.assertEqual((earlier["session_id"], earlier["payload"]["span"]),
                         (later["session_id"], later["payload"]["span"]))
        self.assertLess(earlier["seq"], later["seq"])
        for measure in self.MEASURES[:-1]:
            self.assertLessEqual(earlier["payload"][measure]["value"], later["payload"][measure]["value"])

    def test_the_refusal_fixtures_carry_a_typed_reason(self):
        view = load("q04-run-view-start-failed-reservation-refused.json")
        answer = load("q05-run-start-refused-cap-zero.json")["answer"]
        for start_error in (view["start_error"], answer["start_error"]):
            self.assertEqual(start_error["code"], R.RESERVATION_REFUSED)
            self.assertIn(start_error["detail"], R.REFUSAL_REASONS)
        self.assertEqual((view["state"], answer["code"]), ("start_failed", "start_failed"))

    def test_the_fixtures_validate_against_spines_schemas(self):
        # Review R5 (both): the schemas are part of this branch (SPINE's #368, merged in), so on an interpreter that
        # has jsonschema (3.12, as hosted CI) their absence is a failure. Only an interpreter without jsonschema
        # (the free-threaded venv) skips.
        client_path = REPO / "conformance" / "wire_client.py"
        folder = REPO / "schemas" / "control" / "v1"
        if importlib.util.find_spec("jsonschema") is None:
            self.skipTest("jsonschema is not installed in this interpreter")
        self.assertTrue(client_path.is_file() and folder.is_dir(), "SPINE's schemas (#368) are not in the tree")
        spec = importlib.util.spec_from_file_location("quota_wire_client", client_path)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        schemas = client.SchemaSet(folder)
        for entry in load("index.json")["fixtures"]:
            with self.subTest(fixture=entry["file"]):
                problem = client.check_instance(schemas, {"file": "good/" + entry["file"], "schema": entry["schema"]},
                                                load(entry["file"]))
                self.assertIsNone(problem)


if __name__ == "__main__":
    unittest.main()
