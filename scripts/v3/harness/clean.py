"""Remove finished run state. A run that is still in progress is refused."""

from __future__ import annotations

import shutil
from pathlib import Path

from scripts.v3.harness.errors import HarnessError
from scripts.v3.harness.state import list_runs, load_run, run_path


FINISHED = frozenset(
    {
        "budget-exceeded",
        "completed",
        "failed",
        "review-rejected",
        "timed-out",
        "unreviewed",
    }
)


def clean_runs(state_dir, run_id: str | None = None) -> dict:
    """Delete finished run records, logs, and private workspace folders.

    A named run that is still in progress raises. A sweep leaves those runs
    in place and lists them under ``refused``.
    """

    root = Path(state_dir)
    if run_id is not None:
        _refuse_if_open(root, run_id)
        _delete(root, run_id)
        return {"refused": [], "removed": [run_id]}
    removed = []
    refused = []
    for name in list_runs(root):
        if _open(load_run(root, name)):
            refused.append({"reason": "in progress", "run_id": name})
            continue
        _delete(root, name)
        removed.append(name)
    return {"refused": refused, "removed": removed}


def _refuse_if_open(state_dir: Path, run_id: str) -> None:
    if _open(load_run(state_dir, run_id)):
        raise HarnessError("run is still in progress")


def _open(record: dict) -> bool:
    return record.get("status") not in FINISHED


def _delete(state_dir: Path, run_id: str) -> None:
    record_path = run_path(state_dir, run_id)
    record_path.unlink(missing_ok=True)
    record_path.with_suffix(".jsonl").unlink(missing_ok=True)
    folder = Path(state_dir) / run_id
    if folder.is_symlink():
        folder.unlink()
    elif folder.is_dir():
        shutil.rmtree(folder)
