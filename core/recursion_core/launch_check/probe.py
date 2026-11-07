"""Zero-egress version probe for R2a (CSL-732, candidate).

The probe runs ``<binary> --version`` with:

* no shell, argv as a list, ``close_fds``, a new session (so a timeout kills the group);
* stdin closed (``/dev/null``), stdout and stderr to private temp files read up to a cap;
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


def run_version_probe(spec: EngineSpec, realpath: str, timeout: float = PROBE_TIMEOUT_S) -> ProbeResult:
    """Run ``realpath --version`` under the zero-egress rules above."""
    home = tempfile.mkdtemp(prefix="r2a-probe-")
    try:
        with open(os.path.join(home, ".out"), "w+b") as out, open(os.path.join(home, ".err"), "w+b") as err:
            t0 = time.monotonic()
            try:
                proc = subprocess.Popen(
                    [realpath, "--version"],
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    env=probe_env(spec, home),
                    cwd=home,
                    close_fds=True,
                    shell=False,
                    start_new_session=(os.name != "nt"),
                )
            except OSError as e:
                raise LaunchRefused("binary_missing", spec.engine, f"spawn failed: {e.__class__.__name__}") from None
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                raise LaunchRefused("version_probe_failed", spec.engine, f"--version exceeded {timeout}s") from None
            if os.name != "nt":  # reap anything the probe left behind in its session
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            ms = round((time.monotonic() - t0) * 1000)
            sizes = (os.fstat(out.fileno()).st_size, os.fstat(err.fileno()).st_size)
            if max(sizes) > PROBE_OUTPUT_CAP:
                raise LaunchRefused("version_probe_failed", spec.engine, f"--version output over {PROBE_OUTPUT_CAP} bytes")
            out.seek(0)
            err.seek(0)
            raw = _first_line(out.read()) or _first_line(err.read())
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return ProbeResult(raw, code, ms)
