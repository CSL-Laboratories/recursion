"""The dev set's tool: pin its tasks in eval/devset/suite.json and check them.

    python -m eval.dev check     every task: sha256 and tree pins hold; hidden tests fail on the start
                                 tree and pass with the reference solution (twice)
    python -m eval.dev pin       rewrite the pins in suite.json from the task files

The dev set is not held out: it lives in the repository and is for building and shaking down the
runner. Its start trees are made into a commit with fixed identities and dates (eval/gitutil.py), so
each task's parent commit and tree are the same on every machine and can be pinned.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

from eval import PACKAGE_ROOT, gitutil
from eval import scratch as scratch_mod
from eval import suite as suite_mod
from eval import tasks as task_mod
from eval import validate as validate_mod

DEVSET = PACKAGE_ROOT / "devset"
MANIFEST = DEVSET / "suite.json"


def commits_for(task, tmp_base: Path, xdg_base: Path, *, with_tree: bool = False) -> tuple:
    """(parent commit, parent tree, solution commit[, solution commit's tree]) of a dev task, made with
    the fixed identity."""
    with scratch_mod.attempt(f"pin-{task.id}-{secrets.token_hex(4)}", tmp_base, xdg_base) as scratch:
        work = scratch.tmpdir / "work"
        work.mkdir()
        task.materialize_start(work)
        parent, tree = gitutil.snapshot(work)
        task.apply_solution(work)
        task.write_hidden(work)
        gitutil.git(["add", "-A"], work)
        gitutil.git(["commit", "-q", "--allow-empty", "-m", "reference solution"], work)
        commit = gitutil.git(["rev-parse", "HEAD"], work).decode().strip()
        commit_tree = gitutil.git(["rev-parse", "HEAD^{tree}"], work).decode().strip()
    return (parent, tree, commit, commit_tree) if with_tree else (parent, tree, commit)


def pin(tmp_base: Path, xdg_base: Path) -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for entry in manifest["tasks"]:
        task = task_mod.load_txtar(DEVSET / entry["file"])
        parent, tree, commit, commit_tree = commits_for(task, tmp_base, xdg_base, with_tree=True)
        entry.update(id=task.id, sha256=task.sha256, parent_tree=tree, parent_commit=parent, commit=commit,
                     commit_tree=commit_tree)
    MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def check(tmp_base: Path, xdg_base: Path, *, reruns: int = 2, log=print) -> list:
    """Problems found, one line each. Empty means every task holds."""
    problems = []
    suite = suite_mod.load_dev(MANIFEST)
    for task in suite.tasks:
        found = commits_for(task, tmp_base, xdg_base, with_tree=True)
        if found != (task.parent_commit, task.parent_tree, task.commit, task.commit_tree):
            problems.append(f"{task.id}: pins differ from the task file (found {' '.join(found)})")
            continue
        checked = validate_mod.validate(task, reruns=reruns, tmp_base=tmp_base, xdg_base=xdg_base)
        if not checked.ok:
            problems.append(f"{task.id}: {checked.reason}")
        else:
            log(f"ok {task.id}")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.dev", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("check", "pin"))
    parser.add_argument("--tmp-base", type=Path, default=scratch_mod.DEFAULT_TMP_BASE)
    parser.add_argument("--xdg-base", type=Path, default=scratch_mod.DEFAULT_XDG_BASE)
    args = parser.parse_args(argv)
    if args.command == "pin":
        pin(args.tmp_base, args.xdg_base)
        print(f"pinned {MANIFEST}")
        return 0
    problems = check(args.tmp_base, args.xdg_base)
    for line in problems:
        print(line)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
