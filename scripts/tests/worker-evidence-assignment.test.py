#!/usr/bin/env python3
"""Hermetic controls for worker-evidence.py's Phase 3 (D-016) additions:
the optional --assignment-digest/--assignment-file pair on `start`, and the new
`remote-evidence` subcommand's assignment boundary (remote session/branch/commit/artifact/
cancellation evidence itself is covered by the Phase 5 boundary battery).

Confirms the pre-Phase-3 record shape is byte-for-byte unchanged when the new flags are
omitted (legacy behavior preserved), and that both flags together are required, matching,
and bound to the correct task before anything is embedded (fail closed).
"""

from __future__ import annotations

import json
import hashlib
import os
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
SCRIPTS = SOURCE / "scripts"
WORKER_EVIDENCE = SCRIPTS / "worker-evidence.py"

sys.path.insert(0, str(SCRIPTS))
import assignment as amod  # noqa: E402
import assignment_contract  # noqa: E402

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"ok - {label}")
    else:
        print(f"FAIL - {label}")
        failures.append(label)


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(WORKER_EVIDENCE), *args], capture_output=True, text=True)


def load_worker_module():
    spec = importlib.util.spec_from_file_location("worker_evidence_phase3", WORKER_EVIDENCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def start_args(path: Path, run_id: str, task_id: str) -> list[str]:
    return [
        "start", "--path", str(path), "--run-id", run_id, "--task-id", task_id,
        "--role", "implementer", "--model-alias", "sonnet-routine", "--worktree", "/tmp/wt",
        "--base-commit", "a" * 40, "--pid", "100", "--process-group", "100",
        "--deadline-epoch", "9999999999", "--log", "loop/logs/x.out",
        "--network-policy", "provider-only",
        "--billing-basis", "flat-subscription", "--default-branch", "main",
        "--integration-branch", "recursion/integration",
    ]


def test_legacy_start_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    result = run(start_args(path, "run-legacy", "task-1"))
    check(result.returncode == 0, f"start without assignment flags still exits 0 (got {result.returncode})")
    value = json.loads(path.read_text(encoding="utf-8"))
    for key in ("engine", "provider", "capacity_pool", "host", "sandbox_profile", "assignment_digest"):
        check(key not in value, f"legacy start (no assignment flags) never adds {key!r}")
    check(value["id"] == "run-legacy" and value["task_id"] == "task-1", "legacy start still writes the base record")


def make_assignment(tmp_path: Path, task_id: str = "task-1") -> tuple[Path, str]:
    """Build a complete Phase 5 assignment, including its source/controller roots."""
    fixture = tmp_path / "strict-fixture"
    source = fixture / "source"
    (source / "scripts").mkdir(parents=True)
    (source / "scripts/gate.sh").write_bytes(b"#!/bin/sh\ntest -s result.txt\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "core.autocrlf", "false"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "source"], check=True)

    controller = fixture / "controller"
    (controller / "scripts/engines").mkdir(parents=True)
    for relative in ("validate-gate.py", "phase5_safety.py", "resolve-engine.py",
                     "provider-egress-profiles.json", "capacity-pools.json"):
        shutil.copy2(SCRIPTS / relative, controller / "scripts" / relative)
    shutil.copy2(SCRIPTS / "engines/cursor.json", controller / "scripts/engines/cursor.json")
    subprocess.run(["git", "init", "-q", "-b", "main", str(controller)], check=True)
    subprocess.run(["git", "-C", str(controller), "config", "core.autocrlf", "false"], check=True)
    subprocess.run(["git", "-C", str(controller), "add", "."], check=True)
    subprocess.run(["git", "-C", str(controller), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "controller"], check=True)
    (controller / "state").mkdir()
    key = controller / "state/.worker-evidence.key"
    key.write_bytes(os.urandom(32)); key.chmod(0o600)
    os.environ["RECURSION_ROOT"] = str(controller)

    source_sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    controller_sha = subprocess.check_output(["git", "-C", str(controller), "rev-parse", "HEAD"], text=True).strip()
    gate_bytes = (source / "scripts/gate.sh").read_bytes()
    gate_contract = {
        "acceptance": "bash scripts/gate.sh",
        "acceptance_sha256": hashlib.sha256(b"bash scripts/gate.sh").hexdigest(),
        "gate_files": {"scripts/gate.sh": hashlib.sha256(gate_bytes).hexdigest()},
        "controller_gate_files": {},
    }
    role = {
        "engine": "cursor", "provider": "cursor", "model": "cursor-primary",
        "network_policy": "provider-only", "network_profile": "cursor",
        "argv": ["{{EXE}}", "-p", "--trust", "{{PROMPT}}", "--output-format", "text", "--force",
                 "--model", "{{MODEL}}"],
        "network_endpoints": ["api.cursor.com", "api2.cursor.sh",
                              "agentn.global.api5.cursor.sh", "agentn.global.api5lat.cursor.sh",
                              "agentn.global.api5geo.cursor.sh"],
    }
    value = {
        "task_id": task_id, "project_id": "project-1", "source_repository": str(source),
        "source_branch": "main", "source_sha": source_sha,
        "controller_repository": str(controller), "controller_sha": controller_sha,
        "seed_manifest_digest": "", "engine": "cursor", "provider": "cursor",
        "capacity_pool": "cursor-ultra-grok-bot", "model": "cursor-primary",
        "host": "gs-recursion", "sandbox_profile": "strict-linux", "network_policy": "provider-only",
        "network_profile": "cursor", "network_endpoints": list(role["network_endpoints"]),
        "argv": list(role["argv"]), "allowed_paths": ["scripts"],
        "gate_digest": hashlib.sha256(assignment_contract.canonical_bytes(gate_contract)).hexdigest(),
        "gate_contract": gate_contract,
        "role_assignments": {"implementer": role, "verifier": dict(role)},
        "publication_policy": "verified-integration-only",
    }
    digest = assignment_contract.digest(value)
    path = tmp_path / "assignments.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path, digest


def make_obsolete_assignment(tmp_path: Path) -> tuple[Path, str]:
    """Build the old Phase 3 shape solely to prove strict starts refuse it."""
    value = amod.resolve_assignment(
        task_id="task-1", project_id="proj-1", role="implementer",
        source_repository="/opt/example-project", source_branch="loop/work/task-1",
        source_sha="a" * 40, engine="claude", provider="anthropic-claude-code",
        capacity_pool=None, host="gs-recursion", sandbox_profile="strict-linux",
        network_profile="provider-only", allowed_paths=["apps/api"],
        publication_policy="verified-integration-only", created_at="2026-08-28T00:00:00Z",
    )
    root = tmp_path / "legacy-assignments"
    persisted = amod.persist_assignment(root, value)
    return persisted, value["assignment_digest"]


def test_start_binds_assignment(tmp_path: Path) -> None:
    assignment_file, digest = make_assignment(tmp_path)
    path = tmp_path / "run.json"
    args = start_args(path, "run-bound", "task-1") + \
        ["--assignment-digest", digest, "--assignment-file", str(assignment_file)]
    result = run(args)
    check(result.returncode == 0, f"start with a matching assignment pair exits 0 (got {result.returncode}, stderr={result.stderr!r})")
    value = json.loads(path.read_text(encoding="utf-8"))
    check(value.get("assignment_digest") == digest, "bound start embeds the assignment_digest")
    check(value.get("engine") == "cursor", "bound start embeds engine from the assignment")
    check(value.get("provider") == "cursor", "bound start embeds provider from the assignment")
    check(value.get("host") == "gs-recursion", "bound start embeds host from the assignment")
    check(value.get("sandbox_profile") == "strict-linux", "bound start embeds sandbox_profile from the assignment")
    check(value.get("capacity_pool") == "cursor-ultra-grok-bot",
          "bound start embeds the strict assignment capacity_pool")
    check(value.get("billing_basis") == "flat-subscription",
          "bound start embeds the controller-supplied billing basis")
    check(value.get("metered_approved") is False,
          "bound start records that metered usage was not approved")
    check(value.get("default_branch") == "main" and
          value.get("integration_branch") == "recursion/integration",
          "bound start embeds the controller-supplied branch policy")


def test_start_refuses_obsolete_assignment_shape(tmp_path: Path) -> None:
    assignment_file, digest = make_obsolete_assignment(tmp_path)
    path = tmp_path / "run.json"
    result = run(start_args(path, "run-obsolete", "task-1") +
                 ["--assignment-digest", digest, "--assignment-file", str(assignment_file)])
    check(result.returncode != 0, "a paired obsolete Phase 3 assignment is refused")
    check(not path.exists(), "obsolete assignment refusal writes no partial run evidence")


def test_start_refuses_one_sided_flags(tmp_path: Path) -> None:
    assignment_file, digest = make_assignment(tmp_path)
    path = tmp_path / "run.json"
    only_digest = run(start_args(path, "run-onesided-1", "task-1") + ["--assignment-digest", digest])
    check(only_digest.returncode == 2, f"--assignment-digest without --assignment-file is refused (got {only_digest.returncode})")
    check(not path.exists(), "a refused one-sided start writes no record")

    only_file = run(start_args(path, "run-onesided-2", "task-1") + ["--assignment-file", str(assignment_file)])
    check(only_file.returncode == 2, f"--assignment-file without --assignment-digest is refused (got {only_file.returncode})")


def test_start_refuses_digest_mismatch(tmp_path: Path) -> None:
    assignment_file, _digest = make_assignment(tmp_path)
    path = tmp_path / "run.json"
    wrong_digest = "0" * 64
    result = run(start_args(path, "run-wrong-digest", "task-1") +
                ["--assignment-digest", wrong_digest, "--assignment-file", str(assignment_file)])
    check(result.returncode == 2, f"a digest that does not match the assignment file's own digest is refused (got {result.returncode})")
    check(not path.exists(), "a refused digest-mismatch start writes no record")


def test_start_refuses_task_id_mismatch(tmp_path: Path) -> None:
    assignment_file, digest = make_assignment(tmp_path, task_id="task-1")
    path = tmp_path / "run.json"
    result = run(start_args(path, "run-wrong-task", "task-DIFFERENT") +
                ["--assignment-digest", digest, "--assignment-file", str(assignment_file)])
    check(result.returncode == 2, f"an assignment resolved for a different task_id is refused (got {result.returncode})")


def test_finish_rejects_malformed_and_off_schema_inputs(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    result = run(start_args(path, "run-finish-validation", "task-1"))
    check(result.returncode == 0, "finish-validation fixture starts")
    original = path.read_bytes()

    malformed_usage = tmp_path / "usage.json"
    malformed_usage.write_text("{not json", encoding="utf-8")
    malformed = run(["finish", str(path), "succeeded", "done", "0",
                     "--usage-file", str(malformed_usage)])
    check(malformed.returncode != 0 and "worker-evidence:" in malformed.stderr
          and "Traceback" not in malformed.stderr,
          "malformed usage is a typed refusal without a traceback")
    check(path.read_bytes() == original, "malformed usage cannot publish a partial finish")

    malformed_run = tmp_path / "malformed-run.json"
    malformed_run.write_text("[not an object]", encoding="utf-8")
    loaded = run(["finish", str(malformed_run), "succeeded", "done", "0"])
    check(loaded.returncode != 0 and "worker-evidence:" in loaded.stderr
          and "Traceback" not in loaded.stderr,
          "malformed run evidence is a typed load refusal without a traceback")

    off_enum = run(["finish", str(path), "complete", "done", "0"])
    check(off_enum.returncode != 0 and "Traceback" not in off_enum.stderr,
          "off-enum finish status is refused before publication")
    check(path.read_bytes() == original, "off-enum status leaves the run unchanged")

    result_file = tmp_path / "result.json"
    result_file.write_text("[]", encoding="utf-8")
    array_result = run(["finish", str(path), "succeeded", "done", "0",
                        "--result-file", str(result_file)])
    check(array_result.returncode != 0 and "Traceback" not in array_result.stderr,
          "array finish result is refused before publication")
    check(path.read_bytes() == original, "array result leaves the run unchanged")


def test_usage_validation_applies_to_legacy_and_bound_runs(tmp_path: Path) -> None:
    bad_payloads = {
        "array": "[[]]",
        "negative": json.dumps({"tokens": -1}),
        "boolean-number": json.dumps({"cost_usd": True}),
        "non-finite": "{\"cost_usd\": NaN}",
    }
    for label, payload in bad_payloads.items():
        path = tmp_path / f"legacy-{label}.json"
        started = run(start_args(path, f"run-legacy-{label}", "task-1"))
        check(started.returncode == 0, f"legacy usage fixture {label} starts")
        original = path.read_bytes()
        usage = tmp_path / f"usage-{label}.json"
        usage.write_text(payload, encoding="utf-8")
        result = run(["finish", str(path), "succeeded", "done", "0", "--usage-file", str(usage)])
        check(result.returncode != 0 and "Traceback" not in result.stderr,
              f"legacy {label} usage is a typed refusal")
        check(path.read_bytes() == original, f"legacy {label} usage leaves the run unchanged")

    assignment_file, digest = make_assignment(tmp_path)
    for label, payload in bad_payloads.items():
        path = tmp_path / "runs" / f"bound-{label}.json"
        started = run(start_args(path, f"run-bound-{label}", "task-1") +
                      ["--assignment-digest", digest, "--assignment-file", str(assignment_file)])
        check(started.returncode == 0, f"assignment-bound usage fixture {label} starts")
        original = path.read_bytes()
        usage = tmp_path / f"usage-bound-{label}.json"
        usage.write_text(payload, encoding="utf-8")
        result = run(["finish", str(path), "succeeded", "done", "0", "--usage-file", str(usage)])
        check(result.returncode != 0 and "Traceback" not in result.stderr,
              f"assignment-bound {label} usage is a typed refusal")
        check(path.read_bytes() == original,
              f"assignment-bound {label} usage leaves the run unchanged")


def _make_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise AssertionError(f"could not create Windows junction: {result.stderr or result.stdout}")
    else:
        link.symlink_to(target, target_is_directory=True)


def test_windows_atomic_publish_rejects_parent_swap(tmp_path: Path) -> None:
    if os.name != "nt":
        print("skip - Windows atomic publish parent-swap control is platform-scoped")
        return

    worker = load_worker_module()
    parent = tmp_path / "evidence-parent"
    outside = tmp_path / "outside-evidence"
    parent.mkdir()
    outside.mkdir()
    target = parent / "evidence.json"
    saved = tmp_path / "evidence-parent-renamed"
    swap_attempted = False
    swap_refused = False
    swapped = False

    def race(stage: str) -> None:
        nonlocal swap_attempted, swap_refused, swapped
        if stage != "pre-replace" or swap_attempted:
            return
        swap_attempted = True
        try:
            parent.rename(saved)
            _make_directory_link(parent, outside)
            swapped = True
        except OSError:
            swap_refused = True

    worker._atomic_publish_hook = race
    try:
        try:
            worker.atomic(target, {"payload": "inside"})
            published = True
        except worker.WorkerEvidenceError:
            published = False
    finally:
        worker._atomic_publish_hook = None
        if swapped and parent.exists():
            os.rmdir(parent)
        if saved.exists():
            saved.rename(parent)

    check(swap_attempted, "Windows evidence publication reaches the parent-swap seam")
    check(swap_refused or not published, "Windows parent swap is denied or publication is refused")
    check(not (outside / "evidence.json").exists(),
          "a Windows evidence parent swap creates no outside-root evidence")
    check((parent / "evidence.json").exists() if published else True,
          "a safely denied Windows parent swap leaves only an in-root publication")


def test_remote_evidence(tmp_path: Path) -> None:
    # Strict remote-event persistence is covered by the Phase 5 boundary battery.  This
    # Phase 3 compatibility test intentionally keeps its record legacy/assignment-free.
    path = tmp_path / "run.json"
    started = run(start_args(path, "run-remote-legacy", "task-1"))
    check(started.returncode == 0, "legacy remote fixture starts without an assignment")
    result = run(["remote-evidence", str(path), "--session-id", "sess-legacy",
                  "--event-type", "session", "--branch", "cursor/task-1",
                  "--commit", "b" * 40])
    check(result.returncode != 0 and "remote evidence is invalid" in result.stderr,
          "assignment-less remote evidence is refused without claiming immutable provenance")
    check(not (tmp_path / "remote-events.jsonl").exists(),
          "assignment-less remote evidence writes no event")
    return

    path = tmp_path / "run.json"
    assignment_file, digest = make_assignment(tmp_path)
    persisted = tmp_path / "assignments" / "task-1" / f"{digest}.json"
    bound_args = start_args(path, "run-remote", "task-1") + ["--assignment-digest", digest,
        "--assignment-file", str(assignment_file)]
    run(bound_args)
    first = run(["remote-evidence", str(path), "--session-id", "sess-1", "--event-type", "session",
                "--branch", "cursor/task-1",
                "--commit", "b" * 40, "--artifact", "apps/api/a.txt=" + "c" * 64,
                "--artifact", "apps/api/b.txt=" + "d" * 64])
    check(first.returncode == 0, f"remote-evidence exits 0 (got {first.returncode}, stderr={first.stderr!r})")
    events = [json.loads(line) for line in (tmp_path / "remote-events.jsonl").read_text().splitlines()]
    remote = events[0]
    check(remote.get("session_id") == "sess-1", "remote-evidence records immutable session_id")
    check(remote.get("branch") == "cursor/task-1", "remote-evidence records authorized branch")
    check(remote.get("commit") == "b" * 40, "remote-evidence records exact commit")
    check([a["path"] for a in remote.get("artifacts", [])] == ["apps/api/a.txt", "apps/api/b.txt"],
          "remote-evidence records typed artifact paths")
    check(all(len(a["sha256"]) == 64 for a in remote.get("artifacts", [])),
          "remote-evidence records artifact sha256 values")

    second = run(["remote-evidence", str(path), "--session-id", "sess-1", "--event-type", "cancel",
                 "--cancelled-at", "2026-08-28T01:00:00Z", "--cancellation-reason", "quota-exhausted"])
    check(second.returncode == 0, "a cancellation transition appends successfully")
    check(len((tmp_path / "remote-events.jsonl").read_text().splitlines()) == 2,
          "remote evidence is append-only")
    duplicate = run(["remote-evidence", str(path), "--session-id", "sess-1", "--event-type", "terminal",
                      "--terminal-status", "cancelled"])
    check(duplicate.returncode == 0, "terminal cancellation transition appends successfully")
    duplicate2 = run(["remote-evidence", str(path), "--session-id", "sess-1", "--event-type", "terminal",
                       "--terminal-status", "cancelled"])
    check(duplicate2.returncode == 0 and len((tmp_path / "remote-events.jsonl").read_text().splitlines()) == 3,
          "identical terminal replay is idempotent without appending a duplicate")


def test_remote_state_and_concurrency(tmp_path: Path) -> None:
    # Assignment-bound concurrency is exercised by the boundary battery; preserve this
    # Phase 3 test as a legacy refusal check instead of claiming a strict persisted binding.
    path = tmp_path / "run.json"
    started = run(start_args(path, "run-remote-race-legacy", "task-1"))
    check(started.returncode == 0, "legacy remote-race fixture starts without an assignment")
    result = run(["remote-evidence", str(path), "--session-id", "sess-race",
                  "--event-type", "session", "--branch", "cursor/race",
                  "--commit", "b" * 40])
    check(result.returncode != 0, "assignment-less concurrent remote evidence is refused")
    return

    path = tmp_path / "run.json"
    _assignment_file, digest = make_assignment(tmp_path)
    run(start_args(path, "run-remote-race", "task-1") +
        ["--assignment-digest", digest, "--assignment-file", str(tmp_path / "assignments" / "task-1" / f"{digest}.json")])
    common = ["remote-evidence", str(path), "--session-id", "sess-race", "--event-type", "session",
              "--branch", "cursor/race", "--commit", "b" * 40]
    first = subprocess.Popen([sys.executable, str(WORKER_EVIDENCE), *common],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = subprocess.Popen([sys.executable, str(WORKER_EVIDENCE), *common],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    first.communicate(timeout=15); second.communicate(timeout=15)
    check(sorted(item.returncode for item in (first, second)) == [0, 0],
          "concurrent identical remote evidence is accepted or replayed idempotently")
    events = (tmp_path / "remote-events.jsonl").read_text(encoding="utf-8").splitlines()
    check(len(events) == 1, "concurrent identical remote evidence appends exactly one event")

    conflict = ["remote-evidence", str(path), "--session-id", "sess-race",
                "--branch", "cursor/other", "--commit", "b" * 40]
    result = run(conflict)
    check(result.returncode != 0, "a differing remote branch is a typed refusal")
    check(len((tmp_path / "remote-events.jsonl").read_text(encoding="utf-8").splitlines()) == 1,
          "a conflicting remote branch cannot append a second identity")

    cancel = run(["remote-evidence", str(path), "--session-id", "sess-race", "--event-type", "cancel",
                  "--cancelled-at", "2026-08-28T01:00:00Z", "--cancellation-reason", "test"])
    check(cancel.returncode == 0, "remote cancellation transition remains appendable")
    cancelled_result = run(["remote-evidence", str(path), "--session-id", "sess-race",
                            "--event-type", "result", "--branch", "cursor/race", "--commit", "b" * 40])
    check(cancelled_result.returncode != 0, "a result after cancellation terminal is refused")


def test_terminal_result_is_immutable_and_concurrent(tmp_path: Path) -> None:
    # Terminal replay/concurrency with immutable assignment is covered by the boundary
    # battery; legacy records must not be promoted into remote terminal evidence here.
    path = tmp_path / "run.json"
    started = run(start_args(path, "run-result-legacy", "task-1"))
    check(started.returncode == 0, "legacy terminal fixture starts without an assignment")
    result = run(["remote-evidence", str(path), "--session-id", "sess-result",
                  "--event-type", "result", "--branch", "cursor/result",
                  "--commit", "b" * 40])
    check(result.returncode != 0, "assignment-less terminal remote evidence is refused")
    return

    path = tmp_path / "run.json"
    _assignment_file, digest = make_assignment(tmp_path)
    run(start_args(path, "run-result-terminal", "task-1") +
        ["--assignment-digest", digest, "--assignment-file",
         str(tmp_path / "assignments" / "task-1" / f"{digest}.json")])
    first = ["remote-evidence", str(path), "--session-id", "sess-result",
             "--branch", "cursor/result", "--commit", "b" * 40,
             "--artifact", "apps/api/a.txt=" + "c" * 64,
             "--artifact", "apps/api/c.txt=" + "e" * 64]
    replay = run(first)
    check(replay.returncode == 0, "the first terminal result is accepted")
    same = run(first[:-4] + ["--artifact", first[-1], "--artifact", first[-3]])
    check(same.returncode == 0 and json.loads(same.stdout).get("status") == "duplicate",
          "a byte/semantic-identical (including reordered artifacts) terminal result replay is harmless")
    conflict = run([*first[:-1], "--artifact", "apps/api/b.txt=" + "d" * 64])
    check(conflict.returncode != 0, "a conflicting sequential terminal result is refused")
    check(len((tmp_path / "remote-events.jsonl").read_text(encoding="utf-8").splitlines()) == 1,
          "a conflicting terminal result cannot append a second event")

    path2 = tmp_path / "run-concurrent.json"
    run(start_args(path2, "run-result-concurrent", "task-1") +
        ["--assignment-digest", digest, "--assignment-file",
         str(tmp_path / "assignments" / "task-1" / f"{digest}.json")])
    left = ["remote-evidence", str(path2), "--session-id", "sess-concurrent",
            "--branch", "cursor/result", "--commit", "b" * 40,
            "--artifact", "apps/api/a.txt=" + "c" * 64]
    right = ["remote-evidence", str(path2), "--session-id", "sess-concurrent",
             "--branch", "cursor/result", "--commit", "b" * 40,
             "--artifact", "apps/api/b.txt=" + "d" * 64]
    first_process = subprocess.Popen([sys.executable, str(WORKER_EVIDENCE), *left],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second_process = subprocess.Popen([sys.executable, str(WORKER_EVIDENCE), *right],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    first_process.communicate(timeout=15); second_process.communicate(timeout=15)
    check(sorted(item.returncode for item in (first_process, second_process)) == [0, 1],
          "concurrent conflicting terminal results have one winner and one refusal")
    concurrent_events = [json.loads(line) for line in
                         (tmp_path / "remote-events.jsonl").read_text(encoding="utf-8").splitlines()
                         if json.loads(line).get("run_id") == "run-result-concurrent"]
    check(len(concurrent_events) == 1,
          "concurrent conflicting terminal results append exactly one event")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        test_legacy_start_unchanged(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_start_binds_assignment(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_start_refuses_obsolete_assignment_shape(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_start_refuses_one_sided_flags(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_start_refuses_digest_mismatch(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_start_refuses_task_id_mismatch(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_finish_rejects_malformed_and_off_schema_inputs(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_usage_validation_applies_to_legacy_and_bound_runs(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_windows_atomic_publish_rejects_parent_swap(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_remote_evidence(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_remote_state_and_concurrency(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_terminal_result_is_immutable_and_concurrent(Path(tmp))

    if failures:
        print(f"worker-evidence-assignment.test: {len(failures)} FAILED")
        return 1
    print("worker-evidence-assignment.test: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
