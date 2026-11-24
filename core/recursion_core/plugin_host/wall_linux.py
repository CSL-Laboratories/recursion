"""The wall a plugin host puts around itself on Linux, before it loads any plugin byte.

A host process makes a write folder of its own and calls ``prepare_write_folder(folder)`` on it, calls
``apply_wall(read_only_paths)`` on itself while it has ONE thread, then
``probe_wall(canary_path, daemon_pid, write_file=..., write_folder=folder)``, and sends the record that the
probe returns (``wall.v1``, ``schemas/plugin/v1/wall.v1.json``); when the record is not proven,
``why_not_proven(record)`` gives the sentences a person reads, every word from the one table ``WORDS``. The wall has two parts, both applied by the kernel to this process
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
kernel at that moment, and code in the same process could write any record. Each kind of write
(``WRITE_KINDS``) is tried on a file and in a folder the caller made for the purpose before the wall, each
attempt harmless if it goes through, and the record names, per kind, which part of the wall refused it by
the kernel's answer (Landlock answers EACCES, the filter EPERM); a refusal is credited only to the part that
holds that kind in this process (the filter holds only truncation, and only below Landlock ABI 3), and any
other refusal is ``refused_elsewhere``, which proves nothing. Whatever a weak wall let the probe make in the
folder is the caller's to remove, with the folder. The kinds that cannot be tried harmlessly are named in
the record with their reasons (``WRITES_NOT_TRIED``). It means something under two conditions only: it is made before the plugin is loaded,
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
           "LANDLOCK_SCOPES", "NAMESPACE_CLONE_FLAGS", "OLD_LANDLOCK_DENIED_CALLS", "RECORD_CHECKS", "WORDS",
           "WRITES_NOT_TRIED", "WRITE_FOLDER_NAMES", "WRITE_KINDS", "WRITE_OUTCOMES", "WallRefused", "apply_wall",
           "prepare_write_folder", "probe_wall", "why_not_proven"]

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
# The Landlock rights the write kinds of the probe need (include/uapi/linux/landlock.h).
_FS_WRITE_FILE, _FS_REMOVE_DIR, _FS_REMOVE_FILE, _FS_MAKE_CHAR = 1 << 1, 1 << 4, 1 << 5, 1 << 6
_FS_MAKE_DIR, _FS_MAKE_REG, _FS_MAKE_FIFO, _FS_MAKE_BLOCK, _FS_MAKE_SYM = 1 << 7, 1 << 8, 1 << 10, 1 << 11, 1 << 12
_FS_TRUNCATE = 1 << 14

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
# The kinds of change the probe tries behind the wall, in its order (the record's field ``writes``), each on the
# write file or in the write folder that the caller made for the purpose before the wall.
WRITE_KINDS = ("open_for_write", "truncate", "create_file", "create_folder", "make_fifo", "make_symlink",
               "make_link", "make_char_device", "make_block_device", "remove_file", "remove_folder")
# The outcome of one kind; only the first two are refusals of this wall, and proven needs one of them for every kind.
WRITE_OUTCOMES = ("refused_by_landlock", "refused_by_filter", "refused_elsewhere", "let_through", "error", "not_tried")
_WRITE_REFUSED = WRITE_OUTCOMES[:2]
# The kinds of change the wall refuses that the probe cannot try harmlessly on its own, each with its reason
# (the record's field ``writes_not_tried``).
WRITES_NOT_TRIED = {
    "make_socket_node": "a socket file is made only by binding a new socket, and the wall refuses every new "
                        "socket first (new_socket)",
    "device_ioctl": "a device's own commands need a device opened, and the wall lets nothing be opened outside "
                    "the install, where no device lies (reads_outside)",
    "move_between_folders": "a move or a link into another folder also makes a name there, which the make kinds "
                            "show refused",
}
# The names the probe uses in the write folder: the two that prepare_write_folder makes for the removals, and the
# ones the make kinds try to create (absent before the probe; whatever a weak wall let it make stays there for the
# caller to remove). File names, not figures.
WRITE_FOLDER_NAMES = {"remove_file": "remove-me", "remove_folder": "remove-me.d", "create_file": "made-file",
                      "create_folder": "made-folder", "make_fifo": "made-fifo", "make_symlink": "made-symlink",
                      "make_link": "made-link", "make_char_device": "made-char", "make_block_device": "made-block"}
_LANDLOCK_RIGHT = {"open_for_write": _FS_WRITE_FILE, "truncate": _FS_TRUNCATE, "create_file": _FS_MAKE_REG,
                   "create_folder": _FS_MAKE_DIR, "make_fifo": _FS_MAKE_FIFO, "make_symlink": _FS_MAKE_SYM,
                   "make_link": _FS_MAKE_REG, "make_char_device": _FS_MAKE_CHAR, "make_block_device": _FS_MAKE_BLOCK,
                   "remove_file": _FS_REMOVE_FILE, "remove_folder": _FS_REMOVE_DIR}

# What a person reads when a wall is not proven: ONE table of words (why_not_proven puts them together). The
# entries named after a check or a kind say what was tried; the others are whole sentences.
WORDS = {
    "not_loaded": "The plugin was not loaded. Before any plugin code runs, the plugin host shuts itself in and "
                  "then tests that it is shut in; this time the test found a gap, so nothing of the plugin ran.",
    "reads_outside": "reading a folder outside the Python install",
    "home_canary": "reading a test file that the host made for this test",
    "program_start": "starting another program",
    "new_socket": "opening a new network or local connection",
    "signal_other": "sending a signal to another of your processes",
    "signal_other_ways": "reaching another of your processes in other ways (a queued signal, a pipe's signals, "
                         "its limits)",
    "metadata_change": "changing the permissions of a test file",
    "kernel_surface": "kernel calls that only this sandbox refuses",
    "open_for_write": "opening a test file for writing (nothing was written)",
    "truncate": "cutting a test file to its own length (nothing was cut off)",
    "create_file": "making a new file in a test folder",
    "create_folder": "making a new folder in a test folder",
    "make_fifo": "making a named pipe in a test folder",
    "make_symlink": "making a shortcut (a symbolic link) in a test folder",
    "make_link": "giving a test file a second name (a hard link)",
    "make_char_device": "making a character device file in a test folder",
    "make_block_device": "making a block device file in a test folder",
    "remove_file": "deleting a test file",
    "remove_folder": "deleting a test folder",
    "not_refused": "Not shown to be refused: {what}.",
    "let_through": "A change went through that should have been refused: {what}. It touched only a file or "
                   "folder made for this test, never one of yours.",
    "refused_elsewhere": "Refused, but not by the sandbox itself (by the file's own settings or by another "
                         "security layer), so the test does not show that the sandbox would refuse it: {what}.",
    "error": "Could not be tested, so it shows nothing: {what}.",
    "not_tried": "Not tested, because the host gave no usable test file or folder for it: {what}.",
    "no_landlock": "This system's kernel gives the host no Landlock, the part of Linux the sandbox is built on.",
    "other_arch": "The sandbox knows x86_64 and aarch64 machines only; this one is {arch}.",
    "partial": "A step of the sandbox failed while it was being put up, so the host loads nothing.",
    "what_to_do": "What you can do: run the plugin host on Linux 5.13 or newer with Landlock turned on (most "
                  "distributions turn it on; otherwise add landlock to the kernel's lsm= boot setting), outside "
                  "any container or security tool that changes what a process may do with files, and start it "
                  "again. If it is still refused, keep this message: it says what got through.",
}

# How far apply_wall has gone in THIS process (a forked child inherits it): the steps that changed the
# process, the step that failed, and the Landlock scopes in force. A process with a failed step, or with any
# step, is never walled again.
_STATE: dict = {"applied": [], "failed": None, "scopes": [], "landlock_handled": 0, "filter_truncate": False}


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
    _STATE["filter_truncate"] = landlock_abi < _TRUNCATE_ABI  # what the loaded program refuses, for the record


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
        _STATE["landlock_handled"] = struct.unpack_from("<Q", data)[0]  # the rights the ruleset in force handles
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


def prepare_write_folder(write_folder) -> None:
    """Make, in ``write_folder`` (an EMPTY folder of this user that the caller made for the probe), the file and the
    folder that the probe's removals try to remove (``WRITE_FOLDER_NAMES``). The caller calls it BEFORE
    ``apply_wall``, and afterwards removes the whole folder with whatever a weak wall let the probe make in it."""
    folder = os.fspath(write_folder)
    if os.listdir(folder):
        raise ValueError("the write folder must be empty: a name already there would make a refusal prove nothing")
    os.close(os.open(os.path.join(folder, WRITE_FOLDER_NAMES["remove_file"]),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600))
    os.mkdir(os.path.join(folder, WRITE_FOLDER_NAMES["remove_folder"]), 0o700)


def _refused_by(kind: str, err) -> str:
    """Which part of this wall a refusal of ``kind`` belongs to, by the kernel's answer. Landlock answers EACCES
    and holds the kind only when the ruleset in force handles its right; the filter answers EPERM and holds only
    truncation, only where it was loaded with the rows for a Landlock ABI below 3. Any other refusal (a file's own
    flags, another security layer) is ``refused_elsewhere``: it shows nothing of this wall."""
    applied = _STATE.get("applied", [])
    if (err == errno.EACCES and "landlock_restrict_self" in applied
            and _STATE.get("landlock_handled", 0) & _LANDLOCK_RIGHT[kind]):
        return "refused_by_landlock"
    if err == errno.EPERM and kind == "truncate" and "seccomp_filter" in applied and _STATE.get("filter_truncate"):
        return "refused_by_filter"
    return "refused_elsewhere"


def _attempt(kind: str, act, changed=None) -> str:
    """One write kind, tried once. ``changed`` (when given) says afterwards whether the change took place (a name
    made or gone), so a call that raised after it changed something is counted as let through."""
    try:
        act()
    except PermissionError as exc:
        outcome = _refused_by(kind, exc.errno)
    except Exception:
        outcome = "error"
    else:
        outcome = "let_through"
    if changed is not None and outcome != "let_through":
        try:
            if changed():
                outcome = "let_through"
        except Exception:
            outcome = "error"
    return outcome


def _kind_in(folder_fd: int, name: str):
    """The file type of ``name`` in the folder (not followed), or None when it is not there. A name of another user
    is an OSError: a refusal on it would prove nothing."""
    try:
        found = os.stat(name, dir_fd=folder_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if found.st_uid != os.geteuid():
        raise OSError(f"{name} in the write folder is not this user's")
    return stat.S_IFMT(found.st_mode)


def _write_file_usable(path: str) -> bool:
    """A regular file of this user, writable by its mode: a refusal on anything else would prove nothing."""
    try:
        found = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(found.st_mode) and found.st_uid == os.geteuid() and os.access(path, os.W_OK)


def _write_folder_fd(folder: str):
    """An O_PATH descriptor of the write folder, or None when the probe may not use it: a real folder of this user,
    writable and searchable by its mode, holding the two names prepare_write_folder made and none of the names the
    make kinds create."""
    try:
        fd = os.open(folder, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        found = os.fstat(fd)
        usable = (stat.S_ISDIR(found.st_mode) and found.st_uid == os.geteuid()
                  and os.access(folder, os.W_OK | os.X_OK) and os.lstat(folder).st_ino == found.st_ino
                  and _kind_in(fd, WRITE_FOLDER_NAMES["remove_file"]) == stat.S_IFREG
                  and _kind_in(fd, WRITE_FOLDER_NAMES["remove_folder"]) == stat.S_IFDIR
                  and all(_kind_in(fd, name) is None for kind, name in WRITE_FOLDER_NAMES.items()
                          if not kind.startswith("remove_")))
    except OSError:
        usable = False
    if not usable:
        os.close(fd)
        return None
    return fd


def _write_outcomes(write_file, write_folder) -> dict:
    """The outcome of each kind of ``WRITE_KINDS``, in its order. Each attempt is harmless where it goes through:
    the file is opened for writing and shut with nothing written, and cut to its own length; the folder gets new
    names the caller removes, and loses only the two names prepare_write_folder made for that. A kind whose file or
    folder is not given, or is not one the probe may use, is ``not_tried``."""
    outcomes = dict.fromkeys(WRITE_KINDS, "not_tried")
    if write_file is not None:
        path = os.fspath(write_file)
        if _write_file_usable(path):
            size = os.lstat(path).st_size
            outcomes["open_for_write"] = _attempt("open_for_write", lambda: os.close(
                os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_NOCTTY | os.O_CLOEXEC)))
            outcomes["truncate"] = _attempt("truncate", lambda: os.truncate(path, size))
    fd = None if write_folder is None else _write_folder_fd(os.fspath(write_folder))
    if fd is None:
        return outcomes
    name = WRITE_FOLDER_NAMES
    try:
        def made(kind):
            return lambda: _kind_in(fd, name[kind]) is not None

        def gone(kind):
            return lambda: _kind_in(fd, name[kind]) is None
        attempts = {
            "create_file": (lambda: os.mknod(name["create_file"], stat.S_IFREG | 0o600, dir_fd=fd),
                            made("create_file")),
            "create_folder": (lambda: os.mkdir(name["create_folder"], 0o700, dir_fd=fd), made("create_folder")),
            "make_fifo": (lambda: os.mkfifo(name["make_fifo"], 0o600, dir_fd=fd), made("make_fifo")),
            "make_symlink": (lambda: os.symlink(name["remove_file"], name["make_symlink"], dir_fd=fd),
                             made("make_symlink")),
            "make_link": (lambda: os.link(name["remove_file"], name["make_link"], src_dir_fd=fd, dst_dir_fd=fd,
                                          follow_symlinks=False), made("make_link")),
            # the device 0:0 names no device at all (as a character device it is the kernel's whiteout)
            "make_char_device": (lambda: os.mknod(name["make_char_device"], stat.S_IFCHR | 0o600, os.makedev(0, 0),
                                                  dir_fd=fd), made("make_char_device")),
            "make_block_device": (lambda: os.mknod(name["make_block_device"], stat.S_IFBLK | 0o600, os.makedev(0, 0),
                                                   dir_fd=fd), made("make_block_device")),
            "remove_file": (lambda: os.unlink(name["remove_file"], dir_fd=fd), gone("remove_file")),
            "remove_folder": (lambda: os.rmdir(name["remove_folder"], dir_fd=fd), gone("remove_folder")),
        }
        for kind in WRITE_KINDS:
            if kind in attempts:
                outcomes[kind] = _attempt(kind, *attempts[kind])
    finally:
        os.close(fd)
    return outcomes


def why_not_proven(record: dict) -> list:
    """The sentences a person reads when ``record`` is not proven, in order (an empty list when it is): that the
    plugin was not loaded, each check and each kind of write that did not hold, in plain words, and what they can
    do. Every word comes from ``WORDS``; the record is read as data, so a record of an older draft reads as not
    proven with its missing parts named."""
    if record.get("proven") is True:
        return []
    lines = [WORDS["not_loaded"]]
    for check in RECORD_CHECKS:
        if record.get(check) is not True:
            lines.append(WORDS["not_refused"].format(what=WORDS[check]))
    writes = record.get("writes") if isinstance(record.get("writes"), dict) else {}
    for kind in WRITE_KINDS:
        outcome = writes.get(kind, "not_tried")
        if outcome not in _WRITE_REFUSED:
            key = outcome if outcome in ("let_through", "refused_elsewhere", "not_tried") else "error"
            lines.append(WORDS[key].format(what=WORDS[kind]))
    abi = record.get("landlock_abi")
    if not isinstance(abi, int) or isinstance(abi, bool) or abi < _MIN_LANDLOCK_ABI:
        lines.append(WORDS["no_landlock"])
    if record.get("arch") not in _NR:
        lines.append(WORDS["other_arch"].format(arch=str(record.get("arch") or "unknown")[:32]))
    if len(lines) == 1:  # every part held, yet not proven: a step of the wall failed
        lines.append(WORDS["partial"])
    lines.append(WORDS["what_to_do"])
    return lines


def probe_wall(canary_path, daemon_pid, write_file=None, write_folder=None) -> dict:
    """The record ``wall.v1``: a check the host's own code makes on itself, from inside this process.

    Eight checks are tried; each field is True only when the kernel refused it. Then each kind of
    ``WRITE_KINDS`` is tried on ``write_file`` (a file of this user, writable by its mode, that the caller made:
    the host's canary will do) and in ``write_folder`` (a folder the caller made and gave to
    ``prepare_write_folder`` before the wall); ``writes`` holds the outcome of each, and ``writes_not_tried``
    names the kinds that cannot be tried harmlessly, with their reasons. ``proven`` is True only when all eight
    are refused, every write kind is refused by the part of the wall that holds it, on a supported arch with
    Landlock ABI 1 or above, and no step of ``apply_wall`` failed in this process; without a write file and
    folder it is never True. ``landlock_scopes`` names the Landlock scopes ``apply_wall`` put in force (none
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
        "writes": _write_outcomes(write_file, write_folder),
        "writes_not_tried": dict(WRITES_NOT_TRIED),
        "landlock_abi": _landlock_abi(libc),
        "landlock_scopes": list(_STATE["scopes"]),
        "arch": platform.machine() or "unknown",
    }
    record["proven"] = (all(record[name] for name in RECORD_CHECKS) and record["landlock_abi"] >= _MIN_LANDLOCK_ABI
                        and all(record["writes"][kind] in _WRITE_REFUSED for kind in WRITE_KINDS)
                        and record["arch"] in _NR and _STATE["failed"] is None)
    return record
