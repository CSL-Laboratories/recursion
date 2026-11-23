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
   x32 range of x86_64, is answered EPERM), then answers EPERM to:
   - every call of ``DENIED_CALLS`` (sockets, program starts, tracing, keys, mounts, namespaces, bpf, ...);
   - every call of ``DENIED_METADATA_CALLS``: the mode, owner, times and extended attributes of a file,
     by path and by descriptor (Landlock does not mediate them at any ABI);
   - every call of ``DENIED_SIGNAL_CALLS`` (``kill``, ``tkill``, ``tgkill``, ``rt_sigqueueinfo``,
     ``rt_tgsigqueueinfo``), whatever its target: a walled process cannot signal any process, not even
     itself. A pid is a number that outlives its process, so no call is ever let through because its pid
     argument equals a number; timers, alarms and faults that the kernel delivers still arrive;
   - every call of ``DENIED_IPC_CALLS``: the System V shared memory, semaphores and message queues, and
     the POSIX message queues, which reach objects of other processes by key, id or name;
   - the calls of ``CALLER_ONLY_CALLS`` (a limit, a scheduling class or a CPU set, the pages of memory)
     unless the target pid is 0, the kernel's own form for the caller; ``setpriority`` and
     ``ioprio_set`` (``CALLER_ONLY_PRIORITY_CALLS``) unless they name one process and that is 0;
   - ``fcntl`` with a command of ``DENIED_FCNTL_COMMANDS`` and ``ioctl`` with a command of
     ``DENIED_IOCTL_COMMANDS`` (making another process the receiver of a descriptor's signals, a lease
     that makes another process's open of a file wait, and a file's attribute flags);
   - ``clone`` with any flag of ``NAMESPACE_CLONE_FLAGS``; ``clone3`` is answered ENOSYS, so that the C
     library starts threads and children through ``clone``, where the flags can be read;
   - where the kernel's Landlock ABI is below 3 (it cannot refuse a truncation there),
     ``OLD_LANDLOCK_DENIED_CALLS`` and an ``open`` or ``openat`` that asks ``O_TRUNC``.
   Everything else is allowed. It is a deny list, so ordinary Python keeps working: imports from the
   install, threads, ``fork``, pipes, and ``recvmsg`` of a socket handed over on an already open
   channel all stay allowed.

The deny list is the wall's own definition, not a setting: there is no row, flag or argument that
shortens it. Landlock below ABI 4 does not limit TCP and below ABI 6 does not scope signals; the seccomp
filter shuts both here (``socket``, ``connect`` and every way named above to signal, limit, slow or move
another process are denied whatever the ABI). Where the kernel's Landlock ABI is 6 or above, the ruleset
also asks both of the kernel's scopes (``LANDLOCK_SCOPES``: abstract unix sockets and signals, each kept
to the domain); the filter's refusals stay, as the floor for every older kernel, and the record names
the scopes in force (``landlock_scopes``).

