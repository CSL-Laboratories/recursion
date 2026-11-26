"""An approval whose charge was refused is recorded as not carried out, and its notes go by the person's rows.

A person approves; a check of the core's one charge path refuses the charge (here the profile's most per
effect). Nothing is charged, and the approval queue's record stays approved, as before. The notes file now
says what happened: approved, not carried out, refused with that check's own code. Such notes are not
needed for a carry, so they are pruned like the notes of a consumed approval, and refused charges can no
longer fill the file. Notes still needed for a carry are kept in a second file with no size figure of
ours, so a full file never refuses an approval; when the person sets that file's size row, its refusal
says what is full, why those notes stay and what the person can do, by the row's own name.
"""

from __future__ import annotations

import json
import unittest

from recursion_core.daemon import plugins
from recursion_core.daemon.tests.test_plugin_effect_approval import LINUX, _effect
from recursion_core.daemon.tests import test_plugin_effect_notes_kept as kept
from recursion_core.plugin_security import Refused

try:
    from recursion_core.approvals.model import ApprovalRefused
except ImportError:  # pragma: no cover - the queue is on this branch
    ApprovalRefused = None

AMOUNT = kept.AMOUNT
OVER = 6_000  # past the stand-in profile's most per effect (5_000): refused budget_max_effect_exceeded
INTERNALS = ("PluginEffectSink", ".carry", "NOTES_", "_NOTE_CAP")  # names a person cannot act on


def _needed_path(notes):
    return notes.with_name(getattr(plugins, "EFFECT_NOTES_NEEDED", "absent-before-this-change.json"))


