"""Task validation, shared by the harvest and the dev set: a task is kept only if its hidden tests
FAIL on the start tree and PASS on the start tree with the reference solution, every time.

Each check runs in its own scratch runtime (eval/scratch.py), like an attempt. ``reruns`` repeats
both checks: a task whose result changes between runs is flaky and is not kept (vault
20261028-recursion-frontier-dr-eval-reliability-02 measured about 13% of tasks flipping on rerun).
The start tree's tree id is checked too, so a task whose files do not round-trip (attributes, modes)
is caught here and not at run time; and the commit side is graded only if the start tree with the
reference solution and the hidden tests IS the pinned commit's tree (``commit_tree``), so a deleted
test file or a changed mode cannot leave the graded tree different from the commit.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from eval import arms as arms_mod
from eval import gitutil
from eval import scratch as scratch_mod

NO_TESTS_RAN = 5
# The unittest summary that ends the log. An earlier "Ran N / OK (skipped=N)" can be a nested suite
# the command printed; only a summary that reaches the end of the log decides.
_FINAL_SUMMARY = re.compile(
    rb"Ran (\d+) tests? in [0-9.]+s\s+(OK|FAILED)(?: \(([^)]*)\))?\s*\Z",
    re.DOTALL,
)
_SKIPPED = re.compile(rb"(?:^|, )skipped=(\d+)")
_EXPECTED_FAILURES = re.compile(rb"(?:^|, )expected failures=(\d+)")


def collected_tests_all_skipped(log: bytes) -> bool:
    """True when the final hidden-test summary shows that nothing passed.

    A skipped suite exits 0, the same code as a pass. So does a suite whose only results are skips and
    expected failures. A pass on the parent would say there is nothing to solve, and a pass when grading
    would count the task done. An earlier summary inside the log is not the one that counts: a passing
    suite can print a nested run that skipped everything. Unexpected successes count as passes.
    """
    if not log:
        return False
    found = _FINAL_SUMMARY.search(log)
    if found is None or found.group(2) != b"OK":
        return False
    ran = int(found.group(1))
    body = found.group(3) or b""
    skipped = _SKIPPED.search(body)
    expected = _EXPECTED_FAILURES.search(body)
    skipped_n = int(skipped.group(1)) if skipped else 0
    expected_n = int(expected.group(1)) if expected else 0
    passed = ran - skipped_n - expected_n
    return ran > 0 and passed <= 0


@dataclass
class Validation:
    ok: bool
    reason: str | None
    tree: str | None = None
    runs: list = field(default_factory=list)   # (side, exit, timed_out) per run


def _run(task, side: str, tmp_base: Path, xdg_base: Path) -> tuple:
    attempt_id = f"validate-{task.id}-{side}-{secrets.token_hex(4)}"
    with scratch_mod.attempt(attempt_id, tmp_base, xdg_base) as scratch:
        work = scratch.tmpdir / "work"
        work.mkdir(mode=0o700)
        task.materialize_start(work)
        tree = gitutil.tree_of_files(work)
        if side == "commit":
            task.apply_solution(work)
        task.write_hidden(work)
        if side == "commit":
            # The tree the commit side grades must be the pinned commit's own tree (deletions and modes
            # included), not merely the parent with some files laid over it.
            tree = gitutil.tree_of_files(work)
        done = arms_mod.run_process(task.command(), cwd=work, env=scratch_mod.environment(scratch), stdin=None,
                                    timeout_s=task.grading_timeout_s)
        return tree, done["exit"], done["timed_out"], done["stdout"] + done["stderr"]


def validate(task, *, reruns: int = 2, tmp_base: Path = scratch_mod.DEFAULT_TMP_BASE,
             xdg_base: Path = scratch_mod.DEFAULT_XDG_BASE) -> Validation:
    if reruns < 1:
        raise ValueError("reruns must be at least 1")
    result = Validation(ok=False, reason=None)
    for run in range(1, reruns + 1):
        # A failure on a later run than the first means the outcome changed between runs: flaky.
        prefix = f"run {run} of {reruns}" + (" (flaky: an earlier run agreed)" if run > 1 else "") + ": "
        tree, code, timed_out, log = _run(task, "parent", tmp_base, xdg_base)
        result.runs.append(("parent", code, timed_out))
        result.tree = tree
        if task.parent_tree is not None and tree != task.parent_tree:
            result.reason = f"start tree is {tree}, pinned {task.parent_tree}"
            return result
        if timed_out:
            result.reason = prefix + "hidden tests ran past their limit on the parent"
            return result
        if code == NO_TESTS_RAN or collected_tests_all_skipped(log):
            result.reason = prefix + "the hidden-test command ran no test on the parent"
            return result
        if code == 0:
            result.reason = prefix + "hidden tests pass on the parent: nothing to solve"
            return result
        commit_tree, code, timed_out, log = _run(task, "commit", tmp_base, xdg_base)
        result.runs.append(("commit", code, timed_out))
        if task.commit_tree is not None and commit_tree != task.commit_tree:
            result.reason = (f"the start tree with the reference solution and hidden tests is {commit_tree}, "
                             f"not the pinned commit tree {task.commit_tree}")
            return result
        if code == NO_TESTS_RAN or collected_tests_all_skipped(log):
            result.reason = prefix + "the hidden-test command ran no test on the commit"
            return result
        if timed_out or code != 0:
            result.reason = prefix + ("hidden tests ran past their limit on the commit" if timed_out
                                      else f"hidden tests fail on the commit (exit {code}): "
                                      + log.decode("utf-8", "replace")[-400:])
            return result
    result.ok = True
    return result