What the record is: a check that the host's own code makes on itself, from inside the process, after
``apply_wall`` and BEFORE any plugin code runs. It shows that the named attempts were refused by the
kernel at that moment; it does not show which filter refused them, and code in the same process could
write any record. It means something under two conditions only: it is made before the plugin is loaded,
and its reader takes it only from frames the host sent before the load (everything later on the channel
is the plugin's voice).

Landlock limits only the calling thread, so ``apply_wall`` refuses ``wall_threads_running`` before any
change when the process has more than one thread. The refusals before the first change:
``wall_system_unsupported`` (not Linux: nothing is tried), ``wall_arch_unsupported``,
``wall_threads_running``, ``landlock_unavailable``. A failure of the kernel at any step of the wall, and a
second ``apply_wall`` in a process that has already been walled in whole or in part, is refused
``wall_partial`` with the step it names: such a process must load nothing, and ``probe_wall`` in it never
says proven.

Pure of the daemon: the standard library only (``ctypes`` and ``os``), no new dependency. The system-call
numbers, audit arches, Landlock rights, command values and BPF codes below belong to the kernel's
interface: no person could meaningfully choose them, so none is a setting.
"""
from __future__ import annotations

import ctypes
import errno
import os
import platform
import socket
import stat
import struct
import sys
import threading

__all__ = ["CALLER_ONLY_CALLS", "CALLER_ONLY_PRIORITY_CALLS", "DENIED_CALLS", "DENIED_FCNTL_COMMANDS",
           "DENIED_IOCTL_COMMANDS", "DENIED_IPC_CALLS", "DENIED_METADATA_CALLS", "DENIED_SIGNAL_CALLS",
           "LANDLOCK_SCOPES", "NAMESPACE_CLONE_FLAGS", "OLD_LANDLOCK_DENIED_CALLS", "RECORD_CHECKS", "WallRefused",
           "apply_wall", "probe_wall"]

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
# Every way to send a signal, answered EPERM whatever the target: a pid number outlives its process, and
# these calls have no form that means 'the caller' (kill with 0 or a negative number reaches a whole group).
DENIED_SIGNAL_CALLS = ("kill", "tkill", "tgkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo")
# System V IPC (shared memory, semaphores, message queues) and POSIX message queues: answered EPERM.
# Neither arch here has the multiplexed ``ipc`` call (i386's is refused by the arch check).
DENIED_IPC_CALLS = ("shmget", "shmat", "shmdt", "shmctl", "semget", "semop", "semtimedop", "semctl",
                    "msgget", "msgsnd", "msgrcv", "msgctl",
                    "mq_open", "mq_unlink", "mq_timedsend", "mq_timedreceive", "mq_notify", "mq_getsetattr")
# The mode, owner, times and extended attributes of a file: answered EPERM by path and by descriptor.
# A call the arch does not have (aarch64 has no chmod, chown, lchown, utime, utimes, futimesat) is skipped.
DENIED_METADATA_CALLS = (
    "chmod", "fchmod", "fchmodat", "fchmodat2",
    "chown", "fchown", "lchown", "fchownat",
    "utime", "utimes", "futimesat", "utimensat",
    "setxattr", "lsetxattr", "fsetxattr", "setxattrat",
    "removexattr", "lremovexattr", "fremovexattr", "removexattrat",
    "file_setattr",
)
# A limit, a scheduling class or parameter, a CPU set, the pages of memory: answered EPERM unless the first
# argument (the target pid) is 0, the kernel's form for the caller.
CALLER_ONLY_CALLS = ("prlimit64", "sched_setaffinity", "sched_setscheduler", "sched_setparam", "sched_setattr",
                     "migrate_pages", "move_pages")
# A priority by (which, who): answered EPERM unless `which` names one process and `who` is 0 (the caller).
CALLER_ONLY_PRIORITY_CALLS = ("setpriority", "ioprio_set")
# fcntl commands: another process as the receiver of a descriptor's signals; a lease (F_GETLEASE stays).
DENIED_FCNTL_COMMANDS = {"F_SETOWN": 8, "F_SETOWN_EX": 15, "F_SETLEASE": 1024}
# ioctl commands: another process as the receiver of a socket's signals; a file's attribute flags.
DENIED_IOCTL_COMMANDS = {"FIOSETOWN": 0x8901, "SIOCSPGRP": 0x8902, "FS_IOC_SETFLAGS": 0x40086602,
                         "FS_IOC_FSSETXATTR": 0x401C5820}
# clone with any of these flags (a new mount, cgroup, uts, ipc, user, pid or network namespace) is refused.
NAMESPACE_CLONE_FLAGS = 0x00020000 | 0x02000000 | 0x04000000 | 0x08000000 | 0x10000000 | 0x20000000 | 0x40000000
# Below Landlock ABI 3 the ruleset cannot refuse a truncation, so these are refused by the filter instead,
# with an open or openat that asks O_TRUNC (openat2 carries its flags in a struct the filter cannot read).
OLD_LANDLOCK_DENIED_CALLS = ("truncate", "openat2")
# The kernel's Landlock scopes (struct landlock_ruleset_attr, field ``scoped``), asked from ABI 6 on.
LANDLOCK_SCOPES = {"abstract_unix_socket": 1 << 0, "signal": 1 << 1}

# The kernel's interface (x86_64: arch/x86/entry/syscalls/syscall_64.tbl; aarch64: asm-generic/unistd.h).
_NR = {
    "x86_64": {
        "socket": 41, "socketpair": 53, "connect": 42, "bind": 49, "listen": 50, "accept": 43, "accept4": 288,
        "execve": 59, "execveat": 322, "ptrace": 101, "process_vm_readv": 310, "process_vm_writev": 311,
        "pidfd_open": 434, "pidfd_getfd": 438, "pidfd_send_signal": 424, "keyctl": 250, "add_key": 248,
        "request_key": 249, "mount": 165, "umount2": 166, "unshare": 272, "setns": 308, "bpf": 321,
        "perf_event_open": 298, "userfaultfd": 323, "io_uring_setup": 425, "seccomp": 317,
    },
    "aarch64": {
        "socket": 198, "socketpair": 199, "connect": 203, "bind": 200, "listen": 201, "accept": 202, "accept4": 242,
        "execve": 221, "execveat": 281, "ptrace": 117, "process_vm_readv": 270, "process_vm_writev": 271,
        "pidfd_open": 434, "pidfd_getfd": 438, "pidfd_send_signal": 424, "keyctl": 219, "add_key": 217,
        "request_key": 218, "mount": 40, "umount2": 39, "unshare": 97, "setns": 268, "bpf": 280,
        "perf_event_open": 241, "userfaultfd": 282, "io_uring_setup": 425, "seccomp": 277,
    },
}
# The numbers of the calls the filter reads beyond the first list (kept apart, so that table stays that list).
_NR_MORE = {
    "x86_64": {
        "chmod": 90, "fchmod": 91, "fchmodat": 268, "fchmodat2": 452, "chown": 92, "fchown": 93, "lchown": 94,
        "fchownat": 260, "utime": 132, "utimes": 235, "futimesat": 261, "utimensat": 280, "setxattr": 188,
        "lsetxattr": 189, "fsetxattr": 190, "setxattrat": 463, "removexattr": 197, "lremovexattr": 198,
        "fremovexattr": 199, "removexattrat": 466, "file_setattr": 469,
        "kill": 62, "tkill": 200, "tgkill": 234, "rt_sigqueueinfo": 129, "rt_tgsigqueueinfo": 297,
        "shmget": 29, "shmat": 30, "shmdt": 67, "shmctl": 31, "semget": 64, "semop": 65, "semtimedop": 220,
        "semctl": 66, "msgget": 68, "msgsnd": 69, "msgrcv": 70, "msgctl": 71, "mq_open": 240, "mq_unlink": 241,
        "mq_timedsend": 242, "mq_timedreceive": 243, "mq_notify": 244, "mq_getsetattr": 245,
        "prlimit64": 302, "sched_setaffinity": 203, "sched_setscheduler": 144, "sched_setparam": 142,
        "sched_setattr": 314, "migrate_pages": 256, "move_pages": 279, "setpriority": 141, "ioprio_set": 251,
        "fcntl": 72, "ioctl": 16, "clone": 56, "clone3": 435,
        "truncate": 76, "openat2": 437, "open": 2, "openat": 257,
    },
    "aarch64": {
        "fchmod": 52, "fchmodat": 53, "fchmodat2": 452, "fchown": 55, "fchownat": 54, "utimensat": 88,
        "setxattr": 5, "lsetxattr": 6, "fsetxattr": 7, "setxattrat": 463, "removexattr": 14, "lremovexattr": 15,
        "fremovexattr": 16, "removexattrat": 466, "file_setattr": 469,
        "kill": 129, "tkill": 130, "tgkill": 131, "rt_sigqueueinfo": 138, "rt_tgsigqueueinfo": 240,
        "shmget": 194, "shmat": 196, "shmdt": 197, "shmctl": 195, "semget": 190, "semop": 193, "semtimedop": 192,
        "semctl": 191, "msgget": 186, "msgsnd": 189, "msgrcv": 188, "msgctl": 187, "mq_open": 180, "mq_unlink": 181,
        "mq_timedsend": 182, "mq_timedreceive": 183, "mq_notify": 184, "mq_getsetattr": 185,
        "prlimit64": 261, "sched_setaffinity": 122, "sched_setscheduler": 119, "sched_setparam": 118,
        "sched_setattr": 274, "migrate_pages": 238, "move_pages": 239, "setpriority": 140, "ioprio_set": 30,
        "fcntl": 25, "ioctl": 29, "clone": 220, "clone3": 435,
        "truncate": 45, "openat2": 437, "openat": 56,
    },
}
_AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
_X32_SYSCALL_BIT = 0x40000000  # x86_64 only: the x32 calls share the arch value and are denied whole
_PRIO_PROCESS, _IOPRIO_WHO_PROCESS = 0, 1   # the `which` that names one process, for CALLER_ONLY_PRIORITY_CALLS
_O_TRUNC = 0o1000                           # the same value on x86_64 and aarch64
_TRUNCATE_ABI = 3                           # the Landlock ABI that first handles truncation
_SCOPE_ABI = 6                              # the Landlock ABI that first scopes signals and abstract unix sockets

_SYS_LANDLOCK_CREATE_RULESET, _SYS_LANDLOCK_ADD_RULE, _SYS_LANDLOCK_RESTRICT_SELF = 444, 445, 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_FS_EXECUTE, _FS_READ_FILE, _FS_READ_DIR = 1 << 0, 1 << 2, 1 << 3
_FS_ABI1 = (1 << 13) - 1                     # EXECUTE .. MAKE_SYM
_FS_LATER = {2: 1 << 13, 3: 1 << 14, 5: 1 << 15}  # REFER, TRUNCATE, IOCTL_DEV
_MIN_LANDLOCK_ABI = 1                        # Linux 5.13

_PR_SET_NO_NEW_PRIVS = 38
_SECCOMP_SET_MODE_FILTER, _SECCOMP_FILTER_FLAG_TSYNC = 1, 1
_BPF_LD_W_ABS, _BPF_JEQ_K, _BPF_JGE_K, _BPF_JSET_K, _BPF_RET_K = 0x20, 0x15, 0x35, 0x45, 0x06
_EPERM, _ENOSYS = 1, 38                      # Linux errno values (the kernel's, whatever the host Python says)
_RET_ALLOW, _RET_EPERM, _RET_ENOSYS = 0x7FFF0000, 0x00050000 | _EPERM, 0x00050000 | _ENOSYS
_OFF_NR, _OFF_ARCH = 0, 4                     # struct seccomp_data, little-endian
_OFF_ARG_LOW = (16, 24, 32)                   # the low word of arguments 0, 1 and 2

# The checks of the record, in its order; each is True only when the kernel refused it.
RECORD_CHECKS = ("reads_outside", "home_canary", "program_start", "new_socket", "signal_other",
                 "signal_other_ways", "metadata_change", "kernel_surface")

# How far apply_wall has gone in THIS process (a forked child inherits it): the steps that changed the
# process, the step that failed, and the Landlock scopes in force. A process with a failed step, or with any
# step, is never walled again.
_STATE: dict = {"applied": [], "failed": None, "scopes": []}


class WallRefused(Exception):
    """A typed refusal of the wall. ``code`` is stable; ``detail`` is never secret; ``step`` names the step
    of the wall that failed (``wall_partial`` only), else it is empty."""

    def __init__(self, code: str, detail: str = "", step: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.step = step


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
    except PermissionError as exc:
        raise WallRefused("wall_partial", "the task list of this process is refused to it: a wall is already in "
                          "place, whole or in part; this process must load nothing", step="already_walled") from exc
    except OSError as exc:
        raise WallRefused("wall_threads_running", "the thread count of this process cannot be read") from exc
    return max(tasks, threading.active_count())


def _filter(arch: str, landlock_abi: int = _MIN_LANDLOCK_ABI) -> list[tuple[int, int, int, int]]:
    """The one seccomp program, as (code, jt, jf, k) rows. ``landlock_abi`` below 3 adds the truncation rules.
    No pid number appears in it: the same program for every process."""
    numbers = dict(_NR[arch], **_NR_MORE[arch])
    rows: list[tuple] = [(_BPF_LD_W_ABS, 0, 0, _OFF_ARCH), (_BPF_JEQ_K, 1, 0, _AUDIT_ARCH[arch]),
                         (_BPF_RET_K, 0, 0, _RET_EPERM), (_BPF_LD_W_ABS, 0, 0, _OFF_NR)]
    if arch == "x86_64":
        rows.append((_BPF_JGE_K, "deny", 0, _X32_SYSCALL_BIT))
    rows += [(_BPF_JEQ_K, "deny", 0, numbers[name]) for name in DENIED_CALLS]
    rows += [(_BPF_JEQ_K, "deny", 0, numbers[name]) for name in DENIED_METADATA_CALLS if name in numbers]
    rows += [(_BPF_JEQ_K, "deny", 0, numbers[name]) for name in DENIED_SIGNAL_CALLS + DENIED_IPC_CALLS]
    if landlock_abi < _TRUNCATE_ABI:
        rows += [(_BPF_JEQ_K, "deny", 0, numbers[name]) for name in OLD_LANDLOCK_DENIED_CALLS]
        rows += [(_BPF_JEQ_K, label, 0, numbers[name])
                 for name, label in (("open", "trunc1"), ("openat", "trunc2")) if name in numbers]
    rows += [(_BPF_JEQ_K, "caller", 0, numbers[name]) for name in CALLER_ONLY_CALLS]
    rows += [(_BPF_JEQ_K, "prio", 0, numbers["setpriority"]), (_BPF_JEQ_K, "ioprio", 0, numbers["ioprio_set"]),
             (_BPF_JEQ_K, "fcntl", 0, numbers["fcntl"]), (_BPF_JEQ_K, "ioctl", 0, numbers["ioctl"]),
             (_BPF_JEQ_K, "clone", 0, numbers["clone"]), (_BPF_JEQ_K, "enosys", 0, numbers["clone3"])]
    rows.append((_BPF_RET_K, 0, 0, _RET_ALLOW))
    labels: dict[str, int] = {}

    def block(label, *body):
        labels[label] = len(rows)
        rows.extend(body)

    # pid_t, int commands and flags are 32 bits and the kernel reads only the low word, so only it is compared.
    block("caller", (_BPF_LD_W_ABS, 0, 0, _OFF_ARG_LOW[0]), (_BPF_JEQ_K, "allow", "deny", 0))
    block("prio", (_BPF_LD_W_ABS, 0, 0, _OFF_ARG_LOW[0]), (_BPF_JEQ_K, "who", "deny", _PRIO_PROCESS))
    block("ioprio", (_BPF_LD_W_ABS, 0, 0, _OFF_ARG_LOW[0]), (_BPF_JEQ_K, "who", "deny", _IOPRIO_WHO_PROCESS))
    block("who", (_BPF_LD_W_ABS, 0, 0, _OFF_ARG_LOW[1]), (_BPF_JEQ_K, "allow", "deny", 0))
    block("fcntl", (_BPF_LD_W_ABS, 0, 0, _OFF_ARG_LOW[1]),
          *[(_BPF_JEQ_K, "deny", 0, value) for value in DENIED_FCNTL_COMMANDS.values()], (_BPF_RET_K, 0, 0, _RET_ALLOW))
    block("ioctl", (_BPF_LD_W_ABS, 0, 0, _OFF_ARG_LOW[1]),
          *[(_BPF_JEQ_K, "deny", 0, value) for value in DENIED_IOCTL_COMMANDS.values()], (_BPF_RET_K, 0, 0, _RET_ALLOW))
    block("clone", (_BPF_LD_W_ABS, 0, 0, _OFF_ARG_LOW[0]), (_BPF_JSET_K, "deny", "allow", NAMESPACE_CLONE_FLAGS))
    block("trunc1", (_BPF_LD_W_ABS, 0, 0, _OFF_ARG_LOW[1]), (_BPF_JSET_K, "deny", "allow", _O_TRUNC))
    block("trunc2", (_BPF_LD_W_ABS, 0, 0, _OFF_ARG_LOW[2]), (_BPF_JSET_K, "deny", "allow", _O_TRUNC))
    block("enosys", (_BPF_RET_K, 0, 0, _RET_ENOSYS))
    block("allow", (_BPF_RET_K, 0, 0, _RET_ALLOW))
    block("deny", (_BPF_RET_K, 0, 0, _RET_EPERM))
    program = []
    for i, (code, jt, jf, k) in enumerate(rows):
        jt = labels[jt] - i - 1 if isinstance(jt, str) else jt
        jf = labels[jf] - i - 1 if isinstance(jf, str) else jf
        if not (0 <= jt <= 255 and 0 <= jf <= 255):  # a classic BPF jump is one byte, forward only
            raise ValueError(f"the seccomp program cannot reach row {i}'s target")
        program.append((code, jt, jf, k))
    return program


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def _kernel(name: str, result: int, err: int) -> int:
    """``result`` of the call ``name``, or OSError carrying the kernel's errno."""
    if result < 0:
        raise OSError(err, f"{name}: {os.strerror(err) if err else 'failed'}")
    return result


def _step(name: str, action, *, changes: bool = True):
    """Run one step of the wall. A kernel failure is refused ``wall_partial`` naming the step and what was
    applied before it; the process is marked so that it is never walled again and never proven."""
    try:
        result = action()
    except OSError as exc:
        _STATE["failed"] = name
        applied = ", ".join(_STATE["applied"]) or "nothing"
        raise WallRefused("wall_partial", f"the wall is not whole: the step {name} failed "
                          f"({errno.errorcode.get(exc.errno or 0, exc.errno)}); applied before it: {applied}; "
                          "this process must load nothing", step=name) from exc
    if changes:
        _STATE["applied"].append(name)
    return result


def _no_new_privs(libc) -> None:
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"prctl(PR_SET_NO_NEW_PRIVS): {os.strerror(err)}")


