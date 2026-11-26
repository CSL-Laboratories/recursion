"""QUOTA #375 next round: unsettled holds stay on the books (F9, product line L1884).

The red cases are the ones NOTE.md names:

* a restart sweep keeps a metered hold with no final usage as provisional, and a new
  admission on that key still sees it (B1-6.7, B3-6.7);
* amounts are integer micro-USD, release of an unsettled hold is unresolved and still
  counts, and a settle written twice under one reservation id leaves one row
  (B2-28.1, B2-18.11, B3-6.6, B2-30.9);
* a hold whose run has an unreconciled unknown effect survives release and is never
  settled at 0 (B2-28.7);
* a resumed session's own spend is counted once and is never taken from the engine's
  budget flag (B2-15.2).

Token and action holds are not here (the second reader sends them to LOOP).
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from recursion_core.quota import reservation as R
from recursion_core.quota.tests.test_reservation import (
    KEY, KEY_B, RUN_A, RUN_B, RUN_C, Case, held, key_route, rows,
)


def table_rows(store, table: str) -> list[dict]:
    return [dict(value, row_id=key[1]) for key, value in sorted(store.rows.items()) if key[0] == table]


class UnsettledHoldTest(Case):
    def test_restart_sweep_keeps_a_metered_hold_with_no_final_usage(self):
        # Admit, then drop the object without release: a crash. A new object on the same
        # store runs the restart sweep. The hold stays provisional and still counts.
        answer = self.res.admit(KEY, key_route(), RUN_A)
        self.assertTrue(answer["allowed"], answer)
        reservation_id = answer["reservation_id"]
        del self.res
        restarted = R.Reservations(self.store, self.keys, reads=self.reads)
        sweep = getattr(restarted, "restart_sweep", None)
        if sweep is not None:
            sweep()
            sweep()  # a second sweep writes nothing more
        states = [row["state"] for row in rows(self.store) if row["reservation_id"] == reservation_id]
        self.assertEqual(states, ["held", "provisional"])
        self.assertRefused(restarted.admit(KEY, key_route(), RUN_B), "cap_held")

    def test_unsettled_release_stays_unresolved_in_integer_micro_usd(self):
        answer = self.res.admit(KEY, key_route(), RUN_A)
        self.assertTrue(answer["allowed"], answer)
        reservation_id = answer["reservation_id"]
        record = getattr(self.res, "record_usage", None)
        if record is not None:
            record(RUN_A, cost_micro_usd=None)  # cost unknown: null, never 0
            usage = table_rows(self.store, "quota_usage")
            self.assertEqual([row["cost_micro_usd"] for row in usage], [None])
        self.assertEqual(self.res.release(RUN_A), [reservation_id])
        marker = [row for row in rows(self.store) if row["reservation_id"] == reservation_id][-1]
        self.assertEqual((marker["state"], marker["amount_usd"]), ("unresolved", "3"))
        self.assertNotEqual(marker["amount_usd"], "0")
        self.assertRefused(self.res.admit(KEY, key_route(), RUN_B), "cap_held")
        [still] = [row for row in held(self.store) if row["reservation_id"] == reservation_id]
        self.assertIsInstance(still["amount_micro_usd"], int)
        self.assertEqual(still["amount_micro_usd"], 3_000_000)
        self.assertEqual(R.micro_usd(Decimal("3")), 3_000_000)
        with self.assertRaises(ValueError):
            R.micro_usd(Decimal("0.0000004"))  # finer than one micro-USD
        settle = self.res.settle
        first = settle(reservation_id, status="unresolved")
        second = settle(reservation_id, status="settled", amount_micro_usd=0)
        self.assertEqual(first["reservation_id"], reservation_id)
        self.assertEqual(first["status"], "unresolved")
        self.assertIsNone(first["amount_micro_usd"])
        self.assertEqual(second["status"], "unresolved")
        self.assertIsNone(second["amount_micro_usd"])
        kept = [row for row in table_rows(self.store, "quota_settle") if row["reservation_id"] == reservation_id]
        self.assertEqual(len(kept), 1)
        self.assertIsNone(kept[0]["amount_micro_usd"])

    def test_an_unknown_effect_survives_the_run_and_is_not_settled_at_zero(self):
        answer = self.res.admit(KEY, key_route(), RUN_A)
        self.assertTrue(answer["allowed"], answer)
        reservation_id = answer["reservation_id"]
        record_effect = getattr(self.res, "record_effect", None)
        record_usage = getattr(self.res, "record_usage", None)
        if record_effect is not None:
            record_effect(RUN_A, outcome="outcome_unknown")
        if record_usage is not None:
            # A known cost would end the hold on release. The unknown effect must still keep it.
            record_usage(RUN_A, cost_micro_usd=1_000_000)
        self.res.release(RUN_A)
        self.assertEqual([row["reservation_id"] for row in held(self.store)], [reservation_id])
        self.assertNotEqual(rows(self.store)[-1]["amount_usd"], "0")
        settle = getattr(self.res, "settle", None)
        if settle is not None:
            denied = settle(reservation_id, status="settled", amount_micro_usd=0)
            self.assertFalse(denied["written"])
            self.assertNotEqual(denied.get("amount_micro_usd"), 0)
            stored = table_rows(self.store, "quota_settle")
            self.assertFalse(any(row["amount_micro_usd"] == 0 for row in stored))
            self.assertEqual([row["reservation_id"] for row in held(self.store)], [reservation_id])
            self.res.reconcile_effect(RUN_A)
            done = settle(reservation_id, status="settled", amount_micro_usd=1_000_000)
            self.assertEqual(done["status"], "settled")
            self.assertEqual(done["amount_micro_usd"], 1_000_000)
            self.assertNotIn(reservation_id, [row["reservation_id"] for row in held(self.store)])

    def test_resumed_session_spend_is_counted_once(self):
        # NoReads: the ledger reader still knows nothing (the slice-1 stand-in).
        res = R.Reservations(self.store, self.keys)
        session = "ses-claude-resume-1"
        made = key_route(session_id=session)
        record = getattr(res, "record_session_spend", None)
        if record is not None:
            record(KEY, session, RUN_A, cost_micro_usd=1_000_000,
                   total_cost_usd=Decimal("1"), max_budget_usd=Decimal("50"))
        first = res.admit(KEY, made, RUN_A)
        self.assertTrue(first["allowed"], first)
        self.assertEqual(held(self.store)[0]["amount_usd"], "4")  # cap 5 shrunk by the 1 the run cost
        record_usage = getattr(res, "record_usage", None)
        if record_usage is not None:
            record_usage(RUN_A, cost_micro_usd=1_000_000)
        self.assertEqual(res.release(RUN_A), [first["reservation_id"]])
        if record is not None:
            # The resume reports the same cumulative total again. Summing it would make 2.
            # The engine budget flag is 50 and is not spend.
            record(KEY, session, RUN_B, total_cost_usd=Decimal("1"), max_budget_usd=Decimal("50"))
        restarted = R.Reservations(self.store, self.keys)
        second = restarted.admit(KEY, made, RUN_B)
        self.assertTrue(second["allowed"], second)
        self.assertEqual(
            [row["amount_usd"] for row in held(self.store) if row["reservation_id"] == second["reservation_id"]],
            ["4"])
        stored = [row for row in table_rows(self.store, "quota_session_spend") if row["session_id"] == session]
        self.assertEqual({row["max_budget_micro_usd"] for row in stored}, {50_000_000})
        self.assertNotEqual(held(self.store)[-1]["amount_usd"], "3")


class FinalUsageSweepTest(Case):
    """A hold that already has a final usage is not what the sweep keeps provisional."""

    def test_a_hold_with_final_usage_is_not_marked_provisional(self):
        answer = self.res.admit(KEY_B, key_route("key-B"), RUN_C)
        self.assertTrue(answer["allowed"], answer)
        record = getattr(self.res, "record_usage", None)
        sweep = getattr(self.res, "restart_sweep", None)
        if record is None or sweep is None:
            self.fail("the sweep and the usage record are part of this round")
        record(RUN_C, cost_micro_usd=1_000_000)
        sweep()
        states = [row["state"] for row in rows(self.store) if row["reservation_id"] == answer["reservation_id"]]
        self.assertEqual(states, ["held"])
        self.assertEqual([row["reservation_id"] for row in held(self.store)], [answer["reservation_id"]])


if __name__ == "__main__":
    unittest.main()
