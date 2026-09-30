#!/usr/bin/env python3
"""One-command, source-only acceptance gate for the Recursion v2 MVP.

The focused suites remain the authoritative component controls.  This program
runs them explicitly, adds the cross-component MVP completion probes that do
not belong to a single component, and proves that the gate itself leaves the
repository unchanged.  All provider transports are hermetic or fake.
"""
from __future__ import annotations

import errno
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
TESTS = SCRIPTS / "tests"
sys.path.insert(0, str(SCRIPTS))

SOURCE_BASELINE = "907e66cc9ba6ee45256615f2726fa03a3315e9ea"
FORBIDDEN_DIAGNOSTIC = "2175561a2fc3c17e88193aab8d6004fa47e42f73"
TASK = ROOT / "state" / "tasks" / "recursion-v2-mvp.md"
EXAMPLE_OPS_AUDIT = ROOT / "docs" / "audits" / "recursion-v2-mvp-example-ops-pilot-2026-08-31.md"
COMPLETION_AUDIT = ROOT / "docs" / "audits" / "recursion-v2-mvp-completion-2026-08-31.md"

FOCUSED_GATES = (
    ("MVP contracts", TESTS / "mvp-contracts.test.py"),
    ("MVP kernel", TESTS / "mvp-kernel.test.py"),
    ("MVP adapters", TESTS / "mvp-adapters.test.py"),
    ("MVP orchestration", TESTS / "mvp-orchestrator.test.py"),
    ("MVP control surface", TESTS / "mvp-control-surface.test.py"),
    ("execution fabric", TESTS / "execution-fabric-phase8.test.py"),
    ("v2 contracts", TESTS / "v2-contracts.test.py"),
    ("cross-engine contract", TESTS / "cross-engine-contract.test.py"),
    ("Cursor fake-transport control", TESTS / "cursor-cloud-adapter.test.py"),
)


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=check,
    )


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load fixture module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class MVPCompletionTests(unittest.TestCase):
    def test_exact_source_history_and_required_evidence(self) -> None:
        baseline = _git("merge-base", "--is-ancestor", SOURCE_BASELINE, "HEAD", check=False)
        self.assertEqual(baseline.returncode, 0, "the accepted v2 source baseline is not an ancestor")
        history = set(_git("rev-list", "HEAD").stdout.splitlines())
        self.assertNotIn(FORBIDDEN_DIAGNOSTIC, history, "temporary diagnostic commit entered MVP history")
        self.assertTrue(TASK.is_file(), "bounded MVP task contract is absent")
        task = TASK.read_text(encoding="utf-8")
        self.assertIn("acceptance: python3 scripts/tests/recursion-v2-mvp.test.py", task)
        self.assertRegex(task, r"(?m)^status: passed$")
        self.assertTrue(EXAMPLE_OPS_AUDIT.is_file(), "Example-ops pilot evidence is absent")
        self.assertTrue(COMPLETION_AUDIT.is_file(), "MVP completion audit is absent")
        completion = COMPLETION_AUDIT.read_text(encoding="utf-8")
        self.assertIn(SOURCE_BASELINE, completion)
        self.assertIn("d78a9676b41b968f0fc1e5df3bedfedb71d50c24", completion)

    def test_codex_and_cursor_share_the_exact_evidence_contract(self) -> None:
        fixture = _load("mvp_adapter_completion_fixture", TESTS / "mvp-adapters.test.py")
        packet = fixture.packet()
        cases = {}
        for adapter in ("codex-cli", "cursor-cli"):
            plan = fixture.hermetic_plan(packet, adapter)
            bundle = fixture.a.fake_completion(packet, plan)
            self.assertEqual(plan.execution_mode, "hermetic")
            self.assertEqual(bundle["execution_mode"], "hermetic")
            self.assertEqual(bundle["usage_digest"], bundle["evidence"]["usage_digest"])
            self.assertEqual(bundle["receipt"]["usage"]["cost_cents"], 0)
            self.assertEqual(bundle["packet_digest"], packet.packet_digest)
            self.assertEqual(bundle["assignment_digest"], packet["assignment"]["assignment_digest"])
            self.assertEqual(
                tuple(record["kind"] for record in bundle["records"]),
                ("task", "budget", "capability_grant", "assignment", "checkpoint",
                 "candidate", "evidence", "receipt"),
            )
            self.assertIs(fixture.a.deduplicate_result(bundle, [bundle], packet, plan), bundle)
            cases[adapter] = (plan, bundle)

        codex_plan, codex = cases["codex-cli"]
        cursor_plan, cursor = cases["cursor-cli"]
        self.assertEqual(set(codex), set(cursor))
        self.assertEqual(set(codex["result_envelope"]), set(cursor["result_envelope"]))
        self.assertEqual(codex["packet_digest"], cursor["packet_digest"])
        self.assertEqual(codex["task_digest"], cursor["task_digest"])
        self.assertEqual(codex["budget_digest"], cursor["budget_digest"])
        self.assertEqual(codex["grant_digest"], cursor["grant_digest"])
        self.assertEqual(codex_plan.packet_digest, cursor_plan.packet_digest)
        self.assertEqual(codex_plan.assignment_digest, cursor_plan.assignment_digest)
        self.assertEqual(codex_plan.budget_digest, cursor_plan.budget_digest)
        self.assertEqual(codex_plan.grant_digest, cursor_plan.grant_digest)
        self.assertEqual(codex["adapter"], "codex-cli")
        self.assertEqual(cursor["adapter"], "cursor-cli")

    def test_enospc_rolls_back_and_restart_retry_is_clean(self) -> None:
        fabric_fixture = _load("mvp_fabric_disk_fixture", TESTS / "execution-fabric-phase8.test.py")
        fabric = fabric_fixture.fabric
        registered = fabric_fixture.host()
        assignment, _trace = fabric.resolve_placement(
            fabric_fixture.requirements(), [fabric_fixture.candidate()],
            {registered.host_id: registered},
            {registered.host_id: fabric_fixture.heartbeat(registered)},
            {"pool-one": fabric_fixture.pool()},
            now_epoch=1787918400, created_at=fabric_fixture.NOW,
        )
        self.assertIsNotNone(assignment)
        assert assignment is not None

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = root / "state" / "fabric.jsonl"
            store = fabric.DurableFabricStore(journal, root=root)
            with patch.object(
                fabric, "_append_bytes",
                side_effect=OSError(errno.ENOSPC, "source-only disk pressure probe"),
            ):
                with self.assertRaises(fabric.FabricError):
                    store.create_lease(
                        assignment, lease_id="lease-enospc", attempt_id="attempt-enospc",
                        remote_session_id="session-enospc", now=fabric_fixture.NOW,
                        expires_at="2026-08-28T12:05:00Z",
                    )
            self.assertEqual(store.leases, {})
            self.assertEqual(store.attempts, {})
            self.assertEqual(store.results, {})
            self.assertFalse(journal.exists() and journal.stat().st_size)
            self.assertEqual(list(journal.parent.glob(f".{journal.name}.tmp.*")), [])

            restarted = fabric.DurableFabricStore(journal, root=root)
            self.assertEqual(restarted.leases, {})
            restarted.create_lease(
                assignment, lease_id="lease-enospc", attempt_id="attempt-enospc",
                remote_session_id="session-enospc", now=fabric_fixture.NOW,
                expires_at="2026-08-28T12:05:00Z",
            )
            verified = fabric.DurableFabricStore(journal, root=root)
            self.assertEqual(verified.leases["lease-enospc"]["assignment_digest"],
                             assignment.assignment_digest)