def _load_seccomp_filter(libc, arch: str, landlock_abi: int = _MIN_LANDLOCK_ABI) -> None:
    rows = _filter(arch, landlock_abi)
    program = (_SockFilter * len(rows))(*(_SockFilter(*row) for row in rows))
    fprog = _SockFprog(len(rows), ctypes.addressof(program))
    _step("no_new_privs_before_filter", lambda: _no_new_privs(libc))

    def load():
        result, err = _call(libc, _NR[arch]["seccomp"], _SECCOMP_SET_MODE_FILTER, _SECCOMP_FILTER_FLAG_TSYNC,
                            ctypes.addressof(fprog))
        if result != 0:  # a positive result is the id of a thread that could not be synced
            raise OSError(err or errno.EAGAIN, f"seccomp filter not loaded (result {result})")
    _step("seccomp_filter", load)


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


def _ruleset_attr(abi: int) -> tuple[bytes, list[str]]:
    """struct landlock_ruleset_attr for this ABI, and the names of the scopes it asks. Below ABI 6 it is the
    first field only (handled_access_fs); from ABI 6 on it holds the three fields, with no network right
    handled (the filter refuses sockets) and both scopes asked."""
    handled = _FS_ABI1
    for since, right in _FS_LATER.items():
        if abi >= since:
            handled |= right
    if abi < _SCOPE_ABI:
        return struct.pack("<Q", handled), []
    return struct.pack("<QQQ", handled, 0, sum(LANDLOCK_SCOPES.values())), sorted(LANDLOCK_SCOPES)


