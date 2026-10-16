"""One JSON object per run event. Token-shaped text is redacted."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.v3.harness.errors import HarnessError
from scripts.v3.harness.redact import redact
from scripts.v3.harness.state import load_run, run_path


def append_event(state_dir, run_id: str, name: str, **fields: object) -> dict:
    """Append one redacted event to ``<state>/<run-id>.jsonl``."""

    if not isinstance(name, str) or not name:
        raise HarnessError("event name must not be empty")
    record = redact({"event": name, "ts": _utc_now(), "run_id": run_id, **fields})
    if not isinstance(record, dict):
        raise HarnessError("event must be an object")
    path = _log_path(state_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


def read_log(state_dir, run_id: str) -> list[dict]:
    """Return one run's events. A missing log is empty. Tokens are redacted again."""

    load_run(state_dir, run_id)
    path = _log_path(state_dir, run_id)
    if not path.is_file():
        return []
    events = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise HarnessError(f"cannot read log {run_id}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HarnessError(f"malformed log {run_id}:{number}") from exc
        if not isinstance(record, dict) or not isinstance(record.get("event"), str):
            raise HarnessError(f"malformed log {run_id}:{number}")
        redacted = redact(record)
        if not isinstance(redacted, dict):
            raise HarnessError(f"malformed log {run_id}:{number}")
        events.append(redacted)
    return events


def _log_path(state_dir, run_id: str) -> Path:
    return run_path(state_dir, run_id).with_suffix(".jsonl")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
