"""The keeper a shell command runs under (Linux): ``python -I -S reaper.py <shell> -c <command>``.

It makes itself the command's subreaper (``PR_SET_CHILD_SUBREAPER``), so a process the command starts stays its
descendant even when it leaves the command's process group or session (``setsid``, a double fork): when such a
process's parent ends, it is handed to this keeper, not to init. When the command's shell ends, or when the
keeper is told to stop (SIGTERM, which the loop sends at the deadline or on an interrupt), it kills every
descendant it still has, reaps them, and ends the way the shell ended (its exit status, or the same signal), so
the loop sees the command's own status. If it cannot make itself the subreaper (checked by reading the setting
back), it does not start the command: it says so on standard error and exits with status 125. What it cannot stop, it does not claim to: a process that made itself a
subreaper of its own, or one that runs as another user, is outside its reach.

Standard library only, and nothing from the loop: it runs in a fresh interpreter started with ``-I -S``.
"""

import ctypes
import os
import signal
import sys
import time

PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
REFUSED = 125  # the exit status when the command was not started
KILL_ROUNDS_S = 2.0  # how long it keeps killing what is left (a process that forks while it is being killed)


class _Stop(Exception):
    pass


def _on_term(signum, frame):
    raise _Stop()


def _descendants(me):
    """Every process whose chain of parents leads to ``me``, read from /proc."""

    children = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as f:
                data = f.read()
        except OSError:
            continue
        fields = data[data.rfind(b")") + 2:].split()
        if len(fields) < 2:
            continue
        children.setdefault(int(fields[1]), []).append(int(name))
    found, todo = [], [me]
    while todo:
        for pid in children.get(todo.pop(), ()):
            found.append(pid)
            todo.append(pid)
    return found


def _reap():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _kill_all():
    me = os.getpid()
    until = time.monotonic() + KILL_ROUNDS_S
    while True:
        left = _descendants(me)
        if not left or time.monotonic() > until:
            break
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        _reap()
        time.sleep(0.002)
    _reap()


def _become_subreaper():
    """True when this process is now the subreaper of what it starts, read back from the kernel."""

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
            return False
        flag = ctypes.c_int(0)
        return libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(flag), 0, 0, 0) == 0 and flag.value == 1
    except (OSError, AttributeError, TypeError, ValueError):
        return False


def main():
    signal.signal(signal.SIGTERM, _on_term)
    if not _become_subreaper():
        # Without it, what leaves the chain of parents would not be followed: the command is not started (astra C2).
        os.write(2, b"the command was not run: its keeper could not make itself the subreaper of what it starts\n")
        os._exit(REFUSED)
        return
    status = None
    try:
        shell = os.fork()
        if shell == 0:
            try:
                # Python ignores these two signals when it starts, and an ignored signal stays ignored through
                # execv: the command's shell gets their default action back (a closed pipe ends it, as in any shell).
                signal.signal(signal.SIGPIPE, signal.SIG_DFL)
                signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
                os.execv(sys.argv[1], sys.argv[1:])
            finally:
                os._exit(127)
        while status is None:
            pid, raw = os.waitpid(-1, 0)
            if pid == shell:
                status = raw
    except _Stop:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _kill_all()
        os._exit(128 + signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _kill_all()
    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        try:
            if sig != signal.SIGKILL:  # SIGKILL's action cannot be set, and needs no setting
                signal.signal(sig, signal.SIG_DFL)
            os.kill(os.getpid(), sig)
        except (OSError, ValueError):
            pass
        os._exit(128 + sig)
    os._exit(os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1)


if __name__ == "__main__":
    main()