def _apply_landlock(libc, abi: int, opened: list[tuple[int, int]]) -> None:
    data, scopes = _ruleset_attr(abi)
    attr = ctypes.create_string_buffer(data, len(data))
    ruleset = _step("landlock_create_ruleset", lambda: _kernel("landlock_create_ruleset", *_call(
        libc, _SYS_LANDLOCK_CREATE_RULESET, ctypes.addressof(attr), len(data), 0)), changes=False)
    try:
        for fd, allowed in opened:
            rule = ctypes.create_string_buffer(struct.pack("<Qi", allowed, fd), 12)  # struct landlock_path_beneath_attr
            _step("landlock_add_rule", lambda: _kernel("landlock_add_rule", *_call(
                libc, _SYS_LANDLOCK_ADD_RULE, ruleset, _LANDLOCK_RULE_PATH_BENEATH, ctypes.addressof(rule), 0)),
                changes=False)
        _step("no_new_privs", lambda: _no_new_privs(libc))
        _step("landlock_restrict_self", lambda: _kernel("landlock_restrict_self", *_call(
            libc, _SYS_LANDLOCK_RESTRICT_SELF, ruleset, 0)))
        _STATE["scopes"] = scopes
    finally:
        os.close(ruleset)


def apply_wall(read_only_paths) -> None:
    """Wall this process in: Landlock (read and map only ``read_only_paths``), then the one seccomp filter.

    Refuses, before any change: ``wall_system_unsupported`` (not Linux; nothing is tried),
    ``wall_arch_unsupported`` (not x86_64 or aarch64), ``wall_threads_running`` (more than one thread),
    ``landlock_unavailable`` (no Landlock, or an ABI below 1). A bad path raises ``ValueError`` or
    ``OSError`` before any change as well. A kernel failure at any step, and a second call in a process
    walled already in whole or in part, is refused ``wall_partial`` with ``step`` set.
    """
    libc = _libc()
    arch = _arch()
    if _STATE["failed"] or _STATE["applied"]:
        done = ", ".join(_STATE["applied"]) or "nothing"
        failed = f"; the step {_STATE['failed']} failed" if _STATE["failed"] else ""
        raise WallRefused("wall_partial", f"apply_wall has run in this process already (applied: {done}{failed}); "
                          "a wall is applied once, and a process walled in part must load nothing",
                          step=_STATE["failed"] or "already_walled")
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
    _load_seccomp_filter(libc, arch, abi)


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


