#!/usr/bin/env python3
"""Tests for the CI patches in tools/release/proposals/ (lane RELEASE). Run from the root:

    python -X utf8 -B tools/release/proposal_checks.py

Each patch must apply to the tree (or already be applied in it), the patches must apply
together in either order of every pair, and the files must carry nothing the gate's
whitespace check would flag. The proposed job's shell steps are run here with harmless
stand-ins for python and mktemp (the core suite is never run), so a step that stops
running the suite, masks its failure or loses its private folders fails these tests.
Named proposal_checks.py until tools/release/proposals/ci-gate-test-discovery.patch
renames it to tools/release/test_proposals.py (see tools/release/hash_lock_checks.py).
Stdlib, git and bash only.
"""
from __future__ import annotations

import builtins
import importlib.util
import itertools
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hash_lock as hl  # noqa: E402

ROOT = HERE.parents[1]
PROPOSALS = sorted((ROOT / hl.PROPOSALS).glob("*.patch"))
EXPECTED = {"ci-314t.patch", "retire-review-workflows.patch", "ci-gate-test-discovery.patch",
            "ci-gate-publish-timeout.patch"}
SHELL = shutil.which("bash") is not None and os.name != "nt"
SUITE = ["-X", "gil=0", "-B", "-m", "unittest", "discover", "-s", "core", "-t", "core"]


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


def proposed(case: unittest.TestCase, name: str, extra_dirs=()) -> Path:
    """The tree as the patch leaves it: the repository itself once the patch is applied."""
    patch = ROOT / hl.PROPOSALS / name
    if hl.proposal_state(ROOT, patch) == "applied":
        return ROOT
    holder = scratch_copy(hl.patch_paths(patch.read_text(encoding="utf-8")), extra_dirs)
    case.addCleanup(holder.cleanup)
    result = hl.git_apply(Path(holder.name), patch)
    case.assertEqual(result.returncode, 0, result.stderr)
    return Path(holder.name)


def job_block(text: str, job: str) -> str:
    match = re.search(rf"(?ms)^  {re.escape(job)}:\n(.*?)(?=^  \S|\Z)", text)
    assert match, f"job {job} not found"
    return match.group(1)


