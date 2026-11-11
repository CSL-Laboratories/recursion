"""Read build help for print-mode bare defaults and a documented auth-mode opt-out.

Unknown or incomplete help fails closed. The subscription route runs as beta only
when ``claude --help`` matches the one measured hash and the build is at or above
the 2.1.314 floor (CSLR-D-011, PRODUCT.md 055cc7a5c793b7135e5d0565dc4198cf96230531).
A different help text names the build and refuses until it is re-measured. Unknown
is not beta. Only ``--help`` and ``--version`` are executed; no provider call,
settings edit or credential lookup is performed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping

STATES = ("absent", "present", "unknown")
HELP_TIMEOUT_S = 10.0
HELP_MAX_BYTES = 1 << 20
# One measured help text. Builds 2.1.314 through 2.1.321 share this hash, so the
# table is one entry. It is not a per-version allow list. Classifier "unknown" is
# not an entry and is not an allow.
MEASURED_HELP = (
    {"help_sha256": "5ca3a9dac2026e56c221fdd34380c7d7d45ebbb51c1d01b7c17c75cb076a0be8",
     "label": "beta"},
)
HELP_FLOOR = (2, 1, 314)

# An option line: two leading spaces, then the flag (optionally after a short flag).
_OPTION = re.compile(r"^[ \t]{1,8}(?:-\w,[ \t]*)?(--[a-z0-9][a-z0-9-]*)", re.MULTILINE)
# Present-tense "default for -p" wording in the --bare block. Future-tense notes are filtered first.
_DEFAULT_NOW = re.compile(
    r"(?:\bis\s+(?:now\s+)?(?:the\s+)?default\b[^.]*(?:-p\b|--print\b|print\s+mode|headless)"
    r"|\b(?:on|enabled)\s+by\s+default\b[^.]*(?:-p\b|--print\b|print\s+mode|headless)"
    r"|\bdefault\s+(?:for|in|with|under)\s+(?:-p\b|--print\b|print\s+mode|headless))",
    re.IGNORECASE)
# Inside the --bare block the subject is bare itself, so a bare "this is the default" is enough.
_BARE_SELF_DEFAULT = re.compile(
    r"\b(?:is|becomes)\s+(?:now\s+)?(?:the\s+)?default\b|\(default(?:\s*[:=]\s*(?:true|on|enabled))?\)"
    r"|\b(?:on|enabled)\s+by\s+default\b|\bdefault\s*[:=]\s*(?:true|on|enabled)\b"
    r"|\bdefaults\s+to\s+(?:on|true|enabled)\b",
    re.IGNORECASE)
# Inside the -p/--print block: the sentence must name bare and say it is the default now.
_PRINT_BARE_DEFAULT = re.compile(
    r"\bbare\b[^.;]*?\b(?:is|becomes)\s+(?:now\s+)?(?:the\s+)?default\b"
    r"|\bbare\b[^.;]*?\(default(?:\s*[:=]\s*(?:true|on|enabled))?\)"
    r"|\bbare\b[^.;]*?\b(?:by\s+default|default\s+mode)\b"
    r"|\bdefaults?\s+(?:to|is)\s+(?:the\s+)?(?:--)?bare\b",
    re.IGNORECASE)
_FUTURE = re.compile(r"\bwill\s+become\b|\bfuture\s+release\b|\bin\s+a\s+future\b|\bplanned\b|\bupcoming\b",
                     re.IGNORECASE)
# Negated or conditional wording never proves the default either way.
_NEGATED = re.compile(r"\bnot\b|\bno\s+longer\b|\bnever\b|\bunless\b|\bexcept\b|\bwithout\b|n't\b",
                      re.IGNORECASE)
# An option that turns bare mode off would only exist if bare can be on without the flag.
_OPT_OUT = ("--no-bare",)


def _option_raw_blocks(text: str) -> dict[str, str]:
    """Map each long option to its raw help lines (the option line plus its wrapped description)."""

    blocks: dict[str, str] = {}
    matches = list(_OPTION.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.setdefault(m.group(1), text[m.start():end])
    return blocks


def _option_blocks(text: str) -> dict[str, str]:
    """Each long option's help block joined onto one line."""

    return {opt: " ".join(raw.split()) for opt, raw in _option_raw_blocks(text).items()}


