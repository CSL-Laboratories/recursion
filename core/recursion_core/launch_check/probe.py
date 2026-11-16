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

import math
import os
import select
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
MAX_PROBE_TIMEOUT_S = 3600.0  # the longest probe a caller may ask for; any deadline under it is representable
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
    """The first line that is not blank, stdout's before stderr's (the caller's order).

    Lines are framed by LF; a CR just before the LF belongs to the framing, and only spaces around the
    line are trimmed. Every other control character (a tab, a lone CR, a vertical tab, an escape) stays
    in the line, so ``parse_version`` sees it and refuses the line, instead of a split or a strip
    removing it first (#424 round 1, Codex sol R2).
    """
    for line in data.decode("utf-8", "replace").split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        if line.strip(" "):
            return line.strip(" ")
    return ""


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError):
        proc.kill()
    proc.wait()


class _CappedReader(threading.Thread):
    """Read one pipe into memory, keeping at most ``PROBE_OUTPUT_CAP + 1`` bytes; flag when over.

    On POSIX the pipe is polled with ``select`` so ``stop`` always ends the thread, even when a
    process that left the probe's session still holds the write end open. The stream is closed
    only after the thread has ended, so closing never waits on a blocked read.
    """

    def __init__(self, stream, over: threading.Event) -> None:
        super().__init__(daemon=True)
        self.stream, self.over, self.data = stream, over, bytearray()
        self.stop = threading.Event()
        self.eof = False

    def _keep(self, chunk: bytes) -> bool:
        room = PROBE_OUTPUT_CAP + 1 - len(self.data)
        if room > 0:
            self.data += chunk[:room]
        if len(self.data) > PROBE_OUTPUT_CAP:
            self.over.set()
            return False  # stop reading; the probe is killed and the pipe closed
        return True

    def run(self) -> None:
        try:
            if os.name == "nt":
                while chunk := self.stream.read1(65536):
                    if not self._keep(chunk):
                        return
                self.eof = True
                return
            fd = self.stream.fileno()
            while not self.stop.is_set():
                ready, _, _ = select.select([fd], [], [], _POLL_S)
                if not ready:
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    self.eof = True
                    return
                if not self._keep(chunk):
                    return
        except (OSError, ValueError):
            pass


_POLL_S = 0.01


def check_timeout(spec: EngineSpec, timeout: object) -> None:
    """A probe timeout is an ``int`` or ``float`` (not a bool), above zero and at most ``MAX_PROBE_TIMEOUT_S``.

    A NaN deadline is never reached and an infinite one never ends, so either would let a probe that
    never exits hold the check for ever; an integer too large for a float would raise ``OverflowError``
    in the deadline arithmetic (#424 round 1). All are refused before anything runs. An ``int`` is
    compared exactly, with no conversion to float.
    """
    if type(timeout) is int:
        ok = 0 < timeout <= MAX_PROBE_TIMEOUT_S
    elif type(timeout) is float:
        ok = math.isfinite(timeout) and 0 < timeout <= MAX_PROBE_TIMEOUT_S
    else:
        ok = False
    if not ok:
        shown = repr(timeout) if type(timeout) in (int, float) and len(repr(timeout)) <= 40 else f"<{type(timeout).__name__}>"
        raise LaunchRefused("version_probe_failed", spec.engine,
                            f"probe timeout must be seconds above 0 and at most {MAX_PROBE_TIMEOUT_S:g}, not {shown}")


def run_version_probe(spec: EngineSpec, realpath: str, timeout: float = PROBE_TIMEOUT_S) -> ProbeResult:
    """Run ``realpath --version`` under the zero-egress rules above."""
    check_timeout(spec, timeout)
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
        # From here on every exit, ordinary or not (an interrupt, a poll or thread error, a reader that
        # cannot start), passes the ``finally`` below, which releases what Popen made: the child's session
        # is killed and reaped if it has not ended, the readers end and the pipes close (#424 round 1,
        # Codex astra R1, DeepSeek F2). The initiating exception is re-raised unchanged.
        readers: list[_CappedReader] = []
        try:
            over = threading.Event()
            for stream in (proc.stdout, proc.stderr):
                readers.append(_CappedReader(stream, over))
                readers[-1].start()
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
            drain_until = max(deadline, time.monotonic() + 1.0)
            for r in readers:
                r.join(max(0.0, drain_until - time.monotonic()))
            held = [r for r in readers if r.is_alive()]
            for r in held:
                r.stop.set()
                r.join(1.0)
            ms = round((time.monotonic() - t0) * 1000)
            if over.is_set() or any(len(r.data) > PROBE_OUTPUT_CAP for r in readers):
                raise LaunchRefused("version_probe_failed", spec.engine, f"--version output over {PROBE_OUTPUT_CAP} bytes")
            if held:
                raise LaunchRefused("version_probe_failed", spec.engine,
                                    "--version exited but a process outside its session kept the output open")
            raw = _first_line(bytes(readers[0].data)) or _first_line(bytes(readers[1].data))
        finally:
            if proc.returncode is None:  # not reaped: the loop was left before the child ended
                try:
                    _kill_group(proc)
                except Exception:  # noqa: BLE001 - cleanup never replaces the exception that is leaving
                    pass
            for r in readers:
                r.stop.set()
                if r.ident is not None:  # a reader whose start failed was never running
                    r.join(1.0)
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return ProbeResult(raw, code, ms)
