"""F21 / B7: privileged spawn path detection and measured backends (CSL-734).

PRODUCT.md at csl-research ``055cc7a5c793b7135e5d0565dc4198cf96230531`` (SHA-256
``e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf``): engines
run under a separate engine OS user (grill Q17). Where admin rights are refused,
managed login folders are the fallback. If the engine-user test on a privileged
Linux machine (fleet briefing F21) has not fully run by Nov 11, that fallback
ships as the beta default (Lucas, Nov 7: "Yes the fallback ships as beta").
Section 11.5: same-uid attackers beat every detection and prevention arm through
``/proc`` and ptrace, 20/20; only running engines under a separate uid (A3 arm
P3) closes these. The F21 same-uid channels named in the a3-prevention-fixes
record are ``/proc/<pid>/root``, ptrace, and an absolute-path store read.

This module detects spawn backends WITHOUT mutating system config: it does not
install a setuid helper, edit sudoers, edit polkit rules, create a permanent
system user, or write to ``/etc``. It measures p50/p95 spawn overhead (n>=10)
of every backend that can already spawn another *host* uid, runs the three F21
same-uid probes under each, and selects the managed-login-folder fallback as
beta when no such backend is available.

Selection follows O19: while any F21 engine-user item below is unmeasured, the
managed-login-folder fallback stays the beta default even when an other-uid
backend works; that backend is reported as ``SelectedPath.candidate``, not
selected. A backend is selectable only when all three F21 channels are blocked
on every attempt. ``nobody`` and ``daemon`` are shared system accounts: other
processes run under them, so they are same-uid attackers against an engine
there, and ``nobody`` has no home to hold logins. They are measurement
stand-ins only and are never selected as the engine user.

The dedicated engine user is named by the installer, not by this module:
``detect_backends(engine_user=...)`` or ``RECURSION_ENGINE_USER``. Detection
tries that user first and only then the stand-ins, so a host where sudo or
systemd-run reaches the engine user reports it as ``SelectedPath.candidate``
(and selects it once F21 has run). With no engine user configured, detection
can only reach stand-ins and selection is always the beta fallback.

The probes target the engine process itself: a process whose real,
effective, saved and filesystem uids all equal the backend's target uid (for
systemd-run, the transient unit's MainPID). They never fall back to the
launcher. A probe that cannot run is counted as an error, never as blocked.
Only ``EACCES`` or ``EPERM`` counts as blocked; any other errno (for example
``ENOENT`` or ``ESRCH`` when the engine exited first) is an error.

Unmeasured here (need a founder Linux box, or would require creating a real
other-uid home and signing in):

* creating the engine user at install
* Claude Code sign-in into that home surviving a reboot
* Codex sign-in into that home surviving a reboot
* live Secret Service / keyring behaviour for a headless user

The intended BYOK storage *decision* when Secret Service fails closed is
encoded from PRODUCT.md A44 / section 1 and can be unit-tested without
touching real keys: model-provider keys may use an owner-only file; broker
and venue keys stay keyring-only and therefore fail closed.

No provider or venue calls. No Claude Code or Codex binary is launched. No
credentials are copied or written.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import fcntl
import math
import os
import pwd
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

PRODUCT_MD_COMMIT = "055cc7a5c793b7135e5d0565dc4198cf96230531"
PRODUCT_MD_SHA256 = "e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf"

# Honest unmeasured items. None means "not measured on this path".
ENGINE_USER_CREATED_AT_INSTALL: None = None
CLAUDE_CODE_SIGNIN_SURVIVES_REBOOT: None = None
CODEX_SIGNIN_SURVIVES_REBOOT: None = None
HEADLESS_SECRET_SERVICE_LIVE: None = None

SETUID_HELPER_CANDIDATES = (
    "/usr/local/libexec/recursion-spawn-as",
    "/usr/libexec/recursion-spawn-as",
    "/usr/local/bin/recursion-spawn-as",
)
HELPER_ENV = "RECURSION_SPAWN_HELPER"
# Names the dedicated engine user detection should try first. No default:
# the name and its creation at install are F21 install work.
ENGINE_USER_ENV = "RECURSION_ENGINE_USER"
# Measurement stand-ins for the engine user. Never selected (see
# SHARED_SYSTEM_ACCOUNTS): they only show whether sudo reaches another uid.
SUDO_TARGET_CANDIDATES = ("nobody", "daemon")
# Shared system accounts that other processes also run as. An engine under
# one of these uids shares it with them, so the F21 same-uid channels stay
# open to those processes. 65534 is the kernel overflow uid (nobody).
SHARED_SYSTEM_ACCOUNTS: frozenset[str] = frozenset({"nobody", "daemon"})
SHARED_SYSTEM_UIDS: frozenset[int] = frozenset({65534})
DEFAULT_N = 20
# Commands every measured backend must be able to run as the target user.
# Overhead times ``true``. Process probes launch ``sleep 8`` (not a stand-in
# such as ``sleep 0``). The store writer runs ``python3 -c`` with
# ``store_writer_script`` (not ``python3 -c pass``). Sudoers rules are
# per-command, per-target and per-argument, so detection checks those
# launches rather than shorter aliases.
SLEEP_PROBE_ARGV: tuple[str, ...] = ("sleep", "8")
# Same-uid probe stores. The control probe uses a distinct prefix so the
# stale-directory sweep cannot delete a directory it is still using.
_STORE_DIR_PREFIX = "csl734-store-"
_STORE_SAME_PREFIX = "csl734-store-same-"
# Set in the parent before fork. The child compares getppid() to this value.
_SPAWNER_PID = 0
# PR_SET_PDEATHSIG retries. EINTR and EAGAIN are tried again; anything else,
# or this many failures, refuses the spawn. A bounded few, not an open loop.
_PDEATH_ATTEMPTS = 4
_LIBC: ctypes.CDLL | None = None
OVERHEAD_ARGV: tuple[str, ...] = ("true",)
SUDO_REQUIRED_COMMANDS: tuple[tuple[str, ...], ...] = (
    OVERHEAD_ARGV,
    SLEEP_PROBE_ARGV,
)
# Backends ``spawn_command`` can actually launch. Detection may still report
# others (for example a working subuid map) but they are never selected.
SPAWNABLE_BACKENDS: frozenset[str] = frozenset({"sudo", "systemd_run"})
PTRACE_ATTACH = 16
PTRACE_DETACH = 17
# Only a permission denial counts as a blocked F21 channel. Any other errno
# (the target exited or was reaped: ENOENT, ESRCH; or EIO, ENOTDIR, ...) means
# the channel was not tested on that attempt, so it is an error.
BLOCKED_ERRNOS: frozenset[int] = frozenset({errno.EACCES, errno.EPERM})

BackendName = Literal["setuid_helper", "sudo", "systemd_run", "subuid_userns"]
PathName = Literal[
    "setuid_helper", "sudo", "systemd_run", "subuid_userns", "managed_login_folder"
]
ProbeName = Literal["proc_pid_root", "ptrace", "abs_store_read"]
# True = blocked, False = open, None = the probe could not run (not a result).
ProbeOutcome = tuple[bool | None, str]
ByokKind = Literal["model_provider", "broker", "venue"]
ByokStorage = Literal["owner_only_file", "fail_closed"]

RunFn = Callable[[Sequence[str], float], "ProcResult"]
StatFn = Callable[[str], os.stat_result]


@dataclass(frozen=True, slots=True)
class ProcResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class Backend:
    name: BackendName
    available: bool
    changes_host_uid: bool
    reason: str
    target_user: str | None = None
    target_uid: int | None = None
    helper_path: str | None = None


@dataclass(frozen=True, slots=True)
class SelectedPath:
    name: PathName
    beta: bool
    reason: str
    backend: Backend | None = None
    # A working engine-user backend that is not selected because F21 has not
    # fully run (O19). Only set when ``name`` is ``managed_login_folder``.
    candidate: Backend | None = None


@dataclass(frozen=True, slots=True)
class Overhead:
    n: int
    p50_s: float
    p95_s: float
    min_s: float
    max_s: float
    command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProbeSummary:
    """``n`` attempts = ``blocked_count`` + open + ``error_count``.

    ``targets`` names the processes or files actually probed, for example
    ``sleep uid=65534 x20``, so a reader can check which uid was measured.
    """

    name: ProbeName
    n: int
    blocked_count: int
    detail: str
    error_count: int = 0
    targets: str = ""
    # Attempts whose error was a launch failure (engine process never appeared,
    # store writer failed), counted per attempt so a later success cannot hide
    # one; ``launch_failure_detail`` keeps the first such diagnostic.
    launch_failure_count: int = 0
    launch_failure_detail: str = ""

    @property
    def open_count(self) -> int:
        return self.n - self.blocked_count - self.error_count

    @property
    def blocked(self) -> bool:
        return self.n > 0 and self.error_count == 0 and self.blocked_count == self.n


@dataclass(frozen=True, slots=True)
class HostFacts:
    uid: int
    username: str
    sudo_n_true: bool
    subuid_lines: tuple[str, ...]
    subgid_lines: tuple[str, ...]
    newuidmap_path: str | None
    newuidmap_is_setuid_root: bool
    user_ns_max: int | None
    unshare_user_works: bool
    unshare_host_uid: int | None
    systemd_is_pid1: bool


@dataclass(frozen=True, slots=True)
class Evaluation:
    product_commit: str
    product_sha256: str
    facts: HostFacts
    backends: tuple[Backend, ...]
    selected: SelectedPath
    overhead: dict[str, Overhead]
    probes: dict[str, tuple[ProbeSummary, ...]]
    unmeasured: dict[str, None]
    errors: dict[str, str]


def byok_storage_when_secret_service_fails_closed(kind: str) -> ByokStorage:
    """Intended PRODUCT.md A44 / section 1 decision; not a live keyring probe.

    Live Secret Service behaviour on a headless engine user remains
    ``HEADLESS_SECRET_SERVICE_LIVE`` (unmeasured).
    """

    if kind == "model_provider":
        return "owner_only_file"
    if kind in {"broker", "venue"}:
        return "fail_closed"
    raise ValueError(f"unknown BYOK kind: {kind!r}")


def percentile(sorted_samples: Sequence[float], p: float) -> float:
    """Nearest-rank percentile; ``p`` is 0..100. ``sorted_samples`` must be non-empty."""

    if not sorted_samples:
        raise ValueError("percentile() requires at least one sample")
    if not 0 < p <= 100:
        raise ValueError(f"percentile p must be in (0, 100], got {p!r}")
    rank = max(1, math.ceil(p / 100.0 * len(sorted_samples)))
    return sorted_samples[rank - 1]


def can_spawn(backend: Backend) -> bool:
    """True when ``spawn_command`` can launch an engine through ``backend``."""

    if backend.name not in SPAWNABLE_BACKENDS:
        return False
    try:
        spawn_command(backend, ("true",))
    except ValueError:
        return False
    return True


def f21_open_items() -> tuple[str, ...]:
    """F21 engine-user items not yet measured (``None``). Empty once F21 has run."""

    items = {
        "engine_user_created_at_install": ENGINE_USER_CREATED_AT_INSTALL,
        "claude_code_signin_survives_reboot": CLAUDE_CODE_SIGNIN_SURVIVES_REBOOT,
        "codex_signin_survives_reboot": CODEX_SIGNIN_SURVIVES_REBOOT,
        "headless_secret_service_live": HEADLESS_SECRET_SERVICE_LIVE,
    }
    return tuple(name for name, value in items.items() if value is None)


def is_shared_system_account(backend: Backend) -> bool:
    """True when ``backend`` targets a shared system account such as ``nobody``."""

    return (
        backend.target_user in SHARED_SYSTEM_ACCOUNTS
        or backend.target_uid in SHARED_SYSTEM_UIDS
    )


def is_root_target(backend: Backend) -> bool:
    """True when ``backend`` targets root; root is never the engine user."""

    return backend.target_uid == 0 or backend.target_user == "root"


def select_path(
    backends: Sequence[Backend],
    *,
    f21_open: Sequence[str] | None = None,
) -> SelectedPath:
    """Pick the first spawnable engine-user backend once F21 has run; else beta fallback.

    ``f21_open`` lists unmeasured F21 items (default: ``f21_open_items()``).
    While it is non-empty the managed-login-folder fallback is the beta
    default (O19) and a working engine-user backend is only the ``candidate``.
    Backends that target root or a shared system account are never selected.
    """

    open_items = tuple(f21_open_items() if f21_open is None else f21_open)
    order: tuple[BackendName, ...] = (
        "setuid_helper",
        "sudo",
        "systemd_run",
        "subuid_userns",
    )
    by_name = {b.name: b for b in backends}
    stand_ins: list[str] = []
    candidate: Backend | None = None
    for name in order:
        backend = by_name.get(name)
        if not (
            backend is not None
            and backend.available
            and backend.changes_host_uid
            and can_spawn(backend)
        ):
            continue
        if is_root_target(backend):
            continue
        if is_shared_system_account(backend):
            stand_ins.append(f"{name} as {backend.target_user or backend.target_uid}")
            continue
        candidate = backend
        break
    if candidate is not None and not open_items:
        return SelectedPath(
            candidate.name,
            False,
            f"{candidate.name} can spawn the engine user without mutating system "
            "config, and F21 has run",
            candidate,
        )
    why: list[str] = []
    if candidate is not None:
        why.append(
            f"{candidate.name} can spawn the engine user but F21 has not fully run "
            f"(unmeasured: {', '.join(open_items)})"
        )
    else:
        why.append(
            "no spawnable engine-user backend is available without installing a "
            "helper or editing sudoers/polkit"
        )
    if stand_ins:
        why.append(
            "shared system accounts are measurement stand-ins, never the engine "
            f"user: {', '.join(stand_ins)}"
        )
    return SelectedPath(
        "managed_login_folder",
        True,
        f"{'; '.join(why)}; managed-login-folder fallback ships as beta "
        '(Lucas, Nov 7: "Yes the fallback ships as beta")',
        None,
        candidate,
    )


# Directories whose root-owned binaries this module may execute. ``/bin`` is
# not opened: on merged-/usr hosts it is a symlink, and opening it would
# follow that link. Callers that name ``/bin/<tool>`` are resolved in
# ``/usr/bin`` instead. The caller's ``PATH`` is never searched.
_TRUSTED_BIN_DIRS = ("/usr/bin", "/usr/sbin")


def _safe_leaf(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and "/" not in name and "\x00" not in name


def _trusted_locations(name: str) -> tuple[tuple[str, str], ...]:
    """``(directory, leaf)`` pairs that may name the executable ``name``."""

    if not name or "\x00" in name:
        raise OSError(errno.EINVAL, "refusing to execute an empty path")
    if name.startswith("/"):
        parent, leaf = name.rsplit("/", 1)
        if parent == "/bin":
            parent = "/usr/bin"
        if parent not in _TRUSTED_BIN_DIRS or not _safe_leaf(leaf):
            raise OSError(errno.EPERM, "executable is outside the trusted directories")
        return ((parent, leaf),)
    if not _safe_leaf(name):
        raise OSError(errno.EPERM, "refusing to execute a relative path")
    return tuple((base, name) for base in _TRUSTED_BIN_DIRS)


def _require_trusted_inode(fd: int) -> None:
    """The inode behind ``fd`` is a root-owned regular executable, not writable."""

    st = os.fstat(fd)
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_uid != 0
        or (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
        or not (st.st_mode & stat.S_IXUSR)
    ):
        raise OSError(errno.EPERM, "executable is not a root-owned regular file")


def _open_trusted_symlink(dirfd: int, leaf: str) -> int:
    """Open the same-directory target of a root-owned symlink, and pin that inode.

    ``O_NOFOLLOW`` on ``leaf`` returns ``ELOOP``. The link itself must be
    owned by root and its target must be a single leaf of this directory
    (``python3`` -> ``python3.12``). The fd that is returned is the target
    inode, checked again after the name is re-read, so a replacement of
    either name is not what gets executed.
    """

    link_fd = os.open(leaf, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd)
    try:
        link_stat = os.fstat(link_fd)
        if not stat.S_ISLNK(link_stat.st_mode) or link_stat.st_uid != 0:
            raise OSError(errno.EPERM, "refusing to follow an untrusted symlink")
        target = os.readlink(leaf, dir_fd=dirfd)
        if not _safe_leaf(target):
            raise OSError(errno.ELOOP, "symlink target leaves the trusted directory")
        bin_fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd)
        try:
            if (
                os.fstat(link_fd).st_ino != link_stat.st_ino
                or os.fstat(link_fd).st_dev != link_stat.st_dev
                or os.readlink(leaf, dir_fd=dirfd) != target
            ):
                raise OSError(errno.EAGAIN, "symlink changed between check and exec")
            opened = os.fstat(bin_fd)
            named = os.stat(target, dir_fd=dirfd, follow_symlinks=False)
            if (named.st_ino, named.st_dev) != (opened.st_ino, opened.st_dev):
                raise OSError(errno.EAGAIN, "executable changed between check and exec")
            _require_trusted_inode(bin_fd)
        except BaseException:
            os.close(bin_fd)
            raise
        return bin_fd
    finally:
        os.close(link_fd)


def _open_in_dir(dirfd: int, leaf: str) -> int:
    try:
        return os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dirfd)
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            raise
        return _open_trusted_symlink(dirfd, leaf)


def _open_trusted_executable(name: str) -> int:
    """Return an ``O_CLOEXEC`` fd for the inode ``name`` resolves to in a trusted dir.

    The child executes this fd (``executable=/proc/self/fd/N``), not the path,
    so a change to the directory entry after the check does not change the
    program that runs.
    """

    last: OSError | None = None
    for base, leaf in _trusted_locations(name):
        try:
            dirfd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            last = exc
            continue
        try:
            try:
                fd = _open_in_dir(dirfd, leaf)
            except OSError as exc:
                last = exc
                continue
        finally:
            os.close(dirfd)
        try:
            _require_trusted_inode(fd)
        except OSError as exc:
            os.close(fd)
            last = exc
            continue
        return fd
    if last is not None and last.errno == errno.ENOENT:
        raise FileNotFoundError(name) from last
    if last is None:
        raise FileNotFoundError(name)
    raise last


def _scrubbed_env() -> dict[str, str]:
    """Environment passed to every child.

    Nothing from the parent environment is copied. A parent ``PATH``,
    ``LD_PRELOAD``, ``PYTHONPATH`` or secret variable therefore cannot select
    the binary, inject code, or be read by the other uid via ``/proc``.
    """

    return {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}


def _linux_libc() -> ctypes.CDLL:
    """libc handles used from the child between fork and exec.

    Initialised in the parent so the child does not construct a ``CDLL``.
    ``signal`` and ``sigprocmask`` are the async-signal-safe calls; the
    preexec hook is already Python.
    """

    global _LIBC
    if _LIBC is not None:
        return _LIBC
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = (
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    )
    libc.prctl.restype = ctypes.c_int
    libc.signal.argtypes = (ctypes.c_int, ctypes.c_void_p)
    libc.signal.restype = ctypes.c_void_p
    libc.sigemptyset.argtypes = (ctypes.c_void_p,)
    libc.sigemptyset.restype = ctypes.c_int
    libc.sigaddset.argtypes = (ctypes.c_void_p, ctypes.c_int)
    libc.sigaddset.restype = ctypes.c_int
    libc.sigprocmask.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    libc.sigprocmask.restype = ctypes.c_int
    _LIBC = libc
    return libc


def _reset_and_unblock_sigterm(libc: ctypes.CDLL) -> bool:
    """Make SIGTERM the default, unblocked disposition. False if either call fails.

    Both a ``SIG_IGN`` disposition and a blocked SIGTERM are preserved across
    exec, so a parent-death SIGTERM would otherwise never arrive.
    """

    previous = libc.signal(signal.SIGTERM, ctypes.c_void_p(0))  # SIG_DFL
    if previous == ctypes.c_void_p(-1).value:  # SIG_ERR
        return False
    pending = ctypes.create_string_buffer(128)  # glibc sigset_t
    if libc.sigemptyset(pending) != 0:
        return False
    if libc.sigaddset(pending, signal.SIGTERM) != 0:
        return False
    if libc.sigprocmask(1, pending, None) != 0:  # SIG_UNBLOCK
        return False
    return True


def _prctl_parent_death(libc: ctypes.CDLL, signo: int) -> bool:
    """Arm ``PR_SET_PDEATHSIG`` to ``signo``.

    ``EINTR`` and ``EAGAIN`` are retried a bounded number of times. Any other
    errno, or a retry budget that runs out, is a failure: the child must not
    exec. When ``sigprocmask`` could not unblock SIGTERM, the caller passes
    SIGKILL here. A successful ``prctl(SIGKILL)`` is the documented fallback
    and returns true.
    """

    for _ in range(_PDEATH_ATTEMPTS):
        ctypes.set_errno(0)
        if libc.prctl(1, signo, 0, 0, 0) == 0:  # PR_SET_PDEATHSIG
            return True
        if ctypes.get_errno() not in (errno.EINTR, errno.EAGAIN):
            return False
    return False


def _refuse_spawn(marker_fd: int | None) -> None:
    """Stop the child before exec. The parent treats the marker byte as refusal.

    ``os._exit`` does not return. The write is best-effort: the parent also
    reaps a child that has already exited. A patched ``_exit`` that returns
    (tests) falls through, and the caller returns immediately.
    """

    if marker_fd is not None:
        try:
            os.write(marker_fd, b"!")
        except BaseException:
            pass
    os._exit(127)


def _arm_parent_death_signal(bin_fd: int | None = None, marker_fd: int | None = None) -> None:
    """Child ``preexec_fn``: arm parent death, then drop the exec fd.

    ``PR_SET_PDEATHSIG`` covers SIGTERM and SIGKILL of the parent. Nothing is
    installed in the parent: SIGTERM stays the default action, and there is
    no process-global handler. The child resets SIGTERM to ``SIG_DFL`` and
    unblocks it before arming SIGTERM. If that reset fails, the death signal
    is SIGKILL, which cannot be ignored or blocked, and a successful
    ``prctl(SIGKILL)`` still execs. The spawner pid is the one recorded
    before fork. ``getppid() == 1`` is the wrong race check: it would refuse
    a child whose spawner is pid 1, and a subreaper adoption must refuse
    exec instead of leaving the child alive.

    Fail closed. A non-zero ``prctl`` after the bounded retry, a getppid that
    is not the spawner, or any exception (``MemoryError`` from ``signal`` or
    ``create_string_buffer``, ``getppid`` ``EINTR``, and the rest) calls
    ``os._exit(127)`` after a one-byte marker write. The child never execs.
    The last step on the success path makes ``bin_fd`` non-inheritable so
    ``pass_fds`` does not leave ``/proc/self/fd`` open in the new image.
    """

    try:
        if os.uname().sysname == "Linux":
            libc = _LIBC if _LIBC is not None else _linux_libc()
            death = signal.SIGTERM if _reset_and_unblock_sigterm(libc) else signal.SIGKILL
            if not _prctl_parent_death(libc, death):
                _refuse_spawn(marker_fd)
                return
            spawner = _SPAWNER_PID
            if spawner > 0 and os.getppid() != spawner:
                _refuse_spawn(marker_fd)
                return
        if marker_fd is not None:
            os.set_inheritable(marker_fd, False)
        if bin_fd is not None:
            os.set_inheritable(bin_fd, False)
    except BaseException:
        _refuse_spawn(marker_fd)


def _spawn_preexec(bin_fd: int, marker_fd: int) -> Callable[[], None]:
    def _preexec() -> None:
        _arm_parent_death_signal(bin_fd, marker_fd)

    return _preexec


def _read_spawn_refusal(read_fd: int) -> bool:
    """True when the child wrote the refusal marker before exec.

    ``Popen`` returns once the child's error pipe closes, which is after
    preexec and before the new image has to have exited. A refusal byte is
    already in the pipe. An empty read or ``EAGAIN`` means preexec returned
    and the marker fd was only closed by exec.
    """

    os.set_blocking(read_fd, False)
    try:
        chunk = os.read(read_fd, 16)
    except BlockingIOError:
        return False
    return b"!" in chunk


def _reap_refused(proc: subprocess.Popen[bytes]) -> None:
    """Reap a child that exited in preexec. Kill it if it is somehow still alive."""

    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _popen_checked(
    argv: Sequence[str],
    *,
    stdout: int,
    stderr: int,
) -> subprocess.Popen[bytes]:
    """Spawn ``argv`` from a pinned trusted inode.

    stdin is ``DEVNULL``, the child is in a new session (it does not keep the
    caller's terminal), and the executable fd is passed across the fork only
    so the child can exec it. The preexec hook clears that fd's inherit bit
    before exec, so the new image has standard fds 0, 1 and 2. On Linux the
    child arms ``PR_SET_PDEATHSIG`` so it dies with this process. If the hook
    cannot arm it, the child ``_exit(127)`` and this raises ``OSError`` after
    reaping. No SIGTERM handler is installed here.
    """

    if not argv:
        raise FileNotFoundError("empty command")
    global _SPAWNER_PID
    _SPAWNER_PID = os.getpid()
    if os.uname().sysname == "Linux":
        _linux_libc()
    bin_fd = _open_trusted_executable(argv[0])
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    proc: subprocess.Popen[bytes] | None = None
    refused = False
    try:
        proc = subprocess.Popen(
            list(argv),
            executable=f"/proc/self/fd/{bin_fd}",
            pass_fds=(bin_fd, write_fd),
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            env=_scrubbed_env(),
            start_new_session=True,
            preexec_fn=_spawn_preexec(bin_fd, write_fd),
        )
        os.close(write_fd)
        write_fd = -1
        refused = _read_spawn_refusal(read_fd)
    finally:
        os.close(bin_fd)
        if write_fd >= 0:
            os.close(write_fd)
        os.close(read_fd)
        if refused and proc is not None:
            _reap_refused(proc)
    if refused:
        raise OSError(errno.ECANCELED, "spawn refused before exec")
    if proc is None:
        raise OSError(errno.ECANCELED, "spawn refused before exec")
    return proc


def _default_run(cmd: Sequence[str], timeout: float) -> ProcResult:
    if not cmd:
        return ProcResult(127, "", "empty command")
    try:
        proc = _popen_checked(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        return ProcResult(127, "", str(exc))
    except OSError as exc:
        return ProcResult(127, "", str(exc))
    timed_out = False
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            out, err = proc.communicate()
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
    text_out = (out or b"").decode("utf-8", "replace")
    text_err = (err or b"").decode("utf-8", "replace")
    if timed_out:
        return ProcResult(124, text_out, text_err or "timeout")
    return ProcResult(proc.returncode if proc.returncode is not None else 1, text_out, text_err)


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# Linux uid_t/gid_t are 32-bit. 0 is root. 0xFFFFFFFF is (uid_t)-1, which
# setuid/setgid treat as "do not change" and which is not a host account.
_HOST_ID_MAX = 0xFFFFFFFF


def _parse_host_id(token: str) -> int | None:
    """One canonical host uid or gid, or ``None``.

    Only ASCII digits, at most ten of them (``2**32-1``), and only values that
    fit in 32 bits. ``²``, ``-1`` and ``4294967296`` are rejected here so a
    later ``int()`` cannot raise or accept a value that truncates to root.
    """

    if not token or not token.isascii() or not token.isdigit() or len(token) > 10:
        return None
    try:
        value = int(token)
    except ValueError:
        return None
    if value > _HOST_ID_MAX:
        return None
    return value


def _parse_uid_output(text: str) -> int | None:
    """Parse one line of ``id -u`` or ``id -g``.

    More than one line is rejected. A trailing line such as ``990`` must not
    hide an earlier ``0``.
    """

    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    token = lines[0]
    if token.startswith("uid=") or token.startswith("gid="):
        body = token.split()[0].split("=", 1)[1]
        body = body.split("(", 1)[0]
        return _parse_host_id(body)
    return _parse_host_id(token)


def _parse_id_list(text: str) -> tuple[int, ...] | None:
    """Parse one line of ``id -G`` or a ``/proc`` id field. ``None`` if any token is bad."""

    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    values: list[int] = []
    for part in lines[0].split():
        value = _parse_host_id(part)
        if value is None:
            return None
        values.append(value)
    if not values:
        return None
    return tuple(values)


def _unusable_host_id(value: int | None) -> bool:
    """True for missing, root, the overflow sentinel, and non-canonical ids."""

    if value is None:
        return True
    if value < 0 or value > _HOST_ID_MAX:
        return True
    return value in {0, _HOST_ID_MAX}


def _rejected_target_uid(uid: int | None, our_uid: int) -> bool:
    """True when ``uid`` must not be the engine user."""

    return uid is None or uid == our_uid or _unusable_host_id(uid)


def _caller_group_set(caller_groups: Collection[int] | None = None) -> frozenset[int] | None:
    """The caller's primary, effective and supplementary gids.

    ``caller_groups`` replaces that set in tests. ``None`` reads this process.
    ``None`` as a result means the process group list could not be read:
    ``os.getgroups`` failed, so the set is not usable. When ``/proc/self/status``
    ``Groups`` is readable, those gids are included too, so a short
    ``getgroups`` list cannot hide an overlapping gid.
    """

    if caller_groups is not None:
        return frozenset(caller_groups)
    found = {os.getgid(), os.getegid()}
    try:
        found.update(os.getgroups())
    except OSError:
        return None
    proc_groups = host_groups_of(os.getpid())
    if proc_groups is not None:
        found.update(proc_groups)
    return frozenset(found)


def _gid_set_rejected(
    primary: int | None,
    groups: tuple[int, ...] | None,
    *,
    caller_groups: Collection[int] | None = None,
) -> str | None:
    """Return a rejection when these gids must not own the engine.

    The engine's primary gid, and every gid in its group list, must be
    disjoint from the caller's full group set (``getgid``, ``getegid`` and
    ``getgroups``). A shared gid would make group-readable engine files the
    caller's. Supplementary group 0 is root's group. Pass ``caller_groups``
    to inject that set; ``None`` reads this process.
    """

    if _unusable_host_id(primary):
        return f"rejected: primary gid {primary} is root or not a host gid"
    caller = _caller_group_set(caller_groups)
    if caller is None:
        return "rejected: caller group list unavailable"
    if primary in caller:
        return f"rejected: primary gid {primary} is in the caller's groups"
    if groups is None:
        return "rejected: group list missing"
    if primary not in groups:
        return f"rejected: primary gid {primary} is not in the group list"
    if any(_unusable_host_id(gid) for gid in groups):
        return "rejected: a group id is root or not a host gid"
    shared = next((gid for gid in groups if gid in caller), None)
    if shared is not None:
        return f"rejected: group id {shared} is in the caller's groups"
    return None


def _target_gids_rejected(
    run_fn: RunFn,
    prefix: Sequence[str],
    id_bin: str,
    *,
    caller_groups: Collection[int] | None = None,
) -> str | None:
    """``id -g`` and ``id -G`` under ``prefix``. A reason, or ``None`` when usable."""

    gid_result = run_fn((*prefix, id_bin, "-g"), 8.0)
    groups_result = run_fn((*prefix, id_bin, "-G"), 8.0)
    gid = _parse_uid_output(gid_result.stdout) if gid_result.returncode == 0 else None
    groups = _parse_id_list(groups_result.stdout) if groups_result.returncode == 0 else None
    if gid_result.returncode != 0 or groups_result.returncode != 0:
        err = (
            gid_result.stderr or groups_result.stderr or gid_result.stdout or ""
        ).strip().replace("\n", " ")
        return (
            f"id -g/-G failed rc={gid_result.returncode}/{groups_result.returncode} "
            f"err={err[:160]}"
        )
    return _gid_set_rejected(gid, groups, caller_groups=caller_groups)


def _helper_paths(env: Mapping[str, str] | None) -> tuple[str, ...]:
    extra: list[str] = []
    source = env if env is not None else os.environ
    configured = source.get(HELPER_ENV, "").strip()
    if configured:
        extra.append(configured)
    return tuple(extra + list(SETUID_HELPER_CANDIDATES))


def _configured_engine_user(
    engine_user: str | None, env: Mapping[str, str] | None
) -> str | None:
    """``engine_user`` if given, else ``RECURSION_ENGINE_USER``; ``None`` when unset."""

    if engine_user is None:
        source = env if env is not None else os.environ
        engine_user = source.get(ENGINE_USER_ENV, "")
    engine_user = engine_user.strip()
    return engine_user or None


def _is_setuid_root(st: os.stat_result) -> bool:
    return bool(st.st_mode & stat.S_ISUID) and st.st_uid == 0


def _inspect_helpers(
    paths: Sequence[str],
    stat_fn: StatFn,
) -> tuple[str | None, str]:
    notes: list[str] = []
    found: str | None = None
    for path in paths:
        try:
            st = stat_fn(path)
        except OSError:
            notes.append(f"{path} absent")
            continue
        mode = stat.filemode(st.st_mode)
        setuid_root = _is_setuid_root(st)
        notes.append(
            f"{path} exists mode={mode} owner_uid={st.st_uid} setuid_root={setuid_root}"
        )
        if setuid_root and found is None:
            found = path
    if found is None:
        notes.append("not installing a setuid helper")
    else:
        notes.append(
            "found a setuid-root helper; invocation protocol is not recorded, "
            "so it is not executed"
        )
    return found, "; ".join(notes)


def _sudo_n_true(run_fn: RunFn) -> bool:
    result = run_fn(("sudo", "-n", "true"), 8.0)
    return result.returncode == 0


def store_writer_script(path: str) -> str:
    """Python ``-c`` body used by the absolute-path store probe (and sudo detection)."""

    quoted = repr(str(path))
    return (
        "import os; p=" + quoted + "; "
        "fd=os.open(p, os.O_CREAT|os.O_WRONLY|os.O_TRUNC, 0o600); "
        "os.write(fd, b'store-bytes'); os.close(fd); os.chmod(p, 0o600)"
    )


def _launch_timeout(argv: Sequence[str]) -> float:
    """Timeout for one detection launch: 8 s, plus the sleep itself for ``sleep N``.

    The launch is the exact measured command (``sleep 8``); only the runner's
    timeout grows, so an authorised sleep is not reported as rc 124.
    """

    base = 8.0
    if len(argv) == 2 and argv[0] == "sleep":
        try:
            return base + float(argv[1])
        except ValueError:
            return base
    return base


def _probe_sudo_target(
    run_fn: RunFn,
    our_uid: int,
    candidates: Sequence[str] = SUDO_TARGET_CANDIDATES,
    *,
    caller_groups: Collection[int] | None = None,
) -> tuple[str | None, int | None, str]:
    """First candidate sudo reaches as a non-root other uid for every launch
    the measured path actually runs. Independent of ``sudo -n true`` (root)."""

    notes: list[str] = []
    for user in candidates:
        result = run_fn(("sudo", "-n", "-u", user, "--", "id", "-u"), 8.0)
        uid = _parse_uid_output(result.stdout)
        if result.returncode != 0 or _rejected_target_uid(uid, our_uid):
            err = (result.stderr or result.stdout).strip().replace("\n", " ")
            notes.append(
                f"sudo -n -u {user} -- id -u rc={result.returncode} uid={uid} err={err[:160]}"
            )
            continue
        gid_reason = _target_gids_rejected(
            run_fn,
            ("sudo", "-n", "-u", user, "--"),
            "id",
            caller_groups=caller_groups,
        )
        if gid_reason is not None:
            notes.append(f"sudo -n -u {user} {gid_reason}")
            continue
        notes.append(f"sudo -n -u {user} -- id -u -> {uid}")
        denied = None
        with tempfile.TemporaryDirectory(prefix="csl734-sudo-probe-") as tmp:
            # The writer runs as the target user: like the probe store
            # directory, let another uid create its file (sticky, 1777).
            # chmod after the sleep launches so a local user cannot spend
            # those eight seconds planting a file the writer will open.
            # Unpredictable name: a pre-created probe.secret (r4507085798)
            # must not be the path the target-user writer opens.
            for argv in SUDO_REQUIRED_COMMANDS:
                check = run_fn(
                    ("sudo", "-n", "-u", user, "--", *argv), _launch_timeout(argv)
                )
                if check.returncode != 0:
                    err = (check.stderr or check.stdout).strip().replace("\n", " ")
                    shown = " ".join(argv)
                    denied = (
                        f"sudo -n -u {user} -- {shown} "
                        f"rc={check.returncode} err={err[:160]}"
                    )
                    break
            else:
                os.chmod(tmp, 0o1777)
                store = str(Path(tmp) / f"probe-{os.getpid()}-{time.time_ns()}.secret")
                argv = ("python3", "-c", store_writer_script(store))
                check = run_fn(
                    ("sudo", "-n", "-u", user, "--", *argv), _launch_timeout(argv)
                )
                if check.returncode != 0:
                    err = (check.stderr or check.stdout).strip().replace("\n", " ")
                    denied = (
                        f"sudo -n -u {user} -- python3 -c "
                        f"rc={check.returncode} err={err[:160]}"
                    )
        if denied is not None:
            notes.append(denied)
            continue
        notes.append(
            "sudo -n -u "
            f"{user} runs true, sleep 8 and the store-writer python3 -c script"
        )
        return user, uid, "; ".join(notes)
    return None, None, "; ".join(notes) if notes else "no sudo target candidates"


def _probe_systemd_run(
    run_fn: RunFn,
    user: str,
    our_uid: int,
    *,
    caller_groups: Collection[int] | None = None,
) -> tuple[bool, int | None, str]:
    prefix = (
        "systemd-run",
        f"--uid={user}",
        "--wait",
        "--pipe",
        "--collect",
    )
    result = run_fn((*prefix, "/usr/bin/id", "-u"), 8.0)
    uid = _parse_uid_output(result.stdout)
    err = (result.stderr or "").strip().replace("\n", " ")
    # Like the sudo target: never root, never our own uid, never an id that
    # truncates to root or is the overflow sentinel. Root passes every F21
    # probe from uid 1000, so the gate cannot catch it later.
    ok = result.returncode == 0 and not _rejected_target_uid(uid, our_uid)
    reason = (
        f"systemd-run --uid={user} --wait --pipe --collect /usr/bin/id -u "
        f"rc={result.returncode} uid={uid}"
    )
    if err:
        reason = f"{reason} err={err[:240]}"
    if result.returncode == 0 and uid is not None and _rejected_target_uid(uid, our_uid):
        reason = f"{reason}; rejected: engine user must not be root or the calling uid"
    if not ok:
        return False, None, reason
    gid_reason = _target_gids_rejected(
        run_fn, prefix, "/usr/bin/id", caller_groups=caller_groups
    )
    if gid_reason is not None:
        return False, None, f"{reason}; {gid_reason}"
    return True, uid, reason


def _subid_lines(username: str, text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    prefix = f"{username}:"
    return tuple(
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#") and line.startswith(prefix)
    )


def _proc_field(pid: int, key: str) -> str | None:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    prefix = f"{key}:"
    for line in status.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return None


def _proc_ids(pid: int, key: str) -> tuple[int, ...] | None:
    """Parsed ``Uid`` or ``Gid`` line. ``None`` when missing or not canonical host ids."""

    field = _proc_field(pid, key)
    if not field:
        return None
    return _parse_id_list(field)


def host_uids_of(pid: int) -> tuple[int, ...] | None:
    """Real, effective, saved and filesystem uid of ``pid`` (``/proc`` Uid line)."""

    return _proc_ids(pid, "Uid")


def host_gids_of(pid: int) -> tuple[int, ...] | None:
    """Real, effective, saved and filesystem gid of ``pid`` (``/proc`` Gid line)."""

    return _proc_ids(pid, "Gid")


def host_groups_of(pid: int) -> tuple[int, ...] | None:
    """Supplementary groups of ``pid``. ``()`` when the line is present but empty.

    ``None`` when the line is missing or a token is not a host gid. An empty
    supplementary list is not root. A missing line is not evidence.
    """

    field = _proc_field(pid, "Groups")
    if field is None:
        return None
    if not field:
        return ()
    return _parse_id_list(field)


def _process_gids_ok(
    gids: tuple[int, ...] | None, groups: tuple[int, ...] | None
) -> bool:
    """False when any credential gid is root, overflow, or unknown."""

    if not gids or any(_unusable_host_id(gid) for gid in gids):
        return False
    if groups is None or any(_unusable_host_id(gid) for gid in groups):
        return False
    return True


def host_uid_of(pid: int) -> int | None:
    uids = host_uids_of(pid)
    return uids[0] if uids else None


def _comm_of(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "?"


def _children_of(pid: int) -> list[int]:
    try:
        raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8")
    except OSError:
        return []
    return [int(part) for part in raw.split() if part.isdigit()]


def find_target_uid_pid(
    root_pid: int,
    target_uid: int,
    *,
    timeout_s: float = 1.5,
    uids_fn: Callable[[int], tuple[int, ...] | None] = host_uids_of,
    gids_fn: Callable[[int], tuple[int, ...] | None] = host_gids_of,
    groups_fn: Callable[[int], tuple[int, ...] | None] = host_groups_of,
    children_fn: Callable[[int], list[int]] = _children_of,
) -> int | None:
    """Walk ``root_pid`` and its descendants for the engine process.

    Matches only a process whose real, effective, saved and filesystem uids
    all equal ``target_uid``, and whose gids are not root or the overflow
    sentinel, so a launcher such as sudo (real uid = caller, effective uid = 0)
    is never returned. ``None`` when nothing matches before the timeout;
    callers record that as an error and never probe another pid.
    """

    deadline = time.monotonic() + timeout_s
    while True:
        seen: set[int] = set()
        stack = [root_pid]
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            uids = uids_fn(pid)
            if (
                uids
                and not _unusable_host_id(target_uid)
                and all(uid == target_uid for uid in uids)
                and _process_gids_ok(gids_fn(pid), groups_fn(pid))
            ):
                return pid
            stack.extend(children_fn(pid))
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def systemd_unit_main_pid(
    unit: str,
    target_uid: int,
    *,
    run_fn: RunFn,
    timeout_s: float = 3.0,
    uids_fn: Callable[[int], tuple[int, ...] | None] = host_uids_of,
    gids_fn: Callable[[int], tuple[int, ...] | None] = host_gids_of,
    groups_fn: Callable[[int], tuple[int, ...] | None] = host_groups_of,
) -> int | None:
    """MainPID of a transient systemd-run unit, if it runs entirely as ``target_uid``.

    The unit is forked by PID 1, not by the systemd-run client, so a walk of
    the client's descendants cannot find it.
    """

    deadline = time.monotonic() + timeout_s
    while True:
        result = run_fn(("systemctl", "show", "-p", "MainPID", "--value", unit), 5.0)
        token = (result.stdout or "").strip()
        if result.returncode == 0 and token.isdigit() and int(token) > 0:
            pid = int(token)
            uids = uids_fn(pid)
            if (
                uids
                and not _unusable_host_id(target_uid)
                and all(uid == target_uid for uid in uids)
                and _process_gids_ok(gids_fn(pid), groups_fn(pid))
            ):
                return pid
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def _ptrace(request: int, pid: int) -> tuple[int, int]:
    libc_name = ctypes.util.find_library("c")
    if not libc_name:
        return -1, 0
    libc = ctypes.CDLL(libc_name, use_errno=True)
    libc.ptrace.argtypes = [
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    libc.ptrace.restype = ctypes.c_long
    ctypes.set_errno(0)
    rc = int(libc.ptrace(request, pid, None, None))
    return rc, ctypes.get_errno()


def probe_proc_pid_root(pid: int) -> ProbeOutcome:
    path = f"/proc/{pid}/root"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        detail = f"{type(exc).__name__}:{exc.errno}:{exc.strerror}"
        if exc.errno in BLOCKED_ERRNOS:
            return True, detail
        return None, f"not_denied {detail}"
    os.close(fd)
    return False, "opened"


def probe_ptrace(pid: int) -> ProbeOutcome:
    rc, err = _ptrace(PTRACE_ATTACH, pid)
    if rc == -1 and err == 0:
        return None, "ptrace unavailable: libc not found"
    if rc == 0:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        _ptrace(PTRACE_DETACH, pid)
        return False, "attached"
    detail = f"rc={rc}:errno={err}:{os.strerror(err) if err else 'ptrace failed'}"
    if err in BLOCKED_ERRNOS:
        return True, detail
    return None, f"not_denied {detail}"


def probe_abs_store_read(
    write_cmd: Sequence[str],
    directory: str,
    *,
    expected_owner: int | None = None,
    run_fn: RunFn = _default_run,
) -> ProbeOutcome:
    """Have ``write_cmd`` create a 0600 store, then try to read it by absolute path.

    A writer that fails, leaves no file, or wrote as a uid other than
    ``expected_owner`` is an error (``None``), not a blocked read.
    """

    store = Path(directory) / f"store-{os.getpid()}-{time.time_ns()}.secret"
    result = run_fn([*write_cmd, "python3", "-c", store_writer_script(str(store))], 8.0)
    if result.returncode != 0:
        return None, f"writer_failed rc={result.returncode} err={(result.stderr or '')[:160]}"
    try:
        owner_uid = store.stat().st_uid
    except OSError as exc:
        return None, f"writer_left_no_store {type(exc).__name__}:{exc.errno}"
    if expected_owner is not None and owner_uid != expected_owner:
        try:
            store.unlink()
        except OSError:
            pass
        return None, f"writer_wrong_uid owner={owner_uid} expected={expected_owner}"
    try:
        data = store.read_bytes()
    except OSError as exc:
        owner = None
        mode = None
        try:
            st = store.stat()
            owner = st.st_uid
            mode = oct(st.st_mode)
        except OSError:
            pass
        detail = f"{type(exc).__name__}:{exc.errno}:{exc.strerror} owner={owner} mode={mode}"
        if exc.errno in BLOCKED_ERRNOS:
            return True, detail
        return None, f"not_denied {detail}"
    finally:
        try:
            store.unlink()
        except OSError:
            pass
    return False, f"read {len(data)} bytes"


def spawn_command(
    backend: Backend, argv: Sequence[str], *, unit: str | None = None
) -> list[str]:
    """``unit`` names the transient systemd-run unit; other backends ignore it."""

    if backend.name == "sudo":
        if not backend.target_user:
            raise ValueError("sudo backend needs target_user")
        return ["sudo", "-n", "-u", backend.target_user, "--", *argv]
    if backend.name == "systemd_run":
        if not backend.target_user:
            raise ValueError("systemd_run backend needs target_user")
        return [
            "systemd-run",
            f"--uid={backend.target_user}",
            *([f"--unit={unit}"] if unit else []),
            "--wait",
            "--pipe",
            "--collect",
            "--",
            *argv,
        ]
    if backend.name == "setuid_helper":
        raise ValueError("setuid helper invocation protocol is not recorded")
    if backend.name == "subuid_userns":
        raise ValueError(
            "subuid user-namespace spawn is not implemented or measured; detection only"
        )
    raise ValueError(f"unknown backend: {backend.name}")


def measure_overhead(backend: Backend, n: int = DEFAULT_N) -> Overhead:
    if n < 10:
        raise ValueError(f"overhead n must be >= 10, got {n}")
    cmd = tuple(spawn_command(backend, OVERHEAD_ARGV))
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        result = _default_run(cmd, 10.0)
        dt = time.perf_counter() - t0
        if result.returncode != 0:
            raise RuntimeError(
                f"overhead command failed rc={result.returncode} "
                f"err={(result.stderr or '')[:200]} cmd={cmd}"
            )
        samples.append(dt)
    samples.sort()
    return Overhead(
        n=n,
        p50_s=percentile(samples, 50),
        p95_s=percentile(samples, 95),
        min_s=samples[0],
        max_s=samples[-1],
        command=cmd,
    )


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:
            pass


class _Tally:
    """Per-channel counts: blocked, open, or error (probe could not run)."""

    def __init__(self) -> None:
        self.blocked = {"proc_pid_root": 0, "ptrace": 0, "abs_store_read": 0}
        self.errors = {"proc_pid_root": 0, "ptrace": 0, "abs_store_read": 0}
        self.details = {"proc_pid_root": "", "ptrace": "", "abs_store_read": ""}
        self.launch_failures = {"proc_pid_root": 0, "ptrace": 0, "abs_store_read": 0}
        self.launch_details = {"proc_pid_root": "", "ptrace": "", "abs_store_read": ""}
        self.targets: dict[str, dict[str, int]] = {
            "proc_pid_root": {},
            "ptrace": {},
            "abs_store_read": {},
        }

    def add(self, name: str, outcome: ProbeOutcome, target: str) -> None:
        result, detail = outcome
        if result is None:
            self.errors[name] += 1
            if _is_launch_failure(name, detail):
                self.launch_failures[name] += 1
                if not self.launch_details[name]:
                    self.launch_details[name] = detail
        elif result:
            self.blocked[name] += 1
        self.details[name] = detail
        seen = self.targets[name]
        seen[target] = seen.get(target, 0) + 1

    def summaries(self, n: int) -> tuple[ProbeSummary, ...]:
        def targets(name: str) -> str:
            return ", ".join(f"{k} x{v}" for k, v in sorted(self.targets[name].items()))

        return tuple(
            ProbeSummary(
                name,  # type: ignore[arg-type]
                n,
                self.blocked[name],
                self.details[name],
                self.errors[name],
                targets(name),
                self.launch_failures[name],
                self.launch_details[name],
            )
            for name in ("proc_pid_root", "ptrace", "abs_store_read")
        )


def _probe_pid(tally: _Tally, pid: int | None, missing: str) -> None:
    if pid is None:
        tally.add("proc_pid_root", (None, missing), "not found")
        tally.add("ptrace", (None, missing), "not found")
        return
    uids = host_uids_of(pid)
    target = f"{_comm_of(pid)} uid={uids[0] if uids else '?'}"
    tally.add("proc_pid_root", probe_proc_pid_root(pid), target)
    tally.add("ptrace", probe_ptrace(pid), target)


def _probe_store_reap_age_s() -> float:
    """Age past which a ``csl734-store-<timestamp>-*`` directory is stale.

    This is the runner timeout of the sleep probe command. A directory
    younger than that may belong to a probe that has just started.
    """

    return _launch_timeout(SLEEP_PROBE_ARGV)


def _store_name_created_at(name: str) -> float | None:
    """Creation time recorded in ``csl734-store-<seconds-or-ns>-*``, or ``None``.

    Sticky ``/tmp`` refuses a rename by anyone but the owner, so another user
    cannot rewrite the timestamp in the name. They can create a file inside a
    mode ``01777`` directory and refresh ``st_mtime`` and ``st_ctime``. A
    named store is therefore removed only when the name and both timestamps
    are old, and only when the live holder is not locking it.
    """

    if not name.startswith(_STORE_DIR_PREFIX) or name.startswith(_STORE_SAME_PREFIX):
        return None
    token = name[len(_STORE_DIR_PREFIX):].split("-", 1)[0]
    if not token.isdigit() or len(token) < 9:
        return None
    value = int(token)
    if len(token) >= 16:
        return value / 1_000_000_000
    return float(value)


def _age_past(when: float, now: float, min_age: float) -> bool:
    return now - when > min_age


def _store_dir_is_stale(name: str, st: os.stat_result, now: float, min_age: float) -> bool:
    """True when this directory is old enough to remove.

    A name that parses as a timestamp must be old, and so must ``st_mtime``
    and ``st_ctime``. A fresh timestamp means the directory is still in use
    or was just created, even when the other clock looks old. A name with no
    timestamp is removed only when the mode is exactly ``0700`` and
    ``st_mtime`` is old: another user cannot write inside it.
    """

    created = _store_name_created_at(name)
    if created is None:
        if stat.S_IMODE(st.st_mode) != 0o700:
            return False
        return _age_past(st.st_mtime, now, min_age)
    if not _age_past(created, now, min_age):
        return False
    return _age_past(st.st_mtime, now, min_age) and _age_past(st.st_ctime, now, min_age)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _lock_store_dir(path: str) -> int:
    """Exclusive flock on an ``O_DIRECTORY`` fd. The caller closes it.

    The live probe holds this for the store's whole lifetime. ``O_CLOEXEC``
    keeps the fd out of children. Closing the fd releases the lock.
    """

    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _lock_store_nowait(fd: int) -> bool:
    """``LOCK_EX|LOCK_NB``. False when another holder has the directory.

    ``EINTR`` is retried a bounded number of times. Any other error skips
    the directory: a sweeper that cannot lock must not delete.
    """

    for _ in range(_PDEATH_ATTEMPTS):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno != errno.EINTR:
                return False
    return False


def _path_is_inode(path: str, identity: os.stat_result) -> bool:
    try:
        named = os.lstat(path)
    except OSError:
        return False
    return (
        _same_inode(named, identity)
        and stat.S_ISDIR(named.st_mode)
        and not stat.S_ISLNK(named.st_mode)
    )


def _remove_dir_entries(dirfd: int) -> None:
    """Unlink the entries of the directory open on ``dirfd``. The directory stays.

    Subdirectories are opened ``O_NOFOLLOW`` and removed only when the opened
    inode is the one ``lstat`` just saw. Symlinks are unlinked, not followed.
    """

    try:
        names = os.listdir(dirfd)
    except OSError:
        return
    for name in names:
        try:
            st = os.lstat(name, dir_fd=dirfd)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
            try:
                sub = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=dirfd,
                )
            except OSError:
                continue
            try:
                try:
                    opened = os.fstat(sub)
                except OSError:
                    continue
                if not _same_inode(opened, st):
                    continue
                _remove_dir_entries(sub)
            finally:
                os.close(sub)
            try:
                os.rmdir(name, dir_fd=dirfd)
            except OSError:
                pass
        else:
            try:
                os.unlink(name, dir_fd=dirfd)
            except OSError:
                pass


def _rmdir_if_inode(path: str, identity: os.stat_result) -> None:
    """``rmdir`` ``path`` only when that directory entry is still ``identity``."""

    parent_path = os.path.dirname(path) or "/"
    name = os.path.basename(path)
    if not name or name in {".", ".."}:
        return
    try:
        parent = os.open(parent_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        try:
            named = os.lstat(name, dir_fd=parent)
        except OSError:
            return
        if not _same_inode(named, identity) or not stat.S_ISDIR(named.st_mode):
            return
        if stat.S_ISLNK(named.st_mode):
            return
        try:
            os.rmdir(name, dir_fd=parent)
        except OSError:
            pass
    finally:
        os.close(parent)


def _reap_one_store_dir(
    path: str,
    name: str,
    listed: os.stat_result,
    min_age: float,
    euid: int,
) -> None:
    """Remove one stale store if we can lock it and it is still that inode."""

    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return
    try:
        if not _lock_store_nowait(fd):
            return
        try:
            held = os.fstat(fd)
        except OSError:
            return
        if not _same_inode(held, listed) or held.st_uid != euid:
            return
        if not stat.S_ISDIR(held.st_mode):
            return
        if not _store_dir_is_stale(name, held, time.time(), min_age):
            return
        if not _path_is_inode(path, held):
            return
        _remove_dir_entries(fd)
        _rmdir_if_inode(path, held)
    finally:
        os.close(fd)


def _reap_stale_store_dirs() -> None:
    """Remove stale same-uid probe store directories left by a killed parent.

    Only real directories in the temp dir whose names start with
    ``csl734-store-`` and not ``csl734-store-same-`` (the control probe),
    are not symlinks, and are owned by this euid. A named directory is
    removed only when the name timestamp, ``st_mtime`` and ``st_ctime`` are
    all older than the probe timeout. A name with no timestamp is reaped
    only when the mode is exactly ``0700`` and ``st_mtime`` is old. The
    sweeper takes ``LOCK_EX|LOCK_NB`` on an ``O_DIRECTORY`` fd and skips a
    busy directory. Removal walks that fd and ``rmdir``s the name only when
    ``lstat`` still names the locked inode. A directory owned by another
    euid is left alone. SIGTERM installs no process-global handler, so a
    SIGTERM of the parent does not remove the directory either. A SIGKILL
    of the parent leaving an unlocked directory until this next probe start
    is a residual.
    """

    tmp = tempfile.gettempdir()
    min_age = _probe_store_reap_age_s()
    now = time.time()
    euid = os.geteuid()
    try:
        names = os.listdir(tmp)
    except OSError:
        return
    for name in names:
        if not name.startswith(_STORE_DIR_PREFIX) or name.startswith(_STORE_SAME_PREFIX):
            continue
        path = os.path.join(tmp, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            continue
        if st.st_uid != euid:
            continue
        if not _store_dir_is_stale(name, st, now, min_age):
            continue
        _reap_one_store_dir(path, name, st, min_age, euid)


def run_same_uid_probes(
    backend: Backend,
    n: int = DEFAULT_N,
    *,
    run_fn: RunFn = _default_run,
) -> tuple[ProbeSummary, ...]:
    """F21 probes from the calling uid against an engine running as ``backend.target_uid``.

    The probed process is the one whose uids all equal ``target_uid`` (for
    systemd-run, the unit's MainPID), never the launcher. When it cannot be
    found, the attempt counts as an error, not as blocked.

    The sleep child is in its own session and arms ``PR_SET_PDEATHSIG``, so it
    dies when this process dies. No SIGTERM handler is installed. This call
    holds an exclusive flock on the store directory fd for the directory's
    whole lifetime, so a concurrent sweep skips it. SIGKILL of this process
    drops that lock and can leave the ``csl734-store-<timestamp>-*`` directory
    (mode 01777) until the next call reaps a directory with that prefix,
    owned by this euid, whose name timestamp, mtime and ctime are all older
    than the probe timeout. The ``csl734-store-same-`` control directories
    are not reaped. That leftover directory is a residual.
    """

    if n < 10:
        raise ValueError(f"probe n must be >= 10, got {n}")
    target_uid = backend.target_uid
    if target_uid is None:
        raise ValueError(f"{backend.name} backend has no target_uid to probe")
    _reap_stale_store_dirs()
    tally = _Tally()
    write_prefix = spawn_command(backend, ())
    with tempfile.TemporaryDirectory(prefix=f"{_STORE_DIR_PREFIX}{time.time_ns()}-") as tmp:
        store_fd = _lock_store_dir(tmp)
        try:
            os.chmod(tmp, 0o1777)
            for _ in range(n):
                tally.add(
                    "abs_store_read",
                    probe_abs_store_read(
                        write_prefix, tmp, expected_owner=target_uid, run_fn=run_fn
                    ),
                    f"0600 store owner uid={target_uid}",
                )
                unit = None
                if backend.name == "systemd_run":
                    unit = f"csl734-probe-{uuid.uuid4().hex[:12]}"
                cmd = spawn_command(backend, SLEEP_PROBE_ARGV, unit=unit)
                proc = _popen_checked(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    if unit is not None:
                        pid = systemd_unit_main_pid(unit, target_uid, run_fn=run_fn)
                        missing = f"no MainPID with uid {target_uid} for unit {unit}"
                    else:
                        pid = find_target_uid_pid(proc.pid, target_uid)
                        missing = f"no process with uid {target_uid} under launcher pid {proc.pid}"
                    _probe_pid(tally, pid, missing)
                finally:
                    _terminate(proc)
                    if unit is not None:
                        run_fn(("systemctl", "stop", unit), 5.0)
        finally:
            os.close(store_fd)
    return tally.summaries(n)


def run_same_uid_control_probes(n: int = DEFAULT_N) -> tuple[ProbeSummary, ...]:
    """Same-uid control: spawn as the calling uid. Channels should stay open."""

    if n < 10:
        raise ValueError(f"probe n must be >= 10, got {n}")
    our_uid = os.getuid()
    tally = _Tally()
    with tempfile.TemporaryDirectory(prefix=_STORE_SAME_PREFIX) as tmp:
        os.chmod(tmp, 0o1777)
        for _ in range(n):
            tally.add(
                "abs_store_read",
                probe_abs_store_read((), tmp, expected_owner=our_uid),
                f"0600 store owner uid={our_uid}",
            )
            proc = _popen_checked(
                list(SLEEP_PROBE_ARGV), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            try:
                pid = find_target_uid_pid(proc.pid, our_uid)
                _probe_pid(tally, pid, f"no process with uid {our_uid} at pid {proc.pid}")
            finally:
                _terminate(proc)
    return tally.summaries(n)


def collect_host_facts(*, run_fn: RunFn | None = None) -> HostFacts:
    run = run_fn or _default_run
    uid = os.getuid()
    try:
        username = pwd.getpwuid(uid).pw_name
    except KeyError:
        username = str(uid)
    subuid = _subid_lines(username, _read_text("/etc/subuid"))
    subgid = _subid_lines(username, _read_text("/etc/subgid"))
    newuidmap = None
    newuidmap_setuid = False
    for candidate in ("/usr/bin/newuidmap", "/usr/sbin/newuidmap"):
        try:
            st = os.stat(candidate)
        except OSError:
            continue
        newuidmap = candidate
        newuidmap_setuid = _is_setuid_root(st)
        break
    raw_max = _read_text("/proc/sys/user/max_user_namespaces")
    user_ns_max = int(raw_max.strip()) if raw_max and raw_max.strip().isdigit() else None
    unshare = run(("unshare", "--user", "id", "-u"), 8.0)
    unshare_user_works = unshare.returncode == 0
    unshare_host_uid = None
    proc = None
    try:
        proc = _popen_checked(
            ["unshare", "--user", "sleep", "3"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.05)
        unshare_host_uid = host_uid_of(proc.pid)
    except FileNotFoundError:
        unshare_user_works = False
    finally:
        if proc is not None:
            _terminate(proc)
    systemd_is_pid1 = False
    try:
        comm = Path("/proc/1/comm").read_text(encoding="utf-8", errors="replace").strip()
        systemd_is_pid1 = comm == "systemd"
    except OSError:
        systemd_is_pid1 = False
    return HostFacts(
        uid=uid,
        username=username,
        sudo_n_true=_sudo_n_true(run),
        subuid_lines=subuid,
        subgid_lines=subgid,
        newuidmap_path=newuidmap,
        newuidmap_is_setuid_root=newuidmap_setuid,
        user_ns_max=user_ns_max,
        unshare_user_works=unshare_user_works,
        unshare_host_uid=unshare_host_uid,
        systemd_is_pid1=systemd_is_pid1,
    )


def detect_backends(
    *,
    facts: HostFacts | None = None,
    run_fn: RunFn | None = None,
    stat_fn: StatFn | None = None,
    env: Mapping[str, str] | None = None,
    helper_paths: Sequence[str] | None = None,
    engine_user: str | None = None,
    caller_groups: Collection[int] | None = None,
) -> tuple[Backend, ...]:
    """Detect spawn backends. ``engine_user`` (else ``RECURSION_ENGINE_USER``)
    is tried before the ``nobody``/``daemon`` measurement stand-ins."""

    run = run_fn or _default_run
    stat_fn = stat_fn or os.stat
    facts = facts or collect_host_facts(run_fn=run)
    our_uid = facts.uid
    engine = _configured_engine_user(engine_user, env)
    candidates = SUDO_TARGET_CANDIDATES
    if engine is not None:
        candidates = (engine, *(u for u in SUDO_TARGET_CANDIDATES if u != engine))

    helper_found, helper_note = _inspect_helpers(
        helper_paths if helper_paths is not None else _helper_paths(env),
        stat_fn,
    )
    setuid_backend = Backend(
        "setuid_helper",
        available=False,
        changes_host_uid=False,
        reason=helper_note,
        helper_path=helper_found,
    )

    # Sudoers can grant a target user without granting root, so the target
    # probe runs whatever ``sudo -n true`` (root target) returned.
    root_note = "sudo -n true ok" if facts.sudo_n_true else "sudo -n true failed"
    user, target_uid, sudo_note = _probe_sudo_target(
        run, our_uid, candidates, caller_groups=caller_groups
    )
    if user is not None and target_uid is not None:
        sudo_backend = Backend(
            "sudo",
            True,
            True,
            f"{root_note}; {sudo_note}",
            target_user=user,
            target_uid=target_uid,
        )
    else:
        sudo_backend = Backend(
            "sudo",
            False,
            False,
            f"{root_note}; cannot sudo to another uid for the required commands: "
            f"{sudo_note}; not editing sudoers",
        )

    stand_in = sudo_backend.target_user or "nobody"
    systemd_user = engine or stand_in
    sys_ok, sys_uid, sys_reason = _probe_systemd_run(
        run, systemd_user, our_uid, caller_groups=caller_groups
    )
    if not sys_ok and engine is not None and stand_in != engine:
        # The configured engine user cannot be reached, but systemd-run may still reach the stand-in.
        # Probe it so that working path is still measured and reported (bot review 4512091152); a shared
        # stand-in is never selected (select_path), so this changes evidence, not selection.
        alt_ok, alt_uid, alt_reason = _probe_systemd_run(
            run, stand_in, our_uid, caller_groups=caller_groups
        )
        sys_reason = f"engine user {engine}: {sys_reason}; stand-in {stand_in}: {alt_reason}"
        if alt_ok:
            sys_ok, sys_uid, systemd_user = True, alt_uid, stand_in
    if not facts.systemd_is_pid1 and not sys_ok:
        sys_reason = f"systemd is not PID 1; {sys_reason}"
    systemd_backend = Backend(
        "systemd_run",
        sys_ok,
        sys_ok,
        sys_reason + ("" if sys_ok else "; not editing polkit rules"),
        target_user=systemd_user if sys_ok else None,
        target_uid=sys_uid,
    )

    range_note = (
        f"subuid={','.join(facts.subuid_lines) or 'none'} "
        f"subgid={','.join(facts.subgid_lines) or 'none'}"
    )
    if facts.newuidmap_path and facts.newuidmap_is_setuid_root and facts.subuid_lines:
        # A working newuidmap could map subordinate uids. Probe map-auto and
        # record whether the child's host uid changes. --map-auto maps the
        # first subuid block to ns uid 0, so ns uid 1 is block start + 1.
        # spawn_command does not implement this path, so it is detection only
        # and never available for selection.
        expected_uid = None
        fields = facts.subuid_lines[0].split(":")
        if len(fields) >= 2 and fields[1].isdigit():
            expected_uid = int(fields[1]) + 1
        mapped = run(
            ("unshare", "--user", "--map-auto", "--setuid", "1", "--setgid", "1", "id", "-u"),
            8.0,
        )
        host_changed = False
        child_host_uid = None
        proc = None
        try:
            proc = _popen_checked(
                ["unshare", "--user", "--map-auto", "--setuid", "1", "--setgid", "1", "sleep", "3"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if expected_uid is not None and expected_uid != our_uid:
                child_pid = find_target_uid_pid(proc.pid, expected_uid)
                if child_pid is not None:
                    child_host_uid = expected_uid
                    host_changed = True
        except FileNotFoundError:
            mapped = ProcResult(127, "", "unshare not found")
        finally:
            if proc is not None:
                _terminate(proc)
        host_changed = mapped.returncode == 0 and host_changed
        subuid_backend = Backend(
            "subuid_userns",
            False,
            host_changed,
            (
                f"{range_note}; newuidmap={facts.newuidmap_path} setuid_root; "
                f"unshare --map-auto rc={mapped.returncode} host_uid={child_host_uid}; "
                "spawn path not implemented or measured, so not selectable"
            ),
            target_uid=child_host_uid if host_changed else None,
        )
    else:
        why = []
        if not facts.subuid_lines:
            why.append("no /etc/subuid line for this user")
        if not facts.newuidmap_path:
            why.append("newuidmap absent")
        elif not facts.newuidmap_is_setuid_root:
            why.append(f"{facts.newuidmap_path} is not setuid root")
        extra = ""
        if facts.unshare_user_works:
            extra = (
                f"; unshare --user works but host uid stays "
                f"{facts.unshare_host_uid} (not an other-uid path)"
            )
        subuid_backend = Backend(
            "subuid_userns",
            False,
            False,
            f"{range_note}; {'; '.join(why)}; not installing newuidmap{extra}",
        )

    return (setuid_backend, sudo_backend, systemd_backend, subuid_backend)


def _is_launch_failure(name: str, detail: str) -> bool:
    """True when one probe error means the backend could not launch or write.

    Process-not-found and store-writer failures are permission or availability
    problems, not F21 channel outcomes (blocked vs open). Other probe errors,
    such as ptrace being unavailable on the host, stay with the summaries.
    """

    detail = detail or ""
    if name == "abs_store_read":
        return "writer_" in detail
    if name in {"proc_pid_root", "ptrace"}:
        # "ptrace unavailable: libc not found" is a host limit, not a launch.
        if detail.startswith("ptrace unavailable"):
            return False
        return (
            "not found" in detail
            or "no process with uid" in detail
            or "no MainPID" in detail
        )
    return False


def _launch_failure(probes: Sequence[ProbeSummary]) -> str | None:
    """Return a reason when any attempt shows the backend could not launch or write.

    Uses the per-attempt launch-failure count, so a failure on an early attempt
    is not hidden by a later success. Summaries built without that count fall
    back to classifying the last detail.
    """

    reasons: list[str] = []
    for probe in probes:
        count = probe.launch_failure_count
        detail = probe.launch_failure_detail
        if count <= 0 and probe.error_count > 0 and _is_launch_failure(probe.name, probe.detail):
            count, detail = probe.error_count, probe.detail
        if count <= 0:
            continue
        if probe.name == "abs_store_read":
            reasons.append(
                f"{probe.name} writer failed on {count}/{probe.n} attempts: {detail}"
            )
        else:
            reasons.append(
                f"{probe.name} engine process did not appear on "
                f"{count}/{probe.n} attempts: {detail}"
            )
    if not reasons:
        return None
    return "; ".join(reasons)


def _f21_not_blocked(probes: Sequence[ProbeSummary]) -> str | None:
    """Return a reason when any F21 channel is not blocked on every attempt."""

    reasons = [
        f"{probe.name} {probe.blocked_count} blocked/{probe.open_count} open/"
        f"{probe.error_count} error of {probe.n}: {probe.detail}"
        for probe in probes
        if not probe.blocked
    ]
    names = {probe.name for probe in probes}
    for name in ("proc_pid_root", "ptrace", "abs_store_read"):
        if name not in names:
            reasons.append(f"{name} not probed")
    if not reasons:
        return None
    return "; ".join(reasons)


def evaluate_host(
    *,
    n: int = DEFAULT_N,
    run_fn: RunFn | None = None,
    facts: HostFacts | None = None,
    backends: Sequence[Backend] | None = None,
    measure_fn: Callable[[Backend, int], Overhead] = measure_overhead,
    probe_fn: Callable[[Backend, int], tuple[ProbeSummary, ...]] = run_same_uid_probes,
    f21_open: Sequence[str] | None = None,
    engine_user: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Evaluation:
    """Measure every spawnable other-uid backend, then select a path.

    A backend whose measurement fails (for example a sudoers rule that denies
    a command mid-run) is recorded in ``errors`` and marked unavailable, so
    selection falls through to the next backend or the beta fallback instead
    of aborting the evaluation. Failure to launch the probe process or write
    the store is treated the same way: those are not F21 channel outcomes,
    and they must not leave an unusable backend selected.

    A backend that launches but leaves any F21 channel open or errored is
    kept in ``overhead`` and ``probes`` as evidence, recorded in ``errors``,
    and marked unavailable: the separate uid exists to close those channels.
    ``f21_open`` is passed to ``select_path``; ``engine_user`` and ``env``
    to ``detect_backends`` when ``backends`` is not given.
    """

    if n < 10:
        raise ValueError(f"evaluate_host n must be >= 10, got {n}")

    facts = facts or collect_host_facts(run_fn=run_fn)
    detected = tuple(backends) if backends is not None else detect_backends(
        facts=facts, run_fn=run_fn, env=env, engine_user=engine_user
    )
    overhead: dict[str, Overhead] = {}
    probes: dict[str, tuple[ProbeSummary, ...]] = {}
    errors: dict[str, str] = {}
    final: list[Backend] = []
    for backend in detected:
        if not (backend.available and backend.changes_host_uid and can_spawn(backend)):
            final.append(backend)
            continue
        try:
            measured = measure_fn(backend, n)
            probed = probe_fn(backend, n)
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
            message = f"{type(exc).__name__}: {str(exc)[:300]}"
            errors[backend.name] = message
            final.append(
                replace(
                    backend,
                    available=False,
                    reason=f"{backend.reason}; measurement failed, not selectable: {message}",
                )
            )
            continue
        launch_err = _launch_failure(probed)
        if launch_err is not None:
            errors[backend.name] = launch_err
            final.append(
                replace(
                    backend,
                    available=False,
                    reason=f"{backend.reason}; launch failed, not selectable: {launch_err}",
                )
            )
            continue
        overhead[backend.name] = measured
        probes[backend.name] = probed
        not_blocked = _f21_not_blocked(probed)
        if not_blocked is not None:
            errors[backend.name] = f"F21 channels not blocked: {not_blocked}"
            final.append(
                replace(
                    backend,
                    available=False,
                    reason=(
                        f"{backend.reason}; F21 channels not blocked, not selectable: "
                        f"{not_blocked}"
                    ),
                )
            )
            continue
        final.append(backend)
    backends = tuple(final)
    selected = select_path(backends, f21_open=f21_open)
    return Evaluation(
        product_commit=PRODUCT_MD_COMMIT,
        product_sha256=PRODUCT_MD_SHA256,
        facts=facts,
        backends=backends,
        selected=selected,
        overhead=overhead,
        probes=probes,
        unmeasured={
            "engine_user_created_at_install": ENGINE_USER_CREATED_AT_INSTALL,
            "claude_code_signin_survives_reboot": CLAUDE_CODE_SIGNIN_SURVIVES_REBOOT,
            "codex_signin_survives_reboot": CODEX_SIGNIN_SURVIVES_REBOOT,
            "headless_secret_service_live": HEADLESS_SECRET_SERVICE_LIVE,
        },
        errors=errors,
    )


__all__ = [
    "BLOCKED_ERRNOS",
    "CLAUDE_CODE_SIGNIN_SURVIVES_REBOOT",
    "CODEX_SIGNIN_SURVIVES_REBOOT",
    "DEFAULT_N",
    "ENGINE_USER_CREATED_AT_INSTALL",
    "ENGINE_USER_ENV",
    "HEADLESS_SECRET_SERVICE_LIVE",
    "PRODUCT_MD_COMMIT",
    "PRODUCT_MD_SHA256",
    "SHARED_SYSTEM_ACCOUNTS",
    "SHARED_SYSTEM_UIDS",
    "SLEEP_PROBE_ARGV",
    "SPAWNABLE_BACKENDS",
    "SUDO_REQUIRED_COMMANDS",
    "Backend",
    "Evaluation",
    "HostFacts",
    "Overhead",
    "ProbeSummary",
    "SelectedPath",
    "byok_storage_when_secret_service_fails_closed",
    "can_spawn",
    "collect_host_facts",
    "detect_backends",
    "evaluate_host",
    "f21_open_items",
    "find_target_uid_pid",
    "is_root_target",
    "is_shared_system_account",
    "measure_overhead",
    "percentile",
    "run_same_uid_control_probes",
    "run_same_uid_probes",
    "select_path",
    "spawn_command",
    "store_writer_script",
    "systemd_unit_main_pid",
]
