"""The two arms: an engine run as its own CLI (direct), and the same engine under Recursion.

Same model, route and effort in both (PROTOCOL.md, "Arms"). The direct arm reads tokens and cost
from the engine's own output; it is not a Recursion surface. The Recursion arm is not built yet:
preflight refuses with REFUSAL when seams are missing and NOT_BUILT when they are present.
"""
from __future__ import annotations

import errno
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from eval import PACKAGE_ROOT, REPO_ROOT
from eval import record as record_mod
from eval import scratch as scratch_mod

STANDIN_SCRIPT = PACKAGE_ROOT / "standin_engine.py"
ENGINES = ("standin", "claude_code")
# Claude Code's own flags, as `claude --help` (2.1.318) lists them: --effort and --permission-mode.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
# The only variables that may be passed from the runner's environment to an engine: where its sign-in
# lives and how it reaches the network. Anything else could change what the engine is asked to do (an
# effort, model or permission variable overrides the recorded flags) or the attempt's own runtime, so it
# is refused (PROTOCOL.md, section 1).
ALLOWED_PASS_ENV = ("CLAUDE_CONFIG_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE",
                    "NODE_EXTRA_CA_CERTS")
# Engine arguments that would set what a named setting sets; the named setting is the only way. A fallback
# model or a main-thread agent can change the model the run uses.
OVERRIDING_ARGS = ("--effort", "--model", "--permission-mode", "--settings", "--dangerously-skip-permissions",
                   "--allow-dangerously-skip-permissions", "--fallback-model", "--agent")
# Settings files Claude Code loads besides its flags (its documentation, "Settings files and precedence" and
# "Deploy managed settings"): the user file in its configuration folder, the task repository's shared and
# local files, and the machine's managed policy. Any of them can set the effort, the model, the permission
# mode or an environment variable over the recorded flags (an `env` entry replaces the inherited variable
# after launch, and CLAUDE_CODE_EFFORT_LEVEL outranks --effort), so the direct arm runs Claude Code only with
# none of them in effect (PROTOCOL.md, section 1).
MANAGED_SETTINGS = (Path("/etc/claude-code/managed-settings.json"), Path("/etc/claude-code/managed-settings.d"))
PROJECT_SETTINGS = (".claude/settings.json", ".claude/settings.local.json")
# How long, after the engine's session is empty, its leader may take to be reaped and its pipes to end.
CLEANUP_GRACE_S = 5.0


class SessionCleanupError(RuntimeError):
    """Processes of an engine's session were still alive when cleanup gave up, or its output pipes were still
    held open after the session was empty."""


INFRA_RESULT_ERRORS = {
    "rate_limit": "vendor_limit",
    "authentication_failed": "auth_failed",
    "oauth_token_expired": "auth_failed",
}

REFUSAL = (
    "recursion arm refused: it needs, on the evaluated base, (1) WORKSPACE's `workspace` accepted at "
    "run.start or session.start (seam S6), so the engine works in the task repository, and (2) EVENTS' "
    "usage events readable with events.read, so tokens come from Recursion and not from parsing output. "
    "Missing here: {missing}. The other arms run."
)
NOT_BUILT = (
    "recursion arm refused: the base has {found}, but the arm itself is lane EVAL's slice 3 and is not "
    "built yet. The other arms run."
)


class ArmRefused(RuntimeError):
    """The arm cannot run on this base or with these settings. The message says why."""


class EngineMissing(RuntimeError):
    pass


@dataclass
class EngineOutcome:
    exit: int | None
    signal: int | None
    timed_out: bool
    claimed_done: bool | None
    failure: str | None          # an error class, or None
    detail: str | None
    usage: dict
    stdout: bytes
    stderr: bytes
    seconds: float
    run_id: str | None = None
    trajectory_format: str | None = "stream-json"


def _finite_seconds(timeout_s: float) -> float:
    """The wait below has no deadline when ``timeout_s`` is NaN or Infinity, and a huge int overflows."""
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise ValueError(f"a time limit must be a finite number of seconds, got {timeout_s!r}")
    try:
        number = float(timeout_s)
    except OverflowError:
        raise ValueError("a time limit must be a finite number of seconds") from None
    if not math.isfinite(number):
        raise ValueError(f"a time limit must be finite, got {timeout_s!r}")
    return number


# Engines this process is still waiting on. The runner cancels them when it is interrupted, because the
# signal is delivered to the main thread while the wait sits in a worker.
_live_lock = threading.Lock()
_live: set = set()


