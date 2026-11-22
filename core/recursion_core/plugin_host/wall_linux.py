"""The wall a plugin host puts around itself on Linux, before it loads any plugin byte.

A host process calls ``apply_wall(read_only_paths)`` on itself while it has ONE thread, then
``probe_wall(canary_path, daemon_pid)``, and sends the record that the probe returns (``wall.v1``,
``schemas/plugin/v1/wall.v1.json``). The wall has two parts, both applied by the kernel to this process
and to everything it starts later, and neither can be lifted again from inside:

1. A Landlock ruleset that handles every file-system right the kernel's Landlock ABI knows and allows only
   reading, listing and mapping for execution beneath the paths given (the Python install), and nothing
   else: no write anywhere, no read of anything outside those paths. ``prctl(PR_SET_NO_NEW_PRIVS)`` comes
   first, then ``landlock_restrict_self``.
2. ONE seccomp filter, loaded with ``SECCOMP_FILTER_FLAG_TSYNC`` so that it reaches every thread of the
   process. It checks the audit arch first (x86_64 and aarch64; a call made under another arch, or the
   x32 range of x86_64, is answered EPERM), answers EPERM to every call of ``DENIED_CALLS``, answers EPERM
   to ``kill``, ``tkill`` and ``tgkill`` unless the target is this process itself, and allows everything
   else. It is a deny list, so ordinary Python keeps working: imports from the install, threads, pipes,
   and ``recvmsg`` of a socket handed over on an already open channel all stay allowed.

``DENIED_CALLS`` is the wall's own definition, not a setting: there is no row, flag or argument that
shortens it. Landlock below ABI 4 does not limit TCP and below ABI 6 does not scope signals; the seccomp
filter shuts both here (``socket``, ``connect`` and ``kill`` to another pid are denied whatever the ABI).

Landlock limits only the calling thread, so ``apply_wall`` refuses ``wall_threads_running`` before any
change when the process has more than one thread. Every refusal comes before the first change:
``wall_system_unsupported`` (not Linux: nothing is tried), ``wall_arch_unsupported``,
``wall_threads_running``, ``landlock_unavailable``. A failure of the kernel after the first change raises
``OSError``: the process is then walled in part, must load nothing, and its probe says so.

Pure of the daemon: the standard library only (``ctypes`` and ``os``), no new dependency. The system-call
numbers, audit arches, Landlock rights and BPF codes below belong to the kernel's interface: no person
could meaningfully choose them, so none is a setting.
"""
from __future__ import annotations

import ctypes
import os
import platform
import socket
import stat
import struct
import sys
import threading

__all__ = ["DENIED_CALLS", "RECORD_CHECKS", "SELF_ONLY_SIGNAL_CALLS", "WallRefused", "apply_wall", "probe_wall"]

# The wall's own definition (not a setting, not a tunable): each call is answered EPERM.
DENIED_CALLS = (
    "socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
    "execve", "execveat",
    "ptrace", "process_vm_readv", "process_vm_writev",
    "pidfd_open", "pidfd_getfd", "pidfd_send_signal",
    "keyctl", "add_key", "request_key",
    "mount", "umount2", "unshare", "setns",
    "bpf", "perf_event_open", "userfaultfd", "io_uring_setup",
)
# Answered EPERM unless the first argument (the target pid, tid or thread group) is this process itself.
SELF_ONLY_SIGNAL_CALLS = ("kill", "tkill", "tgkill")

