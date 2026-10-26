"""Process transport seam and the process-tree kill primitive.

``SubprocessTransport`` is exercised only against local helper scripts. It
refuses every known provider executable, so no live provider can start through
it until CSL-460 flips ``LIVE_ADAPTERS_ENABLED``.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from scripts.v3.route_contract.errors import Unsupported
from scripts.v3.route_contract.live import LIVE_ADAPTERS_ENABLED, LIVE_ENGINE_BINARIES

REAP_GRACE_SECONDS = 5.0
KILL_GRACE_SECONDS = 10.0
TREE_POLL_SECONDS = 2.0
_CMD_METACHARACTERS = frozenset('&|<>^"')


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    termination: dict[str, object] | None = field(default=None, compare=False)


class Transport(Protocol):
    def run(
        self, argv: list[str], input_text: str, timeout_s: float, cwd: str | None
    ) -> ProcResult: ...


def resolve_executable(argv: list[str]) -> list[str]:
    """Resolve ``argv[0]`` to a path. A cmd shim is refused if an argument could break out."""

    if not argv:
        return argv
    resolved = shutil.which(argv[0])
    if resolved is None:
        raise FileNotFoundError(f"{argv[0]!r} is not on PATH")
    if resolved.lower().endswith((".cmd", ".bat")):
        for arg in argv[1:]:
            if _CMD_METACHARACTERS.intersection(arg):
                raise ValueError("cmd metacharacter in argument of a cmd shim")
    return [resolved, *argv[1:]]


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_tree(proc: subprocess.Popen) -> dict[str, object]:
    """Kill the child and its process group. Return a ProcessTerminationObservation@1 body.

    ``tree_status`` covers the child's own process group (POSIX) or the
    taskkill tree (Windows, unverified in CI). A descendant that left the
    group is not observable here and is not claimed.
    """

    pid = proc.pid
    status = "unknown"
    remaining: list[str] = []
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=KILL_GRACE_SECONDS,
                check=False,
            )
        else:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
            deadline = time.monotonic() + TREE_POLL_SECONDS
            while _group_alive(pgid) and time.monotonic() < deadline:
                try:
                    proc.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            if _group_alive(pgid):
                status, remaining = "partial", [f"pgid:{pgid}"]
            else:
                status = "terminated"
    except Exception:  # noqa: BLE001 - cleanup stays fail-closed; status stays unknown
        status = "unknown"
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 - an already-exited child is harmless
        pass
    return {
        "type": "ProcessTerminationObservation@1",
        "tree_status": status,
        "remaining_process_refs": remaining,
        "effect_conclusion": "not-evaluated",
    }


class SubprocessTransport:
    """Spawn one local process, feed stdin, bound the whole tree by ``timeout_s``."""

    def run(
        self, argv: list[str], input_text: str, timeout_s: float, cwd: str | None
    ) -> ProcResult:
        if not LIVE_ADAPTERS_ENABLED and argv and Path(argv[0]).stem.lower() in LIVE_ENGINE_BINARIES:
            raise Unsupported()
        argv = resolve_executable(argv)
        popen_kwargs: dict[str, object] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "cwd": cwd,
            "env": os.environ.copy(),
        }
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **popen_kwargs)  # noqa: S603
        try:
            out, err = proc.communicate(input_text, timeout=timeout_s)
            return ProcResult(proc.returncode, out or "", err or "", False)
        except subprocess.TimeoutExpired:
            observation = kill_tree(proc)
            try:
                out, err = proc.communicate(timeout=REAP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            return ProcResult(124, out, err + "\n[timeout]", True, observation)
