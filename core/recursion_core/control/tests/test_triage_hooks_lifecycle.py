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


if __name__ == "__main__":
    unittest.main(verbosity=2)