# The kernel's interface (x86_64: arch/x86/entry/syscalls/syscall_64.tbl; aarch64: asm-generic/unistd.h).
_NR = {
    "x86_64": {
        "socket": 41, "socketpair": 53, "connect": 42, "bind": 49, "listen": 50, "accept": 43, "accept4": 288,
        "execve": 59, "execveat": 322, "ptrace": 101, "process_vm_readv": 310, "process_vm_writev": 311,
        "pidfd_open": 434, "pidfd_getfd": 438, "pidfd_send_signal": 424, "keyctl": 250, "add_key": 248,
        "request_key": 249, "mount": 165, "umount2": 166, "unshare": 272, "setns": 308, "bpf": 321,
        "perf_event_open": 298, "userfaultfd": 323, "io_uring_setup": 425,
        "kill": 62, "tkill": 200, "tgkill": 234, "seccomp": 317,
    },
    "aarch64": {
        "socket": 198, "socketpair": 199, "connect": 203, "bind": 200, "listen": 201, "accept": 202, "accept4": 242,
        "execve": 221, "execveat": 281, "ptrace": 117, "process_vm_readv": 270, "process_vm_writev": 271,
        "pidfd_open": 434, "pidfd_getfd": 438, "pidfd_send_signal": 424, "keyctl": 219, "add_key": 217,
        "request_key": 218, "mount": 40, "umount2": 39, "unshare": 97, "setns": 268, "bpf": 280,
        "perf_event_open": 241, "userfaultfd": 282, "io_uring_setup": 425,
        "kill": 129, "tkill": 130, "tgkill": 131, "seccomp": 277,
    },
}
_AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
_X32_SYSCALL_BIT = 0x40000000  # x86_64 only: the x32 calls share the arch value and are denied whole

_SYS_LANDLOCK_CREATE_RULESET, _SYS_LANDLOCK_ADD_RULE, _SYS_LANDLOCK_RESTRICT_SELF = 444, 445, 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_FS_EXECUTE, _FS_READ_FILE, _FS_READ_DIR = 1 << 0, 1 << 2, 1 << 3
_FS_ABI1 = (1 << 13) - 1                     # EXECUTE .. MAKE_SYM
_FS_LATER = {2: 1 << 13, 3: 1 << 14, 5: 1 << 15}  # REFER, TRUNCATE, IOCTL_DEV
_MIN_LANDLOCK_ABI = 1                        # Linux 5.13

_PR_SET_NO_NEW_PRIVS = 38
_SECCOMP_SET_MODE_FILTER, _SECCOMP_FILTER_FLAG_TSYNC = 1, 1
_BPF_LD_W_ABS, _BPF_JEQ_K, _BPF_JGE_K, _BPF_RET_K = 0x20, 0x15, 0x35, 0x06
_RET_ALLOW, _RET_EPERM = 0x7FFF0000, 0x00050000 | 1
_OFF_NR, _OFF_ARCH, _OFF_ARG0_LOW = 0, 4, 16   # struct seccomp_data, little-endian

# The five escapes of the record, in its order; each is True only when the kernel refused it.
RECORD_CHECKS = ("reads_outside", "home_canary", "program_start", "new_socket", "signal_other")


class WallRefused(Exception):
    """A typed refusal of the wall. ``code`` is stable; ``detail`` is never secret."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _libc():
    """The C library, or ``wall_system_unsupported`` where the two system calls cannot be reached."""
    if not sys.platform.startswith("linux"):
        raise WallRefused("wall_system_unsupported",
                          "the wall needs Linux (Landlock and seccomp); this system has neither")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        libc.prctl.restype = ctypes.c_int
    except (OSError, AttributeError) as exc:
        raise WallRefused("wall_system_unsupported", "the C library's syscall and prctl cannot be reached") from exc
    return libc


def _call(libc, number: int, *args: int) -> tuple[int, int]:
    ctypes.set_errno(0)
    result = libc.syscall(ctypes.c_long(number), *(ctypes.c_long(a) for a in args))
    return result, ctypes.get_errno()


def _arch() -> str:
    machine = platform.machine()
    if machine not in _NR or struct.calcsize("P") != 8:
        raise WallRefused("wall_arch_unsupported", f"the wall knows x86_64 and aarch64 only, not {machine or 'unknown'}")
    return machine


def _landlock_abi(libc) -> int:
    """The kernel's Landlock ABI for this process, read by the version flag; 0 when absent or disabled."""
    abi, _ = _call(libc, _SYS_LANDLOCK_CREATE_RULESET, 0, 0, _LANDLOCK_CREATE_RULESET_VERSION)
    return abi if abi > 0 else 0


def _thread_count() -> int:
    try:
        tasks = len(os.listdir("/proc/self/task"))
    except OSError as exc:
        raise WallRefused("wall_threads_running", "the thread count of this process cannot be read") from exc
    return max(tasks, threading.active_count())


