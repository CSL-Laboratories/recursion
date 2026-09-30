#!/usr/bin/env python3
"""Phase 8 lifecycle race and Lucas-instruction authorization red-to-green controls.

Focused falsifiers for the armed->open window race, the arm PAUSE recheck, and instruction
parsing that must refuse negated or opposite-operation text even when it names the same ids.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from windows_test_support import prepare_shell_env


SOURCE = Path(__file__).resolve().parents[2]
CONTROLS = [
    "an affirmative approve command materializes exactly once while negated or reversed text is refused",
    "an affirmative window-open command arms exactly once while negated or opposite text is refused",
    "a requested window duration is bound into the arm replay identity and equality",
    "an affirmative window-close command closes once while open/authorize text is refused",
    "an armed/open window for another goal prevents a second window in the same project from opening",
    "a competing project window appearing before the arm commit is rechecked and refuses the arm",
    "a PAUSE lifted between arm authorization and arm commit is rechecked and refused",
    "a PAUSE observed between armed read and open commit is rechecked and refuses the open",
    "an authenticated close racing the armed->open commit is rechecked and refuses the open",
    "provider loss after open refuses fresh worker admission while local heartbeat remains available",
    "a PAUSE-held goal starts its wall-clock budget only when the authorized window activates",
    "the terminal heavy-gate deadline uses the same authenticated activation anchor",
    "activation timestamp drift or post-activation materialization fails closed",
    "a lawful task lifecycle version increment preserves the approved immutable contract",
    "a fresh window re-arms a PAUSE-closed pre-worker ready task but refuses execution evidence",
    "terminal gate execution stops when its authenticated window closes",
    "legacy non-windowed pilots retain terminal gate authority without a synthetic window",
    "window-revoked verification evidence reopens and retains same-commit retry eligibility",
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


class Fixture:
    def __init__(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="phase8-race-"))
        self.root = self.temp / "root"
        self.repo = self.temp / "example-ops-repo"
        shutil.copytree(SOURCE / "scripts", self.root / "scripts")
        shutil.copytree(SOURCE / "schemas", self.root / "schemas")
        if os.name == "nt":
            (self.root / "scripts/fcntl.py").write_text(
                "LOCK_EX=2\nLOCK_NB=4\ndef flock(_file, _flags):\n    return None\n",
                encoding="utf-8",
            )
        (self.root / "scripts/provider-network.sh").write_text(
            "#!/usr/bin/env bash\n"
            "[ ! -f \"${RECURSION_ROOT:?}/state/test-provider-down\" ]\n",
            encoding="utf-8",
        )
        (self.root / "scripts/provider-network.sh").chmod(0o755)
        for relative in (
            "state/project-config", "state/pilots", "state/goals", "state/tasks",
            "state/plans/example-ops", "state/pilot-windows", "state/instructions",
            "state/actions", "state/assists", "state/pilot-requests/example-ops",
            "state/decision-queue/pending", "state/decision-queue/running",
            "state/decision-queue/succeeded", "state/decision-queue/failed",
            "state/deliveries", "state/notification-events", "state/.locks", "loop/logs",
        ):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        for relative in ("state/events.jsonl", "state/action-events.jsonl", "state/decisions.jsonl"):
            (self.root / relative).write_text("", encoding="utf-8")
        (self.root / ".env").write_text("", encoding="utf-8")
        self.env = os.environ.copy()
        prepare_shell_env(self.env)
        self.env.update({
            "RECURSION_ROOT": str(self.root),
            "RECURSION_QUEUE_ACTUATION": "1",
            "RECURSION_INTAKE_ALLOW": "8913126684",
            "RECURSION_NO_NOTIFY": "1",
        })
        self._write_repo()
        self._write_project()
        self.write_instruction("101", "pilot-example-ops", "propose the race plan")
        self.planner_plan = self._plan()
        self.plan = json.loads(json.dumps(self.planner_plan))
        self.plan_id = str(self.plan["id"])
        self.goal_id = str(self.plan["goal"]["id"])
        self.digest = canonical_digest(self.plan)

    def close(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def _write_repo(self) -> None:
        gate = self.repo / "user-client/render/tools/native-diff.cjs"
        impl = self.repo / "user-client/render/shade-tops.js"
        gate.parent.mkdir(parents=True, exist_ok=True)
        gate.write_text(
            "const fs=require('fs'); const s=fs.readFileSync('user-client/render/shade-tops.js','utf8'); "
            "process.exit(s.includes('shadeTops') ? 0 : 1);\n", encoding="utf-8",
        )
        impl.write_text("export const shadeTops = false;\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Phase 8 Race Test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "phase8race@example.invalid"], check=True)
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

    @staticmethod
    def _plan() -> dict:
        return {
            "schema_version": 1,
            "id": "example-ops-phase8-race-plan",
            "project_id": "example-ops",
            "goal": {
                "id": "goal-example-ops-phase8-race",
                "outcome": "Add one reversible build-verifiable Example-ops race marker.",
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
                "id": "example-ops-phase8-race-task", "intent": "Add the bounded race marker.",
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

    def write_instruction(self, iid: str, target: str, body: str) -> Path:
        path = self.root / "state/instructions" / f"{int(iid):08d}.md"
        topic = "1" if target == "head-hermes" else "10"
        project = "" if target == "head-hermes" else "example-ops"
        pilot = "" if target == "head-hermes" else "pilot-example-ops"
        path.write_text(
            f"schema_version: 1\nid: {iid}\nprincipal: 8913126684\ntopic: {topic}\n"
            f"project_id: {project}\npilot_id: {pilot}\nrouting_target: {target}\n"
            f"deduplication_key: fixture\nstatus: open\naction_ids:\n\n## INSTRUCTION\n{body}\n",
            encoding="utf-8",
        )
        return path

    def decision(self, *, verb: str, args: list[str], iid: str, actor: str, goal: str,
                 text: str, extra: dict | None = None) -> Path:
        value = {
            "schema_version": 1, "id": f"decision-{verb}-{iid}", "actor": actor,
            "verb": verb, "args": args, "arguments": args, "project": "example-ops",
            "goal": goal, "goal_id": goal, "text": text, "instruction_id": iid,
            "why": "Phase 8 race control.", "reason": "Phase 8 race control.",
            "wrong_if": "Exact correlated state or evidence disagrees.",
            "idempotency_key": f"phase8:{verb}:{iid}", "status": "queued",
        }
        value.update(extra or {})
        return self.write_json(f"decision-{verb}-{iid}.json", value)

    def run(self, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(args, cwd=self.root, env=env, text=True, capture_output=True, check=False)

    def propose(self) -> bool:
        decision = self.decision(
            verb="pilot-plan-propose", args=["example-ops", self.plan_id], iid="101",
            actor="pilot-example-ops", goal=self.goal_id, text="Propose the race plan.",
            extra={"plan": self.plan},
        )
        result = self.run("python3", "scripts/project-pilot.py", "execute", str(decision))
        path = self.root / f"state/plans/example-ops/{self.plan_id}.json"
        if result.returncode or not path.is_file():
            return False
        record = json.loads(path.read_text(encoding="utf-8"))
        self.plan = record.get("plan") or self.plan
        self.digest = str(record.get("plan_digest") or canonical_digest(self.plan))
        return record.get("status") == "proposed"

    def approve(self, iid: str, body: str) -> subprocess.CompletedProcess[str]:
        self.write_instruction(iid, "head-hermes", body)
        decision = self.decision(
            verb="pilot-plan-approve", args=["example-ops", self.plan_id, self.digest], iid=iid,
            actor="head-hermes", goal=self.goal_id, text="Approve the race plan.",
        )
        return self.run("python3", "scripts/project-pilot.py", "execute", str(decision))

    def open_window(self, iid: str, body: str, window: str, seconds: str,
                    env_extra: dict | None = None) -> subprocess.CompletedProcess[str]:
        self.write_instruction(iid, "head-hermes", body)
        decision = self.decision(
            verb="pilot-window-open", args=["example-ops", self.goal_id, window, seconds], iid=iid,
            actor="head-hermes", goal=self.goal_id, text="Authorize the race window.",
        )
        return self.run("python3", "scripts/project-pilot.py", "execute", str(decision),
                        env_extra=env_extra)

    def close_window(self, iid: str, body: str, window: str) -> subprocess.CompletedProcess[str]:
        self.write_instruction(iid, "head-hermes", body)
        decision = self.decision(
            verb="pilot-window-close", args=["example-ops", window], iid=iid,
            actor="head-hermes", goal=self.goal_id, text="Revoke the race window.",
        )
        return self.run("python3", "scripts/project-pilot.py", "execute", str(decision))

    def propose_and_approve(self) -> bool:
        if not self.propose():
            return False
        if self.approve("102", f"approve {self.plan_id} {self.digest}").returncode:
            return False
        return (self.root / f"state/goals/{self.goal_id}.md").is_file()

    def materialize_and_arm(self, window: str) -> bool:
        if not self.propose_and_approve():
            print(f"diag  {window}: proposal/approval setup failed")
            return False
        (self.root / "state/PAUSE").write_text("phase8-race\n", encoding="utf-8")
        result = self.open_window("103", f"authorize {self.goal_id} {window} 900", window, "900")
        passed = result.returncode == 0 and (self.root / f"state/pilot-windows/{window}.json").is_file()
        if not passed:
            print(f"diag  {window}: arm rc={result.returncode} stderr={result.stderr.strip()!r}")
        return passed


def window_status(f: Fixture, window: str) -> str:
    path = f.root / "state/pilot-windows" / f"{window}.json"
    if not path.is_file():
        return "absent"
    return str(json.loads(path.read_text(encoding="utf-8")).get("status") or "")


def set_field(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    replacement = f"{key}: {value}"
    for index, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            lines[index] = replacement
            break
    else:
        lines.append(replacement)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_approve_guards() -> bool:
    f = Fixture()
    try:
        if not f.propose():
            return False
        negated = f.approve("102", f"do not approve {f.plan_id} {f.digest}")
        reversed_ = f.approve("103", f"reject {f.plan_id} {f.digest}")
        goal_path = f.root / f"state/goals/{f.goal_id}.md"
        if negated.returncode == 0 or reversed_.returncode == 0 or goal_path.is_file():
            return False
        control = f.approve("104", f"approve {f.plan_id} {f.digest}")
        return (
            control.returncode == 0 and goal_path.is_file()
            and fields(goal_path).get("status") == "active"
            and len(list((f.root / "state/goals").glob(f"{f.goal_id}.md"))) == 1
        )
    finally:
        f.close()


def test_open_guards() -> bool:
    f = Fixture()
    try:
        if not f.propose_and_approve():
            return False
        (f.root / "state/PAUSE").write_text("phase8-race\n", encoding="utf-8")
        window = "example-ops-window-open"
        negated = f.open_window("105", f"do not authorize {f.goal_id} {window} 900", window, "900")
        opposite = f.open_window("106", f"close {window}", window, "900")
        if negated.returncode == 0 or opposite.returncode == 0 \
                or (f.root / f"state/pilot-windows/{window}.json").is_file():
            return False
        control = f.open_window("107", f"authorize {f.goal_id} {window} 900", window, "900")
        return control.returncode == 0 and window_status(f, window) == "armed"
    finally:
        f.close()


def test_duration_binding() -> bool:
    f = Fixture()
    try:
        window = "example-ops-window-duration"
        if not f.materialize_and_arm(window):
            return False
        replay = f.open_window("103", f"authorize {f.goal_id} {window} 900", window, "900")
        path = f.root / f"state/pilot-windows/{window}.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        f.write_instruction("109", "head-hermes", f"authorize {f.goal_id} {window} 1800")
        decision = f.decision(
            verb="pilot-window-open", args=["example-ops", f.goal_id, window, "1800"], iid="109",
            actor="head-hermes", goal=f.goal_id, text="Replay with a different duration.",
        )
        different = f.run("python3", "scripts/project-pilot.py", "execute", str(decision))
        passed = (
            replay.returncode == 0 and record.get("duration_seconds") == 900
            and different.returncode != 0
            and window_status(f, window) == "armed"
            and json.loads(path.read_text(encoding="utf-8")).get("duration_seconds") == 900
        )
        if not passed:
            print("diag  duration replay:", json.dumps({
                "replay_rc": replay.returncode, "replay_stderr": replay.stderr,
                "different_rc": different.returncode, "different_stderr": different.stderr,
                "record": record, "final_status": window_status(f, window),
            }, sort_keys=True))
        return passed
    finally:
        f.close()


def test_close_guards() -> bool:
    f = Fixture()
    try:
        window = "example-ops-window-close"
        if not f.materialize_and_arm(window):
            return False
        opposite = f.close_window("111", f"open {window}", window)
        if opposite.returncode == 0 or window_status(f, window) != "armed":
            print("diag  close opposite:", json.dumps({
                "rc": opposite.returncode, "stderr": opposite.stderr,
                "status": window_status(f, window),
            }, sort_keys=True))
            return False
        control = f.close_window(
            "112", f"Close pilot window {window} in example-ops.", window)
        passed = control.returncode == 0 and window_status(f, window) == "closed"
        if not passed:
            print("diag  close guard:", json.dumps({
                "opposite_rc": opposite.returncode, "opposite_stderr": opposite.stderr,
                "control_rc": control.returncode, "control_stderr": control.stderr,
                "status": window_status(f, window),
            }, sort_keys=True))
        return passed
    finally:
        f.close()


def test_arm_pause_lift_race() -> bool:
    f = Fixture()
    try:
        if not f.propose_and_approve():
            return False
        (f.root / "state/PAUSE").write_text("phase8-race\n", encoding="utf-8")
        window = "example-ops-window-arm-race"
        result = f.open_window("113", f"authorize {f.goal_id} {window} 900", window, "900",
                               env_extra={"RECURSION_TEST_PAUSE_LIFT_BEFORE_ARM": "1"})
        return result.returncode != 0 \
            and not (f.root / f"state/pilot-windows/{window}.json").is_file()
    finally:
        f.close()


def test_arm_singleton_race() -> bool:
    f = Fixture()
    try:
        if not f.propose_and_approve():
            return False
        (f.root / "state/PAUSE").write_text("phase8-race\n", encoding="utf-8")
        window = "example-ops-window-arm-singleton-race"
        result = f.open_window(
            "114", f"authorize {f.goal_id} {window} 900", window, "900",
            env_extra={"RECURSION_TEST_WINDOW_ARM_RACE": "singleton"},
        )
        competing = f.root / "state/pilot-windows/test-arm-race.json"
        return result.returncode != 0 \
            and not (f.root / f"state/pilot-windows/{window}.json").is_file() \
            and competing.is_file()
    finally:
        f.close()


def test_open_pause_race() -> bool:
    f = Fixture()
    try:
        window = "example-ops-window-open-pause"
        if not f.materialize_and_arm(window):
            return False
        (f.root / "state/PAUSE").unlink(missing_ok=True)
        result = f.run("python3", "scripts/project-reconcile.py", "schedule",
                       env_extra={"RECURSION_TEST_WINDOW_OPEN_RACE": "pause"})
        return result.returncode == 0 and result.stdout.strip() == "0" \
            and window_status(f, window) == "armed" \
            and (f.root / "state/PAUSE").is_file()
    finally:
        f.close()


def test_open_close_race() -> bool:
    f = Fixture()
    try:
        window = "example-ops-window-open-close"
        if not f.materialize_and_arm(window):
            return False
        (f.root / "state/PAUSE").unlink(missing_ok=True)
        result = f.run("python3", "scripts/project-reconcile.py", "schedule",
                       env_extra={"RECURSION_TEST_WINDOW_OPEN_RACE": "close"})
        passed = result.returncode == 0 and result.stdout.strip() == "0" \
            and window_status(f, window) == "closed"
        if not passed:
            print("diag  open/close race:", json.dumps({
                "rc": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
                "status": window_status(f, window),
            }, sort_keys=True))
        return passed
    finally:
        f.close()


def test_open_capability_loss_race() -> bool:
    f = Fixture()
    try:
        window = "example-ops-window-open-capability"
        if not f.materialize_and_arm(window):
            return False
        (f.root / "state/PAUSE").unlink(missing_ok=True)
        opened = f.run("python3", "scripts/project-reconcile.py", "schedule")
        (f.root / "state/test-provider-down").write_text("down\n", encoding="utf-8")
        admitted = f.run(
            "python3", "scripts/project-reconcile.py", "admit-work",
            "example-ops-phase8-race-task",
        )
        heartbeat = f.run(
            "python3", "scripts/project-reconcile.py", "check-work-authority",
            "example-ops-phase8-race-task",
        )
        return opened.returncode == 0 and opened.stdout.strip() == "1" \
            and window_status(f, window) == "open" \
            and admitted.returncode == 74 and heartbeat.returncode == 0
    finally:
        f.close()


def test_window_activation_budget_anchor() -> bool:
    f = Fixture()
    try:
        window = "example-ops-window-budget-anchor"
        if not f.materialize_and_arm(window):
            return False
        goal_path = f.root / f"state/goals/{f.goal_id}.md"
        set_field(goal_path, "started_at_epoch", "1")
        (f.root / "state/PAUSE").unlink(missing_ok=True)
        before = int(time.time())
        result = f.run(
            "python3", "scripts/project-reconcile.py", "reconcile", "example-ops", f.goal_id,
        )
        after = int(time.time())
        window_record = json.loads(
            (f.root / f"state/pilot-windows/{window}.json").read_text(encoding="utf-8")
        )
        pending = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((f.root / "state/decision-queue/pending").glob("*.json"))
        ]
        activation = window_record.get("activated_at_epoch")
        passed = (
            result.returncode == 0
            and window_record.get("status") == "open"
            and isinstance(activation, int) and before <= activation <= after
            and len(pending) == 1
            and pending[0].get("verb") == "task-transition"
            and pending[0].get("args")
                == ["example-ops", f.goal_id, "example-ops-phase8-race-task",
                    "proposed", "ready", "dependencies-passed"]
        )
        if not passed:
            print("diag  activation budget anchor:", json.dumps({
                "rc": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
                "window": window_record, "pending": pending,
            }, sort_keys=True))
        return passed
    finally:
        f.close()


def test_terminal_gate_budget_anchor() -> bool:
    f = Fixture()
    try:
        window = "example-ops-window-terminal-deadline"
        if not f.materialize_and_arm(window):
            return False
        goal_path = f.root / f"state/goals/{f.goal_id}.md"
        set_field(goal_path, "started_at_epoch", "1")
        (f.root / "state/PAUSE").unlink(missing_ok=True)
        opened = f.run(
            "python3", "scripts/project-reconcile.py", "reconcile", "example-ops", f.goal_id,
        )
        probe = r"""
