"""The shell tool: run one command in the workspace. Asks first. Also the runner the acceptance check uses.

The command runs under ``bash -c`` (``sh -c`` where there is no bash), in its own process group and session, with
no standard input, in the workspace. On Linux it runs under a keeper of its own (``reaper.py``), which is made the
subreaper of everything the command starts, so a process that leaves the group or the session (``setsid``, a
double fork) is still followed; a keeper that cannot make itself the subreaper does not start the command (exit
status 125, with a line saying why). Its environment is not the engine's: it is built from a short list of names
(``ENV_PASSED``: the search path, the user, the locale, the time zone, the temporary folder), with pagers and
git's credential prompt turned off so nothing waits for a terminal; nothing else of the engine's environment
reaches it (no key, no proxy setting, no ``BASH_ENV`` or ``ENV`` that a shell would run at its start).

One deadline, counted from the start, covers the whole call: the command, and the reading of its output after it
ends. A command that is cancelled before it starts is not started. When the command ends, when its deadline
passes, or on an interrupt, everything it started is stopped (the keeper kills its descendants; the process
group is killed as well), and reading the output stops within a bounded time even if something the loop could
not stop still holds it open. A process that made itself a subreaper of its own, or that runs as another user, is
outside this reach; nothing here claims more.

Output (standard output and standard error together, in order) is kept up to its first and last 4 MiB, exactly;
the rest is counted, and the registry's cap and spill take it from there. The result starts with the exit status
and the time taken (as Codex reports them; vault record 20261028-recursion-frontier-b4-tool-output-spill-04,
section 6).
"""

from __future__ import annotations

import collections
import os
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recursion_core.loop.providers import base as b
from recursion_core.loop.tools.registry import ERROR, OK, Tool, ToolContext, ToolOutput

DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600
KEEP_HEAD = 4 * 1024 * 1024
KEEP_TAIL = 4 * 1024 * 1024
DRAIN_GRACE_S = 1.0  # how long reading goes on for output already written, once everything was stopped
STOP_GRACE_S = 2.0  # how long the keeper gets to stop the command's descendants before its group is killed
POLL_S = 0.05
CHUNK = 65536
# The environment a command gets: these names when the engine has them, and nothing else (astra R2).
ENV_PASSED = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TEMP", "TMP")
ENV_PREFIXES = ("LC_",)
ENV_FIXED = {"PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "TERM": "dumb"}
REAPER = Path(__file__).with_name("reaper.py")

SPEC = b.ToolSpec(
    name="shell",
    description=(
        "Run a shell command (bash) in the workspace root and return its exit status and output (standard output "
        "and standard error together). There is no standard input and no terminal: do not start editors, pagers "
        "or anything that asks a question. The command is stopped at its timeout (default 120 s, at most 600) and "
        "background processes are stopped when it ends. The user is asked to approve each command."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "minLength": 1, "description": "The command line to run."},
            "description": {"type": "string", "maxLength": 200,
                            "description": "In a few words, what the command is for (shown to the user)."},
            "timeout_s": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_S,
                          "description": "Seconds before the command is stopped (default 120)."},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
)


@dataclass(frozen=True)
class Ran:
    exit_code: int | None
    output: str
    elapsed_s: float
    timed_out: bool
    cancelled: bool
    dropped_bytes: int