def _track(process, *, cancel: bool = True) -> None:
    """Remember a process this run started.

    ``cancel`` is false for the grading command. An interrupt stops engines, not the hidden tests:
    killing those tests and recording the signal as the model failing them is the runner's own act.
    """
    process._csl_cancel_on_interrupt = bool(cancel)
    with _live_lock:
        _live.add(process)


def _untrack(process) -> None:
    with _live_lock:
        _live.discard(process)


def cancel_running() -> None:
    """Stop every engine still being waited on, each in the session this process started.

    A process tracked with cancel false (the grader) is left to finish.
    """
    with _live_lock:
        processes = [process for process in _live if getattr(process, "_csl_cancel_on_interrupt", False)]
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            _kill_session(process.pid, leader=process)
        except (SessionCleanupError, OSError, RuntimeError):
            try:
                process.kill()
            except OSError:
                pass


def _close_stream(stream) -> None:
    if stream is None or getattr(stream, "closed", False):
        return
    try:
        stream.close()
    except OSError:
        pass


def _reap(process, *, timeout: float) -> None:
    if process.poll() is not None:
        return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass


def _held_pipes_sentence(held: list) -> str:
    """The same sentence the ordinary path raises when a survivor still holds a pipe."""
    return (f"the engine's {' and '.join(held)} still open {CLEANUP_GRACE_S:g} s after its "
            "session was emptied: a process outside the session (one that started a session "
            "of its own) holds it")


def _join_readers(threads) -> list:
    """Wait up to the cleanup grace for readers that already started. Names still blocked come back."""
    deadline = time.monotonic() + CLEANUP_GRACE_S
    held = []
    for thread in threads:
        if thread.ident is None and not thread.is_alive():
            continue
        remaining = deadline - time.monotonic()
        if remaining > 0:
            thread.join(timeout=remaining)
        if thread.is_alive():
            held.append(thread.name)
    return held


def _abandon_unwatched(process, started_threads: list) -> list:
    """Popen succeeded and a reader never started, so the child and its pipes are still ours.

    Readers that did start are joined for the cleanup grace. A name that is still blocked is
    returned so the caller can note it; the pipe is not closed from here, because that close
    waits on the blocked read.
    """
    try:
        _kill_session(process.pid, leader=process)
    except (SessionCleanupError, OSError, RuntimeError):
        try:
            process.kill()
        except OSError:
            pass
    _reap(process, timeout=CLEANUP_GRACE_S)
    streams = {"stdout": process.stdout, "stderr": process.stderr, "stdin": process.stdin}
    owned = set(streams) - {thread.name for thread in started_threads}
    for name in owned:
        _close_stream(streams[name])
    return _join_readers(started_threads)


