"""A full disk or a killed process on the approve path: never a second charge, never a charged approval left open.

The approval is consumed inside the ledger's own transaction, after the charge row and before its commit, so
a failure at any write can leave only the safe side: not charged and still approved, or consumed and not
charged, or charged and consumed. Every failure the person reads says in plain words whether the charge was
made and what to do next; the raw error of the system is never all they read. A receipt note that could not
be written after a charge is written at the next approval, and rebuilt from the ledger at the next start.

Each write of the path (the notes files, the approval store, the ledger) is failed in turn by a stand-in of
this test's own: as a full disk (that write and every later one fail until room is made) and as a killed
process (that write never happens and the process is gone), then the same approval is tried again.
"""

from __future__ import annotations

import errno
import json
import sqlite3
import unittest
from unittest import mock

from recursion_core.approvals import store as approval_store
from recursion_core.daemon import plugins
from recursion_core.daemon.tests import test_plugin_effect_approval as base
from recursion_core.daemon.tests import test_plugin_effect_notes_kept as kept
from recursion_core.plugin_security import Refused
from recursion_core.plugin_security.authority import Authority

try:
    from recursion_core.approvals.model import ApprovalRefused
except ImportError:  # pragma: no cover - the queue is on this branch
    ApprovalRefused = None

AMOUNT = kept.AMOUNT
RAW = (OSError, sqlite3.Error, RuntimeError)  # what the system raises: never all the person reads


class _Killed(BaseException):
    """The process is gone at this write: nothing after it runs."""


class _Writes:
    """Counts every write of the approve path and fails the one at ``at`` (and, on a full disk, each later one)."""

    def __init__(self, case):
        self.names, self.at, self.mode = [], None, "count"
        notes_replace = plugins._EffectNotes._replace  # noqa: SLF001 - the one function that writes a notes file
        save = approval_store.save

        def replace(directory, path, payload):
            self.tick("notes:" + path.name, OSError(errno.ENOSPC, "No space left on device"))
            return notes_replace(directory, path, payload)

        def saved(tx, record, **options):
            self.tick("approval:" + str(record.get("state")), sqlite3.OperationalError("database or disk is full"))
            return save(tx, record, **options)

        patches = [mock.patch.object(plugins._EffectNotes, "_replace", staticmethod(replace)),
                   mock.patch.object(approval_store, "save", saved)]
        for patch in patches:
            patch.start()
            case.addCleanup(patch.stop)

    def attach(self, host):
        """Count this host's ledger statements too (installed once per test; every new host is attached)."""
        writes = self
        real = host.authority._db  # noqa: SLF001 - the ledger's own connection; every statement still runs on it

        class Ledger:
            def execute(self, sql, *params):
                word = sql.split(None, 1)[0].upper()
                if word in ("BEGIN", "INSERT", "COMMIT"):
                    writes.tick("ledger:" + " ".join(sql.split()[:3]),
                                sqlite3.OperationalError("database or disk is full"))
                return real.execute(sql, *params)

            def __getattr__(self, name):
                return getattr(real, name)

        host.authority._db = Ledger()

    def tick(self, name, error):
        self.names.append(name)
        if self.at is None:
            return
        if self.mode == "full" and len(self.names) >= self.at:
            raise error
        if self.mode == "killed" and len(self.names) == self.at:
            raise _Killed(name)

    def fail(self, at, mode):
        self.names, self.at, self.mode = [], at, mode

    def room(self):
        self.at = None


