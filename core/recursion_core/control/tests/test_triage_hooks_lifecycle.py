"""Lifecycle regressions: one store, one start, and a frozen engine table.

Every resource here is synthetic. No provider, token or live daemon is used.
Run from the repository root with PYTHONPATH=core.
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

from recursion_core.control import hooks as H
from recursion_core.routes import engines


def configured(name, function):
    hooks = H.Hooks()
    hooks.set(name, function)
    hooks.freeze()
    return hooks


class Store:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class HooksLifecycleRegressionTest(unittest.TestCase):
    def test_f2_repeated_open_store_does_not_replace_or_leak_the_store(self):
        stores = []

        def factory(state_dir):
            store = Store()
            stores.append(store)
            return store

        hooks = configured("event_store", factory)
        try:
            first = hooks.open_store("/unused")
            try:
                second = hooks.open_store("/unused")
            except H.HookError:
                second = first  # A typed refusal before another factory call is also safe.
            self.assertIs(second, first)
            self.assertIs(hooks.methods.store, first)
            self.assertEqual(len(stores), 1)
        finally:
            hooks.finish_close(H.ServiceContext())
        self.assertTrue(first.closed)

    def test_f3_repeated_start_services_cannot_orphan_a_worker(self):
        class SingleWorker:
            name = "demo"
            ready = True

            def __init__(self):
                self.workers = []

            def start(self, context):
                self.quit = threading.Event()
                self.worker = threading.Thread(target=self.quit.wait, args=(3,), daemon=True)
                self.workers.append((self.quit, self.worker))
                self.worker.start()

            def stop(self, context):
                self.quit.set()
                self.worker.join(1)

        service, store = SingleWorker(), Store()
        hooks = H.Hooks()
        hooks.service(service)
        hooks.set("event_store", lambda state_dir: store)
        hooks.freeze()
        try:
            hooks.open_store("/unused")
            hooks.start_services(H.ServiceContext())
            try:
                hooks.start_services(H.ServiceContext())
            except H.HookError:
                pass  # Either a typed refusal or a no-op is safe.
            hooks.finish_close(H.ServiceContext())
            self.assertTrue(store.closed)
            self.assertFalse(any(worker.is_alive() for _, worker in service.workers),
                             "teardown shut the store with a worker still alive")
            self.assertEqual(len(service.workers), 1)
        finally:
            for quit_event, worker in service.workers:
                quit_event.set()
                worker.join(1)

    def test_frozen_hooks_never_publish_an_unfrozen_engine_table(self):
        hooks = H.bare()
        # Eager initialization at freeze is also a valid arrangement.
        if hooks._engines is not None:
            self.assertTrue(hooks.engines._frozen)
            return
        entered, release = threading.Event(), threading.Event()
        reader_entered, reader_done = threading.Event(), threading.Event()
        observed, failures = [], []
        original_freeze = engines.EngineTable.freeze

        def pause_freeze(table):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test synchronization timed out")
            original_freeze(table)

        def first_reader():
            try:
                hooks.engines
            except BaseException as exc:
                failures.append(exc)

        def second_reader():
            reader_entered.set()
            try:
                observed.append(hooks.engines._frozen)
            except BaseException as exc:
                failures.append(exc)
            finally:
                reader_done.set()

        first = threading.Thread(target=first_reader, daemon=True)
        second = threading.Thread(target=second_reader, daemon=True)
        with mock.patch.object(engines.EngineTable, "freeze", pause_freeze):
            try:
                first.start()
                self.assertTrue(entered.wait(3))
                second.start()
                self.assertTrue(reader_entered.wait(3))
                reader_done.wait(0.2)
            finally:
                release.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(observed, [True])

    def test_a_table_made_before_freeze_is_frozen_before_a_reader_sees_it(self):
        """The composition path: a lane registers an engine, then freeze runs.

        A reader that arrives while the table is being frozen must not observe an open table
        from a composition that already says it is frozen, and must not register into that window.
        """
        hooks = H.Hooks()
        table = hooks.engines
        self.assertFalse(table._frozen)
        entered, release = threading.Event(), threading.Event()
        original = engines.EngineTable.freeze
        seen = []

        def pause(table_self):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test synchronization timed out")
            original(table_self)

        def reader():
            # The saved table, under the same lock freeze holds. Going back through the
            # property would freeze the table as a side effect and hide a freeze that
            # publishes the flag before it freezes the table.
            while not hooks._frozen:
                pass
            with hooks._engines_lock:
                seen.append(table._frozen)
                try:
                    table.register("pi", lambda *args, **kwargs: None)
                    seen.append("accepted")
                except engines.EngineTableError:
                    seen.append("refused")

        freer = threading.Thread(target=hooks.freeze, daemon=True)
        watcher = threading.Thread(target=reader, daemon=True)
        with mock.patch.object(engines.EngineTable, "freeze", pause):
            try:
                freer.start()
                self.assertTrue(entered.wait(3), "freeze did not reach the table")
                watcher.start()
                watcher.join(0.2)
            finally:
                release.set()
                freer.join(3)
                watcher.join(3)
        self.assertFalse(freer.is_alive() or watcher.is_alive())
        self.assertEqual(seen, [True, "refused"])
        self.assertTrue(table._frozen)
        self.assertNotIn("pi", tuple(table.names()))

    def test_a_second_directory_is_refused_and_the_first_store_stays(self):
        stores = []

        def factory(state_dir):
            store = Store()
            stores.append(state_dir)
            return store

        hooks = configured("event_store", factory)
        try:
            first = hooks.open_store("/state-a")
            with self.assertRaises(H.HookError):
                hooks.open_store("/state-b")
            self.assertIs(hooks.open_store("/state-a"), first)
            self.assertIs(hooks.store, first)
            self.assertEqual(stores, ["/state-a"])
        finally:
            hooks.finish_close(H.ServiceContext())
        self.assertTrue(first.closed)

    def test_a_factory_that_returns_none_is_called_once(self):
        calls = []

        def factory(state_dir):
            calls.append(state_dir)
            return None

        hooks = configured("event_store", factory)
        self.assertIsNone(hooks.open_store("/state-a"))
        self.assertIsNone(hooks.open_store("/state-a"))
        self.assertEqual(calls, ["/state-a"])

    def test_start_services_after_teardown_with_no_service_is_refused(self):
        hooks = H.Hooks()
        hooks.freeze()
        hooks.finish_close(H.ServiceContext())
        with self.assertRaises(H.HookError):
            hooks.start_services(H.ServiceContext())


if __name__ == "__main__":
    unittest.main(verbosity=2)