def run_process(argv: list, *, cwd: Path, env: dict, stdin: bytes | None, timeout_s: float,
                cancel_on_interrupt: bool = True) -> dict:
    """Run argv in its own session and wait for THAT process, at most ``timeout_s``.

    Then every process left in the session is killed, whatever process group it moved to (helpers the
    engine started, and on timeout the engine itself), so pipes close and nothing outlives the attempt
    inside the session. Waiting on the process rather than on end-of-output means a helper that holds
    stdout cannot turn a finished run into a timeout. A process that started a session of its own
    (setsid) is out of reach (PROTOCOL.md names it); if it still holds the engine's output when the session
    is empty, that is reported as a cleanup failure.

    No wait is unbounded. A cleanup failure is reported after the cleanup grace (``CLEANUP_GRACE_S``),
    including when a helper that started its own session still holds a pipe: that grace is the wait.
    Each reader thread closes its own pipe at the end of output (closing a pipe from here would wait for
    a read blocked on it); the threads are daemons, and the Popen object reaps the engine whenever it
    does exit.
    Times are monotonic.
    """
    # Session cleanup calls getsid. Without it, do not start a process this function cannot reap.
    if not hasattr(os, "getuid") or not hasattr(os, "getsid"):
        raise ArmRefused("platform_unsupported")
    if not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(part, str) for part in argv):
        raise EngineMissing("the command must be a non-empty list of strings")
    if stdin is not None and not isinstance(stdin, bytes):
        raise ValueError("stdin must be bytes")
    timeout_s = _finite_seconds(timeout_s)
    started = time.monotonic()
    process = None
    tracked = False
    try:
        try:
            process = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
        except FileNotFoundError as exc:
            raise EngineMissing(f"{argv[0]}: {exc}") from None
        _remember_leader(process)
        chunks: dict = {"stdout": [], "stderr": []}

        def read(name, pipe):
            try:
                for block in iter(lambda: pipe.read(65536), b""):
                    chunks[name].append(block)
            finally:
                try:
                    pipe.close()
                except OSError:
                    pass

        def feed():
            try:
                if stdin:
                    process.stdin.write(stdin)
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        threads = [threading.Thread(target=read, args=("stdout", process.stdout), name="stdout", daemon=True),
                   threading.Thread(target=read, args=("stderr", process.stderr), name="stderr", daemon=True),
                   threading.Thread(target=feed, name="stdin", daemon=True)]
        try:
            _track(process, cancel=cancel_on_interrupt)
            tracked = True
            started_threads: list = []
            try:
                for thread in threads:
                    thread.start()
                    started_threads.append(thread)
            except BaseException as exc:
                # No reader is blocked on a pipe that never started, so shutting it cannot wait.
                # A reader that did start may still be blocked on a helper outside the session.
                held = _abandon_unwatched(process, started_threads)
                if held:
                    exc.add_note(_held_pipes_sentence(held))
                raise
            timed_out = False
            try:
                process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
            except BaseException as exc:
                # An interruption is what is reported; the session is emptied and the leader reaped first.
                try:
                    _kill_session(process.pid, leader=process)
                except BaseException as cleanup:
                    exc.add_note(f"cleanup after the interruption: {cleanup}")
                try:
                    process.wait(timeout=CLEANUP_GRACE_S)
                except subprocess.TimeoutExpired:
                    exc.add_note(f"the engine (process {process.pid}) was not reaped "
                                 f"{CLEANUP_GRACE_S:g} s after its session was emptied")
                except BaseException:
                    try:
                        process.poll()
                    except Exception:
                        pass
                    if process.returncode is None:
                        try:
                            process.wait(timeout=CLEANUP_GRACE_S)
                        except (subprocess.TimeoutExpired, OSError):
                            pass
                        except BaseException:
                            pass
                    exc.add_note("a second interruption during the reap")
                    if process.returncode is None:
                        exc.add_note(f"the engine (process {process.pid}) was not reaped")
                held = _join_readers(threads)
                if held:
                    exc.add_note(_held_pipes_sentence(held))
                raise
            _kill_session(process.pid, leader=process)
            # The session is empty, so its leader, which can leave neither its session nor its process
            # group, has exited: this wait ends at once. It is bounded all the same.
            try:
                process.wait(timeout=CLEANUP_GRACE_S)
            except subprocess.TimeoutExpired:
                raise SessionCleanupError(
                    f"the engine (process {process.pid}) was not reaped {CLEANUP_GRACE_S:g} s after "
                    "its session was emptied") from None
            held = _join_readers(threads)
            if held:
                raise SessionCleanupError(_held_pipes_sentence(held))
            code = process.returncode
            return {
                "exit": code if code is not None and code >= 0 else None,
                "signal": -code if code is not None and code < 0 else None,
                "timed_out": timed_out,
                "stdout": b"".join(chunks["stdout"]),
                "stderr": b"".join(chunks["stderr"]),
                "seconds": time.monotonic() - started,
            }
        finally:
            if process is not None:
                _untrack(process)
    finally:
        if process is not None and not tracked:
            _stop_untracked(process)
        if process is not None:
            _forget_leader(process)