import importlib.util, json, sys, time
from pathlib import Path
base, goal_id = Path(sys.argv[1]), sys.argv[2]
spec = importlib.util.spec_from_file_location("phase8_reconcile", base / "scripts/project-reconcile.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
goal = module.read_fields(base / "state/goals" / f"{goal_id}.md")
_, pilot = module.project_records(base, "example-ops")
window = module.require_window(base, "example-ops", goal_id)
budget = module.parse_budget(goal)
observed = int(time.time())
deadline = module.goal_wall_clock_deadline(
    base, "example-ops", goal_id, goal, pilot, budget, window, observed,
)
print(json.dumps({"deadline": deadline, "activation": window["activated_at_epoch"]}))
"""
        deadline = f.run("python3", "-c", probe, str(f.root), f.goal_id)
        value = json.loads(deadline.stdout) if deadline.returncode == 0 else {}
        return (
            opened.returncode == 0 and deadline.returncode == 0
            and value.get("deadline") == value.get("activation") + 3600
        )
    finally:
        f.close()


def test_window_activation_anchor_tamper() -> bool:
    activation = Fixture()
    future_goal = Fixture()
    try:
        window = "example-ops-window-activation-tamper"
        if not activation.materialize_and_arm(window):
            return False
        (activation.root / "state/PAUSE").unlink(missing_ok=True)
        opened = activation.run(
            "python3", "scripts/project-reconcile.py", "reconcile",
            "example-ops", activation.goal_id,
        )
        pending_dir = activation.root / "state/decision-queue/pending"
        for path in pending_dir.glob("*.json"):
            path.unlink()
        window_path = activation.root / f"state/pilot-windows/{window}.json"
        record = json.loads(window_path.read_text(encoding="utf-8"))
        record["activated_at_epoch"] = int(record["activated_at_epoch"]) + 1
        record["opened_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(record["activated_at_epoch"]),
        )
        activation.write_json(f"state/pilot-windows/{window}.json", record)
        drift = activation.run(
            "python3", "scripts/project-reconcile.py", "reconcile",
            "example-ops", activation.goal_id,
        )

        future_window = "example-ops-window-future-materialization"
        if not future_goal.materialize_and_arm(future_window):
            return False
        future_path = future_goal.root / f"state/goals/{future_goal.goal_id}.md"
        set_field(future_path, "started_at_epoch", str(int(time.time()) + 60))
        (future_goal.root / "state/PAUSE").unlink(missing_ok=True)
        future = future_goal.run(
            "python3", "scripts/project-reconcile.py", "reconcile",
            "example-ops", future_goal.goal_id,
        )
        passed = (
            opened.returncode == 0
            and drift.returncode != 0 and not list(pending_dir.glob("*.json"))
            and future.returncode != 0
            and not list((future_goal.root / "state/decision-queue/pending").glob("*.json"))
        )
        if not passed:
            print("diag  activation anchor tamper:", json.dumps({
                "opened_rc": opened.returncode,
                "drift_rc": drift.returncode, "drift_stderr": drift.stderr,
                "future_rc": future.returncode, "future_stderr": future.stderr,
            }, sort_keys=True))
        return passed
    finally:
        activation.close()
        future_goal.close()


def test_task_lifecycle_version_preserves_contract() -> bool:
    f = Fixture()
    try:
        if not f.propose_and_approve():
            return False
        task_id = str(f.plan["tasks"][0]["id"])
        task_path = f.root / f"state/tasks/{task_id}.md"
        set_field(task_path, "status", "ready")
        set_field(task_path, "version", "2")
        set_field(task_path, "lease_expires", "0")
        set_field(task_path, "event_seq", "3")
        set_field(task_path, "last_transition_id", "transition-lifecycle-control")
        accepted = f.run(
            "python3", "scripts/project-pilot.py", "verify-approved-goal",
            "example-ops", f.goal_id,
        )
        set_field(task_path, "allowed_paths", "tests")
        immutable_tamper = f.run(
            "python3", "scripts/project-pilot.py", "verify-approved-goal",
            "example-ops", f.goal_id,
        )
        return accepted.returncode == 0 and immutable_tamper.returncode != 0
    finally:
        f.close()


def test_fresh_window_rearms_only_preworker_ready() -> bool:
    def close_after_ready(f: Fixture, window: str) -> bool:
        if not f.materialize_and_arm(window):
            return False
        (f.root / "state/PAUSE").unlink(missing_ok=True)
        opened = f.run("python3", "scripts/project-reconcile.py", "schedule")
        if opened.returncode != 0 or window_status(f, window) != "open":
            return False
        task_id = str(f.plan["tasks"][0]["id"])
        task_path = f.root / f"state/tasks/{task_id}.md"
        set_field(task_path, "status", "ready")
        set_field(task_path, "version", "2")
        set_field(task_path, "lease_expires", "0")
        (f.root / "state/PAUSE").write_text("phase8-race\n", encoding="utf-8")
        closed = f.run("python3", "scripts/project-reconcile.py", "close-paused-windows")
        return closed.returncode == 0 and window_status(f, window) == "closed"

    eligible = Fixture()
    attempted = Fixture()
    recorded = Fixture()
    candidate = Fixture()
    try:
        if not close_after_ready(eligible, "example-ops-window-ready-first"):
            return False
        admitted = eligible.open_window(
            "121", f"authorize {eligible.goal_id} example-ops-window-ready-second 900",
            "example-ops-window-ready-second", "900",
        )

        controls = []
        for fixture, first, second, mutation in (
            (attempted, "example-ops-window-attempt-first", "example-ops-window-attempt-second", "attempt"),
            (recorded, "example-ops-window-run-first", "example-ops-window-run-second", "run"),
            (candidate, "example-ops-window-candidate-first", "example-ops-window-candidate-second", "candidate"),
        ):
            if not close_after_ready(fixture, first):
                return False
            task_id = str(fixture.plan["tasks"][0]["id"])
            task_path = fixture.root / f"state/tasks/{task_id}.md"
            if mutation == "attempt":
                set_field(task_path, "attempts", "1")
            elif mutation == "run":
                fixture.write_json("state/runs/run-execution-control.json", {
                    "schema_version": 1, "id": "run-execution-control", "task_id": task_id,
                    "project_id": "example-ops", "status": "failed",
                })
            else:
                set_field(task_path, "candidate_commit", "a" * 40)
            result = fixture.open_window(
                "121", f"authorize {fixture.goal_id} {second} 900", second, "900",
            )
            controls.append(result.returncode != 0 and window_status(fixture, second) == "absent")

        return admitted.returncode == 0 \
            and window_status(eligible, "example-ops-window-ready-second") == "armed" \
            and all(controls)
    finally:
        eligible.close()
        attempted.close()
        recorded.close()
        candidate.close()


def test_terminal_gate_window_revocation() -> bool:
    f = Fixture()
    try:
        window_id = "example-ops-window-terminal-revocation"
        if not f.materialize_and_arm(window_id):
            return False
        (f.root / "state/PAUSE").unlink(missing_ok=True)
        opened = f.run(
            "python3", "scripts/project-reconcile.py", "reconcile", "example-ops", f.goal_id,
        )
        window_path = f.root / f"state/pilot-windows/{window_id}.json"
        (f.root / "scripts/worker-sandbox.sh").write_text(
            "#!/usr/bin/env bash\nsleep 20\n", encoding="utf-8",
        )
        (f.root / "scripts/worker-sandbox.sh").chmod(0o755)
        probe = r"""
import importlib.util, json, sys, threading, time
from pathlib import Path
base, repo, window_path = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
spec = importlib.util.spec_from_file_location("phase8_reconcile", base / "scripts/project-reconcile.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
window = json.loads(window_path.read_text(encoding="utf-8"))
def close_window():
    time.sleep(0.2)
    closed = dict(window); closed["status"] = "closed"
    window_path.write_text(json.dumps(closed) + "\n", encoding="utf-8")
threading.Thread(target=close_window, daemon=True).start()
outcome, rc = module.run_gate_interruptibly(
    base, repo, "sleep 20", int(time.time()) + 10, "none", window=window,
)
print(json.dumps({"outcome": outcome, "rc": rc}))
"""
        revoked = f.run("python3", "-c", probe, str(f.root), str(f.repo), str(window_path))
        value = json.loads(revoked.stdout) if revoked.returncode == 0 else {}
        return (
            opened.returncode == 0 and revoked.returncode == 0
            and value == {"outcome": "authority-revoked", "rc": 75}
        )
    finally:
        f.close()


def test_legacy_terminal_gate_authority() -> bool:
    f = Fixture()
    try:
        pilot_path = f.root / "state/pilots/pilot-example-ops.json"
        pilot = json.loads(pilot_path.read_text(encoding="utf-8"))
        pilot["window_required"] = False
        f.write_json("state/pilots/pilot-example-ops.json", pilot)
        (f.root / "scripts/worker-sandbox.sh").write_text(
            "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8",
        )
        (f.root / "scripts/worker-sandbox.sh").chmod(0o755)
        probe = r"""
import importlib.util, json, sys, time
from pathlib import Path
base, repo = Path(sys.argv[1]), Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("phase8_reconcile", base / "scripts/project-reconcile.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
_, pilot = module.project_records(base, "example-ops")
synthetic = module.require_window(base, "example-ops", "goal-example-ops-phase8-race")
authority = module.terminal_window_authority(pilot, synthetic)
outcome, rc = module.run_gate_interruptibly(
    base, repo, "true", int(time.time()) + 5, "none", window=authority,
)
print(json.dumps({"authority": authority, "outcome": outcome, "rc": rc}))
"""
        result = f.run("python3", "-c", probe, str(f.root), str(f.repo))
        value = json.loads(result.stdout) if result.returncode == 0 else {}
        return result.returncode == 0 and value == {
            "authority": None, "outcome": "pass", "rc": 0,
        }
    finally:
        f.close()


def test_revoked_verification_reopens() -> bool:
    f = Fixture()
    try:
        if not f.propose_and_approve():
            return False
        goal_path = f.root / f"state/goals/{f.goal_id}.md"
        set_field(goal_path, "status", "verifying")
        f.write_json(f"state/evidence/{f.goal_id}.json", {
            "schema_version": 1,
            "id": f"goal-evidence-{f.goal_id}-authority-revoked",
            "project_id": "example-ops",
            "goal_id": f.goal_id,
            "verdict": "authority-revoked",
        })
        probe = r"""
import importlib.util, sys
from pathlib import Path
base, goal_id = Path(sys.argv[1]), sys.argv[2]
spec = importlib.util.spec_from_file_location("phase8_reconcile", base / "scripts/project-reconcile.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module.transition_goal(
    base, "example-ops", goal_id, "verifying", "active", "verification-reopened",
)
"""
        result = f.run("python3", "-c", probe, str(f.root), f.goal_id)
        evidence = json.loads(
            (f.root / f"state/evidence/{f.goal_id}.json").read_text(encoding="utf-8")
        )
        history = list((f.root / "state/evidence/history").glob(
            f"{f.goal_id}-reopen-*-authority-revoked.json",
        ))
        probe_retry = r"""
import importlib.util, json, sys
from pathlib import Path
base, goal_id = Path(sys.argv[1]), sys.argv[2]
spec = importlib.util.spec_from_file_location("phase8_reconcile", base / "scripts/project-reconcile.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
evidence = module.goal_evidence(base, goal_id)
tasks = module.goal_tasks(base, goal_id)
print(json.dumps({
    "same_graph_retry": module.consumed_revocation_allows_same_graph_retry(evidence),
    "new_task_graph": module.consumed_evidence_has_new_task_graph(evidence, tasks),
}))
"""
        retry = f.run("python3", "-c", probe_retry, str(f.root), f.goal_id)
        retry_value = json.loads(retry.stdout) if retry.returncode == 0 else {}
        return (
            result.returncode == 0
            and fields(goal_path).get("status") == "active"
            and evidence.get("verdict") == "consumed"
            and evidence.get("source_verdict") == "authority-revoked"
            and len(history) == 1
            and retry_value == {"same_graph_retry": True, "new_task_graph": False}
        )
    finally:
        f.close()


def test_project_wide_window_singleton() -> bool:
    f = Fixture()
    try:
        window = "example-ops-window-singleton"
        if not f.materialize_and_arm(window):
            return False
        competing = {
            "schema_version": 1, "id": "example-ops-other-goal-window", "project_id": "example-ops",
            "pilot_id": "pilot-example-ops", "goal_id": "goal-example-ops-other",
            "plan_id": "other-plan", "plan_digest": "f" * 64,
            "instruction_id": "999", "status": "armed",
            "expires_at_epoch": int(time.time()) + 900,
        }
        path = f.root / "state/pilot-windows/example-ops-other-goal-window.json"
        path.write_text(json.dumps(competing), encoding="utf-8")
        (f.root / "state/PAUSE").unlink(missing_ok=True)
        result = f.run("python3", "scripts/project-reconcile.py", "schedule")
        return result.returncode == 0 and result.stdout.strip() == "0" \
            and window_status(f, window) == "armed"
    finally:
        f.close()


def main() -> int:
    tests = [
        test_approve_guards, test_open_guards, test_duration_binding, test_close_guards,
        test_project_wide_window_singleton,
        test_arm_singleton_race, test_arm_pause_lift_race,
        test_open_pause_race, test_open_close_race,
        test_open_capability_loss_race,
        test_window_activation_budget_anchor,
        test_terminal_gate_budget_anchor,
        test_window_activation_anchor_tamper,
        test_task_lifecycle_version_preserves_contract,
        test_fresh_window_rearms_only_preworker_ready,
        test_terminal_gate_window_revocation,
        test_legacy_terminal_gate_authority,
        test_revoked_verification_reopens,
    ]
    failures = 0
    for label, test in zip(CONTROLS, tests):
        try:
            passed = bool(test())
        except Exception as exc:  # noqa: BLE001
            print(f"diag  {label}: {exc}")
            passed = False
        print(("ok    " if passed else "FAIL  ") + label)
        if not passed:
            failures += 1
    print(f"phase8-race: {len(CONTROLS) - failures}/{len(CONTROLS)} controls passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
