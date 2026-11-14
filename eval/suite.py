"""A pinned suite: the manifest that fixes every task, its start tree, the engines and the runner.

``recursion-eval-suite/1`` (a file named suite.json):

    {"schema": "recursion-eval-suite/1", "id": "dev", "version": "1", "split": "dev" | "held_out",
     "frozen": false,
     "runner": null | {"version": "...", "sha256": "..."},
     "evaluated_commit": null | "<the Recursion commit a frozen suite is run against>",
     "engines": {"standin": "eval-standin/1", "claude_code": null},
     "arms": {"claude_code": {"model": ..., "effort": ..., "permission_mode": ..., "extra_args": [...]}},
     "tasks": [{"id": ..., "file": "tasks/x.txtar" | "dir": "x", "sha256": ..., "parent_tree": ...,
                "parent_commit": ..., "commit": ...}]}

A null pin means unpinned, and the record says so (``runner.pinned``). A FROZEN suite has no null
pin: the runner refuses it otherwise. A held-out suite loads only through eval/heldout.py.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from eval import tasks as task_mod

SCHEMA_ID = "recursion-eval-suite/1"
SHA1_OR_256 = ("[0-9a-f]{40}", "[0-9a-f]{64}")


class SuiteError(ValueError):
    pass


class HeldOutRefused(SuiteError):
    """A held-out manifest reached a loader that is not the held-out guard."""


@dataclass
class Suite:
    id: str
    version: str
    split: str
    frozen: bool
    manifest_sha256: str
    path: Path
    runner: dict | None
    evaluated_commit: str | None
    engines: dict
    arms: dict
    tasks: list

    def task(self, task_id: str):
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(task_id)


def _is_object_id(value) -> bool:
    import re
    return isinstance(value, str) and any(re.fullmatch(p, value) for p in SHA1_OR_256)


def _parse(manifest_path: Path, *, allow_held_out: bool) -> Suite:
    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise SuiteError(f"{manifest_path}: not JSON: {exc}") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA_ID:
        raise SuiteError(f"{manifest_path}: not a {SCHEMA_ID} manifest")
    split = manifest.get("split")
    if split not in ("dev", "held_out"):
        raise SuiteError(f"{manifest_path}: split must be dev or held_out")
    if split == "held_out" and not allow_held_out:
        raise HeldOutRefused(f"{manifest_path}: a held-out suite opens only through the held-out guard "
                             "(eval/heldout.py), for scoring")
    frozen = manifest.get("frozen")
    if not isinstance(frozen, bool):
        raise SuiteError(f"{manifest_path}: frozen must be true or false")
    runner = manifest.get("runner")
    if runner is not None and not (isinstance(runner, dict) and set(runner) == {"version", "sha256"}):
        raise SuiteError(f"{manifest_path}: runner must be null or {{version, sha256}}")
    engines = manifest.get("engines") or {}
    arms = manifest.get("arms") or {}
    if not isinstance(engines, dict) or not isinstance(arms, dict):
        raise SuiteError(f"{manifest_path}: engines and arms must be objects")
    evaluated = manifest.get("evaluated_commit")
    if frozen:
        if runner is None:
            raise SuiteError(f"{manifest_path}: a frozen suite pins its runner")
        if evaluated is None:
            raise SuiteError(f"{manifest_path}: a frozen suite pins the evaluated commit")
        if any(v is None for v in engines.values()) or not engines:
            raise SuiteError(f"{manifest_path}: a frozen suite pins every engine version")
    entries = manifest.get("tasks")
    if not isinstance(entries, list) or not entries:
        raise SuiteError(f"{manifest_path}: tasks must be a non-empty list")
    base = manifest_path.parent
    loaded = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise SuiteError(f"{manifest_path}: each task entry must be an object")
        if "file" in entry:
            task = task_mod.load_txtar(base / task_mod.safe_relpath(entry["file"]))
        elif "dir" in entry:
            task = task_mod.load_dir(base / task_mod.safe_relpath(entry["dir"]))
        else:
            raise SuiteError(f"{manifest_path}: a task entry names a file or a dir")
        if entry.get("id") != task.id:
            raise SuiteError(f"{manifest_path}: entry {entry.get('id')!r} holds task {task.id!r}")
        if task.id in seen:
            raise SuiteError(f"{manifest_path}: duplicate task {task.id!r}")
        seen.add(task.id)
        if entry.get("sha256") != task.sha256:
            raise SuiteError(f"{manifest_path}: task {task.id!r} does not match its pinned sha256 "
                             f"(pinned {entry.get('sha256')}, found {task.sha256})")
        for key in ("parent_tree", "parent_commit", "commit"):
            if not _is_object_id(entry.get(key)):
                raise SuiteError(f"{manifest_path}: task {task.id!r} needs a pinned {key}")
        task.parent_tree = entry["parent_tree"]
        task.parent_commit = entry["parent_commit"]
        task.commit = entry["commit"]
        if "cluster" in entry:
            task.cluster = entry["cluster"]
        loaded.append(task)
    return Suite(
        id=str(manifest.get("id")), version=str(manifest.get("version")), split=split, frozen=frozen,
        manifest_sha256=hashlib.sha256(raw).hexdigest(), path=manifest_path, runner=runner,
        evaluated_commit=evaluated, engines=engines, arms=arms, tasks=loaded,
    )


def load_dev(manifest_path: Path) -> Suite:
    """A dev suite (in the repository). Refuses a held-out manifest."""
    return _parse(Path(manifest_path), allow_held_out=False)


def _load_held_out(manifest_path: Path) -> Suite:
    """Only eval/heldout.py calls this, after its checks. A test holds that to be so."""
    suite = _parse(Path(manifest_path), allow_held_out=True)
    if suite.split != "held_out":
        raise SuiteError(f"{manifest_path}: the held-out guard opens held-out suites only")
    return suite