def _session_of(pid: int):
    """The session id of a process, from /proc (None when it is gone)."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None
    try:
        return int(fields[3]), fields[0]          # (session, state)
    except (ValueError, IndexError):
        return None


def _session_members(sid: int) -> list:
    members = []
    for name in os.listdir("/proc"):
        if name.isdigit():
            found = _session_of(int(name))
            if found is not None and found[0] == sid and found[1] not in ("Z", "X"):
                members.append(int(name))
    return members


def _still_in_session(pid: int, sid: int) -> bool:
    """True only when ``pid`` is still that session and not already dead.

    Checked again immediately before a signal to a member. This does not prove the number is the
    process that was started: a session id whose leader has been reaped may have been reused.
    The leader itself is signalled only through a pidfd opened at start.
    """
    found = _session_of(pid)
    return found is not None and found[0] == sid and found[1] not in ("Z", "X")


def _remember_leader(process) -> None:
    """Open a pidfd on the leader now, before its pid can be reused."""
    try:
        process._csl_pidfd = os.pidfd_open(process.pid)
    except OSError:
        process._csl_pidfd = None


def _forget_leader(process) -> None:
    fd = getattr(process, "_csl_pidfd", None)
    if isinstance(fd, int):
        process._csl_pidfd = None
        try:
            os.close(fd)
        except OSError:
            pass


def _stop_untracked(process) -> None:
    """End a process that exists and was never tracked, so an interrupt cannot leave it running."""
    fd = getattr(process, "_csl_pidfd", None)
    if isinstance(fd, int):
        try:
            signal.pidfd_send_signal(fd, signal.SIGKILL)
        except OSError:
            pass
    else:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=CLEANUP_GRACE_S)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _signal_leader(leader) -> bool:
    """Signal the leader through the pidfd opened when it started.

    False when that pidfd is gone (the leader was reaped, so the numeric id may belong to someone
    else) or when no leader was supplied. A bare number is not signalled.
    """
    if leader is None:
        return False
    fd = getattr(leader, "_csl_pidfd", None)
    if not isinstance(fd, int):
        if leader.poll() is not None:
            return False
        try:
            leader.kill()
        except OSError:
            return False
        return True
    try:
        signal.pidfd_send_signal(fd, signal.SIGKILL)
    except OSError as exc:
        if exc.errno in (errno.ESRCH, errno.EPERM, errno.EINVAL):
            return False
        raise
    return True


def _kill_session(sid: int, leader=None) -> None:
    """Kill every live process of session ``sid``, in any process group, rescanning until none is left.

    A process forked between a scan and a kill is found by the next scan, and a killed one forks no more.
    A scan that finds none is confirmed by a fresh one before returning: a process that forks and exits
    while a scan reads /proc hides its child from that scan, never from the next (the child exists before
    its parent exits, so before the next listing). A chain of such exits, each inside one scan, is out of
    reach like a new session (PROTOCOL.md names both). The session is the engine's own
    (start_new_session), never the runner's. The leader is signalled through its pidfd. A bare session
    number is not used to signal that leader. Other members are signalled only after a fresh check that
    the pid is still in this session. When a leader was supplied, its pidfd is already gone, and that
    number is a live session leader, nothing is signalled: the number was reused. When the pidfd is gone
    and the number is not a live leader, other members are still signalled. SessionCleanupError if any
    member we do signal is still alive after 10 s."""
    if sid == os.getsid(0):
        raise RuntimeError("refusing to signal the runner's own session")
    ours = _signal_leader(leader)
    if leader is not None and not ours and _still_in_session(sid, sid):
        # The pidfd is gone and the number is a live session leader: it was reused. Do not signal it.
        return
    if ours and os.path.isdir("/proc"):
        try:
            os.killpg(sid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if not os.path.isdir("/proc"):
        return
    deadline = time.monotonic() + 10
    empty = 0
    while True:
        members = _session_members(sid)
        if not members:
            empty += 1
            if empty == 2:
                return
            time.sleep(0.01)
            continue
        empty = 0
        if time.monotonic() >= deadline:
            raise SessionCleanupError(f"processes {members} of the engine's session {sid} survived cleanup")
        refused_leader = False
        acted = False
        for pid in members:
            if pid == sid and not ours:
                refused_leader = True
                continue
            if not _still_in_session(pid, sid):
                continue
            acted = True
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if refused_leader and not acted:
            return
        time.sleep(0.01)


def _sets_something(data: bytes) -> bool:
    """False only for a settings file that sets nothing: an empty object, or one holding only "$schema"."""
    try:
        value = json.loads(data)
    except ValueError:
        return True
    return not (isinstance(value, dict) and set(value) <= {"$schema"})


def settings_in_effect(paths) -> list:
    """Those of ``paths`` (settings files, or folders of *.json settings files) that set something or cannot
    be read (Claude Code may read what this cannot). An absent path is no setting."""
    found = []
    for path in map(Path, paths):
        if path.is_dir():
            try:
                inside = sorted(item for item in path.iterdir() if item.suffix == ".json")
            except OSError:
                found.append(str(path))
                continue
            found += settings_in_effect(inside)
            continue
        if not os.path.lexists(path):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            found.append(str(path))
            continue
        if _sets_something(data):
            found.append(str(path))
    return found


def _settings_refusal(found: list) -> str:
    return (f"direct arm (claude_code): Claude Code settings in effect: {'; '.join(found)}. A settings file can "
            "set the effort, the model, the permission mode or an environment variable over the recorded flags, "
            "so the direct arm runs only with none (an empty object, or one holding only \"$schema\", sets "
            "nothing)")


def _absolute_folder(value: str, what: str) -> Path:
    """A configuration folder named to the engine, which must be an absolute path: the runner checks it from
    its own working folder, while the engine, started in the task's work tree, would read a relative one
    there (and a "~" in a variable is not expanded: it is a folder named "~"). Empty is not absolute."""
    path = Path(value)
    if not path.is_absolute():
        raise ArmRefused(f"direct arm (claude_code): {what} {value!r} is not an absolute path; the engine would read "
                         "its settings relative to the task's work tree, not where they were checked")
    return path


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


# A credential name, as a suffix, so a prefix (ANTHROPIC_API_KEY) still matches. The whole
# identifier is kept; only the value is dropped. ``token`` does not match ``tokens``.
_CREDENTIAL_NAME = re.compile(
    r"(?i)(?:access_token|refresh_token|authorization|password|passwd|secret|api[_-]?key|token)$"
)


def _next_sep(text, start):
    """Index of the next ``=`` or ``:``, or -1."""
    equals = text.find("=", start)
    colon = text.find(":", start)
    if equals < 0:
        return colon
    if colon < 0:
        return equals
    return equals if equals < colon else colon


def _scheme_value_start(text, start):
    """Index of the token after a Bearer or Basic scheme, or None when the text has no scheme there."""
    for word in ("bearer", "basic"):
        end = start + len(word)
        if end < len(text) and text[start:end].lower() == word and text[end].isspace():
            index = end + 1
            while index < len(text) and text[index].isspace():
                index += 1
            return index
    return None


def _credential_value_end(text, start):
    """End index of one credential value, or None when there is no value.

    A quoted value runs to its closing quote (a backslash keeps the next character). An unquoted
    value is one token. A Bearer or Basic scheme in front of that token is part of the value, so
    ``authorization=Bearer <value>`` does not keep the value after the scheme word.
    """
    length = len(text)
    if start >= length or text[start].isspace():
        return None
    if text[start] in "\"'":
        quote = text[start]
        index = start + 1
        while index < length:
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == quote:
                return index + 1
            index += 1
        return length
    scheme = _scheme_value_start(text, start)
    index = start
    if scheme is not None and scheme < length and not text[scheme].isspace():
        index = scheme
    while index < length and not text[index].isspace():
        index += 1
    return index if index > start else None


def _name_ending_at(text, sep):
    """The identifier immediately before a separator, skipping spaces and one quote. Or None."""
    index = sep
    while index > 0 and text[index - 1].isspace():
        index -= 1
    if index > 0 and text[index - 1] in "\"'":
        index -= 1
    end = index
    while index > 0 and (text[index - 1].isalnum() or text[index - 1] in "_.-"):
        index -= 1
    if index == end:
        return None
    return index, text[index:end]


def _flag_at(text, start):
    """A ``--name value`` or ``--name=value`` flag when the name carries a credential. Or None."""
    if not text.startswith("--", start):
        return None
    length = len(text)
    index = start + 2
    name_at = index
    while index < length and (text[index].isalnum() or text[index] in "_.-"):
        index += 1
    name = text[name_at:index]
    if not name or _CREDENTIAL_NAME.search(name) is None:
        return None
    if index < length and text[index] == "=":
        value_at = index + 1
    elif index < length and text[index].isspace():
        value_at = index
    else:
        return None
    while value_at < length and text[value_at].isspace():
        value_at += 1
    value_end = _credential_value_end(text, value_at)
    if value_end is None:
        return None
    return "--" + name, value_end


def redact_detail(text):
    """Drop values for names that carry a credential.

    The engine's own error text is copied into the record. A value after one of these names is
    replaced so the record keeps the failure and not the value. The separator may be ``=`` or
    ``:``, the name may carry a prefix, the value may be quoted, and a Bearer or Basic scheme is
    part of the value. A flag of the form ``--api-key <value>`` is the same. One pass, with no
    shared state, so a second caller cannot see this caller's text.
    """
    if not isinstance(text, str) or not text:
        return text
    parts = []
    index = 0
    length = len(text)
    while index < length:
        sep = _next_sep(text, index)
        flag = text.find("--", index)
        if sep < 0 and flag < 0:
            parts.append(text[index:])
            break
        if flag >= 0 and (sep < 0 or flag <= sep):
            found = _flag_at(text, flag)
            if found is None:
                parts.append(text[index:flag + 2])
                index = flag + 2
                continue
            shown, value_end = found
            parts.append(text[index:flag])
            parts.append(shown + "=[redacted]")
            index = value_end
            continue
        named = _name_ending_at(text, sep)
        if named is None or _CREDENTIAL_NAME.search(named[1]) is None:
            parts.append(text[index:sep + 1])
            index = sep + 1
            continue
        name_at, name = named
        value_at = sep + 1
        while value_at < length and text[value_at].isspace():
            value_at += 1
        value_end = _credential_value_end(text, value_at)
        if value_end is None:
            parts.append(text[index:sep + 1])
            index = sep + 1
            continue
        parts.append(text[index:name_at])
        parts.append(name + "=[redacted]")
        index = value_end
    return "".join(parts)


def _json_loads(line: bytes):
    """Parse one stream line. An integer past the platform digit limit stays text, not an error."""
    def parse_int(text):
        try:
            return int(text)
        except ValueError:
            return text

    return json.loads(line, parse_int=parse_int)


def _finite_cost(cost):
    """A non-negative finite cost, or None. A huge integer is not a cost and must not raise."""
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return None
    try:
        number = float(cost)
    except OverflowError:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def parse_stream_json(stdout: bytes, *, cost_basis: str) -> dict:
    """What the engine's own stream-json says: done or not, a typed failure, and its usage.

    Usage fields map as Claude Code reports them: input_tokens (uncached), cache_creation_input_tokens
    (cache write), cache_read_input_tokens, output_tokens; a field that is absent or not a count is
    null. total_cost_usd is the engine's own figure.
    """
    result = None
    for line in stdout.splitlines():
        try:
            message = _json_loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("type") == "result":
            result = message
    usage = record_mod.empty_usage()
    if result is None:
        return {"has_result": False, "claimed_done": False, "failure": None, "detail": None, "usage": usage}
    raw = result.get("usage")
    if isinstance(raw, dict):
        usage.update({
            "uncached_input": _count(raw.get("input_tokens")),
            "cache_write": _count(raw.get("cache_creation_input_tokens")),
            "cache_read": _count(raw.get("cache_read_input_tokens")),
            "output": _count(raw.get("output_tokens")),
            "source": "engine_output",
        })
    number = _finite_cost(result.get("total_cost_usd"))
    if number is not None:
        usage.update({"cost": number, "cost_unit": "USD", "cost_basis": cost_basis, "source": "engine_output"})
    if result.get("subtype") == "success" and result.get("is_error") is not True:
        return {"has_result": True, "claimed_done": True, "failure": None, "detail": None, "usage": usage}
    error = result.get("error")
    failure = INFRA_RESULT_ERRORS.get(error) if isinstance(error, str) else None
    params = result.get("api_error_params")
    if failure is None and isinstance(params, dict) and isinstance(params.get("rate_limit_info"), dict):
        failure = "vendor_limit"
    if failure is None and result.get("api_error_status") == 401:
        failure = "auth_failed"
    detail = error if isinstance(error, str) else result.get("subtype")
    if detail is not None:
        detail = redact_detail(str(detail))
    return {"has_result": True, "claimed_done": False, "failure": failure or "engine_error",
            "detail": detail, "usage": usage}


class Arm:
    name = "arm"

    def __init__(self, engine: str, *, model: str | None = None, route: str | None = None,
                 effort: str | None = None, permission_mode: str | None = None, extra_args=()) -> None:
        if effort is not None and effort not in EFFORT_LEVELS:
            raise ArmRefused(f"{self.name} arm: effort {effort!r} is not one of {', '.join(EFFORT_LEVELS)}")
        if permission_mode is not None and permission_mode not in PERMISSION_MODES:
            raise ArmRefused(f"{self.name} arm: permission mode {permission_mode!r} is not one of "
                             f"{', '.join(PERMISSION_MODES)}")
        overriding = [a for a in extra_args if isinstance(a, str) and a.split("=", 1)[0] in OVERRIDING_ARGS]
        if overriding:
            raise ArmRefused(f"{self.name} arm: extra engine arguments {overriding} would set what a named setting "
                             "sets (effort, model, permission mode, settings); use the named setting")
        self.engine = engine
        self.model = model
        self.route = route
        self.effort = effort
        self.permission_mode = permission_mode
        self.extra_args = list(extra_args)

    def settings(self) -> dict:
        """The settings BOTH arms of a comparison must share. No arm name, binary path or passed
        environment: those are how an arm reaches the engine, not what the engine is asked to do."""
        return {"engine": self.engine, "model": self.model, "route": self.route, "effort": self.effort,
                "permission_mode": self.permission_mode, "extra_args": list(self.extra_args)}

    def settings_sha256(self) -> str:
        return record_mod.sha256_json(self.settings())

    def preflight(self) -> None:
        raise NotImplementedError

    def check_tasks(self, tasks) -> None:
        """Before any attempt: ArmRefused if a task's start tree would change what the arm runs."""

    def engine_version(self) -> str | None:
        raise NotImplementedError

    def run(self, task, work: Path, scratch, trial_index: int) -> EngineOutcome:
        raise NotImplementedError


