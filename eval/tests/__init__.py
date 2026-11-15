"""Lane EVAL's tests. Files are named tests_*.py: ``unittest discover`` finds them, and the repository's
frozen gate, which fails on any test_*.py outside its own globs, does not count them.

Run from the repository root:  python -m unittest discover -s eval -t .
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path


def _xdg_parent():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and not runtime.startswith("/run/user/") and os.path.isdir(runtime):
        return runtime
    return None


@contextlib.contextmanager
def scratch_bases():
    """Two fresh base folders for attempts (TMPDIR side, XDG side), removed afterwards."""
    tmp = Path(tempfile.mkdtemp(prefix="eval-tmp-"))
    xdg = Path(tempfile.mkdtemp(prefix="eval-xdg-", dir=_xdg_parent()))
    try:
        yield tmp, xdg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(xdg, ignore_errors=True)


SHA = "0" * 64


def trial(task_id, index, outcome, *, planned=True, replacement_for=None, claimed=None, usage=None, wall=1.0,
          record_id="r"):
    """One trial row. outcome is 'pass', 'fail', or an error class (e.g. 'vendor_limit', 'not_run')."""
    from eval import record as record_mod
    from eval.runner import blank_trial

    row = blank_trial(f"{record_id}.{task_id}.{index}", task_id, index, planned, replacement_for)
    row["grade"].update(grader_id="hidden-tests/1", grader_sha256=SHA, details_sha256=SHA, exit=1)
    row["timing"]["wall_seconds"] = wall
    if outcome == "pass":
        row["grade"].update(verdict="resolved", exit=0)
        row["error"] = {"class": None, "tier": "none", "detail": None}
        row["engine"]["claimed_done"] = True if claimed is None else claimed
    elif outcome == "fail":
        row["grade"]["verdict"] = "unresolved"
        row["error"] = {"class": "tests_failed", "tier": "agent", "detail": None}
        row["engine"]["claimed_done"] = claimed
    else:
        tier = record_mod.CLASS_TIER[outcome]
        verdict = "unresolved" if outcome in ("vendor_limit", "auth_failed", "timeout", "engine_crash",
                                             "no_result", "engine_error") else "not_graded"
        row["grade"]["verdict"] = verdict
        row["error"] = {"class": outcome, "tier": tier, "detail": None}
        if outcome == "not_run":
            row["grade"].update(grader_id=None, grader_sha256=None, details_sha256=None, exit=None)
            row["timing"]["wall_seconds"] = None
    if usage is not None:
        row["usage"] = usage
    return row


def build_record(rows, *, tasks=None, k=2, attempts=3, replacements=2, label="dev", record_id="r",
                 manifest_sha=SHA, clusters=None, arm="direct"):
    """A valid recursion-eval-run/1 record around the given trial rows."""
    from eval import record as record_mod

    task_ids = tasks or sorted({r["task_id"] for r in rows})
    plan = {"k": k, "attempts_per_task": attempts, "replacements_per_task": replacements, "seed": 1,
            "tasks": [{"id": t, "cluster": (clusters or {}).get(t), "task_sha256": SHA, "created_at": None}
                      for t in task_ids]}
    # The shared settings, the same for both arms (eval/arms.py: no arm name in the hash).
    settings = record_mod.sha256_json({"engine": "standin", "model": None, "route": None, "effort": None,
                                       "permission_mode": None, "extra_args": []})
    plan["plan_sha256"] = record_mod.sha256_json(dict(plan, suite_manifest_sha256=manifest_sha,
                                                      arm_settings_sha256=settings))
    return {
        "schema": "recursion-eval-run/1", "record_id": record_id, "label": label,
        "suite": {"id": "dev", "version": "1", "split": "dev", "frozen": False, "manifest_sha256": manifest_sha},
        "arm": {"name": arm, "engine": "standin", "engine_version": "eval-standin/1", "model": None, "route": None,
                "effort": None, "settings_sha256": settings},
        "runner": {"version": "0.1.0", "sha256": SHA, "pinned": False, "evaluated_commit": None},
        "protocol": {"protocol_sha256": SHA, "analysis_sha256": SHA},
        "plan": plan, "environment": {"python": "3", "platform": "test"},
        "started_at": "2026-11-14T00:00:00+00:00", "finished_at": "2026-11-14T00:01:00+00:00",
        "trials": rows,
    }


TINY_TASK = """A tiny task for runner tests.
-- task.json --
{{"id": "{task_id}", "cluster": "{cluster}", "created_at": null,
 "test_command": ["{{python}}", "-B", "-m", "unittest", "{{hidden_modules}}"],
 "timeout_s": {timeout}, "grading_timeout_s": 120}}
-- prompt.md --
Make `answer()` in `tiny/core.py` return 42.
-- start/tiny/__init__.py --
-- start/tiny/core.py --
def answer():
    return 0
-- hidden/test_core_{suffix}.py --
import unittest

from tiny.core import answer


class AnswerTests(unittest.TestCase):
    def test_answer(self):
        self.assertEqual(answer(), 42)
-- solution/tiny/core.py --
def answer():
    return 42
-- wrong/tiny/core.py --
def answer():
    return 41
-- standin.json --
{{"outcomes": {outcomes}}}
"""


def tiny_task(task_id, outcomes, *, timeout=600, cluster="tiny"):
    import json
    return TINY_TASK.format(task_id=task_id, outcomes=json.dumps(outcomes), timeout=timeout, cluster=cluster,
                            suffix=task_id.replace("-", "_"))


def make_suite(folder: Path, tasks: dict, *, split="dev", suite_id="t", tmp_base=None, xdg_base=None,
               bad_tree_for=(), extra=None):
    """Write txtar tasks {task_id: text} and a suite.json pinning them. Returns the manifest path."""
    import json

    from eval import dev
    from eval import tasks as task_mod

    (folder / "tasks").mkdir(parents=True, exist_ok=True)
    entries = []
    for task_id, text in tasks.items():
        path = folder / "tasks" / f"{task_id}.txtar"
        path.write_text(text, encoding="utf-8")
        task = task_mod.load_txtar(path)
        parent, tree, commit, commit_tree = dev.commits_for(task, tmp_base, xdg_base, with_tree=True)
        if task_id in bad_tree_for:
            tree = "f" * 40
        entries.append({"id": task_id, "file": f"tasks/{task_id}.txtar", "sha256": task.sha256,
                        "parent_tree": tree, "parent_commit": parent, "commit": commit, "commit_tree": commit_tree})
    manifest = {"schema": "recursion-eval-suite/1", "id": suite_id, "version": "1", "split": split,
                "frozen": False, "runner": None, "evaluated_commit": None,
                "engines": {"standin": "eval-standin/1"}, "arms": {}, "tasks": entries}
    if extra:
        manifest.update(extra)
    path = folder / "suite.json"
    path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return path


@contextlib.contextmanager
def outside_folder(prefix="eval-out-"):
    """A folder outside the repository (under the temp folder), removed afterwards."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
