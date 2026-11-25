"""The permission is held first and the routine's charge is made inside that hold (control/api.py).

The real ``ControlAPI`` over the real ``RoutineStore`` (a temporary SQLite file, fake clocks) and the
in-memory run registry; the permission is a stand-in with a lock of its own, as the daemon's end of a
plugin host's channel gives one.
"""

from __future__ import annotations

import threading
import unittest
from contextlib import contextmanager

from recursion_core.control import routines as r
from recursion_core.control.registry import InMemoryRunRegistry
from recursion_core.control.tests.test_routine_bounds import OWNER, a_record
from recursion_core.control.tests.test_routine_charge import ORIGIN, ChargeCase, WritesThenRaises


class Permission:
    """A permission that is read under a lock of its own and that a withdrawal turns to no."""

    def __init__(self):
        self.lock = threading.Lock()
        self.granted = True

    def withdraw(self):
        with self.lock:
            self.granted = False

    @contextmanager
    def held(self):
        with self.lock:
            yield self.granted


class WatchedStore(r.RoutineStore):
    """The routine store; it notes whether the permission is held at each charge, and can withdraw it as
    the start's first (uncharged) read of the record answers."""

    permission = None
    withdraw_at_read = False

    def admit_start(self, origin, asked=None, **kw):
        if kw.get("charge", True):
            self.charged_under_hold.append(self.permission.lock.locked())
        answer = super().admit_start(origin, asked, **kw)
        if self.withdraw_at_read and kw.get("charge", True) is False and not kw.get("reread"):
            self.permission.withdraw()  # withdrawn after the record was read and before the start is held
        return answer


class TheHoldFirstThenTheChargeTest(ChargeCase):
    def budget_of_one(self, registry):
        self.registry = registry
        store = self.store(WatchedStore)
        store.permission, store.charged_under_hold = Permission(), []
        store.admit(OWNER, a_record(budget={"unit": "runs", "limit": 1}))
        return store, self.api(store)

    def start_held(self, api, store, key="k1"):
        return api.call("run.start", {"client_key": key}, origin=ORIGIN, admit=store.permission.held)

    def test_a_withdrawn_start_is_neither_launched_nor_charged_and_a_held_one_keeps_the_charge_rule(self):
        with self.subTest(case="withdrawn between the read of the record and the launch"):
            store, api = self.budget_of_one(InMemoryRunRegistry())
            store.withdraw_at_read = True
            answer = self.start_held(api, store)
            self.assertEqual((answer["code"], answer["detail"]), ("start_refused", "not_admitted"), answer)
            self.assertEqual((self.used(store), self.rows(), self.registry.list(), self.starter.calls),
                             (0, [], [], []))
            self.assertIsNone(store.get("morning-digest")["last_refusal"])
            store.withdraw_at_read, store.permission.granted = False, True  # granted again: the budget is whole
            again = self.start_held(api, store, key="k2")
            self.assertEqual((again["code"], self.used(store), len(self.starter.calls)), ("started", 1, 1), again)
            self.assertEqual(store.charged_under_hold, [True])
        # Held throughout, the put of the launch record raises: the charge rule of a put that raised.
        for label, registry, code, charged, used in (
                ("written", WritesThenRaises(), "started", None, 1),
                ("not written", WritesThenRaises(writes=False), "registry_unavailable", False, 0),
                ("cannot be read back", WritesThenRaises(writes=False, unreadable=1), "registry_unavailable",
                 True, 1)):
            with self.subTest(case=label):
                store, api = self.budget_of_one(registry)
                answer = self.start_held(api, store)
                self.assertEqual((answer["code"], answer.get("charged"), self.used(store)), (code, charged, used),
                                 answer)
                self.assertEqual(store.charged_under_hold, [True])  # charged while the permission was held
                self.assertFalse(store.permission.lock.locked())  # and the hold let go once it answered
                self.assertEqual(len(self.starter.calls), 1 if code == "started" else 0)