@dataclass
class ClaudeCodeSettings:
    binary: str = "claude"
    permission_mode: str | None = None
    extra_args: list = field(default_factory=list)
    home: str | None = None            # the route home (where the operator signed in); never a credential
    pass_env: list = field(default_factory=list)   # names of variables copied from the runner's environment


class DirectArm(Arm):
    name = "direct"

    def __init__(self, engine: str, *, model: str | None = None, route: str | None = None,
                 effort: str | None = None, claude: ClaudeCodeSettings | None = None,
                 cost_basis: str = "reported_not_billed") -> None:
        if engine not in ENGINES:
            raise ArmRefused(f"direct arm: engine {engine!r} is not wired (wired: {', '.join(ENGINES)})")
        self.claude = claude or ClaudeCodeSettings()
        if engine == "standin" and (self.claude.permission_mode or self.claude.extra_args):
            raise ArmRefused("direct arm (standin): the stand-in engine takes no permission mode or engine arguments")
        super().__init__(engine, model=model, route=route, effort=effort,
                         permission_mode=self.claude.permission_mode if engine == "claude_code" else None,
                         extra_args=self.claude.extra_args if engine == "claude_code" else ())
        self.cost_basis = cost_basis

    def argv(self) -> list:
        if self.engine == "standin":
            return [sys.executable, "-I", "-B", str(STANDIN_SCRIPT)]
        argv = [self.claude.binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--model", self.model]
        if self.effort:
            argv += ["--effort", self.effort]
        if self.permission_mode:
            argv += ["--permission-mode", self.permission_mode]
        return argv + list(self.extra_args)

    def preflight(self) -> None:
        refused = sorted(set(self.claude.pass_env) - set(ALLOWED_PASS_ENV))
        if refused:
            raise ArmRefused(f"direct arm: {', '.join(refused)} may not be passed to the engine (only "
                             f"{', '.join(ALLOWED_PASS_ENV)}): they could override the recorded settings or the "
                             "attempt's own runtime")
        if self.engine == "claude_code":
            if not self.model:
                raise ArmRefused("direct arm (claude_code): a model is required, the same one the Recursion arm uses")
            if shutil.which(self.claude.binary) is None:
                raise ArmRefused(f"direct arm (claude_code): {self.claude.binary!r} is not on PATH")
            found = settings_in_effect(list(MANAGED_SETTINGS) + self._user_settings(None))
            if found:
                raise ArmRefused(_settings_refusal(found))

    def _user_settings(self, env: dict | None) -> list:
        """The user settings file the engine reads: in CLAUDE_CONFIG_DIR when that is passed (whatever its
        value, even empty), else in HOME/.claude; either folder must be absolute (ArmRefused otherwise).
        Before an attempt (``env`` None) HOME is the route home; without one it is the attempt's fresh
        scratch home, checked at the attempt."""
        if env is not None:
            if "CLAUDE_CONFIG_DIR" in env:
                return [_absolute_folder(env["CLAUDE_CONFIG_DIR"], "CLAUDE_CONFIG_DIR") / "settings.json"]
            return [_absolute_folder(env["HOME"], "HOME") / ".claude" / "settings.json"]
        if "CLAUDE_CONFIG_DIR" in self.claude.pass_env and "CLAUDE_CONFIG_DIR" in os.environ:
            return [_absolute_folder(os.environ["CLAUDE_CONFIG_DIR"], "CLAUDE_CONFIG_DIR") / "settings.json"]
        if self.claude.home:
            return [_absolute_folder(self.claude.home, "the route home") / ".claude" / "settings.json"]
        return []

    def check_tasks(self, tasks) -> None:
        """No task's start tree may hold a project settings file that sets something: Claude Code would load
        it from the task repository over the recorded flags."""
        if self.engine != "claude_code":
            return
        found = []
        for task in tasks:
            for name in PROJECT_SETTINGS:
                try:
                    data = task.start_file(name)
                except ValueError:                 # TaskError: there, but not a regular file
                    found.append(f"task {task.id}: {name}")
                    continue
                if data is not None and _sets_something(data):
                    found.append(f"task {task.id}: {name}")
        if found:
            raise ArmRefused(_settings_refusal(found))

    def engine_version(self) -> str | None:
        if self.engine == "standin":
            from eval import standin_engine
            return standin_engine.VERSION
        try:
            result = subprocess.run([self.claude.binary, "--version"], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        out = result.stdout.decode("utf-8", "replace").strip()
        return out.splitlines()[0] if out else None

    def run(self, task, work: Path, scratch, trial_index: int) -> EngineOutcome:
        extra = {}
        home = None
        if self.engine == "standin":
            plan_path = scratch.tmpdir / "standin-plan.json"
            plan_path.write_text(json.dumps(task.standin_plan()), encoding="utf-8")
            extra = {"EVAL_STANDIN_PLAN": str(plan_path), "EVAL_STANDIN_TRIAL": str(trial_index)}
            if self.effort:
                extra["EVAL_STANDIN_EFFORT"] = self.effort
        else:
            home = Path(self.claude.home) if self.claude.home else None
            extra = {name: os.environ[name] for name in self.claude.pass_env if name in os.environ}
        env = scratch_mod.environment(scratch, home=home, extra=extra)
        if self.engine == "claude_code":
            # Again at the attempt, with the environment the engine gets and the task repository it works in:
            # nothing it would load may have appeared since the checks before the run.
            found = settings_in_effect(list(MANAGED_SETTINGS) + self._user_settings(env)
                                       + [work / name for name in PROJECT_SETTINGS])
            if found:
                raise ArmRefused(_settings_refusal(found))
        message = json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": task.prompt}]}}) + "\n"
        done = run_process(self.argv(), cwd=work, env=env, stdin=message.encode("utf-8"), timeout_s=task.timeout_s)
        parsed = parse_stream_json(done["stdout"], cost_basis=self.cost_basis)
        failure, detail = parsed["failure"], parsed["detail"]
        # A success line does not outrank the process status. A typed infra failure (limit, sign-in, give-up)
        # keeps its class when the process exits non-zero; a success line plus a bad exit or a signal does not.
        abnormal = done["signal"] is not None or done["exit"] not in (0, None)
        if done["timed_out"]:
            failure, detail = "timeout", f"engine ran past {task.timeout_s:g} s and was killed"
        elif abnormal and parsed["claimed_done"] and failure not in ("vendor_limit", "auth_failed"):
            failure = "engine_crash"
            detail = f"engine ended with exit {done['exit']}, signal {done['signal']}"
        elif not parsed["has_result"]:
            failure = "engine_crash" if done["exit"] != 0 else "no_result"
            detail = f"no result line; exit {done['exit']}, signal {done['signal']}"
        claimed_done = None if done["timed_out"] else parsed["claimed_done"]
        if abnormal and failure == "engine_crash":
            claimed_done = False
        return EngineOutcome(
            exit=done["exit"], signal=done["signal"], timed_out=done["timed_out"],
            claimed_done=claimed_done, failure=failure, detail=detail,
            usage=parsed["usage"], stdout=done["stdout"], stderr=done["stderr"], seconds=done["seconds"],
        )


def probe_base(root: Path = REPO_ROOT) -> dict:
    """What the evaluated checkout has for the Recursion arm, read from its files (nothing imported).

    workspace: SPINE's hooks module and WORKSPACE's registrant (which declares ``workspace`` at start).
    usage_events: SPINE's hooks module and EVENTS' registrant (which serves events.read).
    """
    pkg = Path(root) / "core" / "recursion_core"
    hooks = (pkg / "control" / "hooks.py").is_file()
    found = {
        "control_hooks": hooks,
        "workspace": hooks and (pkg / "workspace" / "hooks.py").is_file(),
        "usage_events": hooks and (pkg / "events" / "hooks.py").is_file(),
    }
    return found


class RecursionArm(Arm):
    name = "recursion"

    def __init__(self, engine: str, *, model: str | None = None, route: str | None = None,
                 effort: str | None = None, permission_mode: str | None = None, extra_args=(),
                 base: Path = REPO_ROOT, probe=probe_base) -> None:
        super().__init__(engine, model=model, route=route, effort=effort, permission_mode=permission_mode,
                         extra_args=extra_args)
        self.base = Path(base)
        self.probe = probe

    def preflight(self) -> None:
        found = self.probe(self.base)
        missing = []
        if not found.get("workspace"):
            missing.append("WORKSPACE's `workspace` at start (seam S6)"
                           + ("" if found.get("control_hooks") else ", and SPINE's seams under it"))
        if not found.get("usage_events"):
            missing.append("EVENTS' usage events through events.read"
                           + ("" if found.get("control_hooks") else ", and SPINE's seams under it"))
        if missing:
            raise ArmRefused(REFUSAL.format(missing="; ".join(missing)))
        raise ArmRefused(NOT_BUILT.format(found="the workspace field and usage events"))

    def engine_version(self) -> str | None:
        return None

    def run(self, task, work: Path, scratch, trial_index: int) -> EngineOutcome:
        raise ArmRefused("recursion arm: preflight refuses before any trial")
