"""The ConnectionBreaker contract a downstream consumer builds against, pinned (step 6, CSL-720).

PRODUCT.md d84cdd26 (CSLR-D-012) section 4.2 ("Two breakers"). The public shape of
``plugin_security/breaker.py`` is frozen as it stands at #328 head 4f99503d: no renames, no
argument reordering, no removed or renamed refusal codes. Additions are allowed. Each test
below fails if the frozen part changes; BREAKER-CONTRACT.md is the prose version.
These tests need no signing, so they run where ``cryptography`` is missing too.
"""

from __future__ import annotations

import inspect
import os
import re
import unittest

from recursion_core import plugin_security
from recursion_core.plugin_security import breaker
from recursion_core.plugin_security import ConnectionBreaker, Refused, create_breaker_store
from recursion_core.plugin_security.tests.support import temp_dir

P = inspect.Parameter
# (name, kind, default) per parameter; ``P.empty`` means required.
FROZEN_SIGNATURES = {
    "create_breaker_store": [("path", P.POSITIONAL_OR_KEYWORD, P.empty)],
    "ConnectionBreaker.__init__": [
        ("self", P.POSITIONAL_OR_KEYWORD, P.empty),
        ("store_path", P.POSITIONAL_OR_KEYWORD, P.empty),
        ("clock", P.KEYWORD_ONLY, "time.monotonic"),
        ("probe_lease_seconds", P.KEYWORD_ONLY, 300.0),
    ],
    "ConnectionBreaker.bind_run": [
        ("self", P.POSITIONAL_OR_KEYWORD, P.empty), ("run_id", P.POSITIONAL_OR_KEYWORD, P.empty),
        ("connection", P.POSITIONAL_OR_KEYWORD, P.empty), ("plugin_id", P.POSITIONAL_OR_KEYWORD, P.empty),
        ("provider", P.POSITIONAL_OR_KEYWORD, P.empty), ("model_version", P.POSITIONAL_OR_KEYWORD, P.empty),
    ],
    "ConnectionBreaker.check": [
        ("self", P.POSITIONAL_OR_KEYWORD, P.empty), ("run_id", P.POSITIONAL_OR_KEYWORD, P.empty),
        ("connection", P.POSITIONAL_OR_KEYWORD, P.empty), ("plugin_id", P.POSITIONAL_OR_KEYWORD, P.empty),
    ],
    "ConnectionBreaker.report": [
        ("self", P.POSITIONAL_OR_KEYWORD, P.empty), ("connection", P.POSITIONAL_OR_KEYWORD, P.empty),
        ("event_type", P.POSITIONAL_OR_KEYWORD, P.empty),
    ],
    "ConnectionBreaker.fault_run": [
        ("self", P.POSITIONAL_OR_KEYWORD, P.empty), ("run_id", P.POSITIONAL_OR_KEYWORD, P.empty),
    ],
    "ConnectionBreaker.begin_probe": [
        ("self", P.POSITIONAL_OR_KEYWORD, P.empty), ("connection", P.POSITIONAL_OR_KEYWORD, P.empty),
    ],
    "ConnectionBreaker.reset_after_probe": [
        ("self", P.POSITIONAL_OR_KEYWORD, P.empty), ("connection", P.POSITIONAL_OR_KEYWORD, P.empty),
        ("lease", P.POSITIONAL_OR_KEYWORD, P.empty), ("probe_ok", P.POSITIONAL_OR_KEYWORD, P.empty),
    ],
    "ConnectionBreaker.tripped": [
        ("self", P.POSITIONAL_OR_KEYWORD, P.empty), ("connection", P.POSITIONAL_OR_KEYWORD, P.empty),
    ],
    "ConnectionBreaker.close": [("self", P.POSITIONAL_OR_KEYWORD, P.empty)],
}
# Every refusal code breaker.py can raise at 4f99503d. New codes may be added; none may go.
FROZEN_CODES = frozenset({
    "breaker_store_missing", "breaker_store_invalid",
    "invalid_run_id", "invalid_connection", "invalid_plugin_id", "invalid_provider", "invalid_model_version",
    "run_connection_mismatch", "run_binding_mismatch", "untyped_breaker_event",
    "connection_not_tripped", "probe_lease_held",
    "run_unknown", "run_not_owned", "run_faulted", "connection_tripped",
})
FROZEN_TYPED_LIMITS = frozenset({"plan_limit", "rate_limit", "usage_window_exhausted"})


