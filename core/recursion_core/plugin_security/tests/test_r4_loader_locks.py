"""R4 findings 3 and 7 at the loader (PLUGIN-HOST PR 1).

Finding 3: ``PluginLoader.load`` held the loader's one lock across ``exec()``, so an emergency
``revoke_key`` waited for another plugin's import while an active plugin under the revoked key kept
``order``. Now loads are serialised by their own lock and the state lock is never held while plugin
code runs; a key revoked during a plugin's import turns that plugin's ``order`` off before it is
active.

Finding 7 (pre-execution refusal): a plugin left with no effective permission is refused
``plugin_not_granted`` and its bytes are neither compiled nor run.

Every plugin here is unsigned with a manual grant and every key is a made-up value, so nothing here
needs ``cryptography`` and it runs on every interpreter. The GIL state is SIMULATED (``FakeGilProbe``).
Threads are synchronised on events; the only timeouts are watchdogs that end a broken run instead
of hanging it, and no assertion depends on how long anything took.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import unittest
from unittest import mock

from recursion_core.plugin_security import CoreHealth, Grant, PluginLoader, Refused, ReservedEffects, TrustStore
from recursion_core.plugin_security import create_revocation_store
from recursion_core.plugin_security import loader as loader_module
from recursion_core.plugin_security.loader import NOT_GRANTED
from recursion_core.plugin_security.signing import key_id
from recursion_core.plugin_security.tests.support import FakeGilProbe, manifest_bytes, temp_dir

DEVELOPER = "22" * 32  # a made-up public key: unsigned plugins name it and nothing verifies against it
OTHER_DEVELOPER = "33" * 32
WATCHDOG_S = 60.0       # ends a broken run; never part of an assertion


def _digest(source: bytes) -> str:
    return hashlib.sha256(source).hexdigest()


class _Loaders(unittest.TestCase):
    def loader(self, grants, probe=None):
        store = os.path.join(temp_dir(self), "revoked.sqlite3")
        create_revocation_store(store)
        trust = TrustStore({}, revocation_store=store)
        self.addCleanup(trust.close)
        return PluginLoader(trust, {g.plugin_id: g for g in grants}, ReservedEffects(), CoreHealth(),
                            probe or FakeGilProbe())

    @staticmethod
    def grant(plugin_id, source, permissions):
        return Grant(plugin_id, _digest(source), None, tuple(permissions), unsigned_order_acknowledged=True)

    @staticmethod
    def manifest(plugin_id, source, permissions, developer=DEVELOPER):
        return manifest_bytes(developer, plugin_id=plugin_id, source=source, permissions=tuple(permissions))


class RevocationDuringImportTests(_Loaders):
    """R4 finding 3: ``revoke_key`` never waits for a plugin's import (fix-3's revocation test, adapted)."""

    def setUp(self):
        self.events = []  # what happened, in order
        self.in_import, self.release = threading.Event(), threading.Event()
        sys._r4_locks_hooks = (self.in_import, self.release, self.events)
        self.addCleanup(lambda: delattr(sys, "_r4_locks_hooks"))
        self.addCleanup(self.release.set)

    SLOW = (b"import sys\n"
            b"_in, _release, _events = sys._r4_locks_hooks\n"
            b"_in.set()\n"
            b"_release.wait(%d)\n"
            b"_events.append('import ended')\n" % int(WATCHDOG_S))
    ACTIVE = b"VALUE = 1\n"

    def start_slow_load(self, loader, permissions):
        outcome = {}

        def run():
            try:
                outcome["loaded"] = loader.load(self.manifest("slow.plugin", self.SLOW, permissions), None, self.SLOW)
            except BaseException as exc:  # noqa: BLE001 - reported to the test thread
                outcome["error"] = exc
        thread = threading.Thread(target=run, name="r4-slow-load")
        thread.start()
        self.addCleanup(lambda: (self.release.set(), thread.join(WATCHDOG_S)))  # release first, then join
        self.assertTrue(self.in_import.wait(WATCHDOG_S), "the slow plugin never started importing")
        return thread, outcome

    def revoke_in_thread(self, loader, kid):
        def run():
            try:
                self.events.append(("revoked", tuple(loader.revoke_key(kid))))
            finally:
                self.release.set()  # only now may the import end
        thread = threading.Thread(target=run, name="r4-revoke")
        thread.start()
        return thread

    def test_revoke_key_does_not_wait_for_another_plugins_import(self):
        loader = self.loader([self.grant("active.plugin", self.ACTIVE, ("read", "order")),
                              self.grant("slow.plugin", self.SLOW, ("read", "order"))])
        loader.load(self.manifest("active.plugin", self.ACTIVE, ("read", "order")), None, self.ACTIVE)
        self.assertEqual(loader.permissions("active.plugin"), ("read", "order"))
        slow, outcome = self.start_slow_load(loader, ("read", "order"))
        revoke = self.revoke_in_thread(loader, key_id(DEVELOPER))
        revoke.join(WATCHDOG_S)
        slow.join(WATCHDOG_S)
        # The revocation finished while the import was still held (the import ends only after it).
        self.assertEqual(self.events, [("revoked", ("active.plugin",)), "import ended"])
        self.assertEqual(loader.permissions("active.plugin"), ("read",))
        self.assertNotIn("error", outcome)

    def test_a_key_revoked_while_its_plugin_imports_leaves_that_plugin_without_order(self):
        loader = self.loader([self.grant("slow.plugin", self.SLOW, ("read", "order"))])
        slow, outcome = self.start_slow_load(loader, ("read", "order"))
        self.revoke_in_thread(loader, key_id(DEVELOPER)).join(WATCHDOG_S)
        slow.join(WATCHDOG_S)
        self.assertEqual(self.events, [("revoked", ()), "import ended"])
        loaded = outcome["loaded"]
        self.assertEqual(loaded.permissions, ("read",))
        self.assertEqual(loader.permissions("slow.plugin"), ("read",))
        self.assertIn(("signer_key_revoked_order_off", key_id(DEVELOPER)),
                      [(f["code"], f["detail"]) for f in loaded.findings])
        # The next load of a plugin under that key is unsigned-and-revoked: order stays off there too.
        self.assertIn(key_id(DEVELOPER), loader._trust.revoked)

    def test_a_key_revoked_while_a_plugin_without_order_imports_changes_nothing_for_it(self):
        loader = self.loader([self.grant("slow.plugin", self.SLOW, ("read",))])
        slow, outcome = self.start_slow_load(loader, ("read",))
        self.revoke_in_thread(loader, key_id(DEVELOPER)).join(WATCHDOG_S)
        slow.join(WATCHDOG_S)
        loaded = outcome["loaded"]
        self.assertEqual(loaded.permissions, ("read",))
        self.assertNotIn("signer_key_revoked_order_off", [f["code"] for f in loaded.findings])

    def test_another_developers_revocation_during_an_import_leaves_its_order(self):
        loader = self.loader([self.grant("slow.plugin", self.SLOW, ("read", "order"))])
        slow, outcome = self.start_slow_load(loader, ("read", "order"))
        self.revoke_in_thread(loader, key_id(OTHER_DEVELOPER)).join(WATCHDOG_S)
        slow.join(WATCHDOG_S)
        self.assertEqual(outcome["loaded"].permissions, ("read", "order"))


class SerialLoadTests(_Loaders):
    """Loads are serial (review 1 of #380, Astra R6): a second load is not admitted while one imports."""

    def test_a_second_load_waits_and_then_finds_the_plugin_active(self):
        source = b"VALUE = 1\n"
        loader = self.loader([self.grant("demo.plugin", source, ("read",))])
        raw = self.manifest("demo.plugin", source, ("read",))
        in_first, second_waiting, release = threading.Event(), threading.Event(), threading.Event()
        admitted, errors = [], []
        real_admit, real_lock = loader._verify_admit, loader._load_lock

        class Observed:  # the real load lock, which says when the second load reaches it
            def __enter__(self):
                if threading.current_thread().name == "r4-second":
                    second_waiting.set()
                real_lock.acquire()

            def __exit__(self, *exc):
                real_lock.release()

        def admit(*args):
            admitted.append(threading.current_thread().name)
            if threading.current_thread().name == "r4-second":
                second_waiting.set()  # without the load lock the second load gets here at once
            return real_admit(*args)

        def execute(_code, _namespace):  # stands in for the import of the first load only
            if threading.current_thread().name == "r4-first":
                in_first.set()
                release.wait(WATCHDOG_S)

        def run():
            try:
                loader.load(raw, None, source)
            except Refused as exc:
                errors.append(exc.code)
        loader._load_lock = Observed()
        with mock.patch.object(loader, "_verify_admit", side_effect=admit), \
                mock.patch.object(loader_module, "exec", create=True, side_effect=execute):
            first, second = (threading.Thread(target=run, name=n) for n in ("r4-first", "r4-second"))
            first.start()
            self.addCleanup(lambda: (release.set(), first.join(WATCHDOG_S), second.join(WATCHDOG_S)))
            self.assertTrue(in_first.wait(WATCHDOG_S))
            second.start()
            self.assertTrue(second_waiting.wait(WATCHDOG_S))
            self.assertEqual(admitted, ["r4-first"], "a second load was admitted while the first imported")
            release.set()
            first.join(WATCHDOG_S)
            second.join(WATCHDOG_S)
        self.assertEqual((admitted, errors), (["r4-first", "r4-second"], ["plugin_already_active"]))


class PreExecutionRefusalTests(_Loaders):
    """R4 finding 7: no effective permission, no compile and no exec, and a typed code."""

    SOURCE = b"import sys\nsys._r4_ungranted_ran = True\n"

    def setUp(self):
        self.addCleanup(lambda: hasattr(sys, "_r4_ungranted_ran") and delattr(sys, "_r4_ungranted_ran"))

    def cases(self):
        src = self.SOURCE
        return {
            "no grant at all": ([], ("read",)),
            "a grant for another digest": ([self.grant("demo.plugin", b"other bytes", ("read",))], ("read",)),
            "a grant for a signer it does not have": (
                [Grant("demo.plugin", _digest(src), key_id(DEVELOPER), ("read",))], ("read",)),
            "nothing it asked for is granted": ([self.grant("demo.plugin", src, ("panel",))], ("read",)),
            "it asks for nothing": ([self.grant("demo.plugin", src, ())], ()),
        }

    def test_a_plugin_with_no_effective_permission_is_never_compiled_or_run(self):
        for name, (grants, requested) in self.cases().items():
            with self.subTest(name):
                probe = FakeGilProbe()
                loader = self.loader(grants, probe)
                raw = self.manifest("demo.plugin", self.SOURCE, requested)
                with mock.patch.object(loader_module, "compile", create=True,
                                       side_effect=AssertionError("compiled")) as compiled:
                    with self.assertRaises(Refused) as ctx:
                        loader.load(raw, None, self.SOURCE)
                self.assertEqual(ctx.exception.code, NOT_GRANTED)
                compiled.assert_not_called()
                self.assertFalse(hasattr(sys, "_r4_ungranted_ran"), "ungranted plugin code ran")
                self.assertEqual(loader._active, {})
                self.assertEqual(loader.permissions("demo.plugin"), ())
                self.assertEqual(probe.calls, 1, "the GIL is still read after a refused load")
                self.assertEqual(loader.last_findings[-1]["code"], "effective_permissions")
                # doctor() runs the same checks and ends in the same refusal.
                self.assertEqual(loader.doctor(raw, None, self.SOURCE)[-1],
                                 {"stage": "admit", "code": NOT_GRANTED, "severity": "refuse",
                                  "detail": "no effective permission; the plugin's code was not compiled or run"})

    def test_one_effective_permission_is_enough_to_run(self):
        loader = self.loader([self.grant("demo.plugin", self.SOURCE, ("read",))])
        loaded = loader.load(self.manifest("demo.plugin", self.SOURCE, ("read", "panel")), None, self.SOURCE)
        self.assertEqual(loaded.permissions, ("read",))
        self.assertTrue(sys._r4_ungranted_ran)


if __name__ == "__main__":
    unittest.main()
