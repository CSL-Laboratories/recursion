"""The scopes service's lifetime and the authorizer's one binding per call.

A start and a stop are one ordered sequence: a start that began before a stop that has returned never
attaches the credential store and never sets ``ready``. The store, the run reader and their generation are
published together and read once per call, so a call never decides from the old store and the new run
reader. A method that is not text at all is refused to every principal before the all-scopes shortcut.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest

from recursion_core.control import hooks as H
from recursion_core.control import scopes
from recursion_core.control.credentials import CredentialStore
from recursion_core.control.tests.test_scopes import PRINCIPALS
from recursion_core.control.tests.test_scopes_seams import NEEDS_POSIX, Registry, with_service

# A test figure, not a setting: how many start/stop rounds the stress test races.
RACE_ROUNDS = 3000

PLUGIN = H.Principal("plugin", "plugin.alpha")
SET_ASIDE = ("control.scopes: a start that began before the last stop was not applied; "
             "the service stays stopped")


def state_with_plugin(prefix: str, scopes_=("read", "run")) -> str:
    base = tempfile.mkdtemp(prefix=prefix)
    state = os.path.join(base, "state")
    os.mkdir(state, 0o700)
    if scopes_:
        CredentialStore(os.path.join(state, "control", "credentials.json")).mint("plugin", "plugin.alpha",
                                                                                  list(scopes_))
    return base


class _Context:
    """A service context whose registry read can be held: the start is then inside its registry read."""

    def __init__(self, state_dir, registry, *, entered=None, release=None, mark=None) -> None:
        self.state_dir, self._registry = state_dir, registry
        self._entered, self._release, self._mark = entered, release, mark

    @property
    def control_registry(self):
        if self._mark is not None:
            self._mark()
            time.sleep(0)   # give the other thread the processor here, where the late start is
        if self._entered is not None:
            self._entered.set()
            self._release.wait(30)
        return self._registry


@NEEDS_POSIX
class LateStartTest(unittest.TestCase):
    """A start held inside its registry read while a stop runs and returns: when it finishes it attaches
    nothing and the service stays stopped; a start after that stop attaches as before."""

    def setUp(self) -> None:
        base = state_with_plugin("scopes-late-")
        self.addCleanup(shutil.rmtree, base, True)
        self.state = os.path.join(base, "state")
        self.hooks, self.service = with_service()
        self.authorizer = self.service._authorizer

    def test_a_start_that_began_before_a_stop_attaches_nothing_after_it(self) -> None:
        entered, release = threading.Event(), threading.Event()
        late = threading.Thread(target=self.service.start,
                                args=(_Context(self.state, Registry(), entered=entered, release=release),))
        with self.assertLogs("recursion_core.control.scopes", "WARNING") as logs:
            late.start()
            try:
                self.assertTrue(entered.wait(30))
                self.service.stop(H.ServiceContext(state_dir=self.state, control_registry=Registry()))
                self.assertFalse(self.service.ready)
            finally:
                release.set()
                late.join(30)
        self.assertFalse(late.is_alive())
        self.assertFalse(self.service.ready)
        self.assertEqual(self.authorizer.held(PLUGIN), frozenset())
        self.assertIsNone(self.authorizer._binding().credentials)
        self.assertIsNone(self.authorizer._binding().run_record)
        self.assertEqual(self.hooks.authorize(PLUGIN, "run.list", {}),
                         {"code": "scope_refused", "detail": "needs_read"})
        self.assertIn(SET_ASIDE, "\n".join(logs.output))

    def test_a_start_after_that_stop_attaches_as_before(self) -> None:
        self.test_a_start_that_began_before_a_stop_attaches_nothing_after_it()
        self.service.start(H.ServiceContext(state_dir=self.state, control_registry=Registry()))
        self.assertTrue(self.service.ready)
        self.assertEqual(self.authorizer.held(PLUGIN), frozenset({"read", "run"}))
        self.assertIsNone(self.hooks.authorize(PLUGIN, "run.stop", {"run_id": "run-alpha"}))

    def test_a_stop_never_waits_for_a_start_that_is_reading(self) -> None:
        entered, release = threading.Event(), threading.Event()
        late = threading.Thread(target=self.service.start,
                                args=(_Context(self.state, Registry(), entered=entered, release=release),))
        quiet = logging_quiet()
        self.addCleanup(quiet)
        late.start()
        try:
            self.assertTrue(entered.wait(30))
            done = threading.Event()
            stopper = threading.Thread(target=lambda: (self.service.stop(None), done.set()))
            stopper.start()
            self.assertTrue(done.wait(30))   # the start is still held inside its read
            stopper.join(30)
        finally:
            release.set()
            late.join(30)
        self.assertFalse(self.service.ready)


def race(rounds: int) -> dict:
    """A starter and a stopper race for ``rounds`` rounds, then a final stop. A fault is a round whose start
    entered its reads before the stop began and which leaves the service ready or the store attached once
    both have returned. Returns the counts."""

    base = state_with_plugin("scopes-race-")
    try:
        state = os.path.join(base, "state")
        _, service = with_service()
        authorizer = service._authorizer
        tick_lock, ticks = threading.Lock(), [0]

        def tick() -> int:
            with tick_lock:
                ticks[0] += 1
                return ticks[0]

        marks: dict[str, int] = {}
        context = _Context(state, Registry(), mark=lambda: marks.__setitem__("read", tick()))
        gate = threading.Barrier(3, timeout=60)
        stats = {"rounds": rounds, "faults": 0, "began_before_stop": 0, "applied": 0}
        failures: list[BaseException] = []

        def starter() -> None:
            try:
                for _ in range(rounds):
                    gate.wait()
                    service.start(context)
                    gate.wait()
            except BaseException as exc:  # noqa: BLE001 - reported by the main thread
                failures.append(exc)
                gate.abort()

        def stopper() -> None:
            try:
                for _ in range(rounds):
                    gate.wait()
                    marks["stop"] = tick()
                    service.stop(context)
                    gate.wait()
            except BaseException as exc:  # noqa: BLE001 - reported by the main thread
                failures.append(exc)
                gate.abort()

        quiet = logging_quiet()
        threads = [threading.Thread(target=starter), threading.Thread(target=stopper)]
        for thread in threads:
            thread.start()
        try:
            for _ in range(rounds):
                marks.clear()
                gate.wait()
                gate.wait()
                attached = service.ready or authorizer.held(PLUGIN) != frozenset()
                if attached:
                    stats["applied"] += 1
                if marks.get("read", 0) < marks.get("stop", 0):
                    stats["began_before_stop"] += 1
                    if attached:
                        stats["faults"] += 1
                service.stop(context)   # each round begins stopped
        finally:
            for thread in threads:
                thread.join(60)
            quiet()
        if failures:
            raise failures[0]
        service.stop(context)   # the final stop
        stats["after_final_stop"] = int(service.ready or authorizer.held(PLUGIN) != frozenset())
        return stats
    finally:
        shutil.rmtree(base, True)


def logging_quiet():
    """Silence the set-aside warnings of a race (thousands of them) and give back the undo."""

    import logging
    logger = logging.getLogger("recursion_core.control.scopes")
    level, logger.level = logger.level, logging.ERROR
    return lambda: logger.setLevel(level)


@NEEDS_POSIX
class StartStopRaceTest(unittest.TestCase):
    def test_no_start_that_began_before_a_stop_is_applied_after_it(self) -> None:
        stats = race(RACE_ROUNDS)
        self.assertEqual(stats["faults"], 0, stats)
        self.assertEqual(stats["after_final_stop"], 0, stats)


@NEEDS_POSIX
class OneBindingPerCallTest(unittest.TestCase):
    """A second attach lands between the call's held read and its run-record read (inside the table lookup):
    the call answers from the old pair alone, never from the old store and the new run reader."""

    def setUp(self) -> None:
        self.old_base = state_with_plugin("scopes-old-", ("run",))   # the grant is only in the old store
        self.new_base = state_with_plugin("scopes-new-", ())
        for base in (self.old_base, self.new_base):
            self.addCleanup(shutil.rmtree, base, True)

    def store(self, base) -> CredentialStore:
        return CredentialStore(os.path.join(base, "state", "control", "credentials.json"))

    def test_a_call_never_mixes_the_old_store_with_the_new_run_reader(self) -> None:
        old_reader = {"run-x": {"run_id": "run-x", "launch": {"owner": "host", "session_id": None}}}.get
        new_reader = {"run-x": {"run_id": "run-x", "launch": {"owner": "plugin.alpha", "session_id": None}}}.get
        swaps = []

        def scope_of(method):
            if not swaps:
                swaps.append(method)
                authorizer.attach(credentials=self.store(self.new_base), run_record=new_reader)
            return scopes.table_scope(method)

        authorizer = scopes.Authorizer(scope_of=scope_of)
        authorizer.attach(credentials=self.store(self.old_base), run_record=old_reader)
        self.assertEqual(authorizer(PLUGIN, "run.stop", {"run_id": "run-x"}), ("scope_refused", "run_not_owned"))
        self.assertEqual(swaps, ["run.stop"])
        # the new pair alone: no grant, so no run scope
        self.assertEqual(authorizer(PLUGIN, "run.stop", {"run_id": "run-x"}), ("scope_refused", "needs_run"))

    def test_each_attach_publishes_the_next_generation(self) -> None:
        authorizer = scopes.Authorizer(scope_of=scopes.table_scope)
        first = authorizer._binding()
        authorizer.attach(credentials=self.store(self.old_base), run_record=None)
        second = authorizer._binding()
        authorizer.attach(credentials=None, run_record=None)
        third = authorizer._binding()
        self.assertEqual([first.generation + 1, first.generation + 2], [second.generation, third.generation])
        self.assertIsNotNone(second.credentials)
        self.assertEqual((third.credentials, third.run_record), (None, None))


class _EqualsStop:
    """Not text: an object whose equality and hash say it is run.stop."""

    def __eq__(self, other):
        return other == "run.stop"

    def __hash__(self):
        return hash("run.stop")


class _Text(str):
    pass


class NonTextMethodTest(unittest.TestCase):
    def test_a_method_that_is_not_text_is_refused_to_owner_and_internal(self) -> None:
        authorizer = scopes.Authorizer(scope_of=scopes.table_scope)
        for name in ("owner", "internal"):
            for method in (b"run.stop", _EqualsStop()):
                with self.subTest(principal=name, method=repr(method)):
                    self.assertEqual(authorizer(PRINCIPALS[name], method, {"run_id": "run-x"}),
                                     ("scope_refused", "method_not_text"))

    def test_text_and_a_str_subclass_answer_as_before(self) -> None:
        authorizer = scopes.Authorizer(scope_of=scopes.table_scope)
        for method in ("run.stop", _Text("run.stop")):
            with self.subTest(method=repr(method)):
                self.assertIsNone(authorizer(PRINCIPALS["owner"], method, {"run_id": "run-x"}))
                self.assertEqual(authorizer(PRINCIPALS["internal"], method, {"run_id": "run-x"}),
                                 ("scope_refused", "stop_not_granted_to_kind"))

    def test_a_principal_without_every_scope_keeps_its_answer(self) -> None:
        authorizer = scopes.Authorizer(scope_of=scopes.table_scope)
        for method in (b"run.stop", _EqualsStop()):
            self.assertEqual(authorizer(PRINCIPALS["page"], method, {}), ("scope_refused", "method_not_in_table"))


if __name__ == "__main__":
    unittest.main()
