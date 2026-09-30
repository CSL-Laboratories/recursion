#!/usr/bin/env python3
"""Hermetic Phase 2 repository-contract tests."""

from __future__ import annotations

import json
import hashlib
import hmac
import inspect
import os
import subprocess
import tempfile
import unittest
import uuid
from unittest import mock
from pathlib import Path
import jsonschema

import sys
sys.path.insert(0, str(Path(__file__).parents[1]))
import repository_contract as rc


SCRATCH_ROOT = Path(os.environ.get(
    "RECURSION_PHASE2_SCRATCH",
    tempfile.gettempdir(),
))


class ExplicitScratch:
    """Fixture-local scratch allocation; do not delegate cleanup to TemporaryDirectory."""

    def __init__(self, prefix: str) -> None:
        SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
        self.path = SCRATCH_ROOT / f"{prefix}-{uuid.uuid4().hex}"
        self.path.mkdir()
        self.name = str(self.path)

    def cleanup(self) -> None:
        # Cleanup is limited to this test-owned scratch directory.  Surface cleanup failures so a
        # reparse point cannot silently leak outside the fixture's ownership boundary.
        rc._remove_tree_without_following(self.path)


def remove_reparse_point(path: Path) -> None:
    """Remove a fixture-created junction/symlink without following its target."""
    if not (path.exists() or path.is_symlink()):
        return
    if os.name == "nt":
        path.rmdir()
    else:
        path.unlink()


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True)
    return result.stdout.strip()


def git_raw(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True)
    return result.stdout


def git_raw_bytes(repo: Path, *args: str) -> bytes:
    env = {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "SystemRoot", "WINDIR"}
    }
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    result = subprocess.run(["git", "--no-replace-objects", "-c", f"safe.directory={repo}",
                             "-C", str(repo), *args], env=env,
                            capture_output=True, check=True)
    return result.stdout


class RepositoryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = ExplicitScratch("contract")
        self.key_path = Path(self.temp.name) / "controller.key"
        self.key_path.write_text("hermetic-controller-key\n", encoding="utf-8")
        os.chmod(self.key_path, 0o600)
        if os.name == "nt":
            account = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ['USERNAME']}"
            acl = subprocess.run(
                ["icacls", str(self.key_path), "/inheritance:r", "/grant:r", f"{account}:F"],
                capture_output=True, check=False,
            )
            if acl.returncode:
                self.temp.cleanup()
                raise unittest.SkipTest("fixture key ACL could not be made owner-only")
        self.old_controller_key_path = rc.CONTROLLER_KEY_PATH
        rc.CONTROLLER_KEY_PATH = self.key_path
        self.old_receipt_key = os.environ.get("RECURSION_VERIFIER_RECEIPT_KEY")
        self.old_receipt_key_file = os.environ.get("RECURSION_VERIFIER_RECEIPT_KEY_FILE")
        os.environ["RECURSION_VERIFIER_RECEIPT_KEY"] = "hostile-caller-key"
        os.environ["RECURSION_VERIFIER_RECEIPT_KEY_FILE"] = str(Path(self.temp.name) / "hostile.key")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "contract-test")
        git(self.repo, "config", "user.email", "contract-test@example.invalid")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")
        self.source = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "branch", "recursion/integration", self.source)
        self.contract = {
            "schema_version": 1,
            "id": "test-project",
            "repo": str(self.repo),
            "default_branch": "main",
            "integration_branch": "recursion/integration",
            "task_branch_pattern": "recursion/task/{task_id}",
            "external_branch_patterns": ["cursor/*", "recursion/extern/*"],
            "publication_policy": "verified-integration-only",
            "source_policy": "exact-sha",
            "allowed_paths": ["src"],
            "gate_commands": ["git diff --check"],
            "eligible_hosts": ["gs-recursion"],
            "eligible_pools": ["local"],
        }

    def tearDown(self) -> None:
        rc.CONTROLLER_KEY_PATH = self.old_controller_key_path
        if self.old_receipt_key is None:
            os.environ.pop("RECURSION_VERIFIER_RECEIPT_KEY", None)
        else:
            os.environ["RECURSION_VERIFIER_RECEIPT_KEY"] = self.old_receipt_key
        if self.old_receipt_key_file is None:
            os.environ.pop("RECURSION_VERIFIER_RECEIPT_KEY_FILE", None)
        else:
            os.environ["RECURSION_VERIFIER_RECEIPT_KEY_FILE"] = self.old_receipt_key_file
        self.temp.cleanup()

    def candidate(self) -> dict[str, object]:
        git(self.repo, "checkout", "-qB", "cursor/task-one")
        (self.repo / "src" / "feature.txt").write_text("candidate\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "candidate")
        commit = git(self.repo, "rev-parse", "HEAD")
        candidate = {
            "project_id": "test-project", "task_id": "task-one", "source_branch": "main",
            "source_sha": self.source, "external_branch": "cursor/task-one",
            "candidate_commit": commit, "idempotency_key": "dispatch-1",
        }
        return candidate

    def plumbed_candidate(self, entry: str, name: str, base_candidate: dict[str, object] | None = None,
                          root_entry: bool = False, branch_name: str | None = None) -> dict[str, object]:
        """Build a candidate tree with an arbitrary mode, independent of checkout symlink support."""
        candidate = base_candidate or self.candidate()
        source_tree = git(self.repo, "rev-parse", f"{candidate['candidate_commit']}:src")
        src_entries = [line.rstrip("\r") for line in git_raw(self.repo, "ls-tree", source_tree).splitlines()]
        if not root_entry:
            src_entries.append(f"{entry}\t{name}")
        made_src = subprocess.run(["git", "-C", str(self.repo), "mktree"],
                                  input=("\n".join(sorted(src_entries)) + "\n").encode(),
                                  capture_output=True, check=True).stdout.decode().strip()
        root_tree = git(self.repo, "rev-parse", f"{candidate['candidate_commit']}^{{tree}}")
        root_entries = [line.rstrip("\r") for line in git_raw(self.repo, "ls-tree", root_tree).splitlines()
                        if not line.rstrip("\r").endswith("\tsrc")]
        if root_entry:
            root_entries.append(f"{entry}\t{name}")
        else:
            root_entries.append(f"040000 tree {made_src}\tsrc")
        made_root = subprocess.run(["git", "-C", str(self.repo), "mktree"],
                                   input=("\n".join(sorted(root_entries)) + "\n").encode(),
                                   capture_output=True, check=True).stdout.decode().strip()
        made_commit = subprocess.run(["git", "-C", str(self.repo), "commit-tree", made_root, "-p", self.source],
                                     input=f"{name}\n", text=True, capture_output=True, check=True).stdout.strip()
        branch = f"cursor/{branch_name or name}"
        subprocess.run(["git", "-C", str(self.repo), "update-ref", f"refs/heads/{branch}", made_commit], check=True)
        return {**candidate, "external_branch": branch, "candidate_commit": made_commit}

    def receipt(self, candidate: dict[str, object]) -> dict[str, object]:
        diff = git_raw_bytes(self.repo, "diff", "--binary", str(candidate["source_sha"]), str(candidate["candidate_commit"]))
        paths = [item for item in git(self.repo, "diff", "--name-only", str(candidate["source_sha"]), str(candidate["candidate_commit"])).splitlines() if item]
        body = {
            "schema": "recursion-controller-verifier-receipt/v1", "project_id": "test-project",
            "task_id": candidate["task_id"], "source_sha": candidate["source_sha"],
            "candidate_commit": candidate["candidate_commit"],
            "diff_sha256": hashlib.sha256(diff).hexdigest(),
            "scope_digest": rc._json_digest(candidate.get("allowed_paths", self.contract["allowed_paths"])),
            "gate_digest": rc._json_digest(self.contract["gate_commands"]),
            "verdict": "pass", "verifier": "controller-verifier", "provenance": "recursion-controller",
        }
        receipt_digest = rc._json_digest(body)
        signature = hmac.new(b"hermetic-controller-key", receipt_digest.encode("ascii"), hashlib.sha256).hexdigest()
        return {**body, "receipt_digest": receipt_digest, "signature": signature}

    def test_descriptor_and_exact_source_admission(self) -> None:
        rc.validate_project_descriptor(self.contract)
        task = rc.admit_task(self.contract, {
            "id": "task-one", "source_branch": "main", "source_sha": self.source,
            "allowed_paths": ["src"],
        }, self.repo)
        self.assertEqual(task["source_sha"], self.source)
        with self.assertRaisesRegex(rc.ContractError, "exact source_sha"):
            rc.admit_task(self.contract, {"id": "task-two", "source_branch": "main"}, self.repo)
        with self.assertRaisesRegex(rc.ContractError, "apps/example-ops"):
            rc.admit_task(self.contract, {
                "id": "task-three", "source_branch": "main", "source_sha": self.source,
                "allowed_paths": ["apps/example-ops"],
            }, self.repo)
        with self.assertRaisesRegex(rc.ContractError, "widens"):
            rc.admit_task(self.contract, {
                "id": "task-four", "source_branch": "main", "source_sha": self.source,
                "allowed_paths": ["."],
            }, self.repo)
        wrong = Path(self.temp.name) / "wrong-clone"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(wrong)], check=True)
        with self.assertRaisesRegex(rc.ContractError, "does not match"):
            rc.admit_task(self.contract, {
                "id": "task-five", "source_branch": "main", "source_sha": self.source,
                "allowed_paths": ["src"],
            }, wrong)

    def test_task_gate_and_workdir_fields_are_bound_to_the_descriptor(self) -> None:
        base = {"id": "task-one", "source_branch": "main", "source_sha": self.source,
                "allowed_paths": ["src"]}
        for field, value in (("acceptance", "true"), ("gate_command", "true"),
                             ("gate_commands", ["git status"]), ("workdir", "../../outside"),
                             ("workdir", "C:/outside"), ("workdir", r"\\server\share\outside")):
            with self.subTest(field=field, value=value), self.assertRaises(rc.ContractError):
                rc.admit_task(self.contract, {**base, field: value}, self.repo)

        accepted = rc.admit_task(self.contract, {**base, "acceptance": "git diff --check",
                                                 "gate_command": "git diff --check", "workdir": "."},
                                 self.repo)
        self.assertEqual(accepted["workdir"], ".")

    def test_git_timeout_is_a_typed_fail_closed_refusal(self) -> None:
        timeout = subprocess.TimeoutExpired(["git"], 30)
        with mock.patch.object(rc.subprocess, "run", side_effect=timeout):
            with self.assertRaisesRegex(rc.ContractError, "timed out") as raised:
                rc._git(self.repo, "status")
        self.assertEqual(raised.exception.code, "verification-unavailable")

    def test_git_timeout_at_verify_candidate_caller_boundary_is_typed(self) -> None:
        timeout = subprocess.TimeoutExpired(["git"], 30)
        candidate = {"project_id": "test-project", "task_id": "task-one", "source_branch": "main",
                     "source_sha": "a" * 40, "external_branch": "cursor/task-one",
                     "candidate_commit": "b" * 40}
        with mock.patch.object(rc, "_git", side_effect=timeout):
            with self.assertRaisesRegex(rc.ContractError, "timed out") as raised:
                rc.verify_candidate(self.contract, candidate, self.repo)
        self.assertEqual(raised.exception.code, "verification-unavailable")

    def test_windows_acl_uses_actual_owner_and_rejects_other_user_write(self) -> None:
        with mock.patch.dict(os.environ, {"USERNAME": "attacker"}, clear=False):
            with self.assertRaisesRegex(OSError, "non-owner"):
                rc._validate_windows_acl_records(
                    "S-1-owner", "S-1-owner", [("S-1-attacker", 0, 0x001F01FF, 0)], True)
        with self.assertRaisesRegex(OSError, "owner"):
            rc._validate_windows_acl_records(
                "S-1-attacker", "S-1-controller", [("S-1-attacker", 0, 0x001F01FF, 0)], True)

    def test_windows_acl_wrapper_accepts_legitimate_and_types_native_failures(self) -> None:
        with mock.patch.object(rc, "_check_windows_acl_impl", return_value=None):
            self.assertIsNone(rc._check_windows_acl(Path("controller.key"), True))
        foreign = lambda _path, _required: rc._validate_windows_acl_records(
            "S-1-owner", "S-1-owner", [("S-1-foreign", 0, 0x001F01FF, 0)], True)
        with mock.patch.object(rc, "_check_windows_acl_impl", side_effect=foreign):
            with self.assertRaises(rc.ContractError):
                rc._check_windows_acl(Path("controller.key"), True)
        with mock.patch.object(rc, "_check_windows_acl_impl",
                              side_effect=OSError("native ACL failure")):
            with self.assertRaises(rc.ContractError):
                rc._check_windows_acl(Path("controller.key"), True)
        self.assertNotIn("c_void_p.from_address(ace.value + sid_offset)",
                          inspect.getsource(rc._check_windows_acl_impl))

    def test_declared_gate_profile_must_match_executed_gate_and_evidence(self) -> None:
        divergent = dict(
            self.contract,
            required_gate_profile={"acceptance": "test -s src/result.txt", "gate_class": "light",
                                   "gate_files": [], "controller_gate_files": []},
        )
        with self.assertRaisesRegex(rc.ContractError, "diverges"):
            rc.validate_project_descriptor(divergent)

        profile_contract = dict(
            self.contract,
            gate_commands=["git diff --check"],
            required_gate_profile={"acceptance": "git diff --check", "gate_class": "light",
                                   "gate_files": [], "controller_gate_files": []},
        )
        bad_evidence = {"acceptance_sha256": "0" * 64, "gate_files": {},
                        "controller_gate_files": {}}
        proc = subprocess.CompletedProcess(["validate-gate"], 0,
                                           json.dumps(bad_evidence), "")
        with mock.patch.object(rc.subprocess, "run", return_value=proc):
            with self.assertRaisesRegex(rc.ContractError, "evidence"):
                rc._observed_gate_contract(profile_contract, self.repo)

    def test_gate_profile_compares_resolved_paths_but_rejects_a_divergent_gate(self) -> None:
        controller_root = Path(__file__).parents[2].resolve()
        script = "scripts/sample-shade-tops-gate.py"
        profile_path = "scripts/gates/sample-shade-tops-v1.json"
        relative = f"python3 {script} --repo . --profile {profile_path}"
        absolute = f"{controller_root / script} --repo . --profile {controller_root / profile_path}"
        profile = {"acceptance": absolute, "gate_class": "heavy", "gate_files": [],
                   "controller_gate_files": [script, profile_path]}
        contract = dict(self.contract, gate_commands=[relative], required_gate_profile=profile)
        rc.validate_project_descriptor(contract)
        divergent = dict(contract, required_gate_profile=dict(profile, acceptance=absolute + " --other-profile"))
        with self.assertRaisesRegex(rc.ContractError, "diverges"):
            rc.validate_project_descriptor(divergent)

    def test_reparse_ledger_is_refused_before_any_record_can_escape(self) -> None:
        outside = Path(self.temp.name) / "ledger-outside"
        outside.mkdir()
        ledger = Path(self.temp.name) / "ledger"
        try:
            if os.name == "nt":
                linked = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(ledger), str(outside)],
                                        capture_output=True, check=False)
                if linked.returncode:
                    self.skipTest("junction creation is unavailable on this host")
            else:
                ledger.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(rc.ContractError, "ledger"):
                with rc._pinned_ledger(ledger):
                    self.fail("reparse ledger was pinned")
        finally:
            remove_reparse_point(ledger)
        self.assertFalse(ledger.exists() or ledger.is_symlink())
        self.assertEqual(list(outside.iterdir()), [])

    def test_windows_ledger_pins_parent_components_with_delete_sharing_denied(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows-only parent handle proof")
        source = inspect.getsource(rc._pinned_windows_parents)
        self.assertIn("share_delete=False", source)
        self.assertIn("_same_file(info, pinned)", source)

    def test_windows_ledger_parent_identity_is_revalidated_before_use(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows-only parent handle proof")
        target = Path(__file__).resolve().parents[2] / "scripts" / "tests" / "uncreated-ledger"
        opened: list[tuple[Path, bool]] = []
        pinned: list[os.stat_result] = []

        def open_parent(path: Path, *, share_delete: bool) -> int:
            opened.append((path, share_delete))
            pinned.append(os.lstat(path))
            return 1000 + len(pinned) - 1

        def fstat_parent(fd: int) -> os.stat_result:
            return pinned[fd - 1000]

        with mock.patch.object(rc.os, "name", "nt"), \
                mock.patch.object(rc, "_open_windows_directory", side_effect=open_parent), \
                mock.patch.object(rc.os, "fstat", side_effect=fstat_parent), \
                mock.patch.object(rc.os, "close") as close:
            with rc._pinned_windows_parents(target):
                pass
        self.assertGreaterEqual(len(opened), 2)
        self.assertTrue(all(share_delete is False for _, share_delete in opened))
        self.assertEqual(close.call_count, len(opened))

        with mock.patch.object(rc.os, "name", "nt"), \
                mock.patch.object(rc, "_open_windows_directory", side_effect=open_parent), \
                mock.patch.object(rc.os, "fstat", side_effect=fstat_parent), \
                mock.patch.object(rc, "_same_file", side_effect=[True, False]), \
                mock.patch.object(rc.os, "close"):
            with self.assertRaises(OSError):
                with rc._pinned_windows_parents(target):
                    pass

    def test_runtime_descriptor_uniqueness_and_namespace_overlap(self) -> None:
        invalid_repo = dict(self.contract, repo=7)
        with self.assertRaisesRegex(rc.ContractError, "repo"):
            rc.validate_project_descriptor(invalid_repo)
        duplicate_hosts = dict(self.contract, eligible_hosts=["gs-recursion", "gs-recursion"])
        with self.assertRaisesRegex(rc.ContractError, "eligible_hosts"):
            rc.validate_project_descriptor(duplicate_hosts)
        with self.assertRaisesRegex(rc.ContractError, "canonical"):
            rc.validate_project_descriptor(dict(self.contract, allowed_paths=["src/", "src"]))
        overlap = dict(self.contract, external_branch_patterns=["recursion/*"])
        with self.assertRaisesRegex(rc.ContractError, "overlaps"):
            rc.validate_project_descriptor(overlap)
        self.assertEqual(rc.branch_kind(self.contract, "recursion/task/task-one"), "task")

    def test_executor_branch_rules(self) -> None:
        with self.assertRaisesRegex(rc.ContractError, "default branch"):
            rc.branch_kind(self.contract, "main")
        with self.assertRaisesRegex(rc.ContractError, "integration branch"):
            rc.branch_kind(self.contract, "recursion/integration")
        self.assertEqual(rc.branch_kind(self.contract, "cursor/task-one"), "external")
        self.assertEqual(rc.task_branch(self.contract, "task-one"), "recursion/task/task-one")

    def test_all_new_generation_fields_reject_legacy_example_ops(self) -> None:
        for field in ("workdir", "acceptance", "gate_command", "gate_commands", "seed", "goal"):
            task = {"id": "task-one", "source_branch": "main", "source_sha": self.source,
                    "allowed_paths": ["src"], field: ".\\apps\\example-ops/"}
            if field == "gate_commands":
                task[field] = [task[field]]
            with self.subTest(field=field), self.assertRaisesRegex(rc.ContractError, "apps/example-ops"):
                rc.admit_task(self.contract, task, self.repo)
        punctuation = {"id": "task-one", "source_branch": "main", "source_sha": self.source,
                       "allowed_paths": ["src"], "acceptance": "cd apps/example-ops;)&|"}
        with self.assertRaisesRegex(rc.ContractError, "apps/example-ops"):
            rc.admit_task(self.contract, punctuation, self.repo)
        dotted = dict(punctuation, acceptance="cd apps/./example-ops;", goal="safe")
        with self.assertRaisesRegex(rc.ContractError, "apps/example-ops"):
            rc.admit_task(self.contract, dotted, self.repo)

    def test_verified_import_is_idempotent_and_cas(self) -> None:
        candidate = self.candidate()
        verified = rc.verify_candidate(self.contract, candidate, self.repo, receipt=self.receipt(candidate))
        self.assertEqual(verified["changed_paths"], ["src/feature.txt"])
        wrong = Path(self.temp.name) / "verify-wrong-clone"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(wrong)], check=True)
        with self.assertRaisesRegex(rc.ContractError, "does not match"):
            rc.verify_candidate(self.contract, candidate, wrong, receipt=self.receipt(candidate))
        with self.assertRaisesRegex(rc.ContractError, "does not match"):
            rc.import_candidate(self.contract, candidate, wrong, receipt=self.receipt(candidate))
        self.assertFalse((wrong / ".git" / "recursion-integration.lock").exists())
        first = rc.import_candidate(self.contract, candidate, self.repo, receipt=self.receipt(candidate))
        second = rc.import_candidate(self.contract, candidate, self.repo, receipt=self.receipt(candidate))
        self.assertEqual(first["status"], "imported")
        self.assertEqual(second["status"], "idempotent")
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/recursion/task/task-one"), candidate["candidate_commit"])
        changed = dict(candidate)
        git(self.repo, "checkout", "-qb", "cursor/other")
        (self.repo / "src" / "other.txt").write_text("other\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "other")
        changed["candidate_commit"] = git(self.repo, "rev-parse", "HEAD")
        changed["external_branch"] = "cursor/other"
        with self.assertRaisesRegex(rc.ContractError, "different candidate"):
            rc.import_candidate(self.contract, changed, self.repo, receipt=self.receipt(changed))

        widened = dict(candidate, allowed_paths=["."])
        with self.assertRaisesRegex(rc.ContractError, "widens"):
            rc.verify_candidate(self.contract, widened, self.repo, receipt=self.receipt(widened))

    def test_directory_handle_identity_recheck_is_not_vacuous(self) -> None:
        expected = os.stat(self.repo)
        changed = os.stat(self.temp.path)
        with mock.patch.object(rc.os, "fstat", return_value=changed):
            with self.assertRaisesRegex(
                    OSError, "directory identity changed while pinned",
                    msg="directory handle recheck accepted a different directory",
            ):
                rc._verify_directory_handle(123, expected)

    def test_allow_canonical_requires_exact_task_branch(self) -> None:
        candidate = self.candidate()
        canonical = dict(candidate, external_branch=rc.task_branch(self.contract, candidate["task_id"]))
        git(self.repo, "branch", canonical["external_branch"], candidate["candidate_commit"])
        with self.assertRaisesRegex(rc.ContractError, "exact task branch"):
            rc.verify_candidate(self.contract, dict(canonical, external_branch="recursion/task/other"),
                                self.repo, receipt=self.receipt(canonical), allow_canonical=True)
        verified = rc.verify_candidate(self.contract, canonical, self.repo,
                                       receipt=self.receipt(canonical), allow_canonical=True)
        self.assertEqual(verified["external_branch"], "recursion/task/task-one")

    def test_controller_key_cannot_be_selected_by_hostile_environment(self) -> None:
        candidate = self.candidate()
        receipt = self.receipt(candidate)
        self.assertEqual(rc.verify_candidate(self.contract, candidate, self.repo, receipt=receipt)["task_id"], "task-one")
        if os.name != "nt":
            os.chmod(self.key_path, 0o666)
            with self.assertRaisesRegex(rc.ContractError, "key"):
                rc.verify_candidate(self.contract, candidate, self.repo, receipt=receipt)

    def test_git_plumbed_symlink_and_submodule_tree_escapes_are_refused(self) -> None:
        # Create the exact unsafe target blob without requiring an OS-level symlink or elevated
        # Windows privilege.
        target = subprocess.run(["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
                                input=b"../.GIT/Control-Plane\n", capture_output=True, check=True).stdout.decode().strip()
        base = self.candidate()
        symlink = self.plumbed_candidate(f"120000 blob {target}", "escape", base)
        with self.assertRaisesRegex(rc.ContractError, "symlink"):
            rc.verify_candidate(self.contract, symlink, self.repo, receipt=self.receipt(symlink))
        submodule = self.plumbed_candidate(f"160000 commit {self.source}", "submodule", base)
        with self.assertRaisesRegex(rc.ContractError, "submodule"):
            rc.verify_candidate(self.contract, submodule, self.repo, receipt=self.receipt(submodule))
        outside = self.plumbed_candidate(f"120000 blob {target}", "escape-outside", base, root_entry=True)
        with self.assertRaisesRegex(rc.ContractError, "symlink"):
            rc._inspect_candidate_tree(self.repo, outside["candidate_commit"], ["src"])

        # Case-distinct Git paths must not let an unsafe symlink be overwritten in the
        # case-folded lookup used by Windows consumers.  Both tree insertion orders refuse.
        for first_name, second_name in (("Escape", "escape"), ("escape", "Escape")):
            first = self.plumbed_candidate(f"120000 blob {target}", first_name, base)
            second = self.plumbed_candidate("120000 blob " +
                                            subprocess.run(
                                                ["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
                                                input=b"base.txt", capture_output=True, check=True,
                                            ).stdout.decode().strip(), second_name, first)
            with self.subTest(order=(first_name, second_name)), self.assertRaisesRegex(
                    rc.ContractError, "case-folded path collision"):
                rc._inspect_candidate_tree(self.repo, second["candidate_commit"], ["src"])

        # Protected control-plane paths are forbidden even when the descriptor scope is "." and
        # the entry is an ordinary tree/blob rather than a symlink target.
        protected_blob = subprocess.run(
            ["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
            input=b"protected\n", capture_output=True, check=True,
        ).stdout.decode().strip()
        protected_tree = subprocess.run(
            ["git", "-C", str(self.repo), "mktree"],
            input=f"100644 blob {protected_blob}\tsecret\n".encode(),
            capture_output=True, check=True,
        ).stdout.decode().strip()
        for protected in (".git", "control-plane"):
            protected_candidate = self.plumbed_candidate(
                f"040000 tree {protected_tree}", protected, base, root_entry=True,
                branch_name="protected-" + protected.removeprefix("."),
            )
            with self.subTest(protected=protected), self.assertRaisesRegex(
                    rc.ContractError, "protected path"):
                rc._inspect_candidate_tree(
                    self.repo, protected_candidate["candidate_commit"], ["."])

    def test_sha256_source_prefix_is_not_an_exact_source_sha(self) -> None:
        sha_repo = Path(self.temp.name) / "sha256-repo"
        sha_repo.mkdir()

        def sha_git(*args: str) -> str:
            return git(sha_repo, *args)

        sha_git("init", "-q", "-b", "main", "--object-format=sha256")
        sha_git("config", "user.name", "contract-test")
        sha_git("config", "user.email", "contract-test@example.invalid")
        (sha_repo / "src").mkdir()
        (sha_repo / "src" / "base.txt").write_text("base\n", encoding="utf-8")
        sha_git("add", ".")
        sha_git("commit", "-qm", "base")
        source = sha_git("rev-parse", "HEAD")
        sha_git("branch", "recursion/integration", source)
        sha_git("checkout", "-qb", "cursor/sha256")
        (sha_repo / "src" / "feature.txt").write_text("candidate\n", encoding="utf-8")
        sha_git("add", ".")
        sha_git("commit", "-qm", "candidate")
        commit = sha_git("rev-parse", "HEAD")
        contract = dict(self.contract, repo=str(sha_repo))

        def candidate(source_sha: str) -> dict[str, object]:
            return {
                "project_id": "test-project", "task_id": "task-one", "source_branch": "main",
                "source_sha": source_sha, "external_branch": "cursor/sha256",
                "candidate_commit": commit,
            }

        def receipt(item: dict[str, object]) -> dict[str, object]:
            diff = git_raw_bytes(sha_repo, "diff", "--binary", str(item["source_sha"]), commit)
            body = {
                "schema": "recursion-controller-verifier-receipt/v1", "project_id": "test-project",
                "task_id": "task-one", "source_sha": item["source_sha"],
                "candidate_commit": commit, "diff_sha256": hashlib.sha256(diff).hexdigest(),
                "scope_digest": rc._json_digest(["src"]),
                "gate_digest": rc._json_digest(contract["gate_commands"]), "verdict": "pass",
                "verifier": "controller-verifier", "provenance": "recursion-controller",
            }
            receipt_digest = rc._json_digest(body)
            return {
                **body, "receipt_digest": receipt_digest,
                "signature": hmac.new(b"hermetic-controller-key", receipt_digest.encode("ascii"),
                                      hashlib.sha256).hexdigest(),
            }

        full = candidate(source)
        self.assertEqual(
            rc.verify_candidate(contract, full, sha_repo, receipt=receipt(full))["source_sha"], source)
        prefix = candidate(source[:40])
        with self.assertRaisesRegex(rc.ContractError, "full canonical"):
            rc.verify_candidate(contract, prefix, sha_repo, receipt=receipt(prefix))

    def test_malformed_task_fields_are_typed_before_legacy_guard(self) -> None:
        for field in ("acceptance", "goal", "workdir", "gate_command", "seed"):
            task = {"id": "task-one", "source_branch": "main", "source_sha": self.source,
                    "allowed_paths": ["src"], field: {"apps/example-ops": True}}
            with self.subTest(field=field), self.assertRaisesRegex(rc.ContractError, "must be a string"):
                rc.admit_task(self.contract, task, self.repo)

    def test_invalid_refs_and_vacuous_gates_are_rejected(self) -> None:
        for field in ("default_branch", "integration_branch"):
            for branch in ("cursor/../main", "cursor//x", "foo.lock"):
                with self.subTest(field=field, branch=branch), self.assertRaises(rc.ContractError):
                    rc.validate_project_descriptor(dict(self.contract, **{field: branch}))
        for pattern in ("cursor//x", "cursor/../*", "cursor/\x00*"):
            with self.assertRaises(rc.ContractError):
                rc.validate_project_descriptor(dict(self.contract, external_branch_patterns=[pattern]))
        for path in ("src\\x", "src/./x", "src\x00x", " src"):
            with self.assertRaises(rc.ContractError):
                rc.validate_project_descriptor(dict(self.contract, allowed_paths=[path]))
        for command in ("", "   ", "true", "echo no-op", "printf nope", "pwd", "false||true",
                        "git diff --check||true", "(true)", "(false)", "{ true; }", "exit 0",
                        "git diff --check\ntrue", "x\x00y"):
            with self.subTest(command=command), self.assertRaises(rc.ContractError):
                rc.validate_project_descriptor(dict(self.contract, gate_commands=[command]))
        rc.validate_project_descriptor(dict(self.contract, gate_commands=["git\tdiff --check"]))
        for pattern in ("cursor/.x/{task_id}", "foo.lock/{task_id}"):
            with self.assertRaises(rc.ContractError):
                rc.validate_project_descriptor(dict(self.contract, task_branch_pattern=pattern))
        with self.assertRaises(rc.ContractError):
            rc.validate_project_descriptor(dict(self.contract, external_branch_patterns=["cursor/foo.lock/*"]))
        for command in ("bash -c true", "python3 -c true", "command true", "exec true", "test -n foo"):
            with self.assertRaises(rc.ContractError):
                rc.validate_project_descriptor(dict(self.contract, gate_commands=[command]))

    def test_schema_rejects_the_runtime_descriptor_mismatch_matrix(self) -> None:
        schema = json.loads((Path(__file__).parents[2] /
                             "schemas/control-plane/v2/repository-contract.schema.json").read_text())
        validator = jsonschema.Draft202012Validator(schema)
        refused = (
            dict(self.contract, default_branch="main", integration_branch="main"),
            dict(self.contract, external_branch_patterns=["recursion/*"]),
            dict(self.contract, allowed_paths=["src//nested"]),
            dict(self.contract, gate_commands=["python3 ./src/check.py"]),
            dict(self.contract, gate_commands=[r"test -s \\server\share\result.txt"]),
        )
        for descriptor in refused:
            with self.subTest(descriptor=descriptor):
                self.assertTrue(list(validator.iter_errors(descriptor)))

    def test_schema_and_runtime_gate_grammar_differential_corpus(self) -> None:
        schema = json.loads((Path(__file__).parents[2] /
                             "schemas/control-plane/v2/repository-contract.schema.json").read_text())
        validator = jsonschema.Draft202012Validator(schema)
        # Keep this corpus deliberately enumerated.  The schema is the admission contract and
        # validate-gate.py is the runtime parser; every accepted direct segment and every
        # permitted 1-3 segment `&&` composition must agree across both implementations.
        valid = (
            "git diff --check",
            "test -s src/result.txt",
            "[ -s src/result.txt ]",
            "test -e src/a && test -s src/b",
            "test -e src/a&&test -s src/b",
            " test -e src/a \t&&\t git diff --check ",
            "git diff --check && test -s \"src/result file.txt\" && [ -s src/other.txt ]",
            "test -s src\\result.txt && git status --short",
        )
        refused = (
            "",
            "   ",
            "foo /bar",
            "/bin/foo",
            "python3 --version",
            "bash -c 'test -s src/result.txt'",
            "command python3 src/check.py",
            "exec python3 src/check.py",
            "python3 ../check.py",
            "python3 src/../check.py",
            "test -s src/../result.txt",
            "test -s /tmp/result.txt",
            "test -s \"C:\\tmp\\result.txt\"",
            "test -s src/a & test -s src/b",
            "test -s src/a &&& test -s src/b",
            "test -s src/a &&",
            "test -s src/a && && test -s src/b",
            "test -s src/a && echo ok",
            "test -s src/a || test -s src/b",
            "test -s src/a; test -s src/b",
            "test -s \"$(echo src/result.txt)\"",
            "test -s src/result.txt > /tmp/out",
            "test -s src/result.txt | grep -q x",
            "git -C ../other diff --check",
            "git -C../other diff --check",
            "git --git-dir=../other/.git diff",
            "git --git-dir ../other/.git diff",
            "git --work-tree /tmp diff",
            "git --work-tree=/tmp diff",
            "git -c core.hooksPath=/tmp diff",
            "git -ccore.pager=cat diff",
            "git --config /tmp/cfg diff",
            "git --config=/tmp/cfg diff",
            "git --config-env=GIT_DIR=../other diff",
            "git --output /tmp/out diff",
            "git --output=/tmp/out diff",
            "git diff -C ../other --check",
        )
        for command in valid:
            with self.subTest(command=command):
                jsonschema.Draft202012Validator(schema).validate(dict(self.contract, gate_commands=[command]))
                rc.validate_project_descriptor(dict(self.contract, gate_commands=[command]))
        for command in refused:
            with self.subTest(command=command):
                self.assertTrue(list(validator.iter_errors(dict(self.contract, gate_commands=[command]))))
                with self.assertRaises(rc.ContractError):
                    rc.validate_project_descriptor(dict(self.contract, gate_commands=[command]))

    def test_import_race_reconciles_identical_winner_and_receipt_is_required(self) -> None:
        candidate = self.candidate()
        with self.assertRaisesRegex(rc.ContractError, "verification receipt"):
            rc.verify_candidate(self.contract, candidate, self.repo)
        winner = candidate["candidate_commit"]
        original = rc._git
        raced = {"update": False}

        def race(repo: Path, *args: str):
            if args and args[0] == "update-ref" and not raced["update"]:
                raced["update"] = True
                original(repo, "update-ref", "refs/heads/recursion/task/task-one", winner, "0" * 40)
                return subprocess.CompletedProcess(["git"], 1, "", "simulated CAS loss")
            return original(repo, *args)

        with mock.patch.object(rc, "_git", side_effect=race):
            result = rc.import_candidate(self.contract, candidate, self.repo, receipt=self.receipt(candidate))
        self.assertEqual(result["status"], "idempotent")

    def test_forged_receipt_and_malformed_candidates_are_typed_refusals(self) -> None:
        candidate = self.candidate()
        receipt = self.receipt(candidate)
        forged = dict(receipt, signature="0" * 64)
        with self.assertRaisesRegex(rc.ContractError, "authentication"):
            rc.verify_candidate(self.contract, candidate, self.repo, receipt=forged)
        for malformed in (None, [], {"project_id": []}, {"project_id": "test-project", "task_id": []}):
            with self.subTest(malformed=malformed):
                try:
                    rc.verify_candidate(self.contract, malformed, self.repo, receipt=receipt)
                except rc.ContractError:
                    pass
                else:
                    self.fail("malformed candidate was not refused")

    def test_scope_and_stale_classification(self) -> None:
        candidate = self.candidate()
        self.assertEqual(rc.classify_stale_base(self.contract, candidate, self.repo), "unauthorized-result")
        bad = dict(candidate)
        git(self.repo, "checkout", "-qb", "cursor/bad")
        (self.repo / "outside.txt").write_text("no\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "outside")
        bad["candidate_commit"] = git(self.repo, "rev-parse", "HEAD")
        bad["external_branch"] = "cursor/bad"
        with self.assertRaisesRegex(rc.ContractError, "outside allowed_paths"):
            rc.verify_candidate(self.contract, bad, self.repo)
        # Return to the valid external ref and prove a current source is compatible.
        valid = rc.verify_candidate(self.contract, candidate, self.repo, receipt=self.receipt(candidate))
        self.assertEqual(rc.classify_stale_base(self.contract, valid, self.repo, receipt=self.receipt(candidate)), "current-compatible")
        # Advance default independently; the exact candidate remains reviewable but cannot be
        # silently rebased into the integration ref.
        git(self.repo, "checkout", "main")
        (self.repo / "src" / "main.txt").write_text("main\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "main advances")
        self.assertEqual(rc.classify_stale_base(self.contract, valid, self.repo, receipt=self.receipt(candidate)), "stale-rebase-required")

    def test_plan_is_read_only_and_serialized(self) -> None:
        candidate = self.candidate()
        before = git(self.repo, "rev-parse", "refs/heads/recursion/integration")
        plan = rc.plan_integration(self.contract, candidate, self.repo, receipt=self.receipt(candidate))
        self.assertTrue(plan["serialized"])
        self.assertEqual(plan["expected_destination_tip"], before)
        self.assertTrue(plan["can_integrate"])
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/recursion/integration"), before)


if __name__ == "__main__":
    unittest.main()
