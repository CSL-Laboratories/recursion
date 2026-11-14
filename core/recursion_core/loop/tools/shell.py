"""The shell tool: run one command in the workspace. Asks first. Also the runner the acceptance check uses.

The command runs under ``bash -c`` (``sh -c`` where there is no bash), in its own process group, with no
standard input, in the workspace. Its environment is the engine's own (which the child-environment policy
builds in a run unit) minus every key name the loop knows, with pagers and git's credential prompt turned
off so nothing waits for a terminal. At its deadline or on an interrupt the whole group is killed; so are
background processes it left holding the output open once it exits. Output (standard output and standard
error together, in order) is kept up to its first and last 4 MiB; the rest is counted, and the registry's
cap and spill take it from there. The result starts with the exit status and the time taken (as Codex
reports them; vault record 20261028-recursion-frontier-b4-tool-output-spill-04, section 6).
"""

from __future__ import annotations

import collections
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from recursion_core.loop.providers import base as b
from recursion_core.loop.tools.registry import ERROR, OK, Tool, ToolContext, ToolOutput

DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600
KEEP_HEAD = 4 * 1024 * 1024
KEEP_TAIL = 4 * 1024 * 1024
DRAIN_GRACE_S = 1.0
KEY_ENV = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN")

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


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in KEY_ENV}
    env.update({"PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "TERM": "dumb"})
    return env


class _Capture:
    def __init__(self, stream) -> None:
        self.stream = stream
        self.head = bytearray()
        self.tail: collections.deque[bytes] = collections.deque()
        self.tail_size = 0
        self.dropped = 0
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self) -> None:
        while True:
            try:
                chunk = self.stream.read1(65536) if hasattr(self.stream, "read1") else self.stream.read(65536)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            room = KEEP_HEAD - len(self.head)
            if room > 0:
                self.head += chunk[:room]
                chunk = chunk[room:]
            if chunk:
                self.tail.append(chunk)
                self.tail_size += len(chunk)
                while self.tail and self.tail_size - len(self.tail[0]) >= KEEP_TAIL:
                    gone = self.tail.popleft()
                    self.tail_size -= len(gone)
                    self.dropped += len(gone)

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


def run_command(command: str, cwd: str, cancel: b.CancelToken, timeout_s: float) -> Ran:
    shell = shutil.which("bash") or "/bin/sh"
    start = time.monotonic()
    proc = subprocess.Popen([shell, "-c", command], cwd=cwd, env=child_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    capture = _Capture(proc.stdout)
    unregister = cancel.on_cancel(lambda: _kill_group(proc))
    timed_out = False
    try:
        try:
            proc.wait(timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            proc.wait()
        capture.thread.join(DRAIN_GRACE_S)
        if capture.thread.is_alive():  # something it started still holds the output open
            _kill_group(proc)
            capture.thread.join()
    finally:
        unregister()
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
