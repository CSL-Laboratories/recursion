#!/usr/bin/env python3
"""Controls for deterministic admission of exact Head-Hermes window instructions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

from windows_test_support import bash_command, prepare_shell_env


SOURCE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE / "scripts"))
try:
    import fcntl  # type: ignore  # noqa: F401
except ImportError:  # Windows-only local development shim; production and Linux audits use fcntl.
    sys.modules["fcntl"] = types.SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda *_args: None)

import instruction_routing  # noqa: E402
import pilot_instruction  # noqa: E402


INSTRUCTION_BODY = (
    "Open pilot window window-sample-pilot-5 for example-client, "
    "goal goal-sample-0x6c-shadetops-underemission-v2, for 14400 seconds."
)
OLD_FAILURE = (
    "project-pilot: Lucas instruction does not name the exact Phase 8 command and bounds"
)


class HeadWindowAdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="phase8-head-window-admission-"))
        (self.root / "state/instructions").mkdir(parents=True)
        (self.root / "state/project-config").mkdir(parents=True)
        (self.root / "scripts").mkdir(parents=True)
        for name in (
            "assignment.py", "assignment_contract.py", "phase5_safety.py",
            "assist.sh", "author.sh", "board.sh", "branch-review.sh",
            "capacity.sh", "conduct-hermes.sh", "conduct.sh", "decision-queue.py",
            "engine_contract.py", "git-scope.py", "hermes-decide.sh", "instruction_routing.py",
            "intake.sh", "jsonl_durable.py", "notification-outbox.py", "notify.sh",
            "pilot_instruction.py", "plan.sh", "project-pilot.py", "project-reconcile.py",
            "project_authority.py", "provider-connect-proxy.py", "provider_contract.py", "provider-network.sh", "provider-egress.py", "publish.sh",
            "remote_evidence.py", "resolve-engine.py", "resolve-model-alias.py", "resync.sh",
            "run_contract.py", "runtime-state.py", "seed_contract.py", "taskctl.sh",
            "transition_receipt.py", "validate-assignment-result.py", "validate-control-state.py",
            "validate-decision.py", "validate-gate.py", "validate-verifier-result.py",
            "validate-worker-result.py", "work.sh", "assignment-token.py", "worker-evidence.py", "worker-sandbox.sh",
        ):
            shutil.copy2(SOURCE / "scripts" / name, self.root / "scripts" / name)
        (self.root / "state/PAUSE").write_text("paused for deterministic admission control\n", encoding="utf-8")
        (self.root / "state/project-config/example-client.json").write_text(
            json.dumps({
                "schema_version": 1,
                "id": "example-client",
                "pilot_id": "pilot-example-client",
                "status": "active",
            }),
            encoding="utf-8",
        )
        self.instruction = self.write_instruction(INSTRUCTION_BODY)
        self.prior_allow = os.environ.get("RECURSION_INTAKE_ALLOW")
        os.environ["RECURSION_INTAKE_ALLOW"] = "8913126684"

    def tearDown(self) -> None:
        if self.prior_allow is None:
            os.environ.pop("RECURSION_INTAKE_ALLOW", None)
        else:
            os.environ["RECURSION_INTAKE_ALLOW"] = self.prior_allow
        shutil.rmtree(self.root, ignore_errors=True)

    def write_instruction(
        self,
        body: str,
        *,
        principal: str = "8913126684",
        routing_target: str = "head-hermes",
        project_id: str = "",
        pilot_id: str = "",
    ) -> Path:
        path = self.root / "state/instructions/00000920.md"
        path.write_text(
            "\n".join([
                "schema_version: 1",
                "id: 920",
                "received: 2026-08-17T17:55:20Z",
                f"principal: {principal}",
                "topic: 1",
                f"project_id: {project_id}",
                f"pilot_id: {pilot_id}",
                "pilot_memory_key:",
                f"routing_target: {routing_target}",
                "reply_correlation: telegram:-1003894008732:1:920",
                "deduplication_key: telegram:-1003894008732:1:920",
                "from: 8913126684",
                "chat: -1003894008732",
                "thread: 1",
                "project: -",
                "status: open",
                "action_ids: action-734c91941b50207a26ed3c91",
                "",
                "## INSTRUCTION",
                body,
                "",
                "## CONSUMED 2026-08-17T17:59:30Z",
                "resulting actions: action-734c91941b50207a26ed3c91",
                "",
                "## OPEN 2026-08-17T17:59:31Z",
                "deferred/failed actions reopened instruction: "
                f"action-734c91941b50207a26ed3c91 | last failure: {OLD_FAILURE}",
                "",
            ]),
            encoding="utf-8",
        )
        return path

    def admit(self) -> tuple[int, dict | None]:
        return instruction_routing.admit(self.root, self.instruction, enqueue=False)

    def test_exact_reopened_window_command_is_shaped_before_model(self) -> None:
        rc, result = self.admit()
        self.assertEqual(rc, 11)
        self.assertIsNotNone(result)
        decision = result["decision"]
        expected = [
            "example-client",
            "goal-sample-0x6c-shadetops-underemission-v2",
            "window-sample-pilot-5",
            "14400",
        ]
        self.assertEqual(decision["actor"], "head-hermes")
        self.assertEqual(decision["routed_decider"], "head-hermes")
        self.assertEqual(decision["decision_source"], "instruction-reconciler")
        self.assertEqual(decision["verb"], "pilot-window-open")
        self.assertEqual(decision["args"], expected)
        self.assertEqual(decision["arguments"], expected)
        self.assertEqual(decision["instruction_id"], "920")
        self.assertEqual(decision["project"], expected[0])
        self.assertEqual(decision["goal_id"], expected[1])
        validated = subprocess.run(
            [
                sys.executable,
                str(SOURCE / "scripts/validate-decision.py"),
                "pilot-window-open",
                json.dumps(decision),
                "head-hermes",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)

    def test_enqueue_and_replay_are_one_durable_decision(self) -> None:
        first_rc, first = instruction_routing.admit(self.root, self.instruction, enqueue=True)
        second_rc, second = instruction_routing.admit(self.root, self.instruction, enqueue=True)
        self.assertEqual((first_rc, second_rc), (11, 11))
        self.assertEqual(first["queue_path"], second["queue_path"])
        pending = list((self.root / "state/decision-queue/pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        rows = [
            json.loads(line)
            for line in (self.root / "state/decisions.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision_source"], "instruction-reconciler")
        self.assertEqual(rows[0]["verb"], "pilot-window-open")

    @unittest.skipIf(os.name == "nt", "end-to-end shell admission runs in the Linux audit")
    def test_head_turn_admits_exact_window_without_calling_a_model(self) -> None:
        stub_dir = self.root / "stub-bin"
        stub_dir.mkdir()
        marker = self.root / "model-called"
        hermes = stub_dir / "hermes"
        hermes.write_text(
            "#!/usr/bin/env bash\nprintf called > \"$MODEL_MARKER\"\nexit 99\n",
            encoding="utf-8",
        )
        hermes.chmod(0o755)
        env = prepare_shell_env(os.environ.copy())
        env.update({
            "PATH": str(stub_dir) + os.pathsep + env.get("PATH", ""),
            "MODEL_MARKER": str(marker),
            "RECURSION_ROOT": str(self.root),
            "RECURSION_INTAKE_ALLOW": "8913126684",
        })
        run = subprocess.run(
            bash_command("scripts/hermes-decide.sh", "--dry-run"),
            cwd=self.root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertFalse(marker.exists(), "exact admission unexpectedly invoked the model")
        decision = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(decision["verb"], "pilot-window-open")
        self.assertEqual(decision["instruction_id"], "920")

    def test_bad_production_escalation_stays_refused(self) -> None:
        bad = {
            "actor": "head-hermes",
            "verb": "escalate",
            "args": [
                "pilot-window-command-binding",
                "Authorize exact pilot-window-open command for example-client",
            ],
            "project": "example-client",
            "goal": "goal-sample-0x6c-shadetops-underemission-v2",
            "wrong_if": "The actuator accepts instruction 920 without a corrected instruction.",
        }
        run = subprocess.run(
            [
                sys.executable,
                str(SOURCE / "scripts/validate-decision.py"),
                "escalate pilot-window-open",
                json.dumps(bad),
                "head-hermes",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("unsafe argument(s)", run.stdout + run.stderr)

    def test_near_misses_never_create_a_deterministic_window_decision(self) -> None:
        variants = [
            INSTRUCTION_BODY.replace("Open pilot", "Do not open pilot"),
            INSTRUCTION_BODY + " Close it instead.",
            "for wrong-project, " + INSTRUCTION_BODY,
            INSTRUCTION_BODY.replace("for 14400 seconds", "for 86401 seconds"),
        ]
        for body in variants:
            with self.subTest(body=body):
                self.write_instruction(body)
                try:
                    rc, result = self.admit()
                except instruction_routing.RoutingError:
                    continue
                self.assertNotEqual(rc, 11)
                self.assertFalse(result and result.get("decision", {}).get("verb") == "pilot-window-open")

    def test_noncanonical_bound_tokens_never_enter_the_exact_parser(self) -> None:
        variants = [
            INSTRUCTION_BODY.replace("window-sample-pilot-5", "window-sample-pilot-5'"),
            INSTRUCTION_BODY.replace("example-client,", "example-client',"),
            INSTRUCTION_BODY.replace(
                "goal-sample-0x6c-shadetops-underemission-v2,",
                "goal-sample-0x6c-shadetops-underemission-v2',",
            ),
            INSTRUCTION_BODY.replace("14400 seconds", "14400' seconds"),
            INSTRUCTION_BODY.replace("14400 seconds", "86401 seconds"),
        ]
        for body in variants:
            with self.subTest(body=body):
                self.assertIsNone(pilot_instruction.project_window_open_terms(body))

    def test_authentication_and_scoped_header_conflicts_fail_closed(self) -> None:
        cases = [
            {"principal": "not-lucas"},
            {"project_id": "wrong-project"},
            {"pilot_id": "pilot-wrong-project"},
        ]
        for fields in cases:
            with self.subTest(fields=fields):
                self.write_instruction(INSTRUCTION_BODY, **fields)
                with self.assertRaises(instruction_routing.RoutingError):
                    self.admit()


if __name__ == "__main__":
    unittest.main(verbosity=2)