def _shape(function):
    out = []
    for p in inspect.signature(function).parameters.values():
        default = p.default
        if callable(default) and getattr(default, "__module__", "") == "time":
            default = "time." + default.__name__
        out.append((p.name, p.kind, default))
    return out


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FrozenShapeTests(unittest.TestCase):
    def test_exports(self) -> None:
        for name in ("ConnectionBreaker", "create_breaker_store", "Refused", "TYPED_LIMITS"):
            self.assertIn(name, plugin_security.__all__)
        self.assertIs(plugin_security.ConnectionBreaker, breaker.ConnectionBreaker)
        self.assertIs(plugin_security.create_breaker_store, breaker.create_breaker_store)

    def test_signatures_are_unchanged(self) -> None:
        for dotted, expected in FROZEN_SIGNATURES.items():
            target = breaker
            for part in dotted.split("."):
                target = getattr(target, part)
            with self.subTest(callable=dotted):
                actual = _shape(target)
                self.assertEqual(actual[:len(expected)], expected)
                # Additions only: a new trailing parameter must be optional.
                for name, kind, default in actual[len(expected):]:
                    self.assertTrue(kind is P.KEYWORD_ONLY or default is not P.empty, name)

    def test_refusal_codes_are_a_superset_of_the_frozen_set(self) -> None:
        source = inspect.getsource(breaker)
        raised = set(re.findall(r'Refused\(\s*"([a-z_]+)"', source))
        raised |= set(re.findall(r'_check_id\([^,]+,\s*"([a-z_]+)"\)', source))
        for code in re.findall(r'"([a-z_]+)" if [^"]+ else "([a-z_]+)"', source):
            raised |= set(code)
        self.assertLessEqual(FROZEN_CODES, raised, sorted(FROZEN_CODES - raised))

    def test_typed_limits(self) -> None:
        self.assertLessEqual(FROZEN_TYPED_LIMITS, set(breaker.TYPED_LIMITS))

    def test_refused_carries_a_stable_code(self) -> None:
        refusal = Refused("run_faulted", "detail")
        self.assertEqual((refusal.code, refusal.detail), ("run_faulted", "detail"))


class FrozenBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = os.path.join(temp_dir(self), "breaker.sqlite3")
        self.clock = Clock()

    def open(self) -> ConnectionBreaker:
        b = ConnectionBreaker(self.path, clock=self.clock, probe_lease_seconds=60.0)
        self.addCleanup(b.close)
        return b

    def code(self, call, *args) -> str:
        with self.assertRaises(Refused) as caught:
            call(*args)
        return caught.exception.code

    def test_store_lifecycle(self) -> None:
        self.assertEqual(self.code(ConnectionBreaker, self.path), "breaker_store_missing")
        self.assertIsNone(create_breaker_store(self.path))
        with self.assertRaises(FileExistsError):  # the installer step never replaces a store
            create_breaker_store(self.path)
        other = os.path.join(os.path.dirname(self.path), "not-a-store")
        with open(other, "wb") as handle:
            handle.write(b"x" * 64)
        self.assertEqual(self.code(ConnectionBreaker, other), "breaker_store_invalid")

    def test_run_binding_check_and_fault(self) -> None:
        create_breaker_store(self.path)
        b = self.open()
        self.assertIsNone(b.bind_run("run-1", "conn-a", "plug-a", "prov", "model-1"))
        b.bind_run("run-1", "conn-a", "plug-a", "prov", "model-1")  # same binding again is fine
        self.assertEqual(self.code(b.bind_run, "run-1", "conn-b", "plug-a", "prov", "model-1"),
                         "run_connection_mismatch")
        self.assertEqual(self.code(b.bind_run, "run-1", "conn-a", "plug-b", "prov", "model-1"),
                         "run_binding_mismatch")
        self.assertEqual(self.code(b.bind_run, "bad id!", "conn-a", "plug-a", "prov", "m"), "invalid_run_id")
        self.assertEqual(b.check("run-1", "conn-a", "plug-a"), {"provider": "prov", "model_version": "model-1"})
        self.assertEqual(self.code(b.check, "run-x", "conn-a", "plug-a"), "run_unknown")
        self.assertEqual(self.code(b.check, "run-1", "conn-a", "plug-b"), "run_not_owned")
        self.assertEqual(self.code(b.check, "run-1", "conn-b", "plug-a"), "run_connection_mismatch")
        self.assertIsNone(b.fault_run("run-1"))
        self.assertEqual(self.code(b.check, "run-1", "conn-a", "plug-a"), "run_faulted")

    def test_trip_probe_and_restart(self) -> None:
        create_breaker_store(self.path)
        b = self.open()
        b.bind_run("run-1", "conn-a", "plug-a", "prov", "model-1")
        self.assertIs(b.report("conn-a", "core_timeout"), False)  # the core's own timeouts never trip
        self.assertEqual(self.code(b.report, "conn-a", "something_else"), "untyped_breaker_event")
        self.assertEqual(self.code(b.begin_probe, "conn-a"), "connection_not_tripped")
        self.assertIs(b.report("conn-a", "plan_limit"), True)
        self.assertIs(b.tripped("conn-a"), True)
        self.assertEqual(self.code(b.check, "run-1", "conn-a", "plug-a"), "connection_tripped")
        b.close()

        b = self.open()  # a restart keeps the trip
        self.assertIs(b.tripped("conn-a"), True)
        lease = b.begin_probe("conn-a")
        self.assertIsInstance(lease, str)
        self.assertEqual(self.code(b.begin_probe, "conn-a"), "probe_lease_held")
        self.assertIs(b.reset_after_probe("conn-a", "wrong-lease", True), False)  # leaves the lease held
        self.assertEqual(self.code(b.begin_probe, "conn-a"), "probe_lease_held")
        self.assertIs(b.reset_after_probe("conn-a", lease, False), False)  # a failed probe spends it
        self.assertIs(b.reset_after_probe("conn-a", lease, True), False)  # one use
        self.assertIs(b.tripped("conn-a"), True)
        lease = b.begin_probe("conn-a")
        self.clock.now += 61
        self.assertIs(b.reset_after_probe("conn-a", lease, True), False)  # an expired lease clears nothing
        lease = b.begin_probe("conn-a")
        self.assertIs(b.reset_after_probe("conn-a", lease, True), True)
        self.assertIs(b.tripped("conn-a"), False)
        self.assertEqual(b.check("run-1", "conn-a", "plug-a")["model_version"], "model-1")


if __name__ == "__main__":
    unittest.main()
