"""Zero-egress version probe for R2a (CSL-732, candidate).

The probe runs ``<binary> --version`` with:

* no shell, argv as a list, ``close_fds``, a new session (so a timeout kills the group);
* stdin closed (``/dev/null``), stdout and stderr read from pipes into memory up to a cap while
  the probe runs; past the cap the group is killed at once, so output never reaches disk;
* a minimal environment built from nothing: ``PATH`` fixed to system directories,
  ``HOME``, ``TMPDIR`` and ``XDG_*`` inside a fresh private temp directory, ``LC_ALL=C``,
  plus fixed per-engine update switches. No proxy, credential, token, config-path or
  ``CLI_VERSION`` variable from the parent can reach it, because nothing is copied;
* a timeout (``version_probe_failed``).

Zero egress here means the probe gets nothing that would let it authenticate or reach
a configured proxy, and an empty home with no config. It is not a kernel network
block; a network namespace belongs to the launch sandbox (B6), not this check.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from recursion_core.launch_check.floors import EngineSpec
from recursion_core.launch_check.refusals import LaunchRefused

PROBE_TIMEOUT_S = 20.0
PROBE_OUTPUT_CAP = 64 * 1024
SYSTEM_PATH = "/usr/bin:/bin"

# Names the probe environment may contain; tests assert the environment is a subset.
PROBE_ENV_BASE_NAMES = frozenset(
    {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
     "XDG_DATA_HOME", "XDG_STATE_HOME", "NO_COLOR", "TERM"}
)


@dataclass(frozen=True)
class ProbeResult:
    raw: str  # first non-empty line, stdout before stderr, stripped
    exit_code: int
    ms: int


Probe = Callable[[EngineSpec, str, float], ProbeResult]


def probe_env(spec: EngineSpec, home: str) -> dict[str, str]:
    """The whole probe environment. Built from constants and ``home`` only."""
    env = {
        "PATH": SYSTEM_PATH,
        "HOME": home,
        "TMPDIR": home,
        "LANG": "C",
        "LC_ALL": "C",
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "XDG_CACHE_HOME": os.path.join(home, ".cache"),
        "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
        "XDG_STATE_HOME": os.path.join(home, ".local", "state"),
        "NO_COLOR": "1",
        "TERM": "dumb",
    }
    env.update(dict(spec.probe_env))
    return env


def _first_line(data: bytes) -> str:
    for line in data.decode("utf-8", "replace").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError):
        proc.kill()
    proc.wait()


class _CappedReader(threading.Thread):
    """Read one pipe into memory, keeping at most ``PROBE_OUTPUT_CAP + 1`` bytes; flag when over."""

    def __init__(self, stream, over: threading.Event) -> None:
        super().__init__(daemon=True)
        self.stream, self.over, self.data = stream, over, bytearray()

    def run(self) -> None:
        try:
            while chunk := self.stream.read1(65536):
                room = PROBE_OUTPUT_CAP + 1 - len(self.data)
                if room > 0:
                    self.data += chunk[:room]
                if len(self.data) > PROBE_OUTPUT_CAP:
                    self.over.set()
                    return  # stop reading; the probe is killed and the pipe closed
        except (OSError, ValueError):
            pass


_POLL_S = 0.01


def run_version_probe(spec: EngineSpec, realpath: str, timeout: float = PROBE_TIMEOUT_S) -> ProbeResult:
    """Run ``realpath --version`` under the zero-egress rules above."""
    home = tempfile.mkdtemp(prefix="r2a-probe-")
    try:
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                [realpath, "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=probe_env(spec, home),
                cwd=home,
                close_fds=True,
                shell=False,
                start_new_session=(os.name != "nt"),
            )
        except OSError as e:
            raise LaunchRefused("binary_missing", spec.engine, f"spawn failed: {e.__class__.__name__}") from None
        over = threading.Event()
        readers = [_CappedReader(proc.stdout, over), _CappedReader(proc.stderr, over)]
        for r in readers:
            r.start()
        try:
            deadline = t0 + timeout
            while (code := proc.poll()) is None:
                if over.is_set():
                    _kill_group(proc)
                    raise LaunchRefused("version_probe_failed", spec.engine,
                                        f"--version output over {PROBE_OUTPUT_CAP} bytes")
                if time.monotonic() >= deadline:
                    _kill_group(proc)
                    raise LaunchRefused("version_probe_failed", spec.engine, f"--version exceeded {timeout}s")
                over.wait(_POLL_S)
            if os.name != "nt":  # reap anything the probe left behind in its session
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            for r in readers:
                r.join(1.0)
            ms = round((time.monotonic() - t0) * 1000)
            if over.is_set() or any(len(r.data) > PROBE_OUTPUT_CAP for r in readers):
                raise LaunchRefused("version_probe_failed", spec.engine, f"--version output over {PROBE_OUTPUT_CAP} bytes")
            raw = _first_line(bytes(readers[0].data)) or _first_line(bytes(readers[1].data))
        finally:
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return ProbeResult(raw, code, ms)