class CountingStore:
    """The real routine store behind a stand-in that counts every call made of it."""

    def __init__(self, store):
        self.store, self.calls = store, []

    def __getattr__(self, name):
        method = getattr(self.store, name)

        def counted(*args, **kw):
            self.calls.append(name)
            return method(*args, **kw)
        return counted


class CountingRegistry(InMemoryRunRegistry):
    def __init__(self):
        super().__init__()
        self.puts = 0

    def put(self, rec):
        self.puts += 1
        return super().put(rec)


PARAMS = {"tenant": "default", "engine": "claude_code", "prompt": "p", "client_key": "k1"}  # values in their form


class AWrongOriginIsRefusedBeforeAnythingTest(ChargeCase):
    def test_a_wrong_origin_is_refused_with_words_before_any_write_charge_or_ask(self):
        missing = {name: {k: v for k, v in ORIGIN.items() if k != name} for name in ("occurrence", "record_kind",
                                                                                      "plugin", "kind")}
        wrong = [("agent", {"kind": "agent"}), ("packet", {"kind": "packet", "x": 1}), ("empty", {}),
                 ("kind 5", {"kind": 5}), ("kind None", {"kind": None}), ("an extra key", {**ORIGIN, "extra": 1})]
        wrong += [(f"no {name}", origin) for name, origin in missing.items()]
        for label, origin in wrong:
            with self.subTest(origin=label):
                self.registry = CountingRegistry()
                real = self.store()
                real.admit(OWNER, a_record(budget={"unit": "runs", "limit": 1}))
                store = CountingStore(real)
                api = self.api(store)
                store.calls.clear()  # what the API asked when it was built is not this start's
                asked = []
                answer = api.call("run.start", dict(PARAMS), origin=origin,
                                  admit=lambda: asked.append(1) or Permission().held())
                self.assertEqual((answer["code"], answer["detail"]), ("request_invalid", "origin_not_admitted"))
                self.assertEqual(answer["text"], r.TEXT["origin_not_admitted"])
                self.assertEqual((self.registry.puts, store.calls, asked, self.starter.calls), (0, [], [], []))
                self.assertEqual((real.get("morning-digest")["used"], self.rows()), (0, []))
        with self.assertRaises(ValueError):  # not a mapping: a fault of the in-process caller
            api.call("run.start", dict(PARAMS), origin=["kind", "trigger"])

    def test_the_origin_rule_is_answered_before_the_store_and_the_values(self):
        """A wrong origin is answered origin_not_admitted first: with no routine store, and with bad values."""
        cases = [("an extra key, no routine store", {**ORIGIN, "note": "x"}, PARAMS, False),
                 ("no plugin, no routine store", {k: v for k, v in ORIGIN.items() if k != "plugin"}, PARAMS, False),
                 ("kind agent, no prompt", {"kind": "agent"}, {"tenant": "default", "engine": "claude_code"}, True),
                 ("an extra key, an engine out of its form", {**ORIGIN, "note": "x"},
                  {"tenant": "default", "engine": "nope", "prompt": "p"}, True)]
        for label, origin, params, with_store in cases:
            with self.subTest(origin=label):
                self.registry = CountingRegistry()
                real = self.store()
                real.admit(OWNER, a_record(budget={"unit": "runs", "limit": 1}))
                store = CountingStore(real)
                api = self.api(store if with_store else None)
                store.calls.clear()
                asked = []
                answer = api.call("run.start", dict(params, client_key="k1"), origin=origin,
                                  admit=lambda: asked.append(1) or Permission().held())
                self.assertEqual((answer["code"], answer["detail"]), ("request_invalid", "origin_not_admitted"))
                self.assertEqual(answer["text"], r.TEXT["origin_not_admitted"])
                self.assertEqual((self.registry.puts, store.calls, asked, self.starter.calls), (0, [], [], []))
                self.assertEqual((real.get("morning-digest")["used"], self.rows()), (0, []))


if __name__ == "__main__":
    unittest.main()