def _each_refused(*attempts) -> None:
    """PermissionError when every attempt was refused; anything else (one went through) is not a refusal."""
    for attempt in attempts:
        if not _refused(attempt):
            raise RuntimeError("an attempt was not refused")
    raise PermissionError("every attempt was refused")


def _sys(libc, name: str, *args: int) -> int:
    """One system call by name for this arch; a negative result raises OSError with the kernel's errno."""
    arch = _arch()
    return _kernel(name, *_call(libc, dict(_NR[arch], **_NR_MORE[arch])[name], *args))


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


def _other_of_my_user(pid) -> int:
    """``pid`` when it is another process of this process's own user; else ValueError (a refusal of a pid of
    another user would prove nothing). The owner of /proc/<pid> is read by stat, which no wall here refuses."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid == os.getpid():
        raise ValueError("the stand-in daemon pid must be another process's pid")
    if os.stat(f"/proc/{pid}").st_uid != os.geteuid():
        raise ValueError("the stand-in daemon is not a process of this user, so a refusal would prove nothing")
    return pid


def _signal_other(pid) -> None:
    os.kill(_other_of_my_user(pid), 0)  # signal 0: the kernel checks the right to signal and delivers nothing


def _reach_other(pid) -> None:
    """Three other ways at the daemon, each harmless where it is allowed: a queued signal 0, the daemon made
    the receiver of a pipe's signals (the pipe is closed at once), and a read of its CPU limit."""
    target, libc = _other_of_my_user(pid), _libc()
    info = ctypes.create_string_buffer(struct.pack("<iii", 0, 0, -1), 128)  # signal 0, si_code SI_QUEUE
    old = ctypes.create_string_buffer(16)

    def owner_of_a_pipe() -> None:
        read_end, write_end = os.pipe()
        try:
            _sys(libc, "fcntl", read_end, DENIED_FCNTL_COMMANDS["F_SETOWN"], target)
        finally:
            os.close(read_end)
            os.close(write_end)
    _each_refused(lambda: _sys(libc, "rt_sigqueueinfo", target, 0, ctypes.addressof(info)),
                  owner_of_a_pipe,
                  lambda: _sys(libc, "prlimit64", target, 0, 0, ctypes.addressof(old)))  # RLIMIT_CPU, read only


