"""R4 finding 8: immutable validation and synchronized close.

Snapshot/close tests cover existing invariants. The ID-grammar and TTL owner questions are
not tests here; they stay in R4 fix-8.md until the owner decides them.
Thread hooks deliberately schedule the gap and test real output and SQLite state.
"""

from __future__ import annotations

import importlib
import sqlite3
import threading
import unittest
from unittest import mock

from recursion_core.plugin_security import ApprovalGate, Refused, canonical, check_effect, parse
from recursion_core.plugin_security import approval as approval_module
from recursion_core.plugin_security.tests.support import (
    Clock, World, authenticate, effect, needs_crypto,
)

canonical_module = importlib.import_module("recursion_core.plugin_security.canonical")


def mutate_in_thread(live, value):
    errors = []

    def mutate():
        try:
            live["amount_minor" if "amount_minor" in live else "a"] = value
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=mutate)
    thread.start()
    thread.join(5)
    if thread.is_alive():
        raise AssertionError("mutator did not finish")
    if errors:
        raise errors[0]


class ValueSnapshotTests(unittest.TestCase):
    def test_effect_checks_the_captured_bytes_not_the_changed_caller_dict(self):
        live = effect(amount=-500)
        real_canonical = approval_module.canonical
        drawn = []
        gate = ApprovalGate(lambda *args: drawn.append(args), authenticate, Clock())

        def capture_then_mutate(value):
            raw = real_canonical(value)
            mutate_in_thread(value, 100)
            return raw

        with mock.patch.object(approval_module, "canonical", side_effect=capture_then_mutate):
            with self.assertRaises(Refused) as refused:
                gate.request(live)
        self.assertEqual(refused.exception.code, "effect_amount")
        self.assertEqual(drawn, [])

    def test_effect_returns_valid_captured_bytes_despite_later_invalid_mutation(self):
        live = effect(amount=100)
        real_canonical = approval_module.canonical

        def capture_then_mutate(value):
            raw = real_canonical(value)
            mutate_in_thread(value, -500)
            return raw

        with mock.patch.object(approval_module, "canonical", side_effect=capture_then_mutate):
            raw = check_effect(live)
        self.assertEqual(parse(raw)["amount_minor"], 100)
        self.assertEqual(live["amount_minor"], -500)

    def test_canonical_refuses_values_inserted_after_initial_validation(self):
        real_check = canonical_module.check_value
        for changed in (1.5, "\u00e9", object()):
            with self.subTest(changed=type(changed).__name__):
                live = {"a": 1}

                def check_then_mutate(value, depth=0):
                    real_check(value, depth)
                    if value is live and depth == 0:
                        mutate_in_thread(live, changed)

                with mock.patch.object(canonical_module, "check_value", side_effect=check_then_mutate):
                    try:
                        with self.assertRaises(Refused) as refused:
                            canonical(live)
                    except (TypeError, ValueError, RuntimeError) as exc:
                        self.fail(f"untyped {type(exc).__name__} escaped canonical")
                self.assertEqual(refused.exception.code, "noncanonical_value")

    def test_canonical_refuses_numeric_key_order_inserted_after_validation(self):
        live = {"2": 1, "10": 2}
        real_check = canonical_module.check_value

        def check_then_mutate(value, depth=0):
            real_check(value, depth)
            if value is live and depth == 0:
                def mutate():
                    live.clear()
                    live.update({2: 1, 10: 2})

                thread = threading.Thread(target=mutate)
                thread.start()
                thread.join(5)
                self.assertFalse(thread.is_alive(), "key mutator did not finish")

        with mock.patch.object(canonical_module, "check_value", side_effect=check_then_mutate):
            with self.assertRaises(Refused) as refused:
                canonical(live)
        self.assertEqual(refused.exception.code, "noncanonical_value")


class NotifyingLock:
    """Expose the attempted acquisition, while still using the original real lock."""

    def __init__(self, lock, progress):
        self.lock, self.progress = lock, progress

    def __enter__(self):
        self.progress.set()
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()


@needs_crypto
class StoreCloseTests(unittest.TestCase):
    def test_close_waits_for_an_in_flight_database_critical_section(self):
        world = World(self)
        for store in (world.authority, world.breaker, world.loader._trust):
            with self.subTest(store=type(store).__name__):
                original_lock, database = store._lock, store._db
                progress, finished = threading.Event(), threading.Event()
                errors = []
                store._lock = NotifyingLock(original_lock, progress)

                def close():
                    try:
                        store.close()
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        finished.set()
                        progress.set()

                thread = threading.Thread(target=close)
                try:
                    with original_lock:
                        thread.start()
                        self.assertTrue(progress.wait(5), "close neither acquired the lock nor returned")
                        # If close bypasses the lock, finished is set and the real SQLite
                        # connection is already unusable. This is an ordering assertion,
                        # not a throughput or scheduler deadline assertion.
                        self.assertFalse(finished.is_set(), "close completed inside a live critical section")
                        self.assertEqual(database.execute("SELECT 1").fetchone(), (1,))
                finally:
                    thread.join(5)  # The original lock has been released, even on failure.
                self.assertFalse(thread.is_alive(), "close did not finish after the lock was released")
                self.assertEqual(errors, [])
                with self.assertRaises(sqlite3.ProgrammingError):
                    database.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
