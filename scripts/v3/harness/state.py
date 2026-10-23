"""Saved run records. One JSON file per run, outside the git history."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from scripts.v3.harness.errors import HarnessError


_RUN_ID = re.compile(r"^run-[0-9a-f]{12}$")


def new_run_id() -> str:
    return "run-" + uuid.uuid4().hex[:12]


def save_run(state_dir: Path, record: dict) -> Path:
    path = run_path(state_dir, record["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def load_run(state_dir: Path, run_id: str) -> dict:
    path = run_path(state_dir, run_id)
    if not path.is_file():
        raise HarnessError(f"no saved run {run_id}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"cannot read run {run_id}") from exc
    if not isinstance(payload, dict):
        raise HarnessError(f"run {run_id} is not a JSON object")
    return payload


def list_runs(state_dir: Path) -> list[str]:
    root = Path(state_dir)
    if not root.is_dir():
        return []
    found = []
    for path in root.glob("run-*.json"):
        run_id = path.stem
        if _RUN_ID.fullmatch(run_id):
            found.append(run_id)
    return sorted(found)


def run_path(state_dir: Path, run_id: str) -> Path:
    if _RUN_ID.fullmatch(run_id) is None:
        raise HarnessError("run id must look like run-xxxxxxxxxxxx")
    return Path(state_dir) / f"{run_id}.json"