def _change_mode(path: str) -> None:
    """chmod of the canary to the mode it has (nothing changes where it is allowed). A canary of another
    user is not tried: the kernel's own refusal would prove nothing."""
    try:
        found = os.stat(path)
    except FileNotFoundError:
        mode = 0o600  # absent: the kernel answers ENOENT unless the filter refuses the call first
    else:
        if found.st_uid != os.geteuid():
            raise ValueError("the canary is not this user's, so a refusal would prove nothing")
        mode = stat.S_IMODE(found.st_mode)
    os.chmod(path, mode)


def _lease_on_a_pipe(libc) -> None:
    read_end, write_end = os.pipe()
    try:
        _sys(libc, "fcntl", read_end, DENIED_FCNTL_COMMANDS["F_SETLEASE"], 2)  # F_UNLCK on a pipe: EINVAL if allowed
    finally:
        os.close(read_end)
        os.close(write_end)


def _kernel_surface() -> None:
    """Calls only this wall refuses, each harmless where it is allowed: io_uring_setup with no entries,
    unshare of nothing, a keyctl operation that does not exist, (x86_64) getpid by its x32 number, clone
    asking a new user namespace with flags the kernel itself rejects, msgctl IPC_STAT of the id -1,
    mq_getsetattr of the descriptor -1, signal 0 to this process itself by its own pid, and a lease on a
    pipe; then clone3, which this wall answers ENOSYS (the kernel's own answer to these arguments is EINVAL)."""
    libc, arch = _libc(), _arch()
    attempts = [lambda: _sys(libc, "io_uring_setup", 0, 0),
                lambda: _sys(libc, "unshare", 0),
                lambda: _sys(libc, "keyctl", 0x7FFFFFFF, 0, 0, 0, 0),
                lambda: _sys(libc, "clone", 0x10000000 | 0x00000200, 0, 0, 0, 0),  # CLONE_NEWUSER|CLONE_FS: EINVAL
                lambda: _sys(libc, "msgctl", -1, 2, 0),                             # IPC_STAT of no queue: EINVAL
                lambda: _sys(libc, "mq_getsetattr", -1, 0, 0),                      # no descriptor: EBADF
                lambda: _sys(libc, "kill", os.getpid(), 0),                         # signal 0 to itself: ok
                lambda: _lease_on_a_pipe(libc)]
    if arch == "x86_64":
        attempts.append(lambda: _kernel("x32 getpid", *_call(libc, _X32_SYSCALL_BIT | 39)))
    try:
        _each_refused(*attempts)
    except PermissionError:
        pass
    result, err = _call(libc, _NR_MORE[arch]["clone3"], 0, 0)
    if result >= 0 or err != _ENOSYS:
        raise RuntimeError("clone3 was not answered by the wall")
    raise PermissionError("every call of the kernel's surface was refused")


