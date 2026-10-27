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
import threading
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
_READ_CHUNK = 4096


# Transport-owned stderr suffix on a timeout; not engine output.
TIMEOUT_DIAGNOSTIC = "\n[timeout]"


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    termination: dict[str, object] | None = field(default=None, compare=False)
    oversized: bool = False
    # Raw stdout+stderr bytes read from the process, before decoding and
    # without TIMEOUT_DIAGNOSTIC. None when the transport did not measure it.
    raw_output_bytes: int | None = field(default=None, compare=False)


class Transport(Protocol):
    def run(
        self,
        argv: list[str],
        input_text: str,
        timeout_s: float,
        cwd: str | None,
        max_output_bytes: int | None = None,
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


def _names_for(path: str) -> set[str]:
    names = {Path(path).stem.lower()}
    try:
        names.add(Path(os.path.realpath(path)).stem.lower())
    except OSError:
        pass
    return names


def refuses_live_executable(argv: list[str]) -> bool:
    """True when argv names a known provider CLI, including after symlink resolution."""

    if LIVE_ADAPTERS_ENABLED or not argv:
        return False
    names = _names_for(argv[0])
    located = shutil.which(argv[0])
    if located is not None:
        names |= _names_for(located)
    return bool(names & LIVE_ENGINE_BINARIES)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_tree(proc: subprocess.Popen, pgid: int | None = None) -> dict[str, object]:
    """Kill the child and its process group. Return a ProcessTerminationObservation@1 body.

    ``tree_status`` covers the child's own process group (POSIX) or the
    taskkill tree (Windows, unverified in CI). A descendant that left the
    group is not observable here and is not claimed. ``pgid`` is the group
    captured while the leader was alive; after the leader exits ``getpgid``
    cannot recover it.
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
            group = pgid
            if group is None:
                group = os.getpgid(pid)
            os.killpg(group, signal.SIGKILL)
            deadline = time.monotonic() + TREE_POLL_SECONDS
            while _group_alive(group) and time.monotonic() < deadline:
                try:
                    proc.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            if _group_alive(group):
                status, remaining = "partial", [f"pgid:{group}"]
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


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _communicate_bounded(
    proc: subprocess.Popen,
    input_text: str,
    timeout_s: float,
    max_output_bytes: int | None,
    pgid: int | None,
) -> ProcResult:
    """Read stdout/stderr up to ``max_output_bytes`` combined, then kill if exceeded.

    Stdin is written on its own thread, started with the output readers, so a
    child that fills stdout before reading a large prompt cannot block the
    deadline. The timeout clock starts before any pipe write.
    """

    out = bytearray()
    err = bytearray()
    lock = threading.Lock()
    exceeded = threading.Event()
    cap = max_output_bytes if max_output_bytes is not None else None

    def writer() -> None:
        stream = proc.stdin
        if stream is None:
            return
        try:
            payload = b""
            if input_text:
                payload = input_text.encode("utf-8") if isinstance(input_text, str) else input_text
            view = memoryview(payload)
            while len(view):
                if exceeded.is_set():
                    break
                chunk = bytes(view[:_READ_CHUNK])
                stream.write(chunk)
                view = view[len(chunk):]
            try:
                stream.flush()
            except OSError:
                pass
            stream.close()
        except OSError:
            pass

    def reader(stream, dest: bytearray) -> None:
        try:
            while True:
                # read1 returns bytes already available. BufferedReader.read waits
                # until the chunk is full or the pipe closes, so a short write
                # past the cap would sit until the deadline.
                chunk = stream.read1(_READ_CHUNK)
                if not chunk:
                    break
                with lock:
                    dest.extend(chunk)
                    if cap is not None and len(out) + len(err) > cap:
                        exceeded.set()
                        break
                if exceeded.is_set():
                    break
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - already-closed pipe is harmless
                pass

    threads = [
        threading.Thread(target=reader, args=(proc.stdout, out), daemon=True),
        threading.Thread(target=reader, args=(proc.stderr, err), daemon=True),
        threading.Thread(target=writer, daemon=True),
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout_s
    while True:
        alive = any(thread.is_alive() for thread in threads) or proc.poll() is None
        if exceeded.is_set():
            observation = kill_tree(proc, pgid=pgid)
            for thread in threads:
                thread.join(timeout=REAP_GRACE_SECONDS)
            return ProcResult(1, "", "", False, observation, oversized=True)
        if time.monotonic() >= deadline:
            observation = kill_tree(proc, pgid=pgid)
            for thread in threads:
                thread.join(timeout=REAP_GRACE_SECONDS)
            return ProcResult(
                124, _decode(bytes(out)), _decode(bytes(err)) + TIMEOUT_DIAGNOSTIC, True, observation,
                raw_output_bytes=len(out) + len(err),
            )
        if not alive:
            break
        time.sleep(0.02)

    for thread in threads:
        thread.join()
    if cap is not None and len(out) + len(err) > cap:
        observation = kill_tree(proc, pgid=pgid)
        return ProcResult(1, "", "", False, observation, oversized=True)
    returncode = proc.wait()
    return ProcResult(
        returncode, _decode(bytes(out)), _decode(bytes(err)), False, raw_output_bytes=len(out) + len(err)
    )


class SubprocessTransport:
    """Spawn one local process, feed stdin, bound the whole tree by ``timeout_s``."""

    def run(
        self,
        argv: list[str],
        input_text: str,
        timeout_s: float,
        cwd: str | None,
        max_output_bytes: int | None = None,
    ) -> ProcResult:
        if refuses_live_executable(argv):
            raise Unsupported()
        argv = resolve_executable(argv)
        if refuses_live_executable(argv):
            raise Unsupported()
        popen_kwargs: dict[str, object] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": cwd,
            "env": os.environ.copy(),
        }
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **popen_kwargs)  # noqa: S603
        pgid = None
        if os.name != "nt":
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = None
        return _communicate_bounded(proc, input_text, timeout_s, max_output_bytes, pgid)
