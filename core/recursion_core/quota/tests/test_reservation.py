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

    def test_free_holds_do_not_count_against_a_cap(self):
        self.assertTrue(self.res.admit(SUB, route(), RUN_A)["allowed"])
        self.keys.policies["login-A"] = opted_in("1")
        self.reads.spent_by[SUB] = R.known_spend(0)
        self.reads.paid_by[SUB] = True  # the next run on this login would draw on the plan's paid fallback
        answer = self.res.admit(SUB, route(), RUN_B)
        self.assertEqual((answer["allowed"], answer["metered"]), (True, True))
        self.assertEqual(held(self.store)[-1]["amount_usd"], "1")

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
        with self.assertLogs("recursion_core.quota.reservation", "ERROR"):
            self.assertTrue(res.admit(SUB, route(), RUN_B)["allowed"])
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
                           "no_opt_in")
        billing = route(api_key_helper="present", billing_ack="api_key_helper")
        self.assertRefused(self.res.admit(SUB, billing, RUN_B), "no_opt_in")
        self.assertEqual(rows(self.store)[-1]["basis"], "api_key_helper_billing")
        asks = route(api_key_helper="present")  # not acknowledged: the adapter asks, nothing is billed
        self.assertEqual(self.res.admit(SUB, asks, RUN_C)["reason"], "no_spend")
        ack_without_helper = route(billing_ack="api_key_helper")
        self.assertEqual(self.res.admit(SUB, ack_without_helper, "run-d4")["reason"], "no_spend")

    def test_a_subscription_on_its_plans_paid_fallback_needs_an_opt_in(self):
        self.reads.paid_by[SUB] = True
        self.assertRefused(self.res.admit(SUB, route(), RUN_A), "no_opt_in")
        self.assertEqual(rows(self.store)[-1]["basis"], "plan_paid_fallback")
        self.keys.policies["login-A"] = opted_in("10")
        self.reads.spent_by[SUB] = R.known_spend(1)
        answer = self.res.admit(SUB, route(), RUN_B)
        self.assertEqual((answer["allowed"], answer["metered"]), (True, True))
        for state in (False, None):
            with self.subTest(paid=state):
                self.reads.paid_by[SUB] = state
                self.assertEqual(self.res.admit(SUB, route(), f"run-q{state}")["reason"], "no_spend")

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
            self.assertEqual(res.admit(SUB, route(), RUN_B)["reason"], "no_spend")
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
                 (SUB, {k: v for k, v in route().items() if k != "model"}, RUN_A),
                 ("other:x", dict(route(), connection={"kind": "other", "id": "x"}), RUN_A),
                 (SUB, "not a route", RUN_A))
        for connection, made, run_id in cases:
            with self.subTest(connection=connection, run_id=run_id):
                self.assertRefused(self.res.admit(connection, made, run_id), "route_invalid")
        self.assertEqual(rows(self.store), [])
        self.assertEqual(self.keys.calls, [])

    def test_the_connection_is_the_route_services_meter(self):
        for made in (route(), key_route(), route("passthrough", "cloud-A")):
            self.assertEqual(R.connection_key(made), contract.connection_key(made))

    def test_every_reason_is_a_code_word(self):
        for reason in sorted(R.ALLOWED_REASONS | R.REFUSAL_REASONS):
            self.assertTrue(CODE_WORD.fullmatch(reason), reason)
        self.assertEqual(R.RESERVATION_REFUSED, "reservation_refused")


class ConcurrencyTest(unittest.TestCase):
    """Two admissions on one paid connection at once never both take the headroom."""

    def run_both(self, store: FakeStore, reads) -> list[dict]:
        keys = Policies(store, **{"key-A": opted_in("5")})
        res = R.Reservations(store, keys, reads=reads)
        answers: list = []
        threads = [threading.Thread(target=lambda run_id=run_id: answers.append(res.admit(KEY, key_route(), run_id)))
                   for run_id in (RUN_A, RUN_B)]
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

        class BothOpen(Reads):
            def spent(self, tx, connection_id):
                barrier.wait()  # both transactions have read before either writes
                return R.known_spend(1)

        with self.assertLogs("recursion_core.quota.reservation", "ERROR"):
            answers = self.run_both(store, BothOpen())
        self.assertEqual(sorted(answer["allowed"] for answer in answers), [False, True])
        self.assertEqual(len(held(store)), 1)


class ModuleTest(unittest.TestCase):
    """``reservation.admit`` and ``release``, which the route service calls."""

    def test_before_composition_the_lane_acts_as_if_absent(self):
        with mock.patch.object(R, "_installed", None):
            self.assertEqual(R.admit(SUB, route(), RUN_A),
                             {"allowed": True, "code": None, "reservation_id": None, "reason": "no_spend",
                              "metered": False})
            self.assertEqual(R.admit(KEY, key_route(), RUN_A),
                             {"allowed": True, "code": None, "reservation_id": None, "reason": "quota_not_composed",
                              "metered": True})
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


class SpendTypeTest(unittest.TestCase):
    def test_unknown_is_null_never_zero(self):
        self.assertEqual(R.UNKNOWN_SPEND, R.Spend(None, "unknown", "unavailable"))
        for bad in ((Decimal(0), "unknown", "unavailable"), (None, "unknown", "observed"),
                    (None, "known", "observed"), (Decimal(1), "known", "unavailable"),
                    (Decimal(-1), "known", "observed"), (Decimal("NaN"), "known", "observed"),
                    (1, "known", "observed"), (Decimal(1), "exact", "observed")):
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
        client_path = REPO / "conformance" / "wire_client.py"
        folder = REPO / "schemas" / "control" / "v1"
        if importlib.util.find_spec("jsonschema") is None or not client_path.is_file() or not folder.is_dir():
            self.skipTest("needs jsonschema and SPINE's schemas (#368) in the tree")
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
