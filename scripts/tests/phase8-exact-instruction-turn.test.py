#!/usr/bin/env python3
"""Hermetic controls for supervised exact-instruction selection."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from windows_test_support import bash_command, prepare_shell_env


SOURCE = Path(__file__).resolve().parents[2]
PROJECT = "example-client"
PILOT = "pilot-example-client"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class Fixture:
    def __init__(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="phase8-exact-instruction-"))
        self.root = self.temp / "root"
        self.root.mkdir()
        shutil.copytree(SOURCE / "scripts", self.root / "scripts")
        shutil.copytree(SOURCE / "schemas", self.root / "schemas")
        for relative in (
            "state/instructions", "state/project-config", "state/pilots", "state/.locks",
            "state/tasks", "state/goals", "state/plans", "state/actions",
            "state/decision-queue/pending", "state/decision-queue/running",
            "state/decision-queue/succeeded", "state/decision-queue/failed", "loop/logs",
        ):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        (self.root / ".env").write_text("", encoding="utf-8")
        (self.root / "CONTROL_PLANE.md").write_text("fixture authority\n", encoding="utf-8")
        (self.root / "ANCHOR.md").write_text("fixture scope\n", encoding="utf-8")
        (self.root / "state/DECISIONS.md").write_text("# Decisions\n", encoding="utf-8")
        (self.root / "state/PAUSE").write_text("fixture pause\n", encoding="utf-8")
        (self.root / "state/portfolio.tsv").write_text(
            "#project\tpath\tremote\tbranch\tsshkey\troadmap\ttopic\tstatus\n",
            encoding="utf-8",
        )
        self.fixture_bin = self.temp / "fixture-bin"
        self.host_trap_bin = self.temp / "host-trap-bin"
        self.fixture_bin.mkdir()
        self.host_trap_bin.mkdir()
        self.fixture_hermes_marker = self.root / "loop/logs/fixture-hermes-invoked"
        self.host_hermes_marker = self.root / "loop/logs/host-hermes-invoked"
        fixture_hermes = self.fixture_bin / "hermes"
        fixture_hermes.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "printf 'fixture-hermes\\n' > \"$RECURSION_ROOT/loop/logs/fixture-hermes-invoked\"\n"
            "exit 97\n",
            encoding="utf-8",
            newline="\n",
        )
        fixture_hermes.chmod(0o755)
        host_trap = self.host_trap_bin / "hermes"
        host_trap.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "printf 'host-hermes\\n' > \"$RECURSION_ROOT/loop/logs/host-hermes-invoked\"\n"
            "exit 98\n",
            encoding="utf-8",
            newline="\n",
        )
        host_trap.chmod(0o755)
        if os.name == "nt":
            # scripts/hermes-decide.sh serializes planner/sync work with util-linux flock(1),
            # which Git Bash does not ship, so the turn died at "flock: command not found"
            # before any selection logic ran. This fixture is hermetic and single-process, so
            # there is no second holder to serialize against: a stand-in that satisfies the
            # descriptor form lets the exact-instruction contract be exercised for real. The
            # command form still execs its argument rather than silently skipping it. The
            # production script is untouched; this lives only on the fixture's own PATH.
            flock_stub = self.fixture_bin / "flock"
            flock_stub.write_text(
                "#!/bin/sh\n"
                "# Windows authoring host stand-in for util-linux flock(1); production is Linux.\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  case \"$1\" in\n"
                "    -w|--wait|--timeout) shift 2 ;;\n"
                "    -*) shift ;;\n"
                "    *[!0-9]*) shift; [ \"$#\" -gt 0 ] && exec \"$@\"; exit 0 ;;\n"
                "    *) shift ;;\n"
                "  esac\n"
                "done\n"
                "exit 0\n",
                encoding="utf-8",
                newline="\n",
            )
            flock_stub.chmod(0o755)
        self.write_json("state/project-config/example-client.json", {
            "schema_version": 1, "id": PROJECT, "repo": "/nonexistent/sample",
            "remote": "origin", "default_branch": "main", "deploy_key_ref": "-",
            "charter": "fixture", "allowed_paths": ["user-client/render"], "topic": 6,
            "pilot_id": PILOT, "activation_gate": "fixture", "notification_targets": [],
            "goal_source": "state/goals", "status": "active",
        })
        self.write_json("state/pilots/pilot-example-client.json", {
            "schema_version": 1, "id": PILOT, "project_id": PROJECT, "topic": 6,
            "charter": "fixture", "model_policy": "project-pilot", "concurrency_limit": 0,
            "budget": {"max_inflight_workers": 0}, "notification_targets": [],
            "goal_source": "state/goals", "memory_key": "telegram-topic:6",
            "worktree_root": ".worktrees/sample", "status": "active",
        })
        self.write_instruction(930, "head-hermes", PROJECT, "old-head-marker")
        self.write_instruction(934, PILOT, PROJECT, "fresh-pilot-marker")
        self.env = os.environ.copy()
        prepare_shell_env(self.env)
        self.env.update({
            "RECURSION_ROOT": str(self.root),
            "RECURSION_CONTROL_LOCK_WAIT_S": "2",
            "RECURSION_DECIDE_QUIET": "1",
            "RECURSION_PILOT_MODEL": "fixture-model",
            "PATH": os.pathsep.join((
                str(self.fixture_bin), str(self.host_trap_bin), self.env.get("PATH", ""),
            )),
        })

    def close(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def write_json(self, relative: str, value: dict) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

    def write_instruction(
        self, instruction_id: int, route: str, project: str, marker: str, status: str = "open"
    ) -> Path:
        pilot = PILOT if route == PILOT else ""
        body = (
            f"schema_version: 1\nid: {instruction_id}\nreceived: 2026-08-24T00:00:00Z\n"
            f"principal: 8913126684\ntopic: 6\nproject_id: {project}\npilot_id: {pilot}\n"
            f"routing_target: {route}\nstatus: {status}\naction_ids: \n\n"
            f"## INSTRUCTION\n{marker}\n"
        )
        path = self.root / "state/instructions" / f"{instruction_id:08d}.md"
        path.write_text(body, encoding="utf-8", newline="\n")
        return path

    def run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            bash_command("scripts/hermes-decide.sh", *arguments), cwd=self.root, env=self.env,
            text=True, capture_output=True, check=False,
        )

    def instruction_digest(self, instruction_id: int) -> str:
        path = self.root / "state/instructions" / f"{instruction_id:08d}.md"
        return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    fixture = Fixture()
    try:
        sealed = {value: fixture.instruction_digest(value) for value in (930, 934)}

        default = fixture.run("--brief")
        require(default.returncode == 0, f"default FIFO brief failed: {default.stderr}")
        require("old-head-marker" in default.stdout and "fresh-pilot-marker" not in default.stdout,
                "no-argument/default invocation no longer selected the oldest open instruction")
        wrapper_default = fixture.run("")
        require(wrapper_default.returncode == 1
                and "unknown decision argument" not in wrapper_default.stderr,
                "the bin/recursion empty-argument compatibility path was rejected")
        require(fixture.fixture_hermes_marker.read_text(encoding="utf-8") == "fixture-hermes\n",
                "the empty-argument compatibility path did not reach the fixture-local Hermes")
        require(not fixture.host_hermes_marker.exists(),
                "the empty-argument compatibility path escaped to the host Hermes trap")
        require(not list((fixture.root / "state/decision-queue").glob("*/*.json")),
                "the fixture-local failing Hermes created a decision")

        exact = fixture.run("--instruction", "934", "--brief")
        require(exact.returncode == 0, f"exact cross-actor selection failed: {exact.stderr}")
        require(f"project pilot {PILOT}" in exact.stdout, "exact turn did not bind the routed pilot")
        require("fresh-pilot-marker" in exact.stdout and "old-head-marker" not in exact.stdout,
                "exact turn did not isolate the requested durable instruction")
        require(sealed == {value: fixture.instruction_digest(value) for value in (930, 934)},
                "selection changed an instruction record")

        fixture.write_instruction(932, PILOT, PROJECT, "older-same-pilot-marker")
        same_actor = fixture.run("--instruction", "934", "--brief")
        require(same_actor.returncode == 5
                and "older open instruction is owned by the same routed actor" in same_actor.stderr,
                "exact selection jumped same-actor FIFO")
        fixture.write_instruction(932, PILOT, PROJECT, "older-same-pilot-marker", "acknowledged")
        require(fixture.run("--brief", "--instruction", "934").returncode == 0,
                "a terminal same-actor predecessor incorrectly blocked the exact turn")

        target = fixture.root / "state/instructions/00000934.md"
        original = target.read_text(encoding="utf-8")
        for label, changed, expected in (
            ("closed target", original.replace("status: open", "status: acknowledged"),
             "identity, status, or routing metadata is invalid"),
            ("header mismatch", original.replace("id: 934", "id: 935"),
             "identity, status, or routing metadata is invalid"),
            ("duplicate route", original.replace("routing_target: ", "routing_target: head-hermes\nrouting_target: "),
             "identity, status, or routing metadata is invalid"),
            ("unsupported route", original.replace(f"routing_target: {PILOT}", "routing_target: nobody"),
             "unsupported routing target"),
            ("wrong project binding", original.replace(f"project_id: {PROJECT}", "project_id: other"),
             "invalid project-pilot binding"),
        ):
            target.write_text(changed, encoding="utf-8", newline="\n")
            result = fixture.run("--instruction", "934", "--brief")
            require(result.returncode == 5 and expected in result.stderr,
                    f"{label} did not fail closed: rc={result.returncode} {result.stderr}")
        target.write_text(original, encoding="utf-8", newline="\n")

        actual = target.with_suffix(".actual")
        target.replace(actual)
        try:
            target.symlink_to(actual.name)
        except OSError:
            actual.replace(target)
            if os.name != "nt":
                raise
        else:
            linked = fixture.run("--instruction", "934", "--brief")
            require(linked.returncode == 5
                    and "absent or not a regular durable record" in linked.stderr,
                    "a symlinked exact instruction was accepted")
            target.unlink()
            actual.replace(target)

        for arguments in (
            ("--instruction", "0934", "--brief"),
            ("--instruction", "934x", "--brief"),
            ("--instruction", "", "--brief"),
            ("--instruction", "999", "--brief"),
            ("--instruction",),
            ("--unknown",),
            ("--brief", "--dry-run"),
            ("--instruction", "934", "--instruction", "930", "--brief"),
        ):
            result = fixture.run(*arguments)
            require(result.returncode in {2, 5},
                    f"unsafe invocation shape was accepted {arguments}: {result.returncode}")

        require(not list((fixture.root / "state/decision-queue").glob("*/*.json")),
                "brief-only selector controls created a decision")
        print("phase8-exact-instruction-turn: PASS")
    finally:
        fixture.close()


if __name__ == "__main__":
    main()
