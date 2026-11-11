"""Same-uid cgroup v2 leaf launch with kill-tree on every exit path (CSL-728).

The launcher creates a leaf under an operator-owned, delegated cgroup v2 root and
starts the child through a trampoline that joins the leaf before it execs the target,
so no target code runs outside the leaf. Every exit path (normal return, exception,
launch failure, cancellation) kills the leaf with ``cgroup.kill`` and books
``leaf_empty`` only on positive evidence: ``populated 0`` and the main process's exit
seen through its pidfd and ``waitpid``. Anything else is ``running`` (populated) or
``unknown``.

This launcher never books ``terminated``. A same-uid process can leave the leaf
unseen, and the leaf then reads empty while it lives, so an empty leaf is not proof
that the tree ended (PRODUCT.md 055cc7a5 section 3.4 S5 and the Stop bullet, C1-27:
never ``terminated`` for a tree with an unobservable escapee). ``terminated`` is
reserved for a launch path that can observe escapes; every outcome here carries
``escape_observable=False``.

Same uid only, and no cross-uid kill path. A process of the same uid can leave the
leaf, for example through ``systemd-run --user`` into its user manager, so this is not
a containment or security boundary. The later cross-uid path must require that the
engine user has no user manager during runs (no linger, no running user@ service):
engine linger plus an operator leaf is not containment (Lucas, 2026-11-07, on B6 and B9).
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
CLEANUP_ATTEMPTS = 5  # bounded: an interrupt storm leaves the leaf to the sweep

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
    state: str  # leaf_empty | empty | running | unknown; never terminated (S5, C1-27)
    returncode: int | None
    populated: bool | None
    main_exit_seen: bool
    errors: tuple[str, ...] = ()
    escape_observable: bool = False  # same uid: a process that left the leaf is unseen


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

    def adopt(self, name, kill_timeout=5.0):
        """Re-adopt a registered leaf after a restart: a handle that can kill it later.

        The pid is not this process's child, so its exit can never be seen here; the
        best close() can book is ``empty`` (S5: never terminated without a seen exit).
        """
        leaf = self.leaf(name)
        if leaf.is_symlink() or not leaf.is_dir() or os.lstat(leaf).st_uid != os.getuid():
            raise PermissionError('Leaf is not an operator-owned directory')
        return LeafRun(self.fs, leaf, kill_timeout)

    def sweep(self, keep, kill_timeout=5.0):
        """Kill leftover leaves that the durable run registry does not list.

        ``keep`` is required and must be every leaf name the registry still owns
        (runs to re-adopt and runs of any other launcher on this root). Those leaves
        are not touched; re-adopt them with adopt(). There is deliberately no
        kill-everything default: a daemon restart or upgrade must not end live runs
        (PRODUCT.md 055cc7a5 section 3.1, grill Q38 / O53). Exits of swept leaves are
        unseen, so they are never booked terminated.
        """
        if isinstance(keep, (str, bytes)):
            raise TypeError('keep must be a collection of leaf names, not a string')
        keep = set(keep)
        for name in keep:
            self.leaf(name)  # validate: a malformed name cannot match a leaf
        outcomes = {}
        for leaf in sorted(self.path.iterdir()):
            if leaf.name in keep:
                continue
            if leaf.is_dir() and not leaf.is_symlink() and LEAF_NAME.fullmatch(leaf.name):
                outcomes[leaf.name] = LeafRun(self.fs, leaf, kill_timeout).close()
        return outcomes


class LeafRun:
    def __init__(self, fs: Cgroup2, leaf: Path, kill_timeout: float):
        self.fs, self.leaf, self.kill_timeout = fs, leaf, kill_timeout
        self.proc = None
        self.pidfd = None
        self.outcome = None
        self.removing = self.removed = False
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
        """Kill the leaf and collect evidence. Never raises for kill errors.

        A final outcome (the leaf was removed) is cached and returned again. Any other
        outcome keeps the leaf and, while the main exit is unseen, the pidfd, so a
        later close() (or the startup sweep) retries the kill.
        """
        with self.lock:
            if self.outcome is None or not self.removed:
                self.outcome = self._terminate()
            return self.outcome

    def _terminate(self):
        if self.removing and not os.path.lexists(self.leaf):
            self.removed = True  # interrupted inside or after the rmdir
        if self.removed:  # the leaf was empty and is gone: rebuild the outcome
            seen = self.proc is not None and self.proc.returncode is not None
            return Outcome('leaf_empty' if seen else 'empty',
                           self.proc.returncode if self.proc else None, False, seen)
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
        seen = self.proc is not None and self.proc.returncode is not None
        if seen and self.pidfd is not None:
            # Close only once the exit is seen: until then the pidfd is the kill handle.
            # Detach first, so an interrupt leaks the fd rather than closing it twice.
            fd, self.pidfd = self.pidfd, None
            os.close(fd)
        if populated is False and seen:
            state = 'leaf_empty'  # not 'terminated': a same-uid escapee would be unseen
        elif populated is False and self.proc is None:
            state = 'empty'  # sweep, or no process started: empty, but no exit was seen
        elif populated is True:
            state = 'running'
        else:
            state = 'unknown'  # includes populated 0 with the main exit unseen
        if state in ('leaf_empty', 'empty'):
            self.removing = True
            try:
                self.fs.remove(self.leaf)
                self.removed = True
            except OSError as exc:
                errors.setdefault(f'rmdir: {exc!r}')
        outcome = Outcome(state, self.proc.returncode if self.proc else None, populated,
                          seen, tuple(errors))
        if state not in ('leaf_empty', 'empty') or errors:
            log.error('leaf %s: %s', self.leaf, outcome)
        return outcome

    def _close_retrying(self):
        """close(), retried when an interrupt lands inside it (C2-10: finally can be
        interrupted). Returns (outcome or None, interrupts). If every attempt is
        interrupted the leaf stays on disk for the startup sweep."""
        interrupts = []
        for _ in range(CLEANUP_ATTEMPTS):
            try:
                return self.close(), interrupts
            except BaseException as cleanup:
                interrupts.append(cleanup)
                log.exception('leaf %s cleanup interrupted', self.leaf)
        return None, interrupts

    def _cleanup_after(self, exc):
        """Clean up after a failure without hiding the original exception."""
        outcome, interrupts = self._close_retrying()
        for cleanup in interrupts:
            exc.add_note(f'leaf cleanup raised: {cleanup!r}')
        if outcome is None:
            exc.add_note(f'leaf cleanup abandoned after {CLEANUP_ATTEMPTS} interrupted '
                         'attempts; leaf kept for the startup sweep')
        elif outcome.state not in ('leaf_empty', 'empty') or outcome.errors:
            exc.add_note(f'leaf cleanup incomplete: {outcome}')

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if exc is not None:
            self._cleanup_after(exc)
            return False
        try:
            outcome = self.close()
        except BaseException as interrupt:
            log.exception('leaf %s cleanup interrupted', self.leaf)
            self._cleanup_after(interrupt)  # retries; the interrupt still propagates
            raise
        if outcome.state != 'leaf_empty' or outcome.errors:
            raise CleanupIncomplete(outcome)
        return False