def _sentences(block: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.;])\s+", block) if s]


def _affirmed(pattern: re.Pattern[str], sentence: str) -> bool:
    """True if ``pattern`` matches with no negation in the match or the few words before it."""

    for m in pattern.finditer(sentence):
        if not _NEGATED.search(sentence[max(0, m.start() - 24):m.end()]):
            return True
    return False


def classify_help(text: object) -> str:
    """Return ``"absent"``, ``"present"`` or ``"unknown"`` for one build's ``claude --help`` text."""

    if not isinstance(text, str) or not text.strip():
        return "unknown"
    blocks = _option_blocks(text)
    if "--bare" not in blocks or "--print" not in blocks:
        return "unknown"
    if any(opt in blocks for opt in _OPT_OUT):
        return "present"
    absent = False
    for option in ("--bare", "--print"):
        for sentence in _sentences(blocks[option]):
            if _FUTURE.search(sentence):
                if "bare" in sentence.lower() and _DEFAULT_NOW.search(sentence):
                    absent = True
                continue
            # Either block saying --bare is the default now is enough: print's own help may announce
            # it even when the --bare line does not.
            patterns = (_PRINT_BARE_DEFAULT,) if option == "--print" else (_DEFAULT_NOW, _BARE_SELF_DEFAULT)
            if any(_affirmed(p, sentence) for p in patterns):
                return "present"
            if option == "--bare" and re.search(r"default\s*[:=]?\s*(?:false|off|disabled)\b",
                                                sentence, re.I):
                absent = True
    return "absent" if absent else "unknown"


def auth_mode(text: object) -> dict:
    """Route metadata, including only an opt-out actually advertised by this build."""
    state = classify_help(text)
    blocks = _option_blocks(text) if isinstance(text, str) else {}
    return {"bare_default": state,
            "bare_opt_out": next((flag for flag in _OPT_OUT if flag in blocks), None)}


