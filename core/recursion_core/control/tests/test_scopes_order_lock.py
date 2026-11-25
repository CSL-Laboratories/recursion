"""The scopes service's order lock holds nothing but the order, so how long stop waits never depends on a log
handler; a start that a stop has overtaken before it opens the credential store never opens it; one call's
binding holds even when the principal's own attribute read lands an attach.
"""

from __future__ import annotations

import ast
import logging
import os
import shutil
import threading
import unittest

from recursion_core.control import scopes
from recursion_core.control.tests import lock_guard
from recursion_core.control.tests.test_scopes_lifecycle import PLUGIN, _Context, state_with_plugin
from recursion_core.control.tests.test_scopes_seams import NEEDS_POSIX, Registry, with_service

# A test figure, not a setting: how long a test waits for an event that should come (it ends a wait that would
# otherwise hang; nothing is timed by it).
WAIT = 10
# A test figure: the longest a blocked handler waits for the test to unblock it. Longer than WAIT, so that where
# stop does wait for the handler, the test's wait for stop ends first and the test is red, never green by luck.
HOLD = 3 * WAIT

LOGGER = "recursion_core.control.scopes"

# The one call allowed under ScopesService._order: the authorizer's attach is our own code, which takes only the
# authorizer's binding lock and calls nothing outside it (AuthorizerLockTest holds that).
ORDER_ALLOWED = frozenset({"self._authorizer.attach"})
# Under Authorizer._lock only the binding is built: _Binding is a NamedTuple of this module.
BINDING_ALLOWED = frozenset({"_Binding"})


class _Handler(logging.Handler):
    """A log handler that, when a record reaches it, says so and blocks until the test unblocks it; then it
    does ``then`` (raise, or call stop on the service). (``release`` is the Handler's own method.)"""

    def __init__(self, then=None) -> None:
        super().__init__(logging.WARNING)
        self.entered, self.unblock, self.then, self.records = threading.Event(), threading.Event(), then, []

    def handle(self, record) -> bool:
        # No handler lock around emit: a handler left blocked by a red run must not hold logging's exit too.
        self.emit(record)
        return True

    def emit(self, record) -> None:
        self.records.append(record.getMessage())
        self.entered.set()
        self.unblock.wait(HOLD)
        if self.then is not None:
            self.then()


def _install(test: unittest.TestCase, handler: logging.Handler) -> None:
    logger = logging.getLogger(LOGGER)
    level, propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    test.addCleanup(lambda: (logger.removeHandler(handler), logger.setLevel(level),
                             setattr(logger, "propagate", propagate)))


def _thread(target, *args) -> tuple[threading.Thread, list]:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            target(*args)
        except BaseException as exc:  # noqa: BLE001 - kept for the test to read
            errors.append(exc)

    return threading.Thread(target=run, daemon=True), errors