def step_run(block: str, name: str) -> str:
    step = re.search(rf"(?ms)^      - name: {re.escape(name)}\n(.*?)(?=^      - |\Z)", block)
    assert step, f"step {name!r} not found"
    run = re.search(r"(?m)^        run: (.*)$", step.group(1))
    if run.group(1).strip() != "|":
        return run.group(1).strip()
    lines = []
    for line in step.group(1)[run.end() + 1:].splitlines():
        if line.startswith("          "):
            lines.append(line[10:])
        elif not line.strip():
            lines.append("")
        else:
            break
    return "\n".join(lines).rstrip("\n") + "\n"


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
    PATCH = "ci-314t.patch"

    def ci_text(self) -> str:
        return (proposed(self, self.PATCH) / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    def test_the_job_runs_the_core_suite_free_threaded_with_job_local_folders(self) -> None:
        text = self.ci_text()
        job = job_block(text, "core-314t")
        for needle in ('python-version: "3.14t"', "--require-hashes", "requirements-ci-314t.txt",
                       "python -X gil=0 -B -m unittest discover -s core -t core", "mktemp -d /tmp/",
                       "mktemp -d /dev/shm/", "env -u DBUS_SESSION_BUS_ADDRESS", "Py_GIL_DISABLED",
                       "timeout-minutes: 20\n", "persist-credentials: false"):
            self.assertIn(needle, job)
        linux = job_block(text, "linux")
        self.assertIn("needs: [preflight, settle, linux-tests, core-314t]", linux)

    def fake_bin(self, root: Path) -> Path:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "python").write_text(
            "#!/bin/bash\n"
            "{ printf 'CALL'; for a in \"$@\"; do printf '\\x1f%s' \"$a\"; done; printf '\\n';\n"
            "  printf 'ENV\\x1f%s\\x1f%s\\x1f%s\\x1f%s\\n' \"${TMPDIR-unset}\" \"${XDG_RUNTIME_DIR-unset}\" "
            "\"${DBUS_SESSION_BUS_ADDRESS-unset}\" \"${PYTHONPATH-unset}\"; } >> \"$FAKE_LOG\"\n"
            "for a in \"$@\"; do [ \"$a\" = unittest ] && exit \"${FAKE_SUITE_EXIT:-0}\"; done\n"
            "exit \"${FAKE_PREREQ_EXIT:-0}\"\n", encoding="utf-8")
        (bin_dir / "mktemp").write_text(
            "#!/bin/bash\n[ \"$1\" = -d ] || exit 64\n"
            "printf 'MKTEMP\\x1f%s\\n' \"$2\" >> \"$FAKE_LOG\"\n"
            "dir=\"$FAKE_ROOT/$(basename \"${2%XXXXXX}\")$RANDOM$RANDOM\"\nmkdir -p \"$dir\" && echo \"$dir\"\n",
            encoding="utf-8")
        for tool in bin_dir.iterdir():
            tool.chmod(0o755)
        return bin_dir

    def run_suite_step(self, suite_exit: int = 0, prereq_exit: int = 0) -> tuple[int, list[list[str]]]:
        script = step_run(job_block(self.ci_text(), "core-314t"), "Core suite, free-threaded with the GIL off")
        holder = tempfile.TemporaryDirectory(prefix="proposal-shell-")
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)
        log = root / "log"
        env = {"PATH": f"{self.fake_bin(root)}{os.pathsep}{os.environ['PATH']}", "FAKE_LOG": str(log),
               "FAKE_ROOT": str(root), "FAKE_SUITE_EXIT": str(suite_exit), "FAKE_PREREQ_EXIT": str(prereq_exit),
               "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/bus", "HOME": str(root)}
        # GitHub runs a `shell: bash` step as `bash --noprofile --norc -eo pipefail {0}`.
        result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], env=env,
                                capture_output=True, text=True, check=False)
        lines = [line.split("\x1f") for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        return result.returncode, lines

    @unittest.skipUnless(SHELL, "needs bash on a POSIX host")
    def test_the_suite_step_runs_the_suite_in_private_folders(self) -> None:
        code, lines = self.run_suite_step()
        self.assertEqual(code, 0)
        mktemps = [line[1] for line in lines if line[0] == "MKTEMP"]
        self.assertEqual(mktemps, ["/tmp/recursion-ci-tmp.XXXXXX", "/dev/shm/recursion-ci-run.XXXXXX"])
        calls = [(line[1:], lines[i + 1][1:]) for i, line in enumerate(lines) if line[0] == "CALL"]
        self.assertEqual(len(calls), 2, lines)
        (prereq, _), (suite, suite_env) = calls
        self.assertEqual(prereq[:4], ["-X", "gil=0", "-B", "-c"])
        self.assertFalse({"-O", "-OO"} & set(prereq) or {"-O", "-OO"} & set(suite))
        self.assertEqual(suite, SUITE)
        tmpdir, runtime, bus, pythonpath = suite_env
        self.assertTrue(Path(tmpdir).name.startswith("recursion-ci-tmp.") and Path(tmpdir).is_dir(), tmpdir)
        self.assertTrue(Path(runtime).name.startswith("recursion-ci-run.") and Path(runtime).is_dir(), runtime)
        self.assertEqual((bus, pythonpath), ("unset", "core"))

    @unittest.skipUnless(SHELL, "needs bash on a POSIX host")
    def test_a_failing_suite_fails_the_step(self) -> None:
        code, lines = self.run_suite_step(suite_exit=17)
        self.assertEqual(code, 17)
        self.assertEqual(sum(1 for line in lines if line[0] == "CALL"), 2)

    @unittest.skipUnless(SHELL, "needs bash on a POSIX host")
    def test_a_failing_interpreter_check_stops_before_the_suite(self) -> None:
        code, lines = self.run_suite_step(prereq_exit=1)
        self.assertNotEqual(code, 0)
        self.assertEqual(sum(1 for line in lines if line[0] == "CALL"), 1)

    def prerequisite_code(self) -> str:
        script = step_run(job_block(self.ci_text(), "core-314t"), "Core suite, free-threaded with the GIL off")
        line = next(line for line in script.splitlines() if " -c " in line)
        argv = shlex.split(line)
        self.assertEqual(argv[:4], ["python", "-X", "gil=0", "-B"])
        return argv[argv.index("-c") + 1]

    def run_prerequisite(self, *, gil_disabled: int, gil_enabled: bool, cryptography: bool = True) -> list:
        printed: list = []
        modules = {"sys": types.SimpleNamespace(version="3.14.8 (stand-in)", _is_gil_enabled=lambda: gil_enabled),
                   "sysconfig": types.SimpleNamespace(
                       get_config_var=lambda name: gil_disabled if name == "Py_GIL_DISABLED" else None)}
        if cryptography:
            modules["cryptography"] = types.SimpleNamespace(__version__="50.0.2")

        def fake_import(name, *args, **kwargs):
            if name in modules:
                return modules[name]
            if name == "cryptography":
                raise ModuleNotFoundError(name)
            return builtins.__import__(name, *args, **kwargs)

        namespace = dict(vars(builtins), __import__=fake_import, print=lambda *a, **k: printed.append(a))
        exec(compile(self.prerequisite_code(), "<core-314t prerequisite>", "exec"), {"__builtins__": namespace})
        return printed

    def test_the_interpreter_check_passes_only_free_threaded_with_cryptography(self) -> None:
        printed = self.run_prerequisite(gil_disabled=1, gil_enabled=False)
        self.assertEqual(printed, [("3.14.8 (stand-in)", "cryptography", "50.0.2")])
        for kwargs, error in (({"gil_disabled": 0, "gil_enabled": False}, AssertionError),
                              ({"gil_disabled": 1, "gil_enabled": True}, AssertionError),
                              ({"gil_disabled": 1, "gil_enabled": False, "cryptography": False}, ImportError)):
            with self.subTest(**kwargs), self.assertRaises(error):
                self.run_prerequisite(**kwargs)

    @unittest.skipUnless(SHELL, "needs bash on a POSIX host")
    def test_the_required_check_needs_the_job(self) -> None:
        script = step_run(job_block(self.ci_text(), "linux"), "Required status")
        base = {"EVENT_NAME": "push", "EVENT_ACTION": "", "PR_DRAFT": "", "PREFLIGHT": "success",
                "RUN_FULL": "true", "PAID": "true", "SETTLE": "skipped", "TESTS": "success", "CORE_314T": "success"}
        docs = dict(base, RUN_FULL="false", PAID="false", TESTS="skipped", CORE_314T="skipped")
        cases = [(base, 0), (docs, 0), (dict(docs, CORE_314T="success"), 1), (dict(docs, CORE_314T="failure"), 1)]
        cases += [(dict(base, CORE_314T=result), 1) for result in ("failure", "cancelled", "skipped", "")]
        for env, expected in cases:
            with self.subTest(env=env):
                result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                                        env={"PATH": os.environ["PATH"], **env}, capture_output=True, text=True)
                self.assertEqual(result.returncode != 0, bool(expected), result.stdout + result.stderr)