def help_sha256(text: str) -> str:
    """SHA-256 of help text as UTF-8, the same bytes the measured fixtures commit."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_build(build: object) -> str | None:
    """``major.minor.patch`` from a version string, or None when the build is unknown.

    ``2.1.321 (Claude Code)`` is ``2.1.321``. A suffix on the version token
    (``2.1.321-dev``) is unknown: unknown is not treated as a measured build.
    """
    if not isinstance(build, str) or not build.strip():
        return None
    token = build.strip().split()[0]
    if not re.fullmatch(r"\d+\.\d+\.\d+", token):
        return None
    return token


def _build_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def subscription_beta(build: object, help_text: object) -> dict:
    """Admit the Claude subscription route as beta, or refuse.

    Beta only when ``help_text`` hashes to the single ``MEASURED_HELP`` entry and
    the build is at or above ``HELP_FLOOR`` (2.1.314). Help that differs names the
    build and says it needs re-measuring. A build below the floor refuses even
    when the hash matches. Missing or unreadable build or help refuses. Unknown
    is not beta, and there is no per-version allow list.
    """
    version = parse_build(build)
    if version is None:
        return {"disposition": "refuse", "label": None, "build": None, "help_sha256": None,
                "detail": "claude build is unknown; unknown is not safe"}
    if _build_key(version) < HELP_FLOOR:
        return {"disposition": "refuse", "label": None, "build": version, "help_sha256": None,
                "detail": f"claude {version} is below the 2.1.314 floor"}
    raw = _help_bytes(help_text)
    if raw is None:
        return {"disposition": "refuse", "label": None, "build": version, "help_sha256": None,
                "detail": f"claude {version} help is unknown; unknown is not safe"}
    digest = hashlib.sha256(raw).hexdigest()
    entry = next((row for row in MEASURED_HELP if row["help_sha256"] == digest), None)
    if entry is None:
        return {"disposition": "refuse", "label": None, "build": version, "help_sha256": digest,
                "detail": (f"claude {version} help does not match the measured fixture; "
                           "this build needs re-measuring")}
    return {"disposition": "beta", "label": entry["label"], "build": version,
            "help_sha256": digest, "detail": entry["label"]}


Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def _help_bytes(text: object) -> bytes | None:
    """UTF-8 help bytes within ``HELP_MAX_BYTES``, or None when the text is unusable.

    A lone surrogate cannot be encoded. That is unknown help, not an exception, and the
    text itself is not copied into the refusal.
    """
    if not isinstance(text, str) or text == "" or len(text) > HELP_MAX_BYTES:
        return None
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(raw) > HELP_MAX_BYTES:
        return None
    return raw


def _leader_unreaped(pid: int) -> bool:
    """True when ``pid`` is still our child, alive or zombie.

    ``waitid`` with ``WNOWAIT`` reports that without collecting the zombie.
    The leader keeps its process-group id until it is collected. ``poll`` would
    collect it, and the group id could then be reused.
    """
    if not hasattr(os, "waitid") or not hasattr(os, "WNOWAIT"):
        # No non-collecting wait. A zero signal succeeds for a zombie and fails
        # once the leader has been collected, and it does not collect the leader.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    while True:
        try:
            os.waitid(os.P_PID, pid, flags)
        except InterruptedError:
            continue
        except ChildProcessError:
            return False
        return True


def _signal_group(proc: subprocess.Popen, pid: int) -> None:
    """SIGKILL the process group ``pid`` leads. Fall back to the leader alone."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _proc_stats(proc_root: str = "/proc"):
    """Yield ``(pid, state, ppid, pgrp)`` from ``proc_root``, as bytes.

    ``stat`` is read in binary. A process name is an arbitrary byte string, and
    opening it as text raises ``UnicodeDecodeError`` for a name that is not
    valid in the locale encoding. One bad entry is skipped; it must not abort
    the scan.
    """
    try:
        names = os.listdir(proc_root)
    except (OSError, UnicodeError, ValueError):
        return
    for name in names:
        if not str(name).isdigit():
            continue
        try:
            with open(os.path.join(proc_root, str(name), "stat"), "rb") as handle:
                raw = handle.read()
            end = raw.rfind(b")")
            if end < 0:
                continue
            pid = raw.split(b" ", 1)[0]
            fields = raw[end + 2:].split()
            if len(fields) < 3:
                continue
            # state, ppid, pgrp. The command name can contain spaces and any
            # bytes, so it is taken off above, at the last ")".
            yield pid, fields[0], fields[1], fields[2]
        except (OSError, UnicodeError, ValueError):
            continue


def _group_has_live_member(pgid: int, proc_root: str = "/proc") -> bool:
    """True if some process in ``pgid`` is still running.

    Zombies are not live. ``/proc`` is how a non-parent can see a same-group
    child that was reparented when the leader exited. Without it, the caller
    cannot tell, and the answer is false.
    """
    token = str(pgid).encode()
    for _pid, state, _ppid, pgrp in _proc_stats(proc_root):
        if pgrp == token and state not in (b"Z", b"X"):
            return True
    return False


def _group_still_blocking(pgid: int) -> bool:
    """True while a same-group process would still look alive to the caller.

    A running member counts. So does a zombie reparented to init: ``kill(pid, 0)``
    succeeds until that zombie is collected. A zombie we parent, including the
    leader, is left for our own ``wait``.
    """
    leader = str(pgid).encode()
    me = str(os.getpid()).encode()
    for pid, state, ppid, pgrp in _proc_stats():
        if pgrp != leader:
            continue
        if state in (b"Z", b"X") and (pid == leader or ppid == me):
            continue
        return True
    return False


