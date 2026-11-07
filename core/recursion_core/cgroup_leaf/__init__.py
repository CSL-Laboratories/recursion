"""Same-uid cgroup v2 leaf launch with kill-tree on every exit path (CSL-728).

The launcher creates a leaf under an operator-owned, delegated cgroup v2 root and
starts the child through a trampoline that joins the leaf before it execs the target,
so no target code runs outside the leaf. Every exit path (normal return, exception,
launch failure, cancellation) kills the leaf with ``cgroup.kill`` and books
``terminated`` only on positive evidence: ``populated 0`` and the main process's exit
seen through its pidfd and ``waitpid``. Anything else is ``running`` (populated) or
``unknown``.

Same uid only, and no cross-uid kill path. A process of the same uid can leave the
leaf, for example through ``systemd-run --user`` into its user manager, so this is not
a containment or security boundary. The later cross-uid path must require that the
engine user has no user manager during runs (no linger, no running user@ service):
engine linger plus an operator leaf is not containment (Lucas, Nov 7, on B6 and B9).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

LEAF_NAME = re.compile(r'run-[a-zA-Z0-9_-]{1,64}')

# Runs in the child: join the leaf, then exec. On any failure it reports through
# the error pipe and exits without exec, so the target never runs outside the leaf.
TRAMPOLINE = '''import os, sys
procs, err = int(sys.argv[1]), int(sys.argv[2])
try:
    os.write(procs, str(os.getpid()).encode())
    os.close(procs)
    os.set_inheritable(err, False)
    os.execvp(sys.argv[3], sys.argv[3:])
except BaseException as exc:
    os.write(err, repr(exc).encode()[:4000] or b'?')
    os._exit(127)
'''


class LaunchError(RuntimeError):
    """The target did not start inside the leaf."""


class CleanupIncomplete(RuntimeError):
    def __init__(self, outcome):
        super().__init__(f'leaf cleanup incomplete: {outcome}')
        self.outcome = outcome


@dataclass(frozen=True)
class Outcome:
    state: str  # terminated | empty | running | unknown
    returncode: int | None
    populated: bool | None
    main_exit_seen: bool
    errors: tuple[str, ...] = ()


class Cgroup2:
    """cgroup v2 filesystem operations. Tests substitute a fake."""
    def __init__(self, mount=Path('/sys/fs/cgroup')):
        self.mount = Path(os.path.realpath(mount))

    def make_leaf(self, leaf: Path):
        os.mkdir(leaf, 0o755)  # exclusive: an existing leaf is never reused

    def open_procs(self, leaf: Path) -> int:
        return os.open(leaf / 'cgroup.procs', os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW)

    def kill(self, leaf: Path):
        fd = os.open(leaf / 'cgroup.kill', os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.write(fd, b'1')
        finally:
            os.close(fd)

    def populated(self, leaf: Path) -> bool | None:
        try:
            text = (leaf / 'cgroup.events').read_text()
            value = dict(line.split() for line in text.splitlines()).get('populated')
        except (OSError, ValueError):
            return None
        return {'0': False, '1': True}.get(value)

    def member(self, pid: int) -> Path | None:
        """The cgroup v2 directory holding pid, or None when unreadable."""
        try:
            for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines():
                if line.startswith('0::/'):
                    return self.mount / line[4:].lstrip('/')
        except OSError:
            pass
        return None

    def remove(self, leaf: Path):
        """rmdir the leaf and any sub-cgroups bottom-up; never unlinks files."""
        for parent, dirs, _ in os.walk(leaf, topdown=False, followlinks=False):
            for name in dirs:
                os.rmdir(Path(parent) / name)
        os.rmdir(leaf)


class DelegatedRoot:
    """An operator-owned cgroup v2 directory delegated to this uid.

    The launcher process must itself live inside this subtree: cgroup v2 only lets an
    unprivileged writer migrate a process when it can write the common ancestor's
    cgroup.procs.
    """
    def __init__(self, path, fs: Cgroup2 | None = None):
        self.fs = fs or Cgroup2()
        path = Path(path)
        real = Path(os.path.realpath(path))
        if not path.is_absolute() or real != path:
            raise PermissionError('Delegated root must be an absolute path without symlinks')
        if real == self.fs.mount or not real.is_relative_to(self.fs.mount):
            raise PermissionError('Delegated root must be below the cgroup v2 mount')
        info = os.lstat(real)
        procs = os.lstat(real / 'cgroup.procs')
        if info.st_uid != os.getuid() or procs.st_uid != os.getuid():
            raise PermissionError('Delegated root is not owned by this uid (same uid only)')
        if not (real / 'cgroup.kill').exists():
            raise PermissionError('cgroup.kill missing: no kill path, refusing to launch')
        self.path = real

    def leaf(self, name: str) -> Path:
        if not isinstance(name, str) or not LEAF_NAME.fullmatch(name):
            raise ValueError('Leaf name must match run-[A-Za-z0-9_-]{1,64}')
        return self.path / name

    def launch(self, name, argv, *, env=None, cwd=None, stdin=None, stdout=None,
               stderr=None, start_timeout=30.0, kill_timeout=5.0):
        """Start argv inside a new leaf. Returns a LeafRun; use it as a context manager."""
        if not argv:
            raise ValueError('Empty launch')
        leaf = self.leaf(name)
        self.fs.make_leaf(leaf)
        run = LeafRun(self.fs, leaf, kill_timeout)
        try:
            if leaf.is_symlink() or os.lstat(leaf).st_uid != os.getuid():
                raise PermissionError('Leaf is not an operator-owned directory')
            run._spawn(list(argv), env, cwd, stdin, stdout, stderr, start_timeout)
        except BaseException as exc:
            run._cleanup_after(exc)
            raise
        return run

    def sweep(self, kill_timeout=5.0):
        """Kill leftover leaves (launcher died). Their exits are unseen: never terminated."""
        outcomes = {}
        for leaf in sorted(self.path.iterdir()):
            if leaf.is_dir() and not leaf.is_symlink() and LEAF_NAME.fullmatch(leaf.name):
                outcomes[leaf.name] = LeafRun(self.fs, leaf, kill_timeout).close()
        return outcomes


class LeafRun:
    def __init__(self, fs: Cgroup2, leaf: Path, kill_timeout: float):
        self.fs, self.leaf, self.kill_timeout = fs, leaf, kill_timeout
        self.proc = None
        self.pidfd = None
        self.outcome = None
        self.lock = threading.Lock()

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    def _spawn(self, argv, env, cwd, stdin, stdout, stderr, start_timeout):
        procs = self.fs.open_procs(self.leaf)
        read_end = write_end = -1
        try:
            read_end, write_end = os.pipe2(os.O_CLOEXEC)
            self.proc = subprocess.Popen(
                [sys.executable, '-I', '-S', '-c', TRAMPOLINE, str(procs), str(write_end), *argv],
                pass_fds=(procs, write_end), close_fds=True, start_new_session=True,
                env=env, cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr)
            os.close(write_end)
            write_end = -1
            # Unreaped child: its pid cannot be reused, so this pidfd names exactly it.
            self.pidfd = os.pidfd_open(self.proc.pid)
            message = self._read_status(read_end, start_timeout)
        finally:
            for fd in (procs, read_end, write_end):
                if fd >= 0:
                    os.close(fd)
        if message:
            raise LaunchError(f'trampoline failed before exec: {message}')
        where = self.fs.member(self.proc.pid)
        if where != self.leaf:
            raise LaunchError(f'process is not in the leaf (seen in {where})')

    @staticmethod
    def _read_status(fd, timeout):
        """EOF without data means exec succeeded (the pipe closed on exec)."""
        data, deadline = b'', time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                raise LaunchError('trampoline did not report exec in time')
            chunk = os.read(fd, 4096)
            if not chunk:
                return data.decode(errors='replace')
            data += chunk

    def wait(self, timeout=None):
        """Wait for the main process. Descendants are handled by close()."""
        return self.proc.wait(timeout)

    def close(self) -> Outcome:
        """Kill the leaf and collect evidence. Idempotent; never raises for kill errors."""
        with self.lock:
            if self.outcome is None:
                self.outcome = self._terminate()
            return self.outcome

    def _terminate(self):
        errors = {}  # distinct messages, first-seen order; the loop retries
        deadline = time.monotonic() + self.kill_timeout
        populated = None
        while True:
            try:
                self.fs.kill(self.leaf)
            except OSError as exc:
                errors.setdefault(f'cgroup.kill: {exc!r}')
            if self.pidfd is not None and self.proc.returncode is None:
                try:
                    signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    errors.setdefault(f'pidfd_send_signal: {exc!r}')
            if self.proc is not None:
                self.proc.poll()
            populated = self.fs.populated(self.leaf)
            seen = self.proc is not None and self.proc.returncode is not None
            if populated is False and (seen or self.proc is None):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        if self.pidfd is not None:
            os.close(self.pidfd)
            self.pidfd = None
        seen = self.proc is not None and self.proc.returncode is not None
        if populated is False and seen:
            state = 'terminated'
        elif populated is False and self.proc is None:
            state = 'empty'  # sweep, or no process started: empty, but no exit was seen
        elif populated is True:
            state = 'running'
        else:
            state = 'unknown'
        if populated is False:
            try:
                self.fs.remove(self.leaf)
            except OSError as exc:
                errors.setdefault(f'rmdir: {exc!r}')
        outcome = Outcome(state, self.proc.returncode if self.proc else None, populated,
                          seen, tuple(errors))
        if state not in ('terminated', 'empty') or errors:
            log.error('leaf %s: %s', self.leaf, outcome)
        return outcome

    def _cleanup_after(self, exc):
        """Clean up after a failure without hiding the original exception."""
        try:
            outcome = self.close()
        except BaseException as cleanup:
            exc.add_note(f'leaf cleanup raised: {cleanup!r}')
            log.exception('leaf %s cleanup raised', self.leaf)
            return
        if outcome.state not in ('terminated', 'empty') or outcome.errors:
            exc.add_note(f'leaf cleanup incomplete: {outcome}')

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if exc is not None:
            self._cleanup_after(exc)
            return False
        outcome = self.close()
        if outcome.state != 'terminated' or outcome.errors:
            raise CleanupIncomplete(outcome)
        return False