@unittest.skipUnless(base.LINUX, "the owner-only socket peer and the token file are checked by uid on Linux")
class FullDiskTests(kept.NotesKeptTests):
    def restart(self, host):
        """The daemon stops and starts again on the same state directory."""
        host.close()
        self.breaker.close()
        stored, _created = base.breaker_module.open_breaker_store(self.state, clock=lambda: 0.0)
        self.addCleanup(stored.close)
        with mock.patch.object(Authority, "_verify", lambda _self, _signature, _raw: True):
            made = plugins.PluginHost(self.config, self.state, stored, gil_probe=base.FakeGilProbe(), clock=base.CLOCK,
                                      device_token_path=self.token_path, verification_available=lambda: True)
        self.addCleanup(made.close)
        made.authority._verify = lambda _signature, _raw: True  # the ledger's signature check alone
        self.breaker = stored
        try:
            stored.bind_run(base.RUN_ID, base.CONNECTION, base.PLUGIN_ID, base.PROVIDER, base.MODEL)
        except Exception:  # noqa: BLE001 - a run already bound in the durable store stays bound
            pass
        return made

    def files(self):
        merged = {"envelopes": {}, "receipts": {}, "consumed": {}, "refused": {}}
        for path in (self.notes, self.notes.with_name(plugins.EFFECT_NOTES_NEEDED)):
            if path.exists():
                for key, value in json.loads(path.read_bytes()).items():
                    merged.setdefault(key, {}).update(value)
        return merged

    def attempt(self, call):
        try:
            return call(), None
        except (Refused, *(() if ApprovalRefused is None else (ApprovalRefused,))) as typed:
            return None, typed

    def assert_truth(self, host, approval_id, outcome, failure, where):
        """At most one charge; a charged approval reads consumed; the words say what happened."""
        rows = [key for key, _ in self.charge_rows() if key == approval_id]
        self.assertLessEqual(len(rows), 1, where)
        if rows:
            self.assertEqual(self.record(host, approval_id)["state"], "consumed",
                             "%s: charged, and the approval still reads approved" % where)
        if isinstance(failure, Refused):
            self.assertTrue(failure.detail, "%s: a refusal with no words: %s" % (where, failure.code))
            expected = "the charge was made" if rows else "nothing was charged"
            self.assertIn(expected, failure.detail, "%s: %s %s" % (where, failure.code, failure.detail))
        if outcome is not None and "notice" in outcome:
            self.assertTrue(rows, where)
            self.assertIn("the charge was made", outcome["notice"], where)
            self.assertIn("nothing needs approving again", outcome["notice"], where)
        return bool(rows)

    def approve(self, host, approval_id, effect):
        return host.answer_plugin_effect(self.owner(), approval_id, effect, authority=self.envelope())

    # -- the two cases of the open point, one at a time ----------------------------------------------------

    def test_a_full_disk_before_the_charge_says_nothing_was_charged_and_what_to_do(self):
        host = self.composed()
        effect = base._effect(amount=AMOUNT)
        approval_id = host.request_plugin_effect(effect)

        def first_notes_write(_directory, _path, _payload):
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(plugins._EffectNotes, "_replace", staticmethod(first_notes_write)):
            with self.assertRaises(Refused) as caught:
                self.approve(host, approval_id, effect)
        refused = caught.exception
        self.assertEqual(self.charge_rows(), [])
        for words in ("nothing was charged", "No space left on device", str(self.notes.parent),
                      "request the effect again"):
            self.assertIn(words, refused.detail)
        self.assertEqual(self.record(host, approval_id)["state"], "approved")

    def test_a_full_disk_after_the_commit_says_the_charge_was_made_and_the_approval_is_used(self):
        host = self.composed()
        effect = base._effect(amount=AMOUNT)
        approval_id = host.request_plugin_effect(effect)
        real = plugins._EffectNotes._replace

        def full_once_charged(directory, path, payload):
            if self.charge_rows():
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(directory, path, payload)

        with mock.patch.object(plugins._EffectNotes, "_replace", staticmethod(full_once_charged)):
            carried = self.approve(host, approval_id, effect)
        self.assertEqual(len(self.charge_rows()), 1)
        self.assertEqual(self.record(host, approval_id)["state"], "consumed")
        for words in ("the charge was made", "nothing needs approving again", "No space left on device",
                      str(self.notes.parent), "next approval", "next start"):
            self.assertIn(words, carried["notice"])
        self.assertNotIn(approval_id, self.files()["receipts"])
        with self.assertRaises(ApprovalRefused):  # answered again: used up, never charged twice
            self.approve(host, approval_id, effect)
        self.assertEqual(len(self.charge_rows()), 1)
        # Room again: the next approval writes the note that could not be written.
        self.carry(host, "next")
        self.assertEqual(self.files()["receipts"][approval_id], carried["charge"])

    def test_a_note_lost_with_the_process_is_rebuilt_from_the_ledger_at_the_next_start(self):
        host = self.composed()
        effect = base._effect(amount=AMOUNT)
        approval_id = host.request_plugin_effect(effect)
        real = plugins._EffectNotes._replace

        def full_once_charged(directory, path, payload):
            if self.charge_rows():
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(directory, path, payload)

        with mock.patch.object(plugins._EffectNotes, "_replace", staticmethod(full_once_charged)):
            carried = self.approve(host, approval_id, effect)
        self.assertNotIn(approval_id, self.files()["receipts"])
        again = self.restart(host)
        self.assertEqual(self.files()["receipts"].get(approval_id), carried["charge"])
        self.assertEqual(self.record(again, approval_id)["state"], "consumed")
        self.assertEqual(len(self.charge_rows()), 1)

    # -- every write of the path, in turn ------------------------------------------------------------------

    def path_writes(self, writes):
        """The writes of one approval on a fresh state, as the stand-in counts them."""
        host = self.composed()
        writes.attach(host)
        effect = base._effect(amount=AMOUNT, payload={"note": "count"})
        approval_id = host.request_plugin_effect(effect)
        writes.fail(None, "count")
        self.approve(host, approval_id, effect)
        return list(writes.names)

    def fresh(self):
        base._HostCase.setUp(self)  # a new temporary folder and state directory for this round

    def test_every_write_failed_in_turn_then_tried_again_charges_at_most_once(self):
        writes = _Writes(self)
        names = self.path_writes(writes)
        self.assertTrue(any(name.startswith("ledger:INSERT INTO charges") for name in names), names)
        self.assertTrue(any(name == "approval:consumed" for name in names), names)
        for mode in ("full", "killed"):
            for at in range(1, len(names) + 1):
                where = "%s at write %d (%s)" % (mode, at, names[at - 1])
                with self.subTest(where=where):
                    writes.room()
                    self.fresh()
                    host = self.composed()
                    writes.attach(host)
                    effect = base._effect(amount=AMOUNT, payload={"note": "count"})
                    approval_id = host.request_plugin_effect(effect)
                    writes.fail(at, mode)
                    try:
                        outcome, failure = self.attempt(lambda: self.approve(host, approval_id, effect))
                    except _Killed:
                        outcome, failure = None, None
                    except RAW as raw:
                        self.fail("%s: the raw error reached the person: %r" % (where, raw))
                    writes.room()
                    self.assert_truth(host, approval_id, outcome, failure, where)
                    if mode == "full":
                        for again in (lambda: self.approve(host, approval_id, effect),
                                      lambda: host.effect_sink.carry(approval_id, effect)):
                            try:
                                outcome, failure = self.attempt(again)
                            except RAW as raw:
                                self.fail("%s, tried again: the raw error reached the person: %r" % (where, raw))
                            self.assert_truth(host, approval_id, outcome, failure, where + ", tried again")
                    host = self.restart(host)
                    charged = self.assert_truth(host, approval_id, None, None, where + ", after a start")
                    if charged:
                        self.assertIn(approval_id, self.files()["receipts"], where + ": the note was not rebuilt")
                    try:
                        outcome, failure = self.attempt(lambda: host.effect_sink.carry(approval_id, effect))
                    except RAW as raw:
                        self.fail("%s, after a start: the raw error reached the person: %r" % (where, raw))
                    self.assert_truth(host, approval_id, outcome, failure, where + ", carried after a start")

    def ledger_commit(self, host, *, held):
        """The ledger's COMMIT raises as a full disk does: after it held (``held``) or with nothing written."""
        real = host.authority._db  # noqa: SLF001 - the ledger's own connection; every statement still runs on it

        class Ledger:
            def execute(self, sql, *params):
                if sql.strip().upper() == "COMMIT":
                    if held:
                        real.execute(sql, *params)
                    raise sqlite3.OperationalError("database or disk is full")
                return real.execute(sql, *params)

            def __getattr__(self, name):
                return getattr(real, name)

        host.authority._db = Ledger()
        return lambda: setattr(host.authority, "_db", real)

    def test_a_commit_that_failed_says_nothing_was_charged_and_the_approval_is_used_up(self):
        host = self.composed()
        effect = base._effect(amount=AMOUNT)
        approval_id = host.request_plugin_effect(effect)
        self.ledger_commit(host, held=False)
        with self.assertRaises(Refused) as caught:
            self.approve(host, approval_id, effect)
        self.assertEqual(self.charge_rows(), [])
        self.assertEqual(self.record(host, approval_id)["state"], "consumed")
        for words in ("nothing was charged", "used up", "database or disk is full", "request the effect again"):
            self.assertIn(words, caught.exception.detail)

    def test_a_commit_that_raised_after_it_held_answers_with_the_charge(self):
        host = self.composed()
        effect = base._effect(amount=AMOUNT)
        approval_id = host.request_plugin_effect(effect)
        release = self.ledger_commit(host, held=True)
        try:
            outcome, failure = self.attempt(lambda: self.approve(host, approval_id, effect))
        except RAW as raw:
            self.fail("the raw error reached the person: %r" % (raw,))
        release()
        self.assertIsNone(failure, failure and failure.detail)
        self.assertEqual(len(self.charge_rows()), 1)
        self.assertEqual(outcome["approval"]["state"], "consumed")
        self.assertEqual(outcome["charge"]["profile_id"], "prof-1")
        self.assertEqual(self.files()["receipts"][approval_id], outcome["charge"])

    def test_a_failed_approval_store_write_says_nothing_was_charged(self):
        host = self.composed()
        effect = base._effect(amount=AMOUNT)
        approval_id = host.request_plugin_effect(effect)
        save = approval_store.save

        def full_at_answer(tx, record, **options):
            if record.get("state") == "approved":
                raise sqlite3.OperationalError("database or disk is full")
            return save(tx, record, **options)

        with mock.patch.object(approval_store, "save", full_at_answer):
            with self.assertRaises(Refused) as caught:
                self.approve(host, approval_id, effect)
        self.assertIn("nothing was charged", caught.exception.detail)
        self.assertIn("database or disk is full", caught.exception.detail)
        self.assertIn("answer again", caught.exception.detail)
        self.assertEqual(self.record(host, approval_id)["state"], "pending")
        self.assertEqual(self.charge_rows(), [])
        carried = self.approve(host, approval_id, effect)  # room again: the same approval is answered and charged
        self.assertEqual(carried["approval"]["state"], "consumed")
        self.assertEqual(len(self.charge_rows()), 1)


# The base class's own tests run in their own module; here only the tests above.
for _name in [name for name in vars(kept.NotesKeptTests) if name.startswith("test_")]:
    setattr(FullDiskTests, _name, None)


if __name__ == "__main__":
    unittest.main()