def probe_wall(canary_path, daemon_pid) -> dict:
    """The record ``wall.v1``: a check the host's own code makes on itself, from inside this process.

    Eight checks are tried; each field is True only when the kernel refused it. ``proven`` is True only when
    all eight are refused, on a supported arch with Landlock ABI 1 or above, and no step of ``apply_wall``
    failed in this process. ``landlock_scopes`` names the Landlock scopes ``apply_wall`` put in force (none
    below ABI 6). The record is a self-check, not a proof against a hostile process: it means something only
    when it is made after ``apply_wall`` and before any plugin code is loaded, and when its reader takes it
    only from frames sent before the load. Refuses ``wall_system_unsupported`` where the two system calls
    are absent, without trying.
    """
    libc = _libc()
    canary = os.fspath(canary_path)
    record = {
        "reads_outside": _refused(_read_root),
        "home_canary": _refused(lambda: _read_canary(canary)),
        "program_start": _refused(_start_program),
        "new_socket": _refused(_open_sockets),
        "signal_other": _refused(lambda: _signal_other(daemon_pid)),
        "signal_other_ways": _refused(lambda: _reach_other(daemon_pid)),
        "metadata_change": _refused(lambda: _change_mode(canary)),
        "kernel_surface": _refused(_kernel_surface),
        "landlock_abi": _landlock_abi(libc),
        "landlock_scopes": list(_STATE["scopes"]),
        "arch": platform.machine() or "unknown",
    }
    record["proven"] = (all(record[name] for name in RECORD_CHECKS) and record["landlock_abi"] >= _MIN_LANDLOCK_ABI
                        and record["arch"] in _NR and _STATE["failed"] is None)
    return record