@NEEDS_POSIX
class StopBoundedByLogHandlerTest(unittest.TestCase):
    """A start that a stop overtook is set aside and logs a warning. The handler of that warning blocks; a stop
    from another thread returns while it is still blocked, because nothing is logged under the order."""

    def setUp(self) -> None:
        base = state_with_plugin("scopes-handler-")
        self.addCleanup(shutil.rmtree, base, True)
        self.state = os.path.join(base, "state")
        self.hooks, self.service = with_service()
        self.authorizer = self.service._authorizer

    def set_aside_start_into(self, handler: _Handler) -> tuple[threading.Thread, list]:
        """A start held in its registry read, a stop, the start released: it is set aside and its warning is
        now inside ``handler``, which blocks."""

        _install(self, handler)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.addCleanup(handler.unblock.set)
        late, errors = _thread(self.service.start, _Context(self.state, Registry(), entered=entered, release=release))
        late.start()
        self.assertTrue(entered.wait(WAIT))
        self.service.stop(None)
        release.set()
        self.assertTrue(handler.entered.wait(WAIT))
        return late, errors

    def stop_returns_while_the_handler_blocks(self, handler: _Handler) -> None:
        done = threading.Event()
        stopper, errors = _thread(lambda: (self.service.stop(None), done.set()))
        stopper.start()
        self.assertTrue(done.wait(WAIT), "stop waited for a log handler")
        self.assertFalse(handler.unblock.is_set())   # the handler is still blocked
        stopper.join(WAIT)
        self.assertEqual(errors, [])

    def assert_stopped_and_free(self) -> None:
        self.assertFalse(self.service.ready)
        self.assertEqual(self.authorizer.held(PLUGIN), frozenset())
        self.assertFalse(self.service._order.locked())
        self.service.start(_Context(self.state, Registry()))   # a fresh start applies as before
        self.assertTrue(self.service.ready)
        self.assertEqual(self.authorizer.held(PLUGIN), frozenset({"read", "run"}))

    def test_stop_returns_while_a_log_handler_blocks(self) -> None:
        handler = _Handler()
        late, errors = self.set_aside_start_into(handler)
        self.stop_returns_while_the_handler_blocks(handler)
        self.assertTrue(late.is_alive())   # the start is still inside its warning
        handler.unblock.set()
        late.join(WAIT)
        self.assertFalse(late.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(handler.records, ["control.scopes: a start that began before the last stop was not "
                                           "applied; the service stays stopped"])
        self.assert_stopped_and_free()

    def test_stop_returns_while_a_handler_that_raises_blocks(self) -> None:
        def fail() -> None:
            raise RuntimeError("stand-in handler failure")

        handler = _Handler(then=fail)
        late, errors = self.set_aside_start_into(handler)
        self.stop_returns_while_the_handler_blocks(handler)
        handler.unblock.set()
        late.join(WAIT)
        self.assertFalse(late.is_alive())
        # The handler's error reaches the start's caller, as from every log line of start; the order is free.
        self.assertEqual([type(exc) for exc in errors], [RuntimeError])
        self.assert_stopped_and_free()

    def test_a_handler_that_calls_stop_does_not_deadlock(self) -> None:
        inner = threading.Event()
        counts = []

        def stop_from_the_handler() -> None:
            counts.append(self.service._stops)
            self.service.stop(None)
            counts.append(self.service._stops)
            inner.set()

        handler = _Handler(then=stop_from_the_handler)
        late, errors = self.set_aside_start_into(handler)
        handler.unblock.set()
        self.assertTrue(inner.wait(WAIT), "a stop called from the log handler did not return")
        late.join(WAIT)
        self.assertFalse(late.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(counts[1], counts[0] + 1)   # the stop inside the handler is one more stop of the order
        self.assert_stopped_and_free()


class _StateDirContext:
    """A service context whose state directory read can be held: the start has not yet opened the store."""

    def __init__(self, state_dir, *, entered, release) -> None:
        self._state_dir, self._entered, self._release = state_dir, entered, release
        self.control_registry = Registry()

    @property
    def state_dir(self):
        self._entered.set()
        self._release.wait(WAIT)
        return self._state_dir


@NEEDS_POSIX
class OvertakenStartOpensNothingTest(unittest.TestCase):
    """A start that a stop overtook before it opened the credential store never opens it (no folder made, no
    file lock, no sweep). A narrower window, not a barrier: a start past that check may still open the store
    after a stop returns, and then attaches nothing (LateStartTest)."""

    def setUp(self) -> None:
        base = state_with_plugin("scopes-open-")
        self.addCleanup(shutil.rmtree, base, True)
        self.state = os.path.join(base, "state")
        _, self.service = with_service()
        self.opens = []
        real = scopes.CredentialStore
        opens = self.opens

        class Counting(real):
            def __init__(self, *args, **kwargs) -> None:
                opens.append(args)
                super().__init__(*args, **kwargs)

        scopes.CredentialStore = Counting
        self.addCleanup(setattr, scopes, "CredentialStore", real)
        quiet = logging.getLogger(LOGGER)
        level, quiet.level = quiet.level, logging.CRITICAL
        self.addCleanup(quiet.setLevel, level)

    def test_a_start_overtaken_before_it_opens_the_store_never_opens_it(self) -> None:
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        late, errors = _thread(self.service.start, _StateDirContext(self.state, entered=entered, release=release))
        late.start()
        self.assertTrue(entered.wait(WAIT))
        self.service.stop(None)
        release.set()
        late.join(WAIT)
        self.assertFalse(late.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.opens, [])
        self.assertFalse(self.service.ready)

    def test_a_start_no_stop_overtook_opens_the_store_once(self) -> None:
        self.service.start(_Context(self.state, Registry()))
        self.assertEqual(len(self.opens), 1)
        self.assertTrue(self.service.ready)


class OrderLockGuardTest(unittest.TestCase):
    """Nothing is logged, formatted or called back under ScopesService._order: a static walk of the class."""

    def test_only_the_authorizer_attach_runs_under_the_order(self) -> None:
        report = lock_guard.under_lock(lock_guard.class_tree(scopes.ScopesService), "_order", ORDER_ALLOWED)
        self.assertEqual(report.findings, ())
        self.assertEqual({name for name, _ in report.blocks}, {"start", "stop"})
        self.assertGreaterEqual(len(report.blocks), 3)

    def test_a_planted_log_format_or_callback_under_the_order_is_refused(self) -> None:
        planted = {
            "log": 'log.warning("planted")',
            "format": 'note = f"{self.ready}"',
            "percent": 'note = "%s" % self.ready',
            "callback": "self._on_stop()",
        }
        tree = lock_guard.class_tree(scopes.ScopesService)
        count = len(list(lock_guard.lock_blocks(tree, "_order")))
        for kind, line in planted.items():
            for index in range(count):
                with self.subTest(kind=kind, block=index):
                    copy = lock_guard.class_tree(scopes.ScopesService)
                    _, block = list(lock_guard.lock_blocks(copy, "_order"))[index]
                    block.body.insert(0, ast.parse(line).body[0])
                    source = ast.unparse(copy)
                    report = lock_guard.under_lock(lock_guard.class_tree_from_source(source), "_order",
                                                   ORDER_ALLOWED)
                    self.assertNotEqual(report.findings, ())


class AuthorizerLockTest(unittest.TestCase):
    """The lock the authorizer's attach takes (stop calls attach under the order) holds only the binding."""

    def test_only_the_binding_is_built_under_the_authorizer_lock(self) -> None:
        report = lock_guard.under_lock(lock_guard.class_tree(scopes.Authorizer), "_lock", BINDING_ALLOWED)
        self.assertEqual(report.findings, ())
        self.assertEqual({name for name, _ in report.blocks}, {"attach", "_binding"})


class _Store:
    """A stand-in credential store: ``grants`` are the scopes its one grant gives."""

    def __init__(self, grants) -> None:
        self.grants = frozenset(grants)

    def grant_scopes(self, grant, kind, pid):
        return self.grants

    def held(self, kind, pid):
        return frozenset()


class _AttachingPrincipal:
    """A token principal whose own read of ``grant`` lands an attach of the new pair, once."""

    in_run_unit = False
    kind = "plugin"
    id = "plugin.alpha"

    def __init__(self, swap) -> None:
        self._swap = swap

    @property
    def grant(self):
        self._swap()
        return "grant-alpha"


class BindingAcrossTheAttributeReadTest(unittest.TestCase):
    """The attach lands inside the call's own read of the principal's grant, after the call read its binding:
    the call answers from the old pair alone (old store without the grant, old run reader that owns the run)."""

    def setUp(self) -> None:
        self.authorizer = scopes.Authorizer(scope_of=scopes.table_scope)
        owning = {"run-x": {"run_id": "run-x", "launch": {"owner": "plugin.alpha", "session_id": None}}}.get
        self.not_owning = {"run-x": {"run_id": "run-x", "launch": {"owner": "host", "session_id": None}}}.get
        self.authorizer.attach(credentials=_Store(()), run_record=owning)
        self.swaps = []

    def swap(self) -> None:
        if not self.swaps:
            self.swaps.append(True)
            self.authorizer.attach(credentials=_Store({"run"}), run_record=self.not_owning)

    def test_a_call_answers_from_the_old_pair(self) -> None:
        self.assertEqual(self.authorizer(_AttachingPrincipal(self.swap), "run.stop", {"run_id": "run-x"}),
                         ("scope_refused", "needs_run"))
        self.assertEqual(self.swaps, [True])
        # the new pair alone: the grant, but a run this plugin does not own
        self.assertEqual(self.authorizer(_AttachingPrincipal(self.swap), "run.stop", {"run_id": "run-x"}),
                         ("scope_refused", "run_not_owned"))

    def test_held_answers_from_the_old_store(self) -> None:
        self.assertEqual(self.authorizer.held(_AttachingPrincipal(self.swap)), frozenset())
        self.assertEqual(self.swaps, [True])
        self.assertEqual(self.authorizer.held(_AttachingPrincipal(self.swap)), frozenset({"run"}))


if __name__ == "__main__":
    unittest.main()