def _until_group_dead(pgid: int) -> None:
    """Return once ``pgid`` will not look running, without collecting any child.

    SIGKILL does not finish inside the ``killpg`` call. Collecting the leader
    immediately lets a caller observe the same-group child still running.
    """
    if not os.path.isdir("/proc"):
        time.sleep(0.05)
        return
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _group_still_blocking(pgid):
            return
        time.sleep(0.001)


def _wait_leader(proc: subprocess.Popen, deadline: float) -> str:
    """Wait until the leader exits or ``deadline`` passes. Do not collect it.

    Returns ``"exited"``, ``"timeout"``, or ``"reaped"``. ``Popen.wait`` catches
    ``KeyboardInterrupt`` and waits again, and that second wait collects the
    leader before the caller can signal the group. ``waitid`` with ``WNOWAIT``
    does not. A ``KeyboardInterrupt`` during this wait leaves the leader unreaped.
    """
    pid = proc.pid
    if (os.name == "nt" or not isinstance(pid, int) or pid <= 0
            or not hasattr(os, "waitid") or not hasattr(os, "WNOWAIT")):
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return "timeout"
        return "reaped"
    flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
    while True:
        try:
            got = os.waitid(os.P_PID, pid, flags)
        except InterruptedError:
            continue
        except ChildProcessError:
            return "reaped"
        if got is not None:
            return "exited"
        remain = deadline - time.monotonic()
        if remain <= 0:
            return "timeout"
        time.sleep(min(remain, 0.02))


def _reap(proc: subprocess.Popen) -> None:
    """Kill every process still in the child's session, then wait for the leader.

    The child is started in its own session, so its pid is the process-group id.
    Signal that group before collecting the leader, and wait until it has no
    running member. A leader that has already exited still owns the group until
    it is reaped; collecting it first leaves a same-group child running.
    SIGKILL is not finished when ``killpg`` returns. A descendant that has
    called ``setsid`` is no longer in the group. The read loop stops on its
    deadline instead.
    """
    pid = proc.pid
    if os.name != "nt" and isinstance(pid, int) and pid > 0:
        # Signal before collecting. A leader that Popen.wait already collected
        # (its KeyboardInterrupt handler does that) still names the group while
        # a same-group child is running.
        if proc.returncode is None and _leader_unreaped(pid):
            _signal_group(proc, pid)
            _until_group_dead(pid)
        elif _group_has_live_member(pid):
            _signal_group(proc, pid)
            _until_group_dead(pid)
    elif os.name == "nt" and proc.returncode is None and proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        # Still our child, so this pid is still the group id.
        if os.name != "nt" and isinstance(pid, int) and pid > 0 and proc.returncode is None:
            _signal_group(proc, pid)
            _until_group_dead(pid)
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait(timeout=5)


def _readable(fd: int, remain: float) -> bool:
    """Wait until ``fd`` can be read, or until ``remain`` seconds have passed."""
    if remain <= 0:
        return False
    # select() does not accept pipes on Windows, and poll() is not there either.
    if os.name == "nt" or not hasattr(select, "poll"):
        time.sleep(min(remain, 0.02))
        return True
    try:
        waiter = select.poll()
        waiter.register(fd, select.POLLIN)
        return bool(waiter.poll(remain * 1000.0))
    except InterruptedError:
        return True


def _read_capped(fd: int, deadline: float) -> tuple[bytes, str]:
    """Read at most one byte past the help cap. Status is eof, overflow, or timeout.

    The fd is non-blocking. Nothing here waits past ``deadline``, including when a
    descendant still holds the write end.
    """
    os.set_blocking(fd, False)
    chunks: list[bytes] = []
    total = 0
    limit = HELP_MAX_BYTES + 1
    while total < limit:
        try:
            chunk = os.read(fd, min(65536, limit - total))
        except BlockingIOError:
            remain = deadline - time.monotonic()
            if remain <= 0 or not _readable(fd, remain):
                return b"".join(chunks), "timeout"
            continue
        except InterruptedError:
            continue
        if not chunk:
            return b"".join(chunks), "eof"
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), "overflow"


