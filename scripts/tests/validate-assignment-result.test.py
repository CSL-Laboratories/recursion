#!/usr/bin/env python3
"""Hermetic controls for scripts/validate-assignment-result.py (Phase 3, D-016).

Exercises every typed refusal the module promises: mismatched (2), late (3), duplicate (4),
unauthorized (5), source-unavailable (6), plus the accepted (0) path. Everything runs under a
disposable tempdir; nothing touches the live repo's state/.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]
SCRIPTS = SOURCE / "scripts"
VALIDATOR = SCRIPTS / "validate-assignment-result.py"

sys.path.insert(0, str(SCRIPTS))
import assignment as amod  # noqa: E402

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if condition:
        print(f"ok - {label}")
    else:
        print(f"FAIL - {label}")
        failures.append(label)


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(VALIDATOR), *args], capture_output=True, text=True)


def epoch_now() -> int:
    return int(time.time())


def iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def make_fixture(tmp_path: Path):
    root = tmp_path / "assignments"
    value = amod.resolve_assignment(
        task_id="task-1", project_id="proj-1", role="implementer",
        source_repository="/opt/example-project", source_branch="loop/work/task-1",
        source_sha="a" * 40, engine="claude", provider="anthropic-claude-code",
        host="gs-recursion", sandbox_profile="strict-linux", network_profile="provider-only",
        allowed_paths=["apps/api"], publication_policy="verified-integration-only",
        created_at="2026-08-28T00:00:00Z",
    )
    amod.persist_assignment(root, value)
    digest = value["assignment_digest"]

    evidence_path = tmp_path / "run-evidence.json"
    deadline_epoch = epoch_now() + 3600
    evidence = {
        "schema_version": 1, "id": "run-1", "task_id": "task-1", "role": "implementer",
        "worker": "implementer:sonnet-routine", "worktree": "/tmp/wt",
        "pid": 100, "process_group": 100,
        "lease_expires": iso(deadline_epoch), "deadline": iso(deadline_epoch),
        "usage": {}, "result": {}, "status": "running",
        "assignment_digest": digest, "deadline_epoch": deadline_epoch,
        "base_commit": "a" * 40, "source_evidence": value["source_evidence"],
        "commit_oid": "b" * 40,
    }
    evidence["assignment"] = amod.assignment_projection(value)
    evidence.update(amod.assignment_projection(value))
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    return root, digest, evidence_path, deadline_epoch


def base_args(root: Path, digest: str, evidence_path: Path, run_id: str = "run-1") -> list[str]:
    if run_id != "run-1":
        value = json.loads(evidence_path.read_text(encoding="utf-8"))
        value["id"] = run_id
        evidence_path.write_text(json.dumps(value), encoding="utf-8")
    return ["--assignment-root", str(root), "--task-id", "task-1",
            "--assignment-digest", digest, "--run-id", run_id, "--run-evidence", str(evidence_path)]


def test_accepted_and_duplicate(tmp_path: Path) -> None:
    root, digest, evidence_path, _ = make_fixture(tmp_path)
    args = base_args(root, digest, evidence_path) + ["--changed-path", "apps/api/main.py",
        "--result-branch", "loop/work/task-1", "--result-commit", "b" * 40, "--record"]
    result = run(args)
    check(result.returncode == 0, f"a well-formed, in-scope, on-time result is accepted (got {result.returncode}, stderr={result.stderr!r})")
    check(json.loads(result.stdout).get("status") == "accepted", "accepted result reports status=accepted")

    dup = run(args)
    check(dup.returncode == 4, f"the same run_id accepted twice is refused as duplicate (got {dup.returncode})")


def test_unknown_digest_source_unavailable(tmp_path: Path) -> None:
    root, digest, evidence_path, _ = make_fixture(tmp_path)
    wrong_digest = ("0" * 63) + "1"
    args = base_args(root, wrong_digest, evidence_path, run_id="run-mismatch")
    result = run(args)
    check(result.returncode == 6, f"an assignment digest with no persisted record is source-unavailable (got {result.returncode})")


def test_evidence_names_different_digest(tmp_path: Path) -> None:
    root, digest, evidence_path, deadline = make_fixture(tmp_path)
    other_evidence = tmp_path / "other-evidence.json"
    other_evidence.write_text(json.dumps({
        "schema_version": 1, "id": "run-2", "task_id": "task-1",
        "assignment_digest": "f" * 64, "deadline_epoch": deadline,
    }), encoding="utf-8")
    args = base_args(root, digest, other_evidence, run_id="run-2")
    result = run(args)
    check(result.returncode == 2, f"run evidence bound to a different digest is refused as mismatched (got {result.returncode})")


def test_evidence_projection_cannot_forge_provider(tmp_path: Path) -> None:
    root, digest, evidence_path, _ = make_fixture(tmp_path)
    value = json.loads(evidence_path.read_text(encoding="utf-8"))
    value["provider"] = "evil-provider"
    value["assignment"]["provider"] = "evil-provider"
    evidence_path.write_text(json.dumps(value), encoding="utf-8")
    result = run(base_args(root, digest, evidence_path, run_id="run-evil-provider") + ["--record"])
    check(result.returncode == 2, "correct digest/source cannot authorize a forged provider projection")


def test_projection_does_not_launder_numeric_types(tmp_path: Path) -> None:
    root, digest, evidence_path, _ = make_fixture(tmp_path)
    value = json.loads(evidence_path.read_text(encoding="utf-8"))
    value["source_evidence"]["verified"] = 1
    value["assignment"]["source_evidence"]["verified"] = 1
    evidence_path.write_text(json.dumps(value), encoding="utf-8")
    result = run(base_args(root, digest, evidence_path, run_id="run-type-launder"))
    check(result.returncode == 2,
          "projection comparison distinguishes an integer from the persisted boolean")


def test_late(tmp_path: Path) -> None:
    root, digest, evidence_path, deadline = make_fixture(tmp_path)
    args = base_args(root, digest, evidence_path, run_id="run-late") + \
        ["--received-at", iso(deadline + 60)]
    result = run(args)
    check(result.returncode == 3, f"a result received after the run deadline is refused as late (got {result.returncode})")


def test_unauthorized_branch(tmp_path: Path) -> None:
    root, digest, evidence_path, _ = make_fixture(tmp_path)
    args = base_args(root, digest, evidence_path, run_id="run-branch") + ["--result-branch", "main"]
    result = run(args)
    check(result.returncode == 5, f"a returned branch equal to a protected default branch is refused (got {result.returncode})")


def test_unauthorized_path(tmp_path: Path) -> None:
    root, digest, evidence_path, _ = make_fixture(tmp_path)
    args = base_args(root, digest, evidence_path, run_id="run-path") + ["--changed-path", "scripts/evil.sh"]
    result = run(args)
    check(result.returncode == 5, f"a returned path outside allowed_paths is refused (got {result.returncode})")


def test_unauthorized_repository(tmp_path: Path) -> None:
    root, digest, evidence_path, _ = make_fixture(tmp_path)
    args = base_args(root, digest, evidence_path, run_id="run-repo") + \
        ["--result-repository", "/opt/some-other-repo"]
    result = run(args)
    check(result.returncode == 5, f"a returned repository different from the assigned source_repository is refused (got {result.returncode})")


def test_tampered_assignment_file(tmp_path: Path) -> None:
    root, digest, evidence_path, _ = make_fixture(tmp_path)
    persisted = amod.assignment_path(root, "task-1", digest)
    value = json.loads(persisted.read_text(encoding="utf-8"))
    value["engine"] = "hermes"  # tamper without touching the filename/digest field
    persisted.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args = base_args(root, digest, evidence_path, run_id="run-tamper")
    result = run(args)
    check(result.returncode == 2, f"a tampered on-disk assignment (content no longer hashes to its own digest) is refused (got {result.returncode})")


def test_not_recorded_without_flag(tmp_path: Path) -> None:
    root, digest, evidence_path, _ = make_fixture(tmp_path)
    args = base_args(root, digest, evidence_path, run_id="run-no-record") + ["--changed-path", "apps/api/x.py"]
    first = run(args)
    check(first.returncode == 0, "accepted without --record still exits 0")
    second = run(args)
    check(second.returncode == 0, "without --record, a repeat is NOT durably remembered as a duplicate")


def test_concurrent_record_has_one_winner(tmp_path: Path) -> None:
    root, digest, evidence_path, _ = make_fixture(tmp_path)
    args = base_args(root, digest, evidence_path, run_id="run-race") + [
        "--result-branch", "cursor/task-1", "--result-commit", "b" * 40, "--record"]
    first = subprocess.Popen([sys.executable, str(VALIDATOR), *args], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    second = subprocess.Popen([sys.executable, str(VALIDATOR), *args], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    results = [first, second]
    [item.communicate(timeout=15) for item in results]
    codes = sorted(item.returncode for item in results)
    check(codes == [0, 4], f"concurrent identical result has one winner and one duplicate refusal (got {codes})")
    ledger = root / "results.jsonl"
    check(ledger.exists() and len(ledger.read_text(encoding="utf-8").splitlines()) == 1,
          "concurrent identical result creates exactly one durable ledger row")

    conflict_winner_args = base_args(root, digest, evidence_path, run_id="run-conflict") + [
        "--result-branch", "cursor/task-1", "--result-commit", "b" * 40, "--record"]
    conflict_args = base_args(root, digest, evidence_path, run_id="run-conflict") + [
        "--result-branch", "cursor/task-1", "--result-commit", "c" * 40, "--record"]
    first = subprocess.Popen([sys.executable, str(VALIDATOR), *conflict_winner_args],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = subprocess.Popen([sys.executable, str(VALIDATOR), *conflict_args],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    first.communicate(timeout=15); second.communicate(timeout=15)
    codes = sorted(item.returncode for item in (first, second))
    check(codes == [0, 4] or codes == [0, 2],
          f"concurrent conflicting result has one winner and one typed refusal (got {codes})")
    check(len(ledger.read_text(encoding="utf-8").splitlines()) == 2,
          "concurrent conflicting result cannot lose or duplicate the winning row")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        test_accepted_and_duplicate(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_unknown_digest_source_unavailable(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_evidence_names_different_digest(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_evidence_projection_cannot_forge_provider(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_projection_does_not_launder_numeric_types(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_late(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_unauthorized_branch(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_unauthorized_path(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_unauthorized_repository(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_tampered_assignment_file(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_not_recorded_without_flag(Path(tmp))
    with tempfile.TemporaryDirectory() as tmp:
        test_concurrent_record_has_one_winner(Path(tmp))

    if failures:
        print(f"validate-assignment-result.test: {len(failures)} FAILED")
        return 1
    print("validate-assignment-result.test: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
