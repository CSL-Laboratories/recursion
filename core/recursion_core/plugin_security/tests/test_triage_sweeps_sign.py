"""The revocation store allocates generations from the durable row (triage of the Nov 15 sweeps).

PS1, PS7 and PS8 of the triage of events-routing-plugins, as its red-test patch wrote them (each
was red on master ff3f8071), plus two tests of the rules this change adds. The patch's PS20 test
(a mixed-case reserved host) is not here: draft #374 answers it. Every key id here is made up;
nothing is signed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest

from recursion_core.plugin_security import (
    CoreHealth, PluginLoader, Refused, ReservedEffects, TrustStore, create_revocation_store,
)
from recursion_core.plugin_security.manifest import Grant, effective_permissions, parse_manifest
from recursion_core.plugin_security.signing import key_id
from recursion_core.plugin_security import canonical

KID = "ed25519:" + "ab" * 16


def _store(test):
    folder = tempfile.mkdtemp(prefix="triage-rev-")
    test.addCleanup(shutil.rmtree, folder, True)
    path = os.path.join(folder, "revoked.sqlite3")
    create_revocation_store(path)
    return path


def _set_row(path, generation):
    db = sqlite3.connect(path)
    try:
        db.execute("INSERT OR REPLACE INTO revoked VALUES (?, ?)", (KID, generation))
        db.commit()
    finally:
        db.close()


class RevocationTests(unittest.TestCase):
    def test_PS1_a_stale_store_handle_cannot_roll_a_generation_back(self):
        # s2b-plugin-sec-manifest F1 (P1), s6-disk F1 (P1), s2-sol F1, s2-astra F6, s3b-astra F01, s3b-sol F01.
        path = _store(self)
        a = TrustStore({}, revocation_store=path)
        b = TrustStore({}, revocation_store=path)  # opened before either revokes
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        self.assertEqual([a.revoke(KID), a.revoke(KID)], [1, 2])
        try:
            third = b.revoke(KID)
        except Refused:
            third = None  # refusing the stale writer is an answer too, as long as nothing goes back
        if third is not None:
            self.assertEqual(third, 3)
        fresh = TrustStore({}, revocation_store=path)
        self.addCleanup(fresh.close)
        self.assertGreaterEqual(fresh.generation(KID), 2)

    def test_PS1_a_grant_for_generation_1_stays_off_after_the_third_revocation(self):
        # The consequence the P1 readers named: order comes back under an obsolete grant.
        path = _store(self)
        a = TrustStore({}, revocation_store=path)
        b = TrustStore({}, revocation_store=path)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.revoke(KID)
        a.revoke(KID)
        try:
            b.revoke(KID)
        except Refused:
            pass
        fresh = TrustStore({}, revocation_store=path)
        self.addCleanup(fresh.close)
        source = b"VALUE = 1\n"
        manifest_key = "33" * 32
        manifest = parse_manifest(canonical({
            "schema": "csl-plugin-manifest/vNext", "plugin_id": "demo.plugin", "version": "1.0.0",
            "artifact_sha256": hashlib.sha256(source).hexdigest(), "dependency_lock_sha256": "0" * 64,
            "install_scripts": [], "developer_key": manifest_key, "permissions": ["read", "propose", "order"],
            "network_destinations": [], "credential_refs": []}))
        named = {
            "unsigned_order_acknowledged": True,
            "order_after_revoked_key": KID,
            "order_after_revocation_generation": 1,
        }
        # Draft #374 binds a manual grant to developer_key_id when Grant has that field,
        # and otherwise treats order_after_revoked_key as the bound key. Name the manifest's
        # key so order stays off only because this revocation's generation has moved on.
        if "developer_key_id" in Grant.__dataclass_fields__:
            named["developer_key_id"] = key_id(manifest_key)
        grant = Grant("demo.plugin", manifest.artifact_sha256, None, ("read", "propose", "order"),
                      **named)
        perms, _ = effective_permissions(manifest, "unsigned", None, grant, KID, fresh.generation(KID))
        self.assertIn("read", perms)
        self.assertIn("propose", perms)
        self.assertNotIn("order", perms)

    def test_PS7_malformed_key_ids_are_refused_and_not_stored(self):
        # s2b-plugin-sec-manifest F4; signing.py:46-48 defines the grammar.
        path = _store(self)
        trust = TrustStore({}, revocation_store=path)
        self.addCleanup(trust.close)
        loader = PluginLoader(trust, {}, ReservedEffects(), CoreHealth())
        for bad in ("ed25519:", "ed25519:not-hex", "ed25519:\nINJECTED-LINE"):
            with self.subTest(bad=bad):
                with self.assertRaises(Refused) as caught:
                    loader.revoke_key(bad)
                self.assertEqual(caught.exception.code, "invalid_key_id")
                self.assertNotIn("\n", str(caught.exception))
        db = sqlite3.connect(path)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM revoked").fetchone()[0], 0)
        finally:
            db.close()

    def test_PS8_oversized_stored_generation_is_a_refusal(self):
        # s2b-plugin-sec-manifest F5.
        path = _store(self)
        db = sqlite3.connect(path)
        db.execute("INSERT INTO revoked VALUES (?, ?)", (KID, 2 ** 63 - 1))
        db.commit()
        db.close()
        try:
            trust = TrustStore({}, revocation_store=path)
        except Refused:
            return  # refusing the store at open is an answer too
        self.addCleanup(trust.close)
        with self.assertRaises(Refused):
            trust.revoke(KID)


class GenerationRuleTests(unittest.TestCase):
    """The rules this change adds, each with the refusal a caller sees."""

    def _open(self, path):
        trust = TrustStore({}, revocation_store=path)
        self.addCleanup(trust.close)
        return trust

    def test_the_trust_store_itself_refuses_a_malformed_key_id_and_changes_nothing(self):
        path = _store(self)
        trust = self._open(path)
        for bad in ("ed25519:" + "AB" * 16, "ed25519:" + "ab" * 16 + "\n", "ed25519:" + "ab" * 17, None):
            with self.subTest(bad=bad):
                with self.assertRaises(Refused) as caught:
                    trust.revoke(bad)
                self.assertEqual((caught.exception.code, caught.exception.detail), ("invalid_key_id", ""))
        self.assertEqual(trust.revoked, frozenset())
        self.assertEqual(self._open(path).revoked, frozenset())

    def test_a_failed_write_is_not_reused_by_the_next_one(self):
        # Memory said generation 1 while the write failed; the next revoke must say 2, not 1 again.
        path = _store(self)
        trust = self._open(path)
        trust._db.execute("PRAGMA query_only=ON")
        with self.assertRaises(Refused) as caught:
            trust.revoke(KID)
        self.assertEqual(caught.exception.code, "revocation_not_durable")
        self.assertEqual(trust.generation(KID), 1)
        trust._db.execute("PRAGMA query_only=OFF")
        self.assertEqual(trust.revoke(KID), 2)
        self.assertEqual(self._open(path).generation(KID), 2)

    def test_a_row_another_writer_left_as_text_writes_nothing(self):
        path = _store(self)
        trust = self._open(path)
        _set_row(path, "two")
        with self.assertRaises(Refused) as caught:
            trust.revoke(KID)
        self.assertEqual(caught.exception.code, "revocation_not_durable")
        self.assertIn(KID, trust.revoked)  # memory still fails closed
        db = sqlite3.connect(path)
        try:
            self.assertEqual(db.execute("SELECT generation FROM revoked").fetchall(), [("two",)])
        finally:
            db.close()
        _set_row(path, 4)
        self.assertEqual(trust.revoke(KID), 5)  # the handle is usable after the refusal
        self.assertEqual(trust.generation(KID), 5)  # and its memory follows the row

    def _trigger(self, path, sql):
        db = sqlite3.connect(path, isolation_level=None)
        try:
            db.execute(sql)
        finally:
            db.close()

    def test_a_write_refused_inside_the_transaction_leaves_the_store_writable(self):
        path = _store(self)
        trust = self._open(path)
        self._trigger(path, "CREATE TRIGGER no_insert BEFORE INSERT ON revoked "
                            "BEGIN SELECT RAISE(ABORT, 'refused by a test trigger'); END")
        with self.assertRaises(Refused) as caught:
            trust.revoke(KID)
        self.assertEqual(caught.exception.code, "revocation_not_durable")
        self._trigger(path, "DROP TRIGGER no_insert")  # waits on a write lock the handle kept
        self.assertEqual(trust.revoke(KID), 2)

    def test_a_stored_row_that_reads_back_wrong_is_not_committed(self):
        path = _store(self)
        trust = self._open(path)
        self._trigger(path, "CREATE TRIGGER rewrite AFTER INSERT ON revoked "
                            "BEGIN UPDATE revoked SET generation = 99 WHERE key_id = new.key_id; END")
        with self.assertRaises(Refused) as caught:
            trust.revoke(KID)
        self.assertEqual(caught.exception.code, "revocation_not_durable")
        db = sqlite3.connect(path)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM revoked").fetchone()[0], 0)
        finally:
            db.close()

    def test_handles_revoking_at_once_each_get_their_own_generation(self):
        # The row is read and written in one write transaction: no two revokes share a number.
        path = _store(self)
        handles = [self._open(path) for _ in range(4)]
        per_handle = 25
        barrier = threading.Barrier(len(handles))
        results, errors = [], []

        def work(trust):
            try:
                barrier.wait(timeout=30)
                for _ in range(per_handle):
                    results.append(trust.revoke(KID))
            except BaseException as exc:  # reported below, never lost in the thread
                barrier.abort()
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(h,)) for h in handles]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        total = len(handles) * per_handle
        self.assertEqual(sorted(results), list(range(1, total + 1)))
        self.assertEqual(self._open(path).generation(KID), total)

    def test_the_largest_generation_is_written_and_the_next_is_refused(self):
        path = _store(self)
        trust = self._open(path)
        _set_row(path, 2 ** 63 - 2)
        self.assertEqual(trust.revoke(KID), 2 ** 63 - 1)
        with self.assertRaises(Refused) as caught:
            trust.revoke(KID)
        self.assertEqual(caught.exception.code, "revocation_not_durable")
        self.assertEqual(self._open(path).generation(KID), 2 ** 63 - 1)

    def test_a_row_another_writer_lowered_below_1_writes_nothing(self):
        # A stored 0 or a negative is not a floor. Handing generation 1 out again would
        # let a grant that cites generation 1 take order. The row stays as it was, and a
        # later handle refuses the store, so that grant is not applied from disk.
        for generation in (0, -1):
            with self.subTest(generation=generation):
                path = _store(self)
                trust = self._open(path)
                _set_row(path, generation)
                with self.assertRaises(Refused) as caught:
                    trust.revoke(KID)
                self.assertEqual(caught.exception.code, "revocation_not_durable")
                db = sqlite3.connect(path)
                try:
                    self.assertEqual(
                        db.execute("SELECT generation FROM revoked").fetchall(), [(generation,)])
                finally:
                    db.close()
                applied = False
                try:
                    fresh = TrustStore({}, revocation_store=path)
                except Refused as exc:
                    self.assertEqual(exc.code, "revocation_store_invalid")
                else:
                    self.addCleanup(fresh.close)
                    source = b"VALUE = 1\n"
                    manifest_key = "33" * 32
                    manifest = parse_manifest(canonical({
                        "schema": "csl-plugin-manifest/vNext", "plugin_id": "demo.plugin",
                        "version": "1.0.0",
                        "artifact_sha256": hashlib.sha256(source).hexdigest(),
                        "dependency_lock_sha256": "0" * 64,
                        "install_scripts": [], "developer_key": manifest_key,
                        "permissions": ["read", "propose", "order"],
                        "network_destinations": [], "credential_refs": []}))
                    named = {
                        "unsigned_order_acknowledged": True,
                        "order_after_revoked_key": KID,
                        "order_after_revocation_generation": 1,
                    }
                    if "developer_key_id" in Grant.__dataclass_fields__:
                        named["developer_key_id"] = key_id(manifest_key)
                    grant = Grant(
                        "demo.plugin", manifest.artifact_sha256, None,
                        ("read", "propose", "order"), **named)
                    perms, _ = effective_permissions(
                        manifest, "unsigned", None, grant, KID, fresh.generation(KID))
                    applied = "order" in perms
                self.assertFalse(applied)

    def test_open_refuses_a_generation_below_1_or_not_an_integer(self):
        path = _store(self)
        for generation in (-1, 0, "two", b"\x01"):
            with self.subTest(generation=generation):
                _set_row(path, generation)
                with self.assertRaises(Refused) as caught:
                    TrustStore({}, revocation_store=path)
                self.assertEqual(caught.exception.code, "revocation_store_invalid")
        db = sqlite3.connect(path)
        try:
            db.execute("DROP TABLE revoked")
            db.execute(
                "CREATE TABLE revoked (key_id TEXT PRIMARY KEY, generation INTEGER)")
            db.execute("INSERT INTO revoked VALUES (?, NULL)", (KID,))
            db.commit()
        finally:
            db.close()
        with self.assertRaises(Refused) as caught:
            TrustStore({}, revocation_store=path)
        self.assertEqual(caught.exception.code, "revocation_store_invalid")
        again = sqlite3.connect(path)
        try:
            self.assertEqual(again.execute("SELECT generation FROM revoked").fetchone(), (None,))
        finally:
            again.close()

    def test_a_handle_with_no_store_refuses_revoke(self):
        trust = TrustStore({})
        self.addCleanup(trust.close)
        with self.assertRaises(Refused) as caught:
            trust.revoke(KID)
        self.assertEqual(
            (caught.exception.code, caught.exception.detail),
            ("revocation_not_durable", KID))

    def test_a_roll_back_that_itself_fails_does_not_leave_the_handle_unable_to_revoke(self):
        # A write that fails, and a roll-back that also fails, must not leave this handle
        # inside a transaction. The next revoke on the same handle still writes.
        path = _store(self)
        trust = self._open(path)
        real = trust._db

        class _Poison:
            def execute(self, sql, *args, **kwargs):
                word = sql.lstrip().split(None, 1)[0].upper()
                if word == "INSERT":
                    raise sqlite3.OperationalError("insert refused by the test")
                if word == "ROLLBACK":
                    raise sqlite3.OperationalError("roll-back refused by the test")
                return real.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(real, name)

        trust._db = _Poison()
        with self.assertRaises(Refused) as caught:
            trust.revoke(KID)
        self.assertEqual(caught.exception.code, "revocation_not_durable")
        self.assertFalse(trust._db.in_transaction)
        self.assertEqual(trust.revoke(KID), 2)
        self.assertEqual(self._open(path).generation(KID), 2)


class RefusedWriteTakesUpTheRowTests(unittest.TestCase):
    """After a refused write, a handle answers at least one past the durable row.

    Two handles on one store: A revokes five times, then B's write is refused. B must answer
    6, one past the row (5), not its own next number (1) and not the row itself: a refused
    revoke counts as a new revocation, so neither a grant citing 1 nor a grant citing 5
    (A's last revocation) has order on B. Nothing lowers memory: a damaged row, or a row
    that cannot be read, leaves B's memory as it was.
    """

    def _row_at_5_and_a_stale_handle(self):
        path = _store(self)
        a = TrustStore({}, revocation_store=path)
        b = TrustStore({}, revocation_store=path)  # opened before A revokes
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        self.assertEqual([a.revoke(KID) for _ in range(5)], [1, 2, 3, 4, 5])
        self.assertIsNone(b.generation(KID))
        return path, b

    def _order_for_a_grant_citing(self, cited, generation):
        """Whether order stays on for a grant citing ``cited`` when the handle answers ``generation``.

        It calls ``effective_permissions`` with KID as the revoked key and ``generation`` as
        its generation, the two values the loader hands it. It does not exercise the loader's
        own lookup (``PluginLoader`` derives the key id from the manifest's developer key and
        asks the trust store); the manifest's key here is not KID.
        """
        source = b"VALUE = 1\n"
        manifest = parse_manifest(canonical({
            "schema": "csl-plugin-manifest/vNext", "plugin_id": "demo.plugin", "version": "1.0.0",
            "artifact_sha256": hashlib.sha256(source).hexdigest(), "dependency_lock_sha256": "0" * 64,
            "install_scripts": [], "developer_key": "33" * 32, "permissions": ["read", "propose", "order"],
            "network_destinations": [], "credential_refs": []}))
        grant = Grant("demo.plugin", manifest.artifact_sha256, None, ("read", "propose", "order"),
                      unsigned_order_acknowledged=True, order_after_revoked_key=KID,
                      order_after_revocation_generation=cited)
        perms, _ = effective_permissions(manifest, "unsigned", None, grant, KID, generation)
        return "order" in perms

    def _refused(self, trust):
        with self.assertRaises(Refused) as caught:
            trust.revoke(KID)
        self.assertEqual(caught.exception.code, "revocation_not_durable")

    def _no_order_before_6(self, b):
        self.assertFalse(self._order_for_a_grant_citing(1, b.generation(KID)))
        self.assertFalse(self._order_for_a_grant_citing(5, b.generation(KID)))
        self.assertTrue(self._order_for_a_grant_citing(6, b.generation(KID)))  # the helper can say yes

    def test_a_write_refused_after_the_read_answers_one_past_the_stored_generation(self):
        path, b = self._row_at_5_and_a_stale_handle()
        b._db.execute("PRAGMA query_only=ON")  # the INSERT fails; the SELECT before it ran
        self._refused(b)
        self.assertEqual(b.generation(KID), 6)
        self._no_order_before_6(b)
        b._db.execute("PRAGMA query_only=OFF")
        self.assertEqual(b.revoke(KID), 7)

    def test_a_write_refused_before_the_read_answers_one_past_the_stored_generation(self):
        path, b = self._row_at_5_and_a_stale_handle()
        b._db.execute("PRAGMA busy_timeout=0")
        holder = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")  # B's own BEGIN IMMEDIATE is refused: locked
        try:
            self._refused(b)
        finally:
            holder.execute("ROLLBACK")
        self.assertEqual(b.generation(KID), 6)
        self._no_order_before_6(b)
        b._db.execute("PRAGMA busy_timeout=30000")
        self.assertEqual(b.revoke(KID), 7)

    def test_a_write_that_writes_nothing_answers_one_past_the_stored_generation(self):
        # The row is at the largest generation, so the store's own check writes nothing.
        path = _store(self)
        b = TrustStore({}, revocation_store=path)
        self.addCleanup(b.close)
        _set_row(path, 2 ** 63 - 1)
        self._refused(b)
        self.assertEqual(b.generation(KID), 2 ** 63)
        self.assertFalse(self._order_for_a_grant_citing(1, b.generation(KID)))
        self.assertFalse(self._order_for_a_grant_citing(2 ** 63 - 1, b.generation(KID)))

    def test_a_row_at_the_handles_own_next_number_answers_one_past_it(self):
        # B wrote 4; A then wrote 5, the number B would take next. B's refused write
        # counts after A's: B answers 6, so a grant citing A's 5 has no order on B.
        path = _store(self)
        a = TrustStore({}, revocation_store=path)
        b = TrustStore({}, revocation_store=path)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        self.assertEqual([b.revoke(KID) for _ in range(4)], [1, 2, 3, 4])
        self.assertEqual(a.revoke(KID), 5)
        b._db.execute("PRAGMA query_only=ON")
        self._refused(b)
        self.assertEqual(b.generation(KID), 6)
        self.assertFalse(self._order_for_a_grant_citing(5, b.generation(KID)))

    def test_one_handle_refused_at_row_5_answers_6_as_before(self):
        path = _store(self)
        trust = TrustStore({}, revocation_store=path)
        self.addCleanup(trust.close)
        self.assertEqual([trust.revoke(KID) for _ in range(5)], [1, 2, 3, 4, 5])
        trust._db.execute("PRAGMA query_only=ON")
        self._refused(trust)
        self.assertEqual(trust.generation(KID), 6)
        self.assertFalse(self._order_for_a_grant_citing(5, trust.generation(KID)))

    def test_a_refused_write_never_lowers_memory_below_its_own_number(self):
        # One handle, row 3: its first refused write answers 4 (one past the row, and its
        # own next number); a second refused write answers 5, its own number, not row + 1.
        path = _store(self)
        trust = TrustStore({}, revocation_store=path)
        self.addCleanup(trust.close)
        self.assertEqual([trust.revoke(KID) for _ in range(3)], [1, 2, 3])
        trust._db.execute("PRAGMA query_only=ON")
        self._refused(trust)
        self.assertEqual(trust.generation(KID), 4)
        self._refused(trust)
        self.assertEqual(trust.generation(KID), 5)
        self.assertFalse(self._order_for_a_grant_citing(4, trust.generation(KID)))

    def test_a_row_below_1_leaves_memory_as_it_was(self):
        for damage in (0, -4):
            with self.subTest(damage=damage):
                path = _store(self)
                trust = TrustStore({}, revocation_store=path)
                self.addCleanup(trust.close)
                self.assertEqual([trust.revoke(KID) for _ in range(2)], [1, 2])
                _set_row(path, damage)
                self._refused(trust)  # the store's own check writes nothing
                self.assertEqual(trust.generation(KID), 3)
                self.assertFalse(self._order_for_a_grant_citing(1, trust.generation(KID)))

    def test_a_damaged_row_or_a_failed_read_leaves_memory_as_it_was(self):
        for damage in (7.5, "seven", "no table"):
            with self.subTest(damage=damage):
                path, b = self._row_at_5_and_a_stale_handle()
                db = sqlite3.connect(path)
                try:
                    if damage == "no table":
                        db.execute("ALTER TABLE revoked RENAME TO revoked_moved")
                    else:
                        db.execute("UPDATE revoked SET generation = ? WHERE key_id = ?",
                                   (damage, KID))
                    db.commit()
                finally:
                    db.close()
                self._refused(b)
                self.assertEqual(b.generation(KID), 1)
                self.assertIn(KID, b.revoked)  # the key stays revoked in memory

if __name__ == "__main__":
    unittest.main()