def _bounded_run(argv, *, env=None, stdin=None, stdout=None, stderr=None, timeout=None,
                 check=False, shell=False):
    """Run one argv with ``timeout`` and stop reading after the help cap.

    ``subprocess.run`` buffers all of stdout before the cap can apply. This reads at most
    ``HELP_MAX_BYTES + 1`` from the raw pipe. The read shares one deadline with the wait
    for the child to exit.

    Overflow (one byte past the cap) kills the session and finishes like any other
    non-zero exit: ``check=True`` raises ``CalledProcessError``, otherwise the bytes
    come back with a non-zero code. A timeout also kills the session and discards
    bytes already read: the child did not finish, so the buffer is not a finished
    ``--help``. An under-cap EOF does not kill. The child may close its output and
    still be exiting; it is killed only if it is still running when the deadline hits.
    That wait does not use ``Popen.wait``: a ``KeyboardInterrupt`` there collects the
    leader before the group can be signaled. The group is signaled first, including
    when that interrupt already collected the leader and a same-group child is left.

    ``stdout`` and ``stderr`` are ignored. The child always gets a pipe we own and
    ``stderr=DEVNULL``. A stderr ``PIPE`` is not drained, and a flood on it would block
    the child before stdout closed.
    """
    if shell:
        raise ValueError("shell")
    # Never inherit os.environ. An omitted env is empty, not the parent process.
    child_env = {} if env is None else dict(env)
    # Accept the keywords so callers can pass the subprocess.run shape, then drop them.
    del stdout, stderr
    popen_kwargs: dict = {
        "env": child_env,
        "stdin": subprocess.DEVNULL if stdin is None else stdin,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "shell": False,
    }
    # Own session so a grandchild stays in this process group. Windows rejects the flag.
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(list(argv), **popen_kwargs)
    fd: int | None = None
    try:
        if proc.stdout is None:
            raise OSError("stdout")
        # Dup first, then close the buffered stream. Nothing else is reading it, so
        # close() cannot block on another thread's read lock. The dup is what we read.
        fd = os.dup(proc.stdout.fileno())
        proc.stdout.close()
        waited = HELP_TIMEOUT_S if timeout is None else timeout
        deadline = time.monotonic() + waited
        out, why = _read_capped(fd, deadline)
        if why == "timeout":
            _reap(proc)
            # Drop whatever was already read. A partial page is not help text:
            # classifying it could report absent or present for a read that never finished.
            raise subprocess.TimeoutExpired(list(argv), waited)
        if why == "overflow":
            _reap(proc)
        else:
            state = _wait_leader(proc, deadline)
            if state == "timeout":
                _reap(proc)
                raise subprocess.TimeoutExpired(list(argv), waited) from None
            if state == "exited" and proc.returncode is None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _reap(proc)
                    raise subprocess.TimeoutExpired(list(argv), waited) from None
        code = proc.returncode if isinstance(proc.returncode, int) else 1
        # A leader that already exited 0 can still have written past the cap.
        # That read is not success. check=True and check=False share this result.
        if why == "overflow" and code == 0:
            code = 1
        if check and code != 0:
            raise subprocess.CalledProcessError(code, list(argv), output=out)
        return subprocess.CompletedProcess(list(argv), code, stdout=out)
    except BaseException:
        # Signal the group before this handler returns. Popen.wait may already
        # have collected the leader while handling KeyboardInterrupt; _reap
        # still signals a same-group child that is running.
        _reap(proc)
        raise
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if proc.stdout is not None and not proc.stdout.closed:
            proc.stdout.close()