class RetireTests(unittest.TestCase):
    PATCH = "retire-review-workflows.patch"
    RETIRED = ("codex-gate.yml", "claude.yml", "claude-conflict-writer.yml", "conflict-scan.yml",
               "stuck-thread-watch.yml")
    REVIEW = re.compile(r"anthropics/claude-code|ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN|@codex review"
                        r"|CODEX_TRIGGER_PAT|stuck_thread_watch|codex_gate")

    def test_no_workflow_runs_review_automation_after_it(self) -> None:
        # Copy the whole folders, not only what the patch touches: a file the patch forgets
        # must still be there afterwards for this test to see it.
        root = proposed(self, self.PATCH, [hl.WORKFLOWS, ".github/scripts", "scripts/ci"])
        workflows = root / hl.WORKFLOWS
        for name in self.RETIRED:
            self.assertFalse((workflows / name).exists(), name)
        self.assertEqual([p.name for p in workflows.glob("*.y*ml") if self.REVIEW.search(p.read_text(encoding="utf-8"))], [])
        for gone in (".github/scripts/stuck_thread_watch.py", "scripts/ci/fixer_failure_escalation.py",
                     "scripts/ci/codex_gate.py"):
            self.assertFalse((root / gone).exists(), gone)
        self.assertTrue((workflows / "ci.yml").is_file())

    def test_it_keeps_the_csl_495_tests_and_the_workflow_loader_tests(self) -> None:
        root = proposed(self, self.PATCH)
        for kept in ("tools/test_claude_fixer_guard.py", "tools/test_claude_fixer_publish.py"):
            self.assertTrue((root / kept).is_file(), kept)
        publish = (root / "tools/test_claude_fixer_publish.py").read_text(encoding="utf-8")
        self.assertIn("class WorkflowYamlTests(unittest.TestCase):", publish)
        self.assertIn("def test_every_workflow_loads(self):", publish)
        self.assertNotIn("class WorkflowWiringTests", publish)


class DiscoveryTests(unittest.TestCase):
    PATCH = "ci-gate-test-discovery.patch"

    def test_the_gate_finds_eval_and_release_tests_after_it(self) -> None:
        root = proposed(self, self.PATCH)
        spec = importlib.util.spec_from_file_location("proposed_ci_gate", root / "tools/ci_gate.py")
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        holder = tempfile.TemporaryDirectory(prefix="proposal-discovery-")
        self.addCleanup(holder.cleanup)
        tree = Path(holder.name)
        for rel in ("eval/test_runner.py", "eval/arms/test_direct.py", "tools/release/test_hash_lock.py",
                    "tools/release/hash_lock_checks.py", "tools/test_ci_gate.py"):
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_text("x = 1\n", encoding="utf-8")
        found = gate.glob_suite_tests(tree)
        for rel in ("eval/test_runner.py", "eval/arms/test_direct.py", "tools/release/test_hash_lock.py"):
            self.assertIn(rel, found)
        self.assertNotIn("tools/release/hash_lock_checks.py", found)
        if root != ROOT:
            self.assertTrue((root / "tools/release/test_hash_lock.py").is_file())
            self.assertFalse((root / "tools/release/hash_lock_checks.py").exists())


if __name__ == "__main__":
    unittest.main()
