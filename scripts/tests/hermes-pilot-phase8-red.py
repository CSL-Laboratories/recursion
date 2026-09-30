#!/usr/bin/env python3
"""Phase 8 red-to-green controls for plan synthesis and pilot-window admission."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from windows_test_support import bash_executable, prepare_shell_env


SOURCE = Path(__file__).resolve().parents[2]
CONTROLS = [
    "a fresh instruction produces a canonical charter-inheriting repo-bound plan without reusing legacy Example-ops state",
    "a malformed, unsafe-network, or invalid repo gate plan is rejected without partial state",
    "Lucas exact-digest approval revalidates the repo contract and materializes one new goal DAG idempotently",
    "only the exact goal in an open bounded pilot window is schedulable",
    "observing PAUSE closes the window so resume cannot replay its authority",
    "an answered assist reopens the same goal and task once through the durable outbox path",
    "a tracked .seedlist is controller-bound into the approved plan, goal, and task",
    "source-head drift and an absent provider capability refuse approval or window authority",
    "a project-required semantic gate profile cannot be omitted or substituted",
]


def canonical_digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def fields(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            key, value = line.split(":", 1)
            result.setdefault(key.strip(), value.strip())
    return result


def required_non_goals(fixture: "Fixture") -> set[str]:
    config = json.loads(
        (fixture.root / "state/project-config/example-ops.json").read_text(encoding="utf-8")
    )
    return set(config["required_non_goals"])


class Fixture:
    def __init__(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="phase8-red-"))
        self.root = self.temp / "root"
        self.project = self.temp / "project"
        shutil.copytree(SOURCE / "scripts", self.root / "scripts")
        shutil.copytree(SOURCE / "schemas", self.root / "schemas")
        if os.name == "nt":
            # The controller is deployed on Linux. This no-contention shim lets the hermetic,
            # single-process fixture exercise its state machines from the Windows authoring host.
            (self.root / "scripts/fcntl.py").write_text(
                "LOCK_EX=2\nLOCK_NB=4\ndef flock(_file, _flags):\n    return None\n",
                encoding="utf-8",
            )
        for relative in (
            "state/project-config", "state/pilots", "state/goals", "state/tasks",
            "state/plans/example-ops", "state/pilot-windows", "state/instructions",
            "state/actions", "state/assists", "state/pilot-requests/example-ops",
            "state/decision-queue/pending", "state/decision-queue/running",
            "state/decision-queue/succeeded", "state/decision-queue/failed",
            "state/deliveries", "state/notification-events", "state/.locks", "loop/logs",
        ):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        for relative in ("state/events.jsonl", "state/action-events.jsonl", "state/decisions.jsonl", "state/INBOX.md"):
            (self.root / relative).write_text("", encoding="utf-8")
        (self.root / ".env").write_text("", encoding="utf-8")
        self.env = os.environ.copy()
        prepare_shell_env(self.env)
        self.env.update({
            "RECURSION_ROOT": str(self.root),
            "RECURSION_QUEUE_ACTUATION": "1",
            "RECURSION_INTAKE_ALLOW": "8913126684",
            "RECURSION_NO_NOTIFY": "0",
        })
        self.provider_calls = self.root / "provider-network.calls"
        (self.root / "scripts/provider-network.sh").write_text(
            "#!/usr/bin/env bash\nprintf '%s\\n' \"${1:-}\" >> \"$RECURSION_ROOT/provider-network.calls\"\n",
            encoding="utf-8",
        )
        self._write_repo()
        self._write_project()
        self._write_legacy_state()
        self._write_instruction("101", "pilot-example-ops")
        self.planner_plan = self._plan()
        self.plan = json.loads(json.dumps(self.planner_plan))
        self.plan_id = str(self.plan["id"])
        self.digest = canonical_digest(self.plan)

    def close(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def _write_repo(self) -> None:
        self.repo = self.temp / "example-ops-repo"
        self.project = self.repo
        self.gate_path = self.repo / "user-client/render/tools/native-diff.cjs"
        implementation = self.repo / "user-client/render/shade-tops.js"
        self.gate_path.parent.mkdir(parents=True, exist_ok=True)
        self.gate_path.write_text(
            "const fs=require('fs'); const s=fs.readFileSync('user-client/render/shade-tops.js','utf8'); "
            "process.exit(s.includes('shadeTops') ? 0 : 1);\n", encoding="utf-8",
        )
        implementation.write_text("export const shadeTops = false;\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Phase 8 Test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "phase8@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "core.autocrlf", "false"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "fixture"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "branch", "-M", "main"], check=True)

    def _write_project(self) -> None:
        (self.root / "state/portfolio.tsv").write_text(
            "#project\tpath\tremote\tbranch\tsshkey\troadmap\ttopic\tstatus\n"
            f"example-ops\t{self.repo}\torigin\tmain\t-\t-\t10\tactive\n",
            encoding="utf-8",
        )
        config = {
            "schema_version": 1, "id": "example-ops", "repo": str(self.repo),
            "remote": str(self.repo), "default_branch": "main", "deploy_key_ref": "-",
            "charter": "Build-verifiable Example-ops application changes only.",
            "allowed_paths": ["user-client/render"],
            "required_non_goals": [
                "tests", "fixtures", "oracles", "baselines", "live-game-access",
                "credentials", "merge", "deploy",
            ],
            "topic": 10, "pilot_id": "pilot-example-ops",
            "activation_gate": "clone-auth-freshness-v1",
            "notification_targets": ["project-topic:telegram:-100:10", "head-hermes:telegram:-100:1"],
            "goal_source": "state/goals", "status": "active",
        }
        pilot = {
            "schema_version": 1, "id": "pilot-example-ops", "project_id": "example-ops", "topic": 10,
            "charter": config["charter"], "model_policy": "project-pilot",
            "required_non_goals": config["required_non_goals"],
            "concurrency_limit": 1, "budget": {"max_inflight_workers": 1},
            "notification_targets": config["notification_targets"], "goal_source": "state/goals",
            "memory_key": "telegram-topic:10", "worktree_root": ".worktrees/example-ops",
            "window_required": True, "status": "active",
        }
        self.write_json("state/project-config/example-ops.json", config)
        self.write_json("state/pilots/pilot-example-ops.json", pilot)

    def install_seed_contract(self, manifest: str = "data/corpus\n") -> None:
        (self.project / "data/corpus").mkdir(parents=True, exist_ok=True)
        (self.project / "data/corpus/oracle.bin").write_bytes(b"controller-owned fixture\n")
        (self.project / ".gitignore").write_text("/data/corpus/\n", encoding="utf-8")
        (self.project / ".seedlist").write_text(manifest, encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.project), "add", ".gitignore", ".seedlist"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.project), "-c", "user.name=fixture", "-c",
             "user.email=fixture@example.invalid", "commit", "-qm", "seed contract"], check=True,
        )

    def _write_legacy_state(self) -> None:
        (self.root / "state/goals/goal-example-ops-legacy.md").write_text(
            "schema_version: 1\nversion: 1\nid: goal-example-ops-legacy\nproject_id: example-ops\n"
            "status: active\noutcome: Historical shell.\nacceptance_suite: npm run build\n"
            "falsifier: The historical build fails.\nbudget: historical\n",
            encoding="utf-8",
        )
        (self.root / "state/tasks/example-ops-legacy-task.md").write_text(
            "schema_version: 1\nversion: 1\nid: example-ops-legacy-task\nproject_id: example-ops\n"
            "goal_id: goal-example-ops-legacy\nstatus: passed\nacceptance: npm run build\n",
            encoding="utf-8",
        )
        self.legacy_hash = canonical_digest({
            "goal": (self.root / "state/goals/goal-example-ops-legacy.md").read_text(encoding="utf-8"),
            "task": (self.root / "state/tasks/example-ops-legacy-task.md").read_text(encoding="utf-8"),
        })

    def _write_instruction(self, iid: str, target: str, body: str = "fixture instruction") -> None:
        name = f"{int(iid):08d}.md"
        # Head-Hermes approvals and window commands arrive through General/topic 1. Intake
        # intentionally leaves their project/pilot projection empty; the authority-bearing
        # identifiers live in the authenticated command body and the validated decision.
        topic = "1" if target == "head-hermes" else "10"
        project = "" if target == "head-hermes" else "example-ops"
        pilot = "" if target == "head-hermes" else "pilot-example-ops"
        (self.root / "state/instructions" / name).write_text(
            f"schema_version: 1\nid: {iid}\nprincipal: 8913126684\ntopic: {topic}\n"
            f"project_id: {project}\npilot_id: {pilot}\nrouting_target: {target}\n"
            f"deduplication_key: fixture\nstatus: open\naction_ids:\n\n## INSTRUCTION\n{body}\n",
            encoding="utf-8",
        )

    @staticmethod
    def _plan() -> dict:
        return {
            "schema_version": 1,
            "id": "example-ops-phase8-plan",
            "project_id": "example-ops",
            "goal": {
                "id": "goal-example-ops-phase8-smoke",
                "outcome": "Add one reversible build-verifiable Example-ops smoke marker.",
                "acceptance_suite": "node user-client/render/tools/native-diff.cjs --metric",
                "gate_class": "heavy",
                "gate_files": ["user-client/render/tools/native-diff.cjs"],
                "controller_gate_files": [],
                "falsifier": "The exact candidate commit does not pass the clean Example-ops build.",
                "budget": {
                    "max_concurrency": 1, "max_tokens": 12000, "max_cost_usd": 5,
                    "max_wall_clock_seconds": 3600, "max_task_attempts": 2,
                    "max_failures": 1, "max_review_queue": 1, "max_publish_attempts": 1,
                    "publication_required": True,
                },
            },
            "tasks": [{
                "id": "example-ops-phase8-smoke", "intent": "Add the bounded smoke marker.",
                "dependencies": [], "allowed_paths": ["user-client/render"],
                "non_goals": ["learning-loop", "credentials", "merge", "deploy"],
                "worker_class": "claude", "engine": "claude", "model_alias": "sonnet-routine",
                "verifier_model_alias": "opus-review", "deadline_seconds": 1800,
                "verifier_deadline_seconds": 900, "network_policy": "provider-only",
                "evidence_contract": "worker-result-v1", "verification": "independent",
                "expected_change": "required", "gate_class": "heavy",
                "gate_files": ["user-client/render/tools/native-diff.cjs"],
                "controller_gate_files": [],
                "acceptance": "node user-client/render/tools/native-diff.cjs --metric",
            }],
        }

    def write_json(self, relative: str, value: dict) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        return path

    def decision(self, *, verb: str, args: list[str], iid: str, actor: str, goal: str,
                 text: str, extra: dict | None = None) -> Path:
        value = {
            "schema_version": 1, "id": f"decision-{verb}-{iid}", "actor": actor,
            "verb": verb, "args": args, "arguments": args, "project": "example-ops",
            "goal": goal, "goal_id": goal, "text": text, "instruction_id": iid,
            "why": "Phase 8 bounded pilot control.", "reason": "Phase 8 bounded pilot control.",
            "wrong_if": "Exact correlated state or evidence disagrees.",
            "idempotency_key": f"phase8:{verb}:{iid}", "status": "queued",
        }
        value.update(extra or {})
        return self.write_json(f"decision-{verb}-{iid}.json", value)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=self.root, env=self.env, text=True, capture_output=True, check=False)

    def legacy_unchanged(self) -> bool:
        return self.legacy_hash == canonical_digest({
            "goal": (self.root / "state/goals/goal-example-ops-legacy.md").read_text(encoding="utf-8"),
            "task": (self.root / "state/tasks/example-ops-legacy-task.md").read_text(encoding="utf-8"),
        })


def test_fresh_plan(f: Fixture) -> bool:
    decision = f.decision(
        verb="pilot-plan-propose", args=["example-ops", f.plan_id], iid="101",
        actor="pilot-example-ops", goal=str(f.plan["goal"]["id"]),
        text="Propose a fresh bounded Example-ops Phase 8 plan.", extra={"plan": f.plan},
    )
    result = f.run("python3", "scripts/project-pilot.py", "execute", str(decision))
    path = f.root / f"state/plans/example-ops/{f.plan_id}.json"
    if result.returncode or not path.is_file() or not f.legacy_unchanged():
        return False
    record = json.loads(path.read_text(encoding="utf-8"))
    canonical = record.get("plan") or {}
    task = (canonical.get("tasks") or [{}])[0]
    required = required_non_goals(f)
    f.plan = canonical
    f.digest = str(record.get("plan_digest") or "")
    return (
        record.get("status") == "proposed"
        and record.get("plan_digest") == canonical_digest(canonical)
        and record.get("instruction_id") == "101"
        and required <= set(task.get("non_goals") or [])
        and task.get("gate_class") == "heavy"
        and set((task.get("gate_contract") or {}).get("gate_files") or {})
            == {"user-client/render/tools/native-diff.cjs"}
        and not (f.root / "state/goals/goal-example-ops-phase8-smoke.md").exists()
    )


def test_invalid_plan(f: Fixture) -> bool:
    def refused(suffix: str, mutate) -> bool:
        bad = json.loads(json.dumps(f.planner_plan)); bad["id"] = f"example-ops-phase8-invalid-{suffix}"
        mutate(bad)
        decision = f.decision(
            verb="pilot-plan-propose", args=["example-ops", bad["id"]], iid="101",
            actor="pilot-example-ops", goal=str(bad["goal"]["id"]), text="Propose an invalid plan.",
            extra={"plan": bad, "id": f"decision-invalid-{suffix}",
                   "idempotency_key": f"phase8:invalid:{suffix}"},
        )
        result = f.run("python3", "scripts/project-pilot.py", "execute", str(decision))
        return result.returncode != 0 and not (f.root / f"state/plans/example-ops/{bad['id']}.json").exists()

    return all((
        refused("gate", lambda p: p["tasks"][0].__setitem__("acceptance", "true")),
        refused("gate-class", lambda p: p["tasks"][0].pop("gate_class")),
        refused("goal-gate-files", lambda p: p["goal"].pop("gate_files")),
        refused("cloud-offline", lambda p: p["tasks"][0].__setitem__("network_policy", "none")),
        refused("indirect-gate", lambda p: p["tasks"][0].__setitem__(
            "acceptance", "env node user-client/render/tools/native-diff.cjs --metric")),
        refused("wrong-gate-file", lambda p: p["tasks"][0].__setitem__(
            "gate_files", ["user-client/render/shade-tops.js"])),
        refused("absent-gate-file", lambda p: p["tasks"][0].__setitem__(
            "gate_files", ["user-client/render/tools/native-diff.cjs", "user-client/render/tools/absent.cjs"])),
        refused("review-zero", lambda p: p["goal"]["budget"].__setitem__("max_review_queue", 0)),
        refused("metered", lambda p: p["tasks"][0].__setitem__("engine", "hermes")),
    ))


def test_approval(f: Fixture) -> bool:
    f._write_instruction("102", "head-hermes", f"approve {f.plan_id} {f.digest}")
    decision = f.decision(
        verb="pilot-plan-approve", args=["example-ops", f.plan_id, f.digest], iid="102",
        actor="head-hermes", goal=str(f.plan["goal"]["id"]),
        text="Approve exactly this plan digest for materialization.",
    )
    gate_bytes = f.gate_path.read_bytes()
    f.gate_path.write_bytes(gate_bytes + b"// drift before approval\n")
    drifted = f.run("python3", "scripts/project-pilot.py", "execute", str(decision))
    f.gate_path.write_bytes(gate_bytes)
    if drifted.returncode == 0 or (f.root / "state/goals/goal-example-ops-phase8-smoke.md").exists():
        return False
    first = f.run("python3", "scripts/project-pilot.py", "execute", str(decision))
    second = f.run("python3", "scripts/project-pilot.py", "execute", str(decision))
    goal_path = f.root / "state/goals/goal-example-ops-phase8-smoke.md"
    task_path = f.root / "state/tasks/example-ops-phase8-smoke.md"
    if first.returncode or second.returncode or not goal_path.is_file() or not task_path.is_file():
        return False
    proposal = json.loads((f.root / f"state/plans/example-ops/{f.plan_id}.json").read_text(encoding="utf-8"))
    f._write_instruction("111", "head-hermes", f"approve {f.plan_id} {f.digest}")
    conflicting_instruction = f.root / "state/instructions/00000111.md"
    conflicting_instruction.write_text(
        conflicting_instruction.read_text(encoding="utf-8")
        .replace("project_id: ", "project_id: another-project")
        .replace("pilot_id: ", "pilot_id: pilot-another-project"),
        encoding="utf-8",
    )
    conflicting = f.decision(
        verb="pilot-plan-approve", args=["example-ops", f.plan_id, f.digest], iid="111",
        actor="head-hermes", goal=str(f.plan["goal"]["id"]),
        text="Conflicting projected ownership must not authorize approval.",
    )
    conflicting_refused = f.run(
        "python3", "scripts/project-pilot.py", "execute", str(conflicting),
    ).returncode != 0
    f._write_instruction("110", "pilot-example-ops", "add an extra task")
    extra = f.decision(
        verb="pilot-task-create",
        args=["example-ops", "goal-example-ops-phase8-smoke", "unapproved-extra", "user-client/render"],
        iid="110", actor="pilot-example-ops", goal="goal-example-ops-phase8-smoke",
        text="Try to mutate the approved DAG.",
    )
    frozen = f.run("python3", "scripts/project-pilot.py", "execute", str(extra))
    return (
        fields(goal_path).get("status") == "active"
        and fields(goal_path).get("instruction_id") == "101"
        and fields(goal_path).get("plan_id") == f.plan_id
        and fields(task_path).get("status") == "proposed"
        and fields(task_path).get("workstream") == "example-ops"
        and fields(task_path).get("acceptance") == "node user-client/render/tools/native-diff.cjs --metric"
        and fields(task_path).get("gate_class") == "heavy"
        and fields(task_path).get("source_head") == proposal["plan"]["authority_contract"]["source_head"]
        and fields(goal_path).get("gate_contract")
            == json.dumps(proposal["plan"]["goal"]["gate_contract"], sort_keys=True, separators=(",", ":"))
        and required_non_goals(f) <= set(fields(task_path).get("non_goals", "").split(","))
        and proposal.get("status") == "approved"
        and proposal.get("approval_instruction_id") == "102"
        and len(list((f.root / "state/goals").glob("goal-example-ops-phase8-smoke.md"))) == 1
        and len(list((f.root / "state/tasks").glob("example-ops-phase8-smoke.md"))) == 1
        and conflicting_refused
        and frozen.returncode != 0
        and "an exact-digest Phase 8 goal has an immutable task DAG" in frozen.stderr
        and not (f.root / "state/tasks/unapproved-extra.md").exists()
        and f.legacy_unchanged()
    )


def open_window(f: Fixture) -> bool:
    f._write_instruction(
        "103", "head-hermes",
        "Open pilot window example-ops-window-1 for example-ops, "
        "goal goal-example-ops-phase8-smoke, for 900 seconds."
    )
    decision = f.decision(
        verb="pilot-window-open",
        args=["example-ops", "goal-example-ops-phase8-smoke", "example-ops-window-1", "900"], iid="103",
        actor="head-hermes", goal="goal-example-ops-phase8-smoke",
        text="Authorize only the Example-ops success pilot window.",
    )
    (f.root / "state/PAUSE").write_text("phase8-preflight\n", encoding="utf-8")
    result = f.run("python3", "scripts/project-pilot.py", "execute", str(decision))
    (f.root / "state/PAUSE").unlink(missing_ok=True)
    return result.returncode == 0


def test_window_admission(f: Fixture) -> bool:
    sink = f.run(bash_executable(), "scripts/work.sh", "example-ops-phase8-smoke")
    if sink.returncode != 75:
        return False
    before = f.run("python3", "scripts/project-reconcile.py", "schedule")
    if before.returncode or before.stdout.strip() != "0" or list((f.root / "state/decision-queue/pending").glob("*.json")):
        return False
    # A matching duration in metadata is not human authority. The exact bound must be a
    # token in the instruction body itself.
    f._write_instruction("109", "head-hermes", "authorize goal-example-ops-phase8-smoke example-ops-window-bad")
    bad_instruction = f.root / "state/instructions/00000109.md"
    bad_instruction.write_text(
        bad_instruction.read_text(encoding="utf-8").replace("topic: 1", "topic: 900"),
        encoding="utf-8",
    )
    bad_decision = f.decision(
        verb="pilot-window-open",
        args=["example-ops", "goal-example-ops-phase8-smoke", "example-ops-window-bad", "900"], iid="109",
        actor="head-hermes", goal="goal-example-ops-phase8-smoke",
        text="Metadata must not satisfy an omitted instruction-body bound.",
    )
    (f.root / "state/PAUSE").write_text("phase8-preflight\n", encoding="utf-8")
    bad = f.run("python3", "scripts/project-pilot.py", "execute", str(bad_decision))
    (f.root / "state/PAUSE").unlink(missing_ok=True)
    if bad.returncode == 0 or (f.root / "state/pilot-windows/example-ops-window-bad.json").exists():
        return False
    f._write_instruction(
        "108", "head-hermes",
        "For example-client: Open pilot window example-ops-window-wrong-project "
        "for goal goal-example-ops-phase8-smoke for 900 seconds.",
    )
    wrong_project_decision = f.decision(
        verb="pilot-window-open",
        args=["example-ops", "goal-example-ops-phase8-smoke", "example-ops-window-wrong-project", "900"],
        iid="108", actor="head-hermes", goal="goal-example-ops-phase8-smoke",
        text="A different named project must not authorize this window.",
    )
    (f.root / "state/PAUSE").write_text("phase8-preflight\n", encoding="utf-8")
    wrong_project = f.run(
        "python3", "scripts/project-pilot.py", "execute", str(wrong_project_decision),
    )
    (f.root / "state/PAUSE").unlink(missing_ok=True)
    if wrong_project.returncode == 0 \
            or (f.root / "state/pilot-windows/example-ops-window-wrong-project.json").exists():
        return False
    gate_bytes = f.gate_path.read_bytes()
    f.gate_path.write_bytes(gate_bytes + b"// drift before window\n")
    drift_refused = open_window(f)
    f.gate_path.write_bytes(gate_bytes)
    (f.root / "state/PAUSE").unlink(missing_ok=True)
    if drift_refused or (f.root / "state/pilot-windows/example-ops-window-1.json").exists():
        return False
    if not open_window(f):
        return False
    if "probe" not in f.provider_calls.read_text(encoding="utf-8").splitlines():
        return False
    task_path = f.root / "state/tasks/example-ops-phase8-smoke.md"
    original_task = task_path.read_text(encoding="utf-8")
    task_path.write_text(original_task.replace(
        "acceptance: node user-client/render/tools/native-diff.cjs --metric",
        "acceptance: true"), encoding="utf-8")
    projection_refused = f.run("python3", "scripts/project-reconcile.py", "schedule")
    armed = json.loads((f.root / "state/pilot-windows/example-ops-window-1.json").read_text(encoding="utf-8"))
    task_path.write_text(original_task, encoding="utf-8")
    if projection_refused.returncode or projection_refused.stdout.strip() != "0" \
            or armed.get("status") != "armed":
        return False
    provider_before_schedule = f.provider_calls.read_text(encoding="utf-8").splitlines()
    scheduled = f.run("python3", "scripts/project-reconcile.py", "schedule")
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in (f.root / "state/decision-queue/pending").glob("*.json")]
    provider_after_schedule = f.provider_calls.read_text(encoding="utf-8").splitlines()
    admitted = f.run(
        "python3", "scripts/project-reconcile.py", "admit-work", "example-ops-phase8-smoke",
    )
    provider_after_admit = f.provider_calls.read_text(encoding="utf-8").splitlines()
    heartbeat = f.run(
        "python3", "scripts/project-reconcile.py", "check-work-authority",
        "example-ops-phase8-smoke",
    )
    provider_after_heartbeat = f.provider_calls.read_text(encoding="utf-8").splitlines()
    return (
        scheduled.returncode == 0 and scheduled.stdout.strip() == "1" and len(rows) == 1
        and provider_after_schedule == provider_before_schedule
        and admitted.returncode == 0
        and provider_after_admit == provider_after_schedule + ["check"]
        and heartbeat.returncode == 0
        and provider_after_heartbeat == provider_after_admit
        and rows[0].get("verb") == "reconcile-goal"
        and rows[0].get("args") == ["example-ops", "goal-example-ops-phase8-smoke"]
        and rows[0].get("project") == "example-ops"
    )


def test_pause_closes_window(f: Fixture) -> bool:
    (f.root / "state/PAUSE").write_text("phase8-test\n", encoding="utf-8")
    result = f.run("python3", "scripts/project-reconcile.py", "schedule")
    window = f.root / "state/pilot-windows/example-ops-window-1.json"
    if result.returncode or not window.is_file():
        return False
    value = json.loads(window.read_text(encoding="utf-8"))
    return value.get("status") == "closed" and value.get("close_reason") == "pause-observed"


def test_assist_resume(f: Fixture) -> bool:
    (f.root / "state/PAUSE").unlink(missing_ok=True)
    goal_path = f.root / "state/goals/goal-example-ops-phase8-smoke.md"
    task_path = f.root / "state/tasks/example-ops-phase8-smoke.md"
    goal_text = goal_path.read_text(encoding="utf-8").replace("status: active", "status: blocked")
    task_text = task_path.read_text(encoding="utf-8").replace("status: proposed", "status: blocked")
    goal_path.write_text(goal_text, encoding="utf-8")
    task_path.write_text(task_text + "blocked_on: ambiguous marker placement\n", encoding="utf-8")
    (f.root / "state/assists/example-ops-phase8-smoke.md").write_text(
        "task: example-ops-phase8-smoke\nkind: decision\nstatus: open\nasked: now\nasked_by: worker\n"
        "answered:\nanswered_by:\n\n## QUESTION\nWhich marker?\n\n## WHAT WOULD SATISFY THIS\nA decision.\n"
        "\n## ANSWER\n_(unanswered)_\n",
        encoding="utf-8",
    )


    assist_path = f.root / "state/assists/example-ops-phase8-smoke.md"
    assist_path.write_text(
        assist_path.read_text(encoding="utf-8")
        .replace("answered:\n", "answered: 2026-08-10T00:00:00Z\n")
        .replace("answered_by:\n", "answered_by: 8913126684\n")
        .replace("_(unanswered)_", "Use the marker after the canonical header."),
        encoding="utf-8",
    )
    first = f.run("python3", "scripts/project-reconcile.py", "resume-assist", "example-ops-phase8-smoke", "104")
    second = f.run("python3", "scripts/project-reconcile.py", "resume-assist", "example-ops-phase8-smoke", "104")
    events = []
    for line in (f.root / "state/events.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") == "assist-answer" and row.get("task") == "example-ops-phase8-smoke":
            events.append(row)
    return (
        first.returncode == 0 and second.returncode == 0
        and fields(goal_path).get("status") == "active"
        and fields(task_path).get("status") == "queued"
        and len(events) == 1
        and events[0].get("project") == "example-ops"
        and events[0].get("goal") == "goal-example-ops-phase8-smoke"
        and fields(assist_path).get("status") == "answered"
        and fields(assist_path).get("resume_applied") == "1"
    )


def test_seed_binding(_fixture: Fixture) -> bool:
    good = Fixture()
    stale = Fixture()
    hostile = Fixture()
    try:
        good.install_seed_contract()
        propose = good.decision(
            verb="pilot-plan-propose", args=["example-ops", good.plan_id], iid="101",
            actor="pilot-example-ops", goal=str(good.plan["goal"]["id"]), text="Bind the seed contract.",
            extra={"plan": good.plan},
        )
        result = good.run("python3", "scripts/project-pilot.py", "execute", str(propose))
        if result.returncode:
            return False
        record = json.loads((good.root / f"state/plans/example-ops/{good.plan_id}.json").read_text(encoding="utf-8"))
        contract = record.get("plan", {}).get("seed_contract", {})
        digest = record.get("plan_digest", "")
        if not (
            digest == canonical_digest(record["plan"])
            and contract.get("manifest_path") == ".seedlist"
            and contract.get("paths") == ["data/corpus"]
            and len(contract.get("manifest_sha256", "")) == 64
            and len(contract.get("content_sha256", "")) == 64
        ):
            return False
        good._write_instruction("102", "head-hermes", f"approve {good.plan_id} {digest}")
        approve = good.decision(
            verb="pilot-plan-approve", args=["example-ops", good.plan_id, digest], iid="102",
            actor="head-hermes", goal=str(good.plan["goal"]["id"]), text="Approve the bound seed plan.",
        )
        approved = good.run("python3", "scripts/project-pilot.py", "execute", str(approve))
        goal = fields(good.root / "state/goals/goal-example-ops-phase8-smoke.md")
        task = fields(good.root / "state/tasks/example-ops-phase8-smoke.md")
        materialized = (
            approved.returncode == 0
            and goal.get("seed") == "data/corpus" == task.get("seed")
            and goal.get("seed_digest") == contract["content_sha256"] == task.get("seed_digest")
            and goal.get("seed_manifest_sha256") == contract["manifest_sha256"]
            and task.get("seed_manifest_sha256") == contract["manifest_sha256"]
        )

        stale.install_seed_contract()
        stale_propose = stale.decision(
            verb="pilot-plan-propose", args=["example-ops", stale.plan_id], iid="101",
            actor="pilot-example-ops", goal=str(stale.plan["goal"]["id"]), text="Bind stale seed.",
            extra={"plan": stale.plan},
        )
        if stale.run("python3", "scripts/project-pilot.py", "execute", str(stale_propose)).returncode:
            return False
        stale_record = json.loads(
            (stale.root / f"state/plans/example-ops/{stale.plan_id}.json").read_text(encoding="utf-8")
        )
        stale_digest = stale_record["plan_digest"]
        (stale.project / "data/corpus/oracle.bin").write_bytes(b"changed after proposal\n")
        stale._write_instruction("102", "head-hermes", f"approve {stale.plan_id} {stale_digest}")
        stale_approve = stale.decision(
            verb="pilot-plan-approve", args=["example-ops", stale.plan_id, stale_digest], iid="102",
            actor="head-hermes", goal=str(stale.plan["goal"]["id"]), text="Must refuse drift.",
        )
        drift_refused = (
            stale.run("python3", "scripts/project-pilot.py", "execute", str(stale_approve)).returncode != 0
            and not (stale.root / "state/goals/goal-example-ops-phase8-smoke.md").exists()
        )

        hostile.install_seed_contract("data/corpus\ndata/corpus\n")
        hostile_plan = hostile.decision(
            verb="pilot-plan-propose", args=["example-ops", hostile.plan_id], iid="101",
            actor="pilot-example-ops", goal=str(hostile.plan["goal"]["id"]), text="Must refuse duplicate seed.",
            extra={"plan": hostile.plan},
        )
        duplicate_refused = (
            hostile.run("python3", "scripts/project-pilot.py", "execute", str(hostile_plan)).returncode != 0
            and not (hostile.root / f"state/plans/example-ops/{hostile.plan_id}.json").exists()
        )
        return materialized and drift_refused and duplicate_refused
    finally:
        good.close(); stale.close(); hostile.close()


def test_source_and_provider_admission(_fixture: Fixture) -> bool:
    stale = Fixture(); provider = Fixture()
    try:
        proposed = stale.decision(
            verb="pilot-plan-propose", args=["example-ops", stale.plan_id], iid="101",
            actor="pilot-example-ops", goal=str(stale.plan["goal"]["id"]), text="Bind source HEAD.",
            extra={"plan": stale.plan},
        )
        if stale.run("python3", "scripts/project-pilot.py", "execute", str(proposed)).returncode:
            return False
        record = json.loads((stale.root / f"state/plans/example-ops/{stale.plan_id}.json").read_text())
        digest = record["plan_digest"]
        implementation = stale.repo / "user-client/render/shade-tops.js"
        implementation.write_text("export const shadeTops = true;\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(stale.repo), "add",
                        "user-client/render/shade-tops.js"], check=True)
        subprocess.run(["git", "-C", str(stale.repo), "-c", "user.name=fixture", "-c",
                        "user.email=fixture@example.invalid", "commit", "-qm", "source drift"], check=True)
        stale._write_instruction("102", "head-hermes", f"approve {stale.plan_id} {digest}")
        approval = stale.decision(
            verb="pilot-plan-approve", args=["example-ops", stale.plan_id, digest], iid="102",
            actor="head-hermes", goal=str(stale.plan["goal"]["id"]), text="Refuse stale source.",
        )
        source_refused = stale.run(
            "python3", "scripts/project-pilot.py", "execute", str(approval),
        ).returncode != 0 and not (stale.root / "state/goals/goal-example-ops-phase8-smoke.md").exists()

        if not test_fresh_plan(provider) or not test_approval(provider):
            return False
        (provider.root / "scripts/provider-network.sh").write_text(
            "#!/usr/bin/env bash\nexit 96\n", encoding="utf-8",
        )
        capability_refused = not open_window(provider) \
            and not (provider.root / "state/pilot-windows/example-ops-window-1.json").exists()
        return source_refused and capability_refused
    finally:
        stale.close(); provider.close()


def test_required_gate_profile(_fixture: Fixture) -> bool:
    fixture = Fixture()
    try:
        config_path = fixture.root / "state/project-config/example-ops.json"
        pilot_path = fixture.root / "state/pilots/pilot-example-ops.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        pilot = json.loads(pilot_path.read_text(encoding="utf-8"))
        required = {
            "acceptance": fixture.plan["goal"]["acceptance_suite"],
            "gate_class": fixture.plan["goal"]["gate_class"],
            "gate_files": fixture.plan["goal"]["gate_files"],
            "controller_gate_files": fixture.plan["goal"]["controller_gate_files"],
        }
        config["required_gate_profile"] = required
        pilot["required_gate_profile"] = required
        fixture.write_json("state/project-config/example-ops.json", config)
        fixture.write_json("state/pilots/pilot-example-ops.json", pilot)

        substitute = json.loads(json.dumps(fixture.plan))
        substitute["goal"]["acceptance_suite"] = "node user-client/render/tools/native-diff.cjs --other-profile"
        substitute["tasks"][0]["acceptance"] = substitute["goal"]["acceptance_suite"]
        decision = fixture.decision(
            verb="pilot-plan-propose", args=["example-ops", fixture.plan_id], iid="101",
            actor="pilot-example-ops", goal=str(substitute["goal"]["id"]), text="Try a substitute gate.",
            extra={"plan": substitute},
        )
        refused = fixture.run(
            "python3", "scripts/project-pilot.py", "execute", str(decision),
        ).returncode != 0 and not (fixture.root / f"state/plans/example-ops/{fixture.plan_id}.json").exists()

        good = fixture.decision(
            verb="pilot-plan-propose", args=["example-ops", fixture.plan_id], iid="101",
            actor="pilot-example-ops", goal=str(fixture.plan["goal"]["id"]), text="Use the exact gate.",
            extra={"plan": fixture.plan},
        )
        accepted = fixture.run(
            "python3", "scripts/project-pilot.py", "execute", str(good),
        ).returncode == 0 and (fixture.root / f"state/plans/example-ops/{fixture.plan_id}.json").is_file()
        return refused and accepted
    finally:
        fixture.close()


def main() -> int:
    fixture = Fixture()
    tests = [test_fresh_plan, test_invalid_plan, test_approval, test_window_admission,
             test_pause_closes_window, test_assist_resume, test_seed_binding,
             test_source_and_provider_admission, test_required_gate_profile]
    failures = 0
    try:
        for label, test in zip(CONTROLS, tests):
            try:
                passed = bool(test(fixture))
            except Exception as exc:  # A falsifier reports failure; it never aborts later controls.
                print(f"diag  {label}: {exc}")
                passed = False
            if passed:
                print(f"ok    {label}")
            else:
                print(f"FAIL  {label}")
                failures += 1
    finally:
        fixture.close()
    print(f"phase8: {len(CONTROLS) - failures}/{len(CONTROLS)} controls passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
