"""Per-provider cooldowns stored with the run state.

The file is ``<state-dir>/cooldowns.json``. It stores a reason and a reset
time. Process output is not stored. Later runs in the same state directory
skip a provider until the reset time.
"""

from __future__ import annotations

import calendar
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from scripts.v3.harness.errors import HarnessError


DEFAULT_COOLDOWN_SECONDS = 60
_LOCK = threading.Lock()


class CooldownStore:
    """One JSON file of active provider cooldowns."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def active(self, now: float | None = None) -> dict[str, dict]:
        """Return cooldowns that have not expired. Expired rows are removed."""

        moment = time.time() if now is None else now
        with _LOCK:
            if not self.path.is_file():
                return {}
            with _file_lock(self.path.parent / ".cooldowns.lock"):
                payload = self._read()
                providers = payload["providers"]
                kept = {}
                for name, item in providers.items():
                    if not isinstance(item, dict):
                        continue
                    until = item.get("until")
                    epoch = _parse(until)
                    if epoch is None or epoch > moment:
                        kept[name] = {"reason": item.get("reason"), "until": until}
                if set(kept) != set(providers):
                    self._write({"providers": kept})
                return kept

    def record(self, name: str, reason: str, until_epoch: float) -> None:
        until = format_until(until_epoch)
        with _LOCK:
            with _file_lock(self.path.parent / ".cooldowns.lock"):
                payload = self._read() if self.path.is_file() else {"providers": {}}
                providers = payload["providers"]
                providers[name] = {"reason": reason, "until": until}
                self._write({"providers": providers})

    def _read(self) -> dict:
        return _read_payload(self.path)

    def _write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


def format_until(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))


def resolve_until(
    until: str | None,
    retry_after: float | None,
    now: float,
    cooldown_seconds: float,
) -> float:
    """Prefer a reported reset time, then a retry delay, then the config cooldown."""

    parsed = _parse(until)
    if parsed is not None:
        return parsed
    if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool) and retry_after >= 0:
        return now + float(retry_after)
    return now + float(cooldown_seconds)


def _parse(text: object) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def _read_payload(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HarnessError("malformed cooldown file") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("providers"), dict):
        raise HarnessError("malformed cooldown file")
    return payload


@contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) != b"\0":
                handle.seek(0)
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            import msvcrt

            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