def child_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a command (and the acceptance check) gets: the names in ENV_PASSED and the LC_ ones, when
    present, and the fixed settings; nothing else."""

    source = os.environ if environ is None else environ
    env = {k: v for k, v in source.items() if k in ENV_PASSED or k.startswith(ENV_PREFIXES)}
    env.setdefault("PATH", os.defpath)
    env.update(ENV_FIXED)
    return env


def _use_reaper() -> bool:
    return sys.platform.startswith("linux") and bool(sys.executable) and REAPER.is_file()


class _Capture:
    """Reads a stream on a thread of its own, keeping the first KEEP_HEAD and the last KEEP_TAIL bytes. Reading
    a pipe waits at most POLL_S at a time, so ``stop`` ends it within a bounded time even when a process the loop
    could not stop still holds the pipe open. A pipe the selector cannot wait on is made non-blocking and polled
    (the read never waits, so neither does closing the stream); one that can be neither is refused, and the
    command is stopped (astra C1). A stream with no descriptor at all (in memory) is read plainly."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self.head = bytearray()
        self.tail: collections.deque[bytes] = collections.deque()
        self.tail_size = 0
        self.dropped = 0
        self._stop = threading.Event()
        self._selector = None
        try:
            self._fd = stream.fileno()
        except (OSError, ValueError, AttributeError):
            self._fd = None  # no descriptor (an in-memory stream): it is read plainly, and never waits
        if self._fd is not None:
            try:
                self._selector = selectors.DefaultSelector()
                self._selector.register(self._fd, selectors.EVENT_READ)
            except (OSError, ValueError):
                if self._selector is not None:
                    self._selector.close()
                    self._selector = None
                os.set_blocking(self._fd, False)  # an OSError here refuses the pipe: run_command stops the command
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _read(self) -> bytes | None:
        """The next chunk, b"" at the end, None when nothing came within POLL_S."""

        if self._fd is None:
            return self.stream.read1(CHUNK) if hasattr(self.stream, "read1") else self.stream.read(CHUNK)
        if self._selector is None:
            try:
                return os.read(self._fd, CHUNK)  # non-blocking
            except BlockingIOError:
                self._stop.wait(POLL_S)
                return None
        if not self._selector.select(POLL_S):
            return None
        return os.read(self._fd, CHUNK)

    def _pump(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    chunk = self._read()
                except (OSError, ValueError):
                    return
                if chunk is None:
                    continue
                if not chunk:
                    return
                self._keep(chunk)
        finally:
            if self._selector is not None:
                self._selector.close()

    def _keep(self, chunk: bytes) -> None:
        room = KEEP_HEAD - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if not chunk:
            return
        self.tail.append(chunk)
        self.tail_size += len(chunk)
        while self.tail_size > KEEP_TAIL:  # the oldest bytes go, a part of a chunk too (sol R9)
            excess = self.tail_size - KEEP_TAIL
            first = self.tail[0]
            if len(first) <= excess:
                self.tail.popleft()
                gone = len(first)
            else:
                self.tail[0] = first[excess:]
                gone = excess
            self.tail_size -= gone
            self.dropped += gone

    def wait(self, timeout: float) -> bool:
        """Wait up to ``timeout`` for the end of the output; True when it was reached."""

        self.thread.join(max(0.0, timeout))
        return not self.thread.is_alive()

    def stop(self) -> None:
        """Stop reading, whatever still holds the stream; returns once the reading thread has ended (a pipe) or
        after a bounded wait (a stream that cannot be waited on)."""

        self._stop.set()
        self.thread.join(DRAIN_GRACE_S + 2 * POLL_S)

    def text(self) -> str:
        tail = b"".join(self.tail)
        middle = f"\n[{self.dropped} bytes of output were not kept]\n".encode() if self.dropped else b""
        return (bytes(self.head) + middle + tail).decode("utf-8", errors="replace")


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _ask_stop(proc: subprocess.Popen, reaper: bool) -> None:
    """Tell the command to stop at once: its keeper stops everything it started; without one, its group is
    killed. Does not wait (an interrupt calls this from the input thread)."""

    if not reaper:
        _kill_group(proc)
        return
    try:
        proc.send_signal(signal.SIGTERM)
    except OSError:
        pass


def _stop(proc: subprocess.Popen, reaper: bool) -> None:
    """Stop the command and everything it started, and wait (bounded) for it to end."""

    _ask_stop(proc, reaper)
    try:
        proc.wait(STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    _kill_group(proc)  # what the keeper could not stop in time, and anything left in the group
    try:
        proc.wait(STOP_GRACE_S)
    except subprocess.TimeoutExpired:
        pass


def run_command(command: str, cwd: str, cancel: b.CancelToken, timeout_s: float) -> Ran:
    start = time.monotonic()
    if cancel.cancelled:
        return Ran(None, "", 0.0, False, True, 0)  # cancelled before it started: it never starts (sol R2)
    deadline = start + timeout_s
    shell = shutil.which("bash") or "/bin/sh"
    reaper = _use_reaper()
    argv = [shell, "-c", command]
    if reaper:
        argv = [sys.executable, "-I", "-S", "-B", str(REAPER), *argv]
    proc = subprocess.Popen(argv, cwd=cwd, env=child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    capture: _Capture | None = None
    unregister = None
    timed_out = False
    try:
        capture = _Capture(proc.stdout)
        unregister = cancel.on_cancel(lambda: _ask_stop(proc, reaper))  # at once if cancelled since the check
        try:
            proc.wait(max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = not cancel.cancelled
        if timed_out or cancel.cancelled:
            _stop(proc, reaper)
            capture.wait(DRAIN_GRACE_S)
        else:
            # It ended by itself. What it left running is stopped now (sol R1; the keeper already did), and the
            # rest of its output is read within the same deadline.
            _kill_group(proc)
            if not capture.wait(deadline - time.monotonic()):
                timed_out = not cancel.cancelled
                capture.wait(DRAIN_GRACE_S)
    finally:
        if unregister is not None:
            unregister()
        if proc.poll() is None:
            _stop(proc, reaper)  # an exception on the way: nothing it started is left running
        if capture is not None:
            capture.stop()
        if proc.stdout is not None:
            proc.stdout.close()
    elapsed = time.monotonic() - start
    cancelled = cancel.cancelled and not timed_out
    code = None if (timed_out or cancelled) else proc.returncode
    return Ran(code, capture.text(), elapsed, timed_out, cancelled, capture.dropped)


def run(args: Mapping[str, Any], ctx: ToolContext) -> ToolOutput:
    timeout_s = int(args.get("timeout_s", DEFAULT_TIMEOUT_S))
    ran = run_command(args["command"], str(ctx.workspace.root), ctx.cancel, timeout_s)
    if ran.cancelled:
        raise b.Cancelled()
    if ran.timed_out:
        head = f"timed out after {timeout_s} s; the command and everything it started were stopped"
        return ToolOutput(f"{head}\n{ran.output}", ERROR)
    head = f"exit {ran.exit_code} after {ran.elapsed_s:.1f} s"
    return ToolOutput(f"{head}\n{ran.output}", OK if ran.exit_code == 0 else ERROR)


TOOL = Tool(SPEC, run, needs_approval=True)
