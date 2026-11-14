#!/usr/bin/env python3
"""Tests for the CI patches in tools/release/proposals/ (lane RELEASE). Run from the root:

    python -X utf8 -B tools/release/proposal_checks.py

Each patch must apply to the tree (or already be applied in it), the patches must apply
together in either order of every pair, and the files must carry nothing the gate's
whitespace check would flag. Named proposal_checks.py until
tools/release/proposals/ci-gate-release-tests.patch renames it to
tools/release/test_proposals.py (see tools/release/hash_lock_checks.py). Stdlib and git only.
"""
from __future__ import annotations

import itertools
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hash_lock as hl  # noqa: E402

ROOT = HERE.parents[1]
PROPOSALS = sorted((ROOT / hl.PROPOSALS).glob("*.patch"))
EXPECTED = {"ci-314t.patch", "retire-review-workflows.patch", "ci-gate-release-tests.patch",
            "ci-gate-publish-timeout.patch"}


def touched(patches) -> set[str]:
    return {rel for patch in patches for rel in hl.patch_paths(patch.read_text(encoding="utf-8"))}


def scratch_copy(rels, extra_dirs=()) -> tempfile.TemporaryDirectory:
    """LF copies of rels and of every file under extra_dirs, outside the repository."""
    holder = tempfile.TemporaryDirectory(prefix="proposal-checks-")
    rels = set(rels)
    for rel in extra_dirs:
        rels |= {p.relative_to(ROOT).as_posix() for p in (ROOT / rel).rglob("*") if p.is_file()}
    hl.lf_copy(ROOT, sorted(rels), Path(holder.name))
    return holder


class ProposalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.states = {patch.name: hl.proposal_state(ROOT, patch) for patch in PROPOSALS}
        cls.pending = [patch for patch in PROPOSALS if cls.states[patch.name] == "applies"]

    def test_the_expected_patches_are_here(self) -> None:
        self.assertEqual({patch.name for patch in PROPOSALS}, EXPECTED)

    def test_each_patch_applies_or_is_already_applied(self) -> None:
        self.assertEqual({name: state for name, state in self.states.items() if state == "conflicts"}, {})

    def test_every_pair_applies_in_either_order(self) -> None:
        for first, second in itertools.permutations(self.pending, 2):
            with self.subTest(first=first.name, second=second.name):
                holder = scratch_copy(touched([first, second]))
                try:
                    root = Path(holder.name)
                    for patch in (first, second):
                        result = hl.git_apply(root, patch)
                        self.assertEqual(result.returncode, 0, f"{patch.name}: {result.stderr}")
                finally:
                    holder.cleanup()

    def test_patch_files_pass_the_gate_whitespace_check(self) -> None:
        # Read with newline translation: a Windows checkout may add CRs that the blob lacks.
        for patch in PROPOSALS:
            with self.subTest(patch=patch.name):
                text = patch.read_text(encoding="utf-8")
                bad = [n for n, line in enumerate(text.split("\n"), 1) if re.search(r"[ \t\r]$", line)]
                self.assertEqual(bad, [])
                self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))

    def test_each_patch_says_what_it_is_and_how_to_apply_it(self) -> None:
        for patch in PROPOSALS:
            with self.subTest(patch=patch.name):
                head = patch.read_text(encoding="utf-8").split("\ndiff --git ", 1)[0]
                self.assertTrue(head.startswith("Proposal (lane RELEASE"), head[:60])
                self.assertIn(f"git apply --check tools/release/proposals/{patch.name}", head)
                self.assertIn("only Lucas applies this", head)


class Ci314tTests(unittest.TestCase):
    PATCH = ROOT / hl.PROPOSALS / "ci-314t.patch"

    def ci_text(self) -> str:
        if hl.proposal_state(ROOT, self.PATCH) == "applied":
            return (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        holder = scratch_copy(hl.patch_paths(self.PATCH.read_text(encoding="utf-8")))
        self.addCleanup(holder.cleanup)
        result = hl.git_apply(Path(holder.name), self.PATCH)
        self.assertEqual(result.returncode, 0, result.stderr)
        return (Path(holder.name) / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    def test_the_job_runs_the_core_suite_free_threaded_with_job_local_folders(self) -> None:
        text = self.ci_text()
        job = re.search(r"(?ms)^  core-314t:\n(.*?)(?=^  \S|\Z)", text).group(1)
        for needle in ('python-version: "3.14t"', "--require-hashes", "requirements-ci-314t.txt",
                       "python -X gil=0 -B -m unittest discover -s core -t core", "mktemp -d /tmp/",
                       "mktemp -d /dev/shm/", "env -u DBUS_SESSION_BUS_ADDRESS", "Py_GIL_DISABLED"):
            self.assertIn(needle, job)
        linux = re.search(r"(?ms)^  linux:\n(.*?)(?=^  \S|\Z)", text).group(1)
        self.assertIn("needs: [preflight, settle, linux-tests, core-314t]", linux)
        self.assertIn('if [ "$CORE_314T" != success ]; then', linux)
        self.assertIn('if [ "$CORE_314T" != skipped ]; then', linux)


class RetireTests(unittest.TestCase):
    PATCH = ROOT / hl.PROPOSALS / "retire-review-workflows.patch"
    RETIRED = ("codex-gate.yml", "claude.yml", "claude-conflict-writer.yml", "conflict-scan.yml")

    def test_no_workflow_runs_a_model_writer_after_it(self) -> None:
        if hl.proposal_state(ROOT, self.PATCH) == "applied":
            root, holder = ROOT, None
        else:
            holder = scratch_copy(hl.patch_paths(self.PATCH.read_text(encoding="utf-8")), [hl.WORKFLOWS])
            self.addCleanup(holder.cleanup)
            root = Path(holder.name)
            result = hl.git_apply(root, self.PATCH)
            self.assertEqual(result.returncode, 0, result.stderr)
        workflows = root / hl.WORKFLOWS
        for name in self.RETIRED:
            self.assertFalse((workflows / name).exists(), name)
        writers = [p.name for p in workflows.glob("*.y*ml")
                   if re.search(r"anthropics/claude-code|ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN|codex_gate",
                                p.read_text(encoding="utf-8"))]
        self.assertEqual(writers, [])
        self.assertTrue((workflows / "ci.yml").is_file())


if __name__ == "__main__":
    unittest.main()