def _capture(binary: str, flag: str, parent_env: Mapping[str, str], run: Runner) -> str | None:
    """Run ``<binary> <flag>`` once. The child gets only ``PATH``, ``HOME`` and ``LANG``."""
    if not isinstance(binary, str) or binary == "" or not isinstance(parent_env, Mapping):
        return None
    env = {k: parent_env[k] for k in ("PATH", "HOME", "LANG") if isinstance(parent_env.get(k), str)}
    try:
        done = run([binary, flag], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL, timeout=HELP_TIMEOUT_S, check=False, shell=False)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    out = getattr(done, "stdout", None)
    code = getattr(done, "returncode", None)
    if code != 0 or not isinstance(out, bytes) or len(out) > HELP_MAX_BYTES:
        return None
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_help(binary: str, parent_env: Mapping[str, str], run: Runner = _bounded_run) -> str | None:
    """Run ``<binary> --help`` once and return its text, or ``None`` if it cannot be read.

    The child gets only ``PATH``, ``HOME`` and ``LANG`` from the explicit ``parent_env`` (never
    ``os.environ``), so no credential variable reaches it. A non-zero exit, timeout, launch failure,
    output over the byte cap, or non-UTF-8 output is ``None`` (classified ``"unknown"``). The read
    stops at the cap and the child is reaped. A timeout is unknown even when the child already
    wrote a short page and then hung: that page is not a finished ``--help``. A grandchild that
    keeps the pipe open cannot stretch that timeout. Closing stdout is not itself a failure:
    the child is allowed to exit within the time still left.
    """
    return _capture(binary, "--help", parent_env, run)


def read_version(binary: str, parent_env: Mapping[str, str], run: Runner = _bounded_run) -> str | None:
    """Run ``<binary> --version`` once under the same limit as ``read_help``, or ``None``."""
    return _capture(binary, "--version", parent_env, run)


def probe(binary: str, parent_env: Mapping[str, str], run: Runner = _bounded_run) -> str:
    """``classify_help(read_help(...))``: the value a surface reports as ``route.bare_default``."""

    return classify_help(read_help(binary, parent_env, run))


def probe_auth_mode(binary: str, parent_env: Mapping[str, str], run: Runner = _bounded_run) -> dict:
    """Read help once and report the explicit subscription auth-mode capability."""
    return auth_mode(read_help(binary, parent_env, run))


def main(argv: list[str] | None = None, run: Runner = _bounded_run) -> int:
    """Print one build's classification and the help lines it came from.

    Runs only ``--version`` and ``--help``.
    """

    parser = argparse.ArgumentParser(prog="python -m recursion_core.routes.bare_default",
                                     description="Classify whether --bare is the -p default for one claude build.")
    parser.add_argument("--claude", required=True, help="path to the claude binary; only --version and --help are run")
    args = parser.parse_args(argv)
    version = read_version(args.claude, os.environ, run)
    text = read_help(args.claude, os.environ, run)
    metadata = auth_mode(text)
    # The adapter owns the refusal rule. Pass the build and the help text so the
    # measured-hash gate decides: beta, or a named refusal. Unknown is not beta.
    from recursion_core.routes import contract as c
    from recursion_core.routes.claude_code import ClaudeCodeAdapter
    reported = dict(metadata)
    reported["claude_build"] = version if isinstance(version, str) else ""
    reported["help_text"] = text if isinstance(text, str) else ""
    try:
        ClaudeCodeAdapter._subscription_auth_args(reported)
        route = "beta"
    except c.RouteError as exc:
        route = f"refuses ({exc})"
    print(f"binary: {args.claude}")
    print(f"build: {version if version is not None else 'unreadable'}")
    print(f"help: {'read' if text is not None else 'unreadable'}")
    print(f"bare_default: {metadata['bare_default']}")
    print(f"bare_opt_out: {metadata['bare_opt_out']}")
    print(f"subscription route: {route}")
    raw = _option_raw_blocks(text) if text is not None else {}
    for option in ("--bare", "--print", *_OPT_OUT):
        if option in raw:
            print(f"--- {option} help block:")
            print(raw[option].rstrip("\n"))
        else:
            print(f"--- {option}: not in help")
    return 0


if __name__ == "__main__":
    sys.exit(main())