def _run_focused_gates() -> bool:
    for label, path in FOCUSED_GATES:
        print(f"\n== {label} ==", flush=True)
        result = subprocess.run([sys.executable, "-u", str(path)], cwd=ROOT)
        if result.returncode != 0:
            print(f"FAILED: {label}", file=sys.stderr)
            return False
    print("\n== compileall ==", flush=True)
    compile_result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q",
         str(SCRIPTS / "mvp_contracts.py"), str(SCRIPTS / "mvp_kernel.py"),
         str(SCRIPTS / "mvp_adapters.py"), str(SCRIPTS / "mvp_orchestrator.py"),
         str(SCRIPTS / "mvp_control_surface.py"), str(SCRIPTS / "execution_fabric.py")],
        cwd=ROOT,
    )
    return compile_result.returncode == 0


def main() -> int:
    before = _git("status", "--porcelain=v1", "--untracked-files=all").stdout
    focused_ok = _run_focused_gates()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(MVPCompletionTests)
    completion = unittest.TextTestRunner(verbosity=2).run(suite)
    diff_ok = _git("diff", "--check", check=False).returncode == 0
    after = _git("status", "--porcelain=v1", "--untracked-files=all").stdout
    clean_run = before == "" and after == ""
    if not clean_run:
        print("repository was dirty or changed while the MVP gate ran", file=sys.stderr)
        print("before:\n" + before + "after:\n" + after, file=sys.stderr)
    if focused_ok and completion.wasSuccessful() and diff_ok and clean_run:
        print("\nrecursion-v2-mvp.test: ALL PASS")
        return 0
    print("\nrecursion-v2-mvp.test: FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
