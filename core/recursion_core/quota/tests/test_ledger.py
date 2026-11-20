"""The settle row, read back after a restart of the reader (F9, product line L1884).

One row per reservation id, status settled or unresolved. This is the part of the
usage ledger the settle row gives. The usage-event feed, the meters and the two
read verbs are not in this test.
"""

from __future__ import annotations

import unittest

from recursion_core.quota.ledger import Ledger
from recursion_core.quota.tests.test_reservation import KEY, KEY_B, RUN_A, RUN_B, Case, key_route


def table_rows(store, table: str) -> list[dict]:
    return [dict(value, row_id=key[1]) for key, value in sorted(store.rows.items()) if key[0] == table]


class SettleReadBackTest(Case):
    """A new Ledger on the same store reads the settle rows the first writer left."""

    def test_a_settle_row_is_read_back_after_a_restart(self):
        unsettled = self.res.admit(KEY, key_route(), RUN_A)
        self.assertTrue(unsettled["allowed"], unsettled)
        self.res.record_usage(RUN_A, cost_micro_usd=None)
        self.assertEqual(self.res.release(RUN_A), [unsettled["reservation_id"]])
        wrote = self.res.settle(unsettled["reservation_id"], status="unresolved")
        self.assertEqual(wrote["status"], "unresolved")
        self.assertTrue(wrote["written"])

        known = self.res.admit(KEY_B, key_route("key-B"), RUN_B)
        self.assertTrue(known["allowed"], known)
        self.res.record_usage(RUN_B, cost_micro_usd=0)  # a known zero, not an unknown
        self.res.release(RUN_B)
        settled = self.res.settle(known["reservation_id"], status="settled", amount_micro_usd=0)
        self.assertEqual(settled["status"], "settled")
        self.assertEqual(settled["amount_micro_usd"], 0)

        before = table_rows(self.store, "quota_settle")
        del self.res
        ledger = Ledger(self.store)
        again = Ledger(self.store)  # a restart: a new reader, the same store

        unread = ledger.read(unsettled["reservation_id"])
        reread = again.read(unsettled["reservation_id"])
        self.assertEqual(unread, reread)
        self.assertEqual(unread["reservation_id"], unsettled["reservation_id"])
        self.assertEqual(unread["run_id"], RUN_A)
        self.assertEqual(unread["status"], "unresolved")
        self.assertIsNone(unread["amount_micro_usd"])

        zero = again.read(known["reservation_id"])
        self.assertEqual(zero["status"], "settled")
        self.assertEqual(zero["amount_micro_usd"], 0)
        self.assertIsInstance(zero["amount_micro_usd"], int)

        missing = "rsv-" + "ab" * 16
        self.assertIsNone(ledger.read(missing))
        with self.assertRaises(ValueError):
            ledger.read("not-a-reservation")

        both = again.read_all()
        self.assertEqual([row["reservation_id"] for row in both],
                         [unsettled["reservation_id"], known["reservation_id"]])
        self.assertEqual([row["status"] for row in both], ["unresolved", "settled"])
        self.assertEqual(table_rows(self.store, "quota_settle"), before)  # a read writes nothing


if __name__ == "__main__":
    unittest.main()