@unittest.skipUnless(LINUX, "the owner-only socket peer and the token file are checked by uid on Linux")
class RefusedChargeNotesTests(kept.NotesKeptTests):
    def files(self):
        """Both notes files merged as a later process reads them (the needed file only after this change)."""
        merged = {"envelopes": {}, "receipts": {}, "consumed": {}, "refused": {}}
        for path in (self.notes, _needed_path(self.notes)):
            if path.exists():
                for key, value in json.loads(path.read_bytes()).items():
                    merged.setdefault(key, {}).update(value)
        return merged

    def held_ids(self):
        data = self.files()
        return set(data["envelopes"]) | set(data["receipts"])

    def refused_charge(self, host, note):
        effect = _effect(amount=OVER, payload={"note": note})
        approval_id = host.request_plugin_effect(effect)
        with self.assertRaises(Refused) as caught:
            host.answer_plugin_effect(self.owner(), approval_id, effect, authority=self.envelope())
        return approval_id, caught.exception.code

    def test_refused_charges_cannot_fill_the_file_and_the_next_approval_goes_through(self):
        host = self.composed()
        self.figures(size=4000, keep_for=30 * 24 * 3600, keep_at_most=3)
        refused = []
        for number in range(25):  # before this change the file fills within these, and stays full
            approval_id, _code = self.refused_charge(host, "r%d" % number)
            refused.append(approval_id)
        self.assertEqual(self.charge_rows(), [])
        try:
            approval_id, _effect_, carried = self.carry(host, "after")
        except Refused as stopped:
            for name in INTERNALS:
                self.assertNotIn(name, stopped.detail)
            self.fail("an approval was refused after refused charges: %s %s" % (stopped.code, stopped.detail))
        self.assertEqual(carried["approval"]["state"], "consumed")
        self.assertEqual([key for key, _ in self.charge_rows()], [approval_id])
        self.assertLessEqual(self.notes.stat().st_size, 4000)
        self.assertLessEqual(len(self.held_ids() & set(refused)), 3, "refused approvals' notes are kept for good")

    def test_a_refused_charge_is_recorded_as_given_and_not_carried_out(self):
        host = self.composed()
        self.figures(size=10 ** 7, keep_for=None, keep_at_most=None)
        approval_id, code = self.refused_charge(host, "pinned")
        self.assertEqual(code, "budget_max_effect_exceeded")
        self.assertEqual(self.charge_rows(), [])
        self.assertEqual(self.record(host, approval_id)["state"], "approved")  # the queue record, unchanged
        entry = self.files()["refused"].get(approval_id)
        self.assertIsNotNone(entry, "the notes do not say the approval was refused")
        self.assertEqual({key: entry[key] for key in ("answered", "carried", "code")},
                         {"answered": "approve", "carried": False, "code": "budget_max_effect_exceeded"})
        self.assertNotIn(approval_id, self.files()["consumed"])
        shown = host.inventory()["effect_notes"]
        self.assertEqual((shown["held"], shown["awaiting_carry"], shown["refused"]), (1, 0, 1))

    def test_a_refused_approvals_notes_go_by_the_rows_like_finished_ones(self):
        host = self.composed()
        self.figures(size=10 ** 7, keep_for=30 * 24 * 3600, keep_at_most=0)
        first, _code = self.refused_charge(host, "gone")
        self.carry(host, "next")
        data = self.files()
        self.assertNotIn(first, self.held_ids())
        self.assertNotIn(first, data["refused"])

    def test_needed_notes_past_the_size_figure_never_refuse_an_approval(self):
        host = self.composed()
        self.figures(size=2500, keep_for=0, keep_at_most=0)
        release = self.hold_consume(host)
        waiting = []
        for number in range(20):
            effect = _effect(amount=AMOUNT, payload={"note": "h%d" % number})
            approval_id = host.request_plugin_effect(effect)
            with self.assertRaises(ApprovalRefused):  # charged; its consume step held: its notes are needed
                host.answer_plugin_effect(self.owner(), approval_id, effect, authority=self.envelope())
            waiting.append(approval_id)
        self.assertEqual(len(self.charge_rows()), 20)
        self.assertEqual(self.held_ids(), set(waiting))
        self.assertLessEqual(self.notes.stat().st_size, 2500)
        release()
        host._approver._receipts._memory.clear()  # noqa: SLF001 - what a new process reads is the files
        host._approver._envelopes.clear()
        receipt = host._approver.charge(_effect(amount=AMOUNT, payload={"note": "h0"}), waiting[0])
        self.assertEqual(receipt["profile_id"], "prof-1")
        self.assertEqual(len(self.charge_rows()), 20)

    def test_a_needed_file_at_its_size_row_says_what_why_and_what_the_person_can_do(self):
        host = self.composed()
        self.figures(size=10 ** 7, keep_for=None, keep_at_most=None)
        had = getattr(plugins, "NOTES_NEEDED_SIZE_LIMIT_BYTES", "absent")
        self.assertIsNone(had, "the needed file's size row ships as 'no limit'")
        plugins.NOTES_NEEDED_SIZE_LIMIT_BYTES = 1500
        self.addCleanup(setattr, plugins, "NOTES_NEEDED_SIZE_LIMIT_BYTES", None)
        self.hold_consume(host)
        stopped = None
        for number in range(20):
            effect = _effect(amount=AMOUNT, payload={"note": "q%d" % number})
            approval_id = host.request_plugin_effect(effect)
            try:
                host.answer_plugin_effect(self.owner(), approval_id, effect, authority=self.envelope())
            except ApprovalRefused:
                continue
            except Refused as exc:
                self.assertEqual(exc.code, "approval_notes_full")
                if approval_id in {key for key, _ in self.charge_rows()}:
                    self.assertIn("the charge was made", exc.detail)
                    continue
                stopped = exc
                break
        self.assertIsNotNone(stopped, "the needed notes never reached the row")
        row = plugins.NOTES_ROWS["NOTES_NEEDED_SIZE_LIMIT_BYTES"]
        self.assertIn(str(_needed_path(self.notes)), stopped.detail)
        self.assertIn(row, stopped.detail)
        self.assertIn("'no limit'", stopped.detail)
        self.assertIn("nothing was charged", stopped.detail)
        self.assertIn("request the effect again", stopped.detail)
        for name in INTERNALS:
            self.assertNotIn(name, stopped.detail)
        self.assertNotIn(approval_id, {key for key, _ in self.charge_rows()})
        self.assertEqual(self.files()["refused"][approval_id]["code"], "approval_notes_full")


# The base class's own tests run in their own module; here only the tests above.
for _name in [name for name in vars(kept.NotesKeptTests) if name.startswith("test_")]:
    setattr(RefusedChargeNotesTests, _name, None)


if __name__ == "__main__":
    unittest.main()
