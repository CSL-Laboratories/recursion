"""Adversarial interleavings on the daemon registry (recursion #324, CSL-738) - CSL-801 L11b.

Each test forces one specific window open with a ``Gate`` (or races many writers behind one
barrier) and asserts the invariant the registry documents:

* B14: a run with a recorded Stop is never written ``running``, even by a reconcile that
  read its ``Run`` before the Stop committed.
* Terminal states (``terminated``, ``lost_reboot``) are sticky under any writer order.

Duplicate ``add``, ``close`` racing in-flight calls and ``UserLock`` exclusivity are covered
under load by ``drivers/daemon_324.py``.

Needs ``RACE_STRESS_TREE_324`` (a checkout of #324). Run under ``python3.14t -X gil=0``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from pathlib import Path

from recursion_core.race_stress.tests.interleave_support import (
    ITERATIONS, THREADS, Gate, SeededCase, load_stacked, run_threads)

BOOT = "boot-a"


def _daemon_mod():
    return load_stacked("RACE_STRESS_TREE_324", "recursion_core.daemon")


class FakeManager:
    """Service manager double. ``observe`` can be paused on a gate to hold a reconcile open."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.units: dict[str, str] = {}
        self.stopped: list[str] = []
        self.observe_gate: Gate | None = None

    def start(self, unit, argv):
        with self.lock:
            self.units[unit] = "running"

    def stop(self, unit):
        with self.lock:
            self.stopped.append(unit)
            self.units[unit] = "empty"

    def observe(self, unit):
        gate = self.observe_gate
        if gate is not None:
            self.observe_gate = None
            with self.lock:
                seen = self.units.get(unit, "unknown")
            gate.hit()
            return seen  # deliberately stale: what the unit looked like before the pause
        with self.lock:
            return self.units.get(unit, "unknown")

    def owned_units(self):
        with self.lock:
            return {u for u, s in self.units.items() if s == "running"}


class RegistryCase(SeededCase):
    def setUp(self) -> None:
        self.mod = _daemon_mod()
        self.dir = Path(tempfile.mkdtemp(prefix="race-registry-"))
        os.chmod(self.dir, 0o700)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def registry(self, name="state"):
        reg = self.mod.Registry(self.dir / name)
        self.addCleanup(reg.close)
        return reg


class StaleReconcileTests(RegistryCase):
    def test_stop_committed_mid_reconcile_is_never_overwritten_as_running(self) -> None:
        """Reconcile reads Run(stop=False), pauses in observe; Stop commits; reconcile writes."""
        for i in range(ITERATIONS):
            reg = self.registry(f"s{i}")
            mgr = FakeManager()
            daemon = self.mod.Daemon(reg, mgr, boot_id=lambda: BOOT)
            daemon.start("r1", ["true"])
            stale = reg.get("r1")
            self.assertFalse(stale.stop_requested)
            gate = Gate()
            mgr.observe_gate = gate
            t = threading.Thread(target=daemon._reconcile, args=(stale,))
            t.start()
            gate.wait_reached()
            reg.request_stop("r1")  # a second writer commits Stop inside the window
            gate.release()
            t.join(30)
            self.assertFalse(t.is_alive())
            run = reg.get("r1")
            self.assertTrue(run.stop_requested)
            self.assertNotEqual(run.state, "running", f"B14 broken on iteration {i}")

    def test_daemon_stop_waits_out_a_paused_sweep(self) -> None:
        """Daemon.stop issued while sweep is paused inside observe: Stop still lands and wins."""
        reg = self.registry()
        mgr = FakeManager()
        daemon = self.mod.Daemon(reg, mgr, boot_id=lambda: BOOT)
        daemon.start("r1", ["true"])
        gate = Gate()
        mgr.observe_gate = gate
        sweeper = threading.Thread(target=daemon.sweep)
        sweeper.start()
        gate.wait_reached()
        out: dict = {}
        stopper = threading.Thread(target=lambda: out.setdefault("run", daemon.stop("r1")))
        stopper.start()
        stopper.join(0.2)
        self.assertTrue(stopper.is_alive(), "Daemon.stop must serialize behind an in-flight sweep")
        gate.release()
        sweeper.join(30)
        stopper.join(30)
        self.assertTrue(out["run"].stop_requested)
        self.assertIn(out["run"].state, ("stopping", "terminated"))
        self.assertIn("recursion-run-r1.service", mgr.stopped)


class TerminalStickinessTests(RegistryCase):
    def test_random_writers_never_leave_a_terminal_state(self) -> None:
        values = ("running", "unknown", "stopping", "terminated", "lost_reboot", "starting")
        for i in range(ITERATIONS):
            reg = self.registry(f"t{i}")
            reg.add("r1", BOOT)
            first_terminal: list[str] = []
            stop_seen = threading.Event()
            violations: list[str] = []
            vlock = threading.Lock()

            def writer(salt):
                rng = self.rng(f"{i}:{salt}")
                for _ in range(40):
                    if rng.random() < 0.1:
                        reg.request_stop("r1")
                    else:
                        reg.state("r1", rng.choice(values), f"w{salt}")

            def reader():
                for _ in range(200):
                    run = reg.get("r1")
                    with vlock:
                        if run.stop_requested:
                            stop_seen.set()
                        if first_terminal and run.state != first_terminal[0]:
                            violations.append(f"{first_terminal[0]} -> {run.state}")
                        if run.state in self.mod.TERMINAL and not first_terminal:
                            first_terminal.append(run.state)

            errors = run_threads([lambda s=s: writer(s) for s in range(THREADS)] + [reader, reader])
            self.assertEqual(errors, [], f"iteration {i}")
            self.assertEqual(violations, [], f"terminal state left on iteration {i}")
            final = reg.get("r1")
            if final.stop_requested:
                self.assertNotEqual(final.state, "running", "B14: stop=1 with state running")


if __name__ == "__main__":
    import unittest
    unittest.main()
