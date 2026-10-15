"""Saved run records. One JSON file per run, outside the git history."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from pathlib import Path

from scripts.v3.harness.errors import HarnessError


_RUN_ID = re.compile(r"^run-[0-9a-f]{12}$")


def new_run_id() -> str:
    return "run-" + uuid.uuid4().hex[:12]


def save_run(state_dir: Path, record: dict) -> Path:
    """Write one private run record.

    A symlink or hard link at the record path is refused, so the write cannot
    truncate another file. The bytes are written to a new mode-0600 file and
    published with ``os.replace``.
    """

    path = run_path(state_dir, record["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    _refuse_linked_record(path)
    text = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _publish_private(path, text)
    return path


def _refuse_linked_record(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or info.st_nlink > 1:
        raise HarnessError(f"refusing to write linked run record {path.name}")


def _publish_private(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = -1
    opened = False
    published = False
    try:
        fd = os.open(temporary, flags, 0o600)
        opened = True
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        if os.fstat(fd).st_nlink != 1:
            raise HarnessError(f"refusing to publish linked run record {path.name}")
        payload = memoryview(text.encode("utf-8"))
        while payload:
            written = os.write(fd, payload)
            if written <= 0:
                raise OSError("run record write made no progress")
            payload = payload[written:]
        os.fsync(fd)
        if os.fstat(fd).st_nlink != 1:
            raise HarnessError(f"refusing to publish linked run record {path.name}")
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        published = True
    except HarnessError:
        raise
    except OSError as exc:
        raise HarnessError(f"cannot write run {path.stem}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if opened and not published:
            try:
                os.unlink(temporary)
            except OSError:
                pass


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