def _filter(arch: str, own_pid: int) -> list[tuple[int, int, int, int]]:
    """The one seccomp program, as (code, jt, jf, k) rows."""
    numbers = _NR[arch]
    rows: list[tuple] = [(_BPF_LD_W_ABS, 0, 0, _OFF_ARCH), (_BPF_JEQ_K, 1, 0, _AUDIT_ARCH[arch]),
                         (_BPF_RET_K, 0, 0, _RET_EPERM), (_BPF_LD_W_ABS, 0, 0, _OFF_NR)]
    if arch == "x86_64":
        rows.append((_BPF_JGE_K, "deny", 0, _X32_SYSCALL_BIT))
    rows += [(_BPF_JEQ_K, "deny", 0, numbers[name]) for name in DENIED_CALLS]
    rows += [(_BPF_JEQ_K, "signal", 0, numbers[name]) for name in SELF_ONLY_SIGNAL_CALLS]
    rows.append((_BPF_RET_K, 0, 0, _RET_ALLOW))
    labels = {"signal": len(rows)}
    # pid_t is 32 bits and the kernel reads only the low word, so only the low word is compared.
    rows += [(_BPF_LD_W_ABS, 0, 0, _OFF_ARG0_LOW), (_BPF_JEQ_K, 0, "deny", own_pid), (_BPF_RET_K, 0, 0, _RET_ALLOW)]
    labels["deny"] = len(rows)
    rows.append((_BPF_RET_K, 0, 0, _RET_EPERM))
    return [(code, labels[jt] - i - 1 if isinstance(jt, str) else jt, labels[jf] - i - 1 if isinstance(jf, str) else jf, k)
            for i, (code, jt, jf, k) in enumerate(rows)]


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def _no_new_privs(libc) -> None:
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"prctl(PR_SET_NO_NEW_PRIVS): {os.strerror(err)}")


def _load_seccomp_filter(libc, arch: str) -> None:
    rows = _filter(arch, os.getpid())
    program = (_SockFilter * len(rows))(*(_SockFilter(*row) for row in rows))
    fprog = _SockFprog(len(rows), ctypes.addressof(program))
    _no_new_privs(libc)
    result, err = _call(libc, _NR[arch]["seccomp"], _SECCOMP_SET_MODE_FILTER, _SECCOMP_FILTER_FLAG_TSYNC,
                        ctypes.addressof(fprog))
    if result != 0:
        raise OSError(err, f"seccomp filter not loaded (result {result}): {os.strerror(err) if err else 'a thread did not sync'}")


def _open_paths(read_only_paths) -> list[tuple[int, int]]:
    """(fd, allowed rights) for each path, all opened before any change; nothing is left open on a raise."""
    opened: list[tuple[int, int]] = []
    try:
        for entry in read_only_paths:
            path = os.fspath(entry)
            if not isinstance(path, str) or not os.path.isabs(path):
                raise ValueError(f"a wall path must be an absolute path: {path!r}")
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            opened.append((fd, 0))
            is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
            opened[-1] = (fd, _FS_EXECUTE | _FS_READ_FILE | (_FS_READ_DIR if is_dir else 0))
    except BaseException:
        for fd, _ in opened:
            os.close(fd)
        raise
    return opened


def _apply_landlock(libc, abi: int, opened: list[tuple[int, int]]) -> None:
    handled = _FS_ABI1
    for since, right in _FS_LATER.items():
        if abi >= since:
            handled |= right
    attr = ctypes.c_uint64(handled)
    ruleset, err = _call(libc, _SYS_LANDLOCK_CREATE_RULESET, ctypes.addressof(attr), ctypes.sizeof(attr), 0)
    if ruleset < 0:
        raise OSError(err, f"landlock_create_ruleset: {os.strerror(err)}")
    try:
        for fd, allowed in opened:
            rule = ctypes.create_string_buffer(struct.pack("<Qi", allowed, fd), 12)  # struct landlock_path_beneath_attr
            result, err = _call(libc, _SYS_LANDLOCK_ADD_RULE, ruleset, _LANDLOCK_RULE_PATH_BENEATH, ctypes.addressof(rule), 0)
            if result != 0:
                raise OSError(err, f"landlock_add_rule: {os.strerror(err)}")
        _no_new_privs(libc)
        result, err = _call(libc, _SYS_LANDLOCK_RESTRICT_SELF, ruleset, 0)
        if result != 0:
            raise OSError(err, f"landlock_restrict_self: {os.strerror(err)}")
    finally:
        os.close(ruleset)


