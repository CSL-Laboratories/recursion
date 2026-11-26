"""The approval notes file is kept by the person's rule, and a full file of finished notes never refuses.

The notes of an approval (its authority envelope and, once charged, its receipt) are kept while it is
approved and not yet consumed, because a later carry reads them back. Once consumed they are kept only
as the figures say (keep for, keep at most, each with 'no limit'), and every write prunes first, the
oldest consumed notes last of all under the size figure. Only notes still needed for a carry can fill
the file, and that refusal names the file, how many approvals hold it and how to free it.
"""

from __future__ import annotations

import json
import os
import unittest

from recursion_core.daemon import plugins
from recursion_core.daemon.tests.test_plugin_effect_approval import LINUX, PLUGIN_ID, _HostCase, _effect
from recursion_core.plugin_security import Refused

try:
    from recursion_core.approvals.model import ApprovalRefused
except ImportError:  # pragma: no cover - the queue is on this branch
    ApprovalRefused = None

AMOUNT = 100  # the stand-in profile's budget (10_000) holds a hundred of these
SIZE_NAMES = ("NOTES_SIZE_LIMIT_BYTES", "_NOTE_CAP")  # the size figure's name after, and before, this change


@unittest.skipUnless(LINUX, "the owner-only socket peer and the token file are checked by uid on Linux")
class NotesKeptTests(_HostCase):
    def composed(self):
        host = self.loaded_host(anchor="ef" * 32)
        self.assertIn("order", host.loader.permissions(PLUGIN_ID))
        host.authority._verify = lambda _signature, _raw: True  # the ledger's signature check alone
        self.notes = self.state / "plugin-security" / plugins.EFFECT_NOTES
        return host

    def envelope(self):
        from recursion_core.daemon.tests.test_plugin_effect_one_charge_path import \
            StandInSignatureOneChargePathTests as standin
        return standin.envelope(self)

    def figures(self, *, size, keep_for, keep_at_most):
        """Set the notes figures for this test through their one named place, and put them back after."""
        wanted = {"NOTES_KEEP_FOR_S": keep_for, "NOTES_KEEP_AT_MOST": keep_at_most}
        for name in SIZE_NAMES:
            if hasattr(plugins, name):
                wanted[name] = size
        for name, value in wanted.items():
            had, old = hasattr(plugins, name), getattr(plugins, name, None)
            setattr(plugins, name, value)
            self.addCleanup(lambda n=name, h=had, o=old: setattr(plugins, n, o) if h else delattr(plugins, n))

    def carry(self, host, note):
        effect = _effect(amount=AMOUNT, payload={"note": note})
        approval_id = host.request_plugin_effect(effect)
        return approval_id, effect, host.answer_plugin_effect(self.owner(), approval_id, effect,
                                                              authority=self.envelope())

    def held_ids(self):
        if not self.notes.exists():
            return set()
        data = json.loads(self.notes.read_bytes())
        return set(data["envelopes"]) | set(data["receipts"])

    def hold_consume(self, host):
        queue = host._approval_queue  # noqa: SLF001 - the composed queue; the stand-in holds its consume step
        real = queue.consume

        def held(*_args, **_kwargs):
            raise ApprovalRefused("store_busy")

        queue.consume = held
        return lambda: setattr(queue, "consume", real)

    def test_more_approvals_than_fit_are_all_charged_and_the_file_stays_under_the_figure(self):
        host = self.composed()
        self.figures(size=4000, keep_for=30 * 24 * 3600, keep_at_most=1000)
        for number in range(12):
            try:
                _approval_id, _effect_, carried = self.carry(host, "n%d" % number)
            except Refused as refused:
                self.fail("approval %d refused once the notes filled: %s %s" % (number, refused.code, refused.detail))
            self.assertEqual(carried["approval"]["state"], "consumed")
        self.assertEqual(len(self.charge_rows()), 12)
        self.assertLessEqual(self.notes.stat().st_size, 4000)

    def test_notes_still_needed_for_a_carry_are_never_pruned_and_replay_after_a_restart(self):
        host = self.composed()
        self.figures(size=10 ** 7, keep_for=0, keep_at_most=0)
        release = self.hold_consume(host)
        waiting = []
        for number in range(3):
            effect = _effect(amount=AMOUNT, payload={"note": "w%d" % number})
            approval_id = host.request_plugin_effect(effect)
            with self.assertRaises(ApprovalRefused):
                host.answer_plugin_effect(self.owner(), approval_id, effect, authority=self.envelope())
            self.assertEqual(self.record(host, approval_id)["state"], "approved")
            waiting.append((approval_id, effect))
        release()
        finished = [self.carry(host, "f%d" % number)[0] for number in range(2)]
        self.assertEqual(self.held_ids(), {approval_id for approval_id, _ in waiting})
        rows = len(self.charge_rows())
        host._approver._receipts._memory.clear()  # noqa: SLF001 - what a new process reads is the file
        host._approver._envelopes.clear()
        for approval_id, effect in waiting:
            receipt = host._approver.charge(effect, approval_id)
            self.assertEqual(receipt["profile_id"], "prof-1")
        self.assertEqual(len(self.charge_rows()), rows)
        self.assertTrue(set(finished).isdisjoint(self.held_ids()))

    def test_when_needed_notes_alone_fill_the_figure_the_refusal_says_what_and_how(self):
        host = self.composed()
        self.figures(size=2500, keep_for=0, keep_at_most=0)
        self.hold_consume(host)
        refused = None
        for number in range(20):
            effect = _effect(amount=AMOUNT, payload={"note": "h%d" % number})
            approval_id = host.request_plugin_effect(effect)
            try:
                host.answer_plugin_effect(self.owner(), approval_id, effect, authority=self.envelope())
            except ApprovalRefused:
                continue  # charged, consume held: its notes are still needed
            except Refused as exc:
                self.assertEqual(exc.code, "approval_notes_full")
                if approval_id in {key for key, _ in self.charge_rows()}:
                    # Full at the receipt, after the ledger committed: the words say the charge was made.
                    self.assertIn("the charge was made", exc.detail)
                    continue
                refused = exc
                break
        self.assertIsNotNone(refused, "the needed notes never filled the figure")
        self.assertIn(str(self.notes), refused.detail)
        self.assertIn("%d approval(s)" % (len(self.held_ids()) + 1), refused.detail)  # the waiting ones and this one
        self.assertIn("nothing was charged", refused.detail)
        self.assertIn("PluginEffectSink.carry", refused.detail)
        self.assertNotIn(approval_id, {key for key, _ in self.charge_rows()})

    def test_keep_at_most_is_honoured_newest_first(self):
        host = self.composed()
        self.figures(size=10 ** 7, keep_for=30 * 24 * 3600, keep_at_most=2)
        ids = [self.carry(host, "k%d" % number)[0] for number in range(5)]
        self.assertEqual(self.held_ids(), set(ids[-2:]))

    def test_keep_for_is_honoured_on_the_clock(self):
        host = self.composed()
        now = [1_000_000]
        host._clock = lambda: now[0]
        self.figures(size=10 ** 7, keep_for=100, keep_at_most=1000)
        first = self.carry(host, "t0")[0]
        now[0] += 200
        second = self.carry(host, "t1")[0]
        self.assertNotIn(first, self.held_ids())
        self.assertIn(second, self.held_ids())

    def test_no_limit_keeps_every_note(self):
        host = self.composed()
        self.figures(size=None, keep_for=None, keep_at_most=None)
        ids = [self.carry(host, "u%d" % number)[0] for number in range(5)]
        self.assertEqual(self.held_ids(), set(ids))

    def test_status_shows_the_three_figures_and_the_count_held(self):
        host = self.composed()
        self.figures(size=10 ** 7, keep_for=None, keep_at_most=7)
        self.carry(host, "s0")
        shown = host.inventory()["effect_notes"]
        self.assertEqual((shown["size_limit_bytes"], shown["keep_for_s"], shown["keep_at_most"]),
                         (10 ** 7, "no limit", 7))
        self.assertEqual((shown["held"], shown["awaiting_carry"]), (1, 0))
        self.assertEqual(shown["path"], str(self.notes))


if __name__ == "__main__":
    unittest.main()
