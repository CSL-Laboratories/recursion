"""Task validation, shared by the harvest and the dev set: a task is kept only if its hidden tests
FAIL on the start tree and PASS on the start tree with the reference solution, every time.

Each check runs in its own scratch runtime (eval/scratch.py), like an attempt. ``reruns`` repeats
both checks: a task whose result changes between runs is flaky and is not kept (vault
20261028-recursion-frontier-dr-eval-reliability-02 measured about 13% of tasks flipping on rerun).
The start tree's tree id is checked too, so a task whose files do not round-trip (attributes, modes)
is caught here and not at run time.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path

from eval import arms as arms_mod
from eval import gitutil
from eval import scratch as scratch_mod

NO_TESTS_RAN = 5


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
        if code == 0:
            result.reason = prefix + "hidden tests pass on the parent: nothing to solve"
            return result
        if code == NO_TESTS_RAN:
            result.reason = prefix + "the hidden-test command ran no test on the parent"
            return result
        _, code, timed_out, log = _run(task, "commit", tmp_base, xdg_base)
        result.runs.append(("commit", code, timed_out))
        if timed_out or code != 0:
            result.reason = prefix + ("hidden tests ran past their limit on the commit" if timed_out
                                      else f"hidden tests fail on the commit (exit {code}): "
                                      + log.decode("utf-8", "replace")[-400:])
            return result
    result.ok = True
    return result