def apply_wall(read_only_paths) -> None:
    """Wall this process in: Landlock (read and map only ``read_only_paths``), then the one seccomp filter.

    Refuses, before any change: ``wall_system_unsupported`` (not Linux; nothing is tried),
    ``wall_arch_unsupported`` (not x86_64 or aarch64), ``wall_threads_running`` (more than one thread),
    ``landlock_unavailable`` (no Landlock, or an ABI below 1). A bad path raises ``ValueError`` or
    ``OSError`` before any change as well.
    """
    libc = _libc()
    arch = _arch()
    threads = _thread_count()
    if threads != 1:
        raise WallRefused("wall_threads_running", f"the wall is applied by a process of one thread; this one has {threads}")
    abi = _landlock_abi(libc)
    if abi < _MIN_LANDLOCK_ABI:
        raise WallRefused("landlock_unavailable", "this kernel gives this process no Landlock (ABI 1, Linux 5.13, at least)")
    opened = _open_paths(read_only_paths)
    try:
        _apply_landlock(libc, abi, opened)
    finally:
        for fd, _ in opened:
            os.close(fd)
    _load_seccomp_filter(libc, arch)


def _refused(check) -> bool:
    """True only when the check was refused by the kernel (PermissionError). Success, or any other error,
    is not a refusal: a check that cannot be made proves nothing (fail closed)."""
    try:
        check()
    except PermissionError:
        return True
    except Exception:
        return False
    return False


def _read_root() -> None:
    if not os.access("/", os.R_OK):
        raise OSError("the root folder is not readable by its mode, so the check proves nothing")
    os.listdir("/")


def _read_canary(path: str) -> None:
    if not os.access(path, os.R_OK):
        raise OSError("the canary is not readable by its mode, so the check proves nothing")
    with open(path, "rb") as handle:
        handle.read(1)


def _start_program() -> None:
    if not sys.executable:
        raise OSError("no program to start")
    pid = os.posix_spawn(sys.executable, [sys.executable, "-I", "-S", "-c", ""], {})
    os.waitpid(pid, 0)


def _open_sockets() -> None:
    for family in (socket.AF_INET, socket.AF_UNIX):
        try:
            made = socket.socket(family, socket.SOCK_STREAM)
        except PermissionError:
            continue
        made.close()
        return  # one socket was made: not refused
    raise PermissionError("both sockets refused")


def _signal_other(pid) -> None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid == os.getpid():
        raise ValueError("the stand-in daemon pid must be another process's pid")
    os.kill(pid, 0)  # signal 0: the kernel checks the right to signal and delivers nothing


def probe_wall(canary_path, daemon_pid) -> dict:
    """The record ``wall.v1``, proved from inside this process.

    Five escapes are tried; each field is True only when the kernel refused it. ``proven`` is True only when
    all five are refused, on a supported arch with Landlock ABI 1 or above. Refuses
    ``wall_system_unsupported`` where the two system calls are absent, without trying.
    """
    libc = _libc()
    canary = os.fspath(canary_path)
    record = {
        "reads_outside": _refused(_read_root),
        "home_canary": _refused(lambda: _read_canary(canary)),
        "program_start": _refused(_start_program),
        "new_socket": _refused(_open_sockets),
        "signal_other": _refused(lambda: _signal_other(daemon_pid)),
        "landlock_abi": _landlock_abi(libc),
        "arch": platform.machine() or "unknown",
    }
    record["proven"] = (all(record[name] for name in RECORD_CHECKS) and record["landlock_abi"] >= _MIN_LANDLOCK_ABI
                        and record["arch"] in _NR)
    return record
