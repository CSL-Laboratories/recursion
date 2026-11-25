"""The wall a plugin host puts around itself on Linux, before it loads any plugin byte.

A host process makes a write folder of its own and calls ``prepare_write_folder(folder)`` on it, calls
``apply_wall(read_only_paths, keep_descriptors=(channel,))`` on itself while it has ONE thread, then
``probe_wall(canary_path, daemon_pid, write_file=..., write_folder=folder)``, and sends the record that the
probe returns (``wall.v1``, ``schemas/plugin/v1/wall.v1.json``); when the record is not proven,
``why_not_proven(record)`` gives the sentences a person reads, every word from the one table ``WORDS``. The wall has two parts, both applied by the kernel to this process
and to everything it starts later, and neither can be lifted again from inside:

1. A Landlock ruleset that handles every file-system right the kernel's Landlock ABI knows and allows only
   reading, listing and mapping for execution beneath the paths given (the Python install), and nothing
   else: no file opened after the wall can be written, no read of anything outside those paths.
   ``prctl(PR_SET_NO_NEW_PRIVS)`` comes first, then ``landlock_restrict_self``. Landlock checks a file when it
   is opened, so a file the process opened for writing BEFORE the wall can still be written through, and a
   file mapped shared and writable before it can still be written through the map: the host closes every
   such file before the wall, and ``probe_wall`` is never proven while it finds one open for writing or one
   mapped shared and writable. ``apply_wall`` opens the kernel's own list of the process's descriptors
   (``/proc/self/fd``) and of its maps (``/proc/self/maps``) for reading BEFORE the wall, reads both AFTER it,
   and closes both before it returns, so that no plugin code holds either; the record is built from what it
   read there. ``probe_wall`` then names every descriptor the process holds at the probe's own moment (found by
   number, up to the kernel's own count of them where the kernel gives one): a descriptor that the kernel's list
   did not hold after the wall (one handed in over a connection held from before it) is named, by kind, and
   keeps the record from proven, as does a record made in a process where ``apply_wall`` did not run.
2. ONE seccomp filter, loaded with ``SECCOMP_FILTER_FLAG_TSYNC`` so that it reaches every thread of the
   process. It checks the audit arch first (x86_64 and aarch64; a call made under another arch, or the
   x32 range of x86_64, is answered EPERM), then answers EPERM to:
   - every call of ``DENIED_CALLS`` (sockets, program starts, tracing, keys, every mount call: mount, umount2 and the newer
     open_tree, move_mount, fsopen, fsconfig, fsmount, fspick, mount_setattr and open_tree_attr; namespaces, bpf, a ring of
     kernel calls and its use, a message sent by ``sendmsg`` or ``sendmmsg``, ...), and ``sendto`` with a
     destination address: a ring or a datagram socket the host held from before the wall can neither submit work
     nor send to an address of its choosing after it;
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
     that makes another process's open of a file wait, and a file's attribute flags), and ``ioctl`` with a
     command of ``DENIED_TERMINAL_COMMANDS`` (a terminal's foreground group, its controlling session, its
     settings, size, lines and input: on a terminal the host inherited, each acts on the person's terminal or
     on the processes of its foreground group), and ``fcntl`` with ``F_SETFL`` asking ``O_ASYNC``: with
     ``F_SETSIG`` and ``F_NOTIFY`` among the refused commands, a walled process cannot choose or start the signals
     a descriptor sends to an owner set before the wall; the terminal commands that only read (``TCGETS``, which
     ``isatty`` asks, ``TIOCGPGRP``, ``TIOCGSID``, ``TIOCGWINSZ``, ``TIOCMGET``, ``TIOCOUTQ``, ``FIONREAD``) and the
     ones that touch only the caller's own descriptor (``FIONBIO``, which a socket's ``setblocking`` asks,
     ``FIOCLEX`` and ``FIONCLEX``) stay allowed (``ALLOWED_TERMINAL_COMMANDS``), and every other command of the
     terminal range (0x5400 to 0x54FF), named or not, is answered EPERM;
   - ``clone`` with any flag of ``NAMESPACE_CLONE_FLAGS``; ``clone3`` is answered ENOSYS, so that the C
     library starts threads and children through ``clone``, where the flags can be read;
   - where the kernel's Landlock ABI is below 3 (it cannot refuse a truncation there),
     ``OLD_LANDLOCK_DENIED_CALLS`` (a truncation by path and by descriptor) and an ``open`` or ``openat``
     that asks ``O_TRUNC``.
   Every call numbered above the newest call the wall's tables name is answered EPERM, by one comparison (as the
   x32 range is), so a call a newer kernel adds stays refused until the wall names it.
   The filter is an ALLOW LIST: after the refusals above it lets through only the calls named in the list
   ``ALLOW_LIST_FILE`` (``wall_allow_linux.json`` beside this module), each row with its number on each arch, the
   check of its arguments where it is allowed only in part (the checks above, which a row cannot drop or change),
   and the reason it is allowed; EVERY call the list does not name is answered EPERM, so a call the kernel adds
   later, or one nobody thought of, is refused by default. The list was derived from the calls the wall's own
   tests make behind the wall (recorded on CPython 3.12 and the free-threaded 3.14t, x86_64); a row those
   recordings did not see carries the written reason it is on the list all the same. Ordinary Python keeps
   working: imports from the install, threads, ``fork``, pipes, and ``recvmsg`` of a socket handed over on an
   already open channel stay allowed. The list is read when the wall is applied, before its first change, and a
   list that is missing, unreadable, not of the wall's form, or with a row that carries no reason, names a call
   the wall refuses by name, drops a check, or gives a number the wall's tables do not reach, is refused
   ``wall_allow_list_refused`` with words that say which row and what to do: the wall then applies nothing.

Before its first change ``apply_wall`` makes one object of its own of each kind a process can hold from before a
wall and still use after it (``HELD_KINDS``: a ring of kernel calls, an unconnected datagram socket with a receiver
of its own, a connected stream socket pair, a pipe that signals this process itself on every byte), and then CLOSES
EVERY DESCRIPTOR of the process but the ones the caller keeps (``keep_descriptors``: the host's channel, nothing else),
by the kernel's ``close_range`` over the gaps between the kept numbers (where the kernel has no ``close_range``, each
number the kernel's own list names); the standard numbers 0, 1 and 2, where not kept, then point at ``/dev/null`` so
that no file opened later takes one. A kept descriptor that is not open, or that has a receiver of its signals set (an
owner, or ``O_ASYNC``), is refused ``wall_keep_refused`` before any change. So a socket connected before the wall, or a
descriptor whose ``O_ASYNC`` was set before it, acts after the wall only when it is kept, and a kept one sends no
signal. Where ``keep_descriptors`` is left out (None) nothing is closed and the record is never proven. The record names
the kept descriptors by kind (``descriptors_kept``) and every other one the kernel's list held after the wall
(``descriptors_not_closed``, which a proven record holds empty), and ``probe_wall`` tries one harmless act through each
object the wall made (``held_objects``): proven only when each was closed by the wall or its act refused by the filter;
a kind the wall does not make is named with its reason (``HELD_NOT_TRIED``), never covered by proven in silence. The
kernel's list after the wall is kept with each descriptor's identity (device, inode and file type: two sockets differ
by inode), so a descriptor received after the wall at the number of one closed since is named as one handed in.

The refusals by name are the wall's own definition, not a setting: there is no row, flag or argument that
shortens them. The allow list is data: ``apply_wall(..., allow_list=path)`` takes another file in its place, so
that a person can widen it knowingly (a row can add a call; it can never allow a call refused by name or drop a
check), and the record then says that the list in force is not the shipped one: 'proven' speaks for the shipped
list only. Landlock below ABI 4 does not limit TCP and below ABI 6 does not scope signals; the seccomp
filter shuts both here (``socket``, ``connect`` and every way named above to signal, limit, slow or move
another process are denied whatever the ABI). Where the kernel's Landlock ABI is 6 or above, the ruleset
also asks both of the kernel's scopes (``LANDLOCK_SCOPES``: abstract unix sockets and signals, each kept
to the domain); the filter's refusals stay, as the floor for every older kernel, and the record names
the scopes in force (``landlock_scopes``).

What the record is: a check that the host's own code makes on itself, from inside the process, after
``apply_wall`` and BEFORE any plugin code runs. It shows that the named attempts were refused by the
kernel at that moment, and code in the same process could write any record. Each kind of write
(``WRITE_KINDS``) is tried on a file and in a folder the caller made for the purpose before the wall, each
attempt harmless if it goes through (the file is one ``prepare_write_folder`` made itself, reached only through a
descriptor opened without following a link), and the record names, per kind, which part of the wall refused it by
the kernel's answer (Landlock answers EACCES, the filter EPERM); a refusal is credited only to the part that
holds that kind in this process (the filter holds only truncation, and only below Landlock ABI 3), and any
other refusal is ``refused_elsewhere``, which proves nothing. Whatever a weak wall let the probe make in the
folder is the caller's to remove, with the folder. 'proven' covers the kinds of ``WRITE_KINDS`` only: every
other kind the wall claims, or leaves to the host, is named in the record with the reason it is not tried
(``WRITES_NOT_TRIED``), and the record names the threads of the process and each descriptor it holds open for
writing, either of which keeps it from proven. It means something under two conditions only: it is made before the plugin is loaded,
and its reader takes it only from frames the host sent before the load (everything later on the channel
is the plugin's voice).

Landlock limits only the calling thread, so ``apply_wall`` reads its whole argument into a list first and
then counts the threads: more than one is refused ``wall_threads_running`` before any change. It counts again
immediately before ``landlock_restrict_self`` and after the filter is loaded; more than one thread there is
refused ``wall_partial`` with the step (``threads_before_landlock``, ``threads_after_filter``). ``probe_wall``
counts the threads itself and is never proven with more than one. The refusals before the first change:
``wall_system_unsupported`` (not Linux: nothing is tried), ``wall_arch_unsupported``,
``wall_threads_running``, ``wall_children_running`` (a child process of this one is alive at the wall: the wall
binds this process and what it starts afterwards, never a process that already runs, which could act for it through
memory they share), ``landlock_unavailable``. A failure of the kernel at any step of the wall, and a
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
import json
import os
import platform
import socket
import stat
import struct
import sys
import threading

__all__ = ["ALLOW_LIST_FILE", "CALLER_ONLY_CALLS", "CALLER_ONLY_PRIORITY_CALLS", "DENIED_CALLS", "DENIED_FCNTL_COMMANDS",
           "DENIED_IOCTL_COMMANDS", "DENIED_IPC_CALLS", "DENIED_METADATA_CALLS", "DENIED_SIGNAL_CALLS",
           "ALLOWED_TERMINAL_COMMANDS", "DENIED_TERMINAL_COMMANDS", "HELD_KINDS", "HELD_NOT_TRIED", "HELD_OUTCOMES", "LANDLOCK_SCOPES", "NAMESPACE_CLONE_FLAGS", "OLD_LANDLOCK_DENIED_CALLS", "RECORD_CHECKS", "WORDS",
           "WRITES_NOT_TRIED", "WRITE_FILE_NAME", "WRITE_FOLDER_NAMES", "WRITE_KINDS", "WRITE_OUTCOMES", "WallRefused", "apply_wall",
           "load_allow_list", "prepare_write_folder", "probe_wall", "why_not_proven"]

# The wall's own definition (not a setting, not a tunable): each call is answered EPERM.
DENIED_CALLS = (
    "socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
    "execve", "execveat",
    "ptrace", "process_vm_readv", "process_vm_writev",
    "pidfd_open", "pidfd_getfd", "pidfd_send_signal",
    "keyctl", "add_key", "request_key",
    "mount", "umount2", "open_tree", "move_mount", "fsopen", "fsconfig", "fsmount", "fspick", "mount_setattr", "open_tree_attr",
    "unshare", "setns",
    "bpf", "perf_event_open", "userfaultfd", "io_uring_setup", "io_uring_enter", "io_uring_register",
    "sendmsg", "sendmmsg",
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
# The signal such a receiver gets, and the folder changes that send it: a walled process may not choose either.
DENIED_FCNTL_COMMANDS = {"F_SETOWN": 8, "F_SETOWN_EX": 15, "F_SETLEASE": 1024, "F_SETSIG": 10, "F_NOTIFY": 1026}
# fcntl F_SETFL is refused only when its argument asks O_ASYNC (signals on every change of the descriptor); the
# other flags (O_NONBLOCK, which a socket's setblocking sets) stay allowed. The same values on x86_64 and aarch64.
_F_SETFL, _O_ASYNC = 4, 0o20000
_F_GETOWN_EX = 16             # struct f_owner_ex {int type; pid_t pid}: a pid of 0 is no owner
_SIGURG = 23                  # the same on x86_64 and aarch64; ignored unless a process asks for it
_RING_PARAMS = 120            # sizeof(struct io_uring_params): the kernel's interface
_CLOSE_RANGE_TOP = 0xFFFFFFFF  # close_range's last number: every number above the last kept one
# ioctl commands: another process as the receiver of a socket's signals; signals on every change of a descriptor
# (FIOASYNC, the ioctl form of F_SETFL asking O_ASYNC, the same value on x86_64 and aarch64); a file's attribute flags.
DENIED_IOCTL_COMMANDS = {"FIOSETOWN": 0x8901, "SIOCSPGRP": 0x8902, "FS_IOC_SETFLAGS": 0x40086602,
                         "FS_IOC_FSSETXATTR": 0x401C5820, "FIOASYNC": 0x5452}
# ioctl commands of a terminal that act on the person's terminal or on the processes of its foreground group
# (asm-generic/ioctls.h: the same values on x86_64 and aarch64), answered EPERM on every descriptor.
DENIED_TERMINAL_COMMANDS = {
    "TCSETS": 0x5402, "TCSETSW": 0x5403, "TCSETSF": 0x5404, "TCSETA": 0x5406, "TCSETAW": 0x5407, "TCSETAF": 0x5408,
    "TCSBRK": 0x5409, "TCXONC": 0x540A, "TCFLSH": 0x540B, "TIOCEXCL": 0x540C, "TIOCNXCL": 0x540D,
    "TIOCSCTTY": 0x540E, "TIOCSPGRP": 0x5410, "TIOCSTI": 0x5412, "TIOCSWINSZ": 0x5414, "TIOCMBIS": 0x5416,
    "TIOCMBIC": 0x5417, "TIOCMSET": 0x5418, "TIOCSSOFTCAR": 0x541A, "TIOCCONS": 0x541D, "TIOCSSERIAL": 0x541F,
    "TIOCPKT": 0x5420, "TIOCNOTTY": 0x5422, "TIOCSETD": 0x5423, "TCSBRKP": 0x5425, "TIOCSBRK": 0x5427,
    "TIOCCBRK": 0x5428, "TCSETS2": 0x402C542B, "TCSETSW2": 0x402C542C, "TCSETSF2": 0x402C542D,
    "TIOCSRS485": 0x542F, "TIOCSPTLCK": 0x40045431, "TIOCSIG": 0x40045436, "TIOCVHANGUP": 0x5437,
    "TIOCGPTPEER": 0x5441, "TIOCSERCONFIG": 0x5453, "TIOCSLCKTRMIOS": 0x5457,
    "TIOCLINUX": 0x541C, "TCSETX": 0x5433, "TCSETXF": 0x5434, "TCSETXW": 0x5435, "TIOCSERSETMULTI": 0x545B,
}
# The commands of the terminal range (0x5400 to 0x54FF, asm-generic/ioctls.h) that only read, or that touch only the
# caller's own descriptor: the only ones of the range the filter lets through. Every other command of the range, named
# above or not, is answered EPERM, so a setter the kernel adds to the range later stays refused.
ALLOWED_TERMINAL_COMMANDS = {"TCGETS": 0x5401, "TIOCGPGRP": 0x540F, "TIOCOUTQ": 0x5411, "TIOCGWINSZ": 0x5413,
                             "TIOCMGET": 0x5415, "FIONREAD": 0x541B, "FIONBIO": 0x5421, "TIOCGSID": 0x5429,
                             "FIONCLEX": 0x5450, "FIOCLEX": 0x5451}
_TERMINAL_RANGE = (0x5400, 0x5500)  # the kernel's: type byte 'T' (0x54) with no size or direction bits
# A call number the kernel will never give a call (the last one below the x32 range): the probe's planted unknown call.
_UNKNOWN_CALL = 0x40000000 - 1
# The allow list: data beside this module, read when the wall is applied (see load_allow_list).
ALLOW_LIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wall_allow_linux.json")
_ALLOW_FORMAT = "wall-allow-list.v1"
_ALLOW_ROW_KEYS = ("call", "x86_64", "aarch64", "check", "reason", "recorded", "beyond_recording")
_ALLOW_CHUNK = 250  # allow rows per run of comparisons: a classic BPF jump reaches 255 rows forward at most
_RESOLVE_NO_SYMLINKS = 0x04  # struct open_how's resolve flag (include/uapi/linux/openat2.h)
# clone with any of these flags (a new mount, cgroup, uts, ipc, user, pid or network namespace) is refused.
NAMESPACE_CLONE_FLAGS = 0x00020000 | 0x02000000 | 0x04000000 | 0x08000000 | 0x10000000 | 0x20000000 | 0x40000000
# Below Landlock ABI 3 the ruleset cannot refuse a truncation, so these are refused by the filter instead
# (by path and by descriptor), with an open or openat that asks O_TRUNC (openat2 carries its flags in a struct
# the filter cannot read).
OLD_LANDLOCK_DENIED_CALLS = ("truncate", "ftruncate", "openat2")
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
        "io_uring_enter": 426, "io_uring_register": 427, "sendmsg": 46, "sendmmsg": 307,
        "open_tree": 428, "move_mount": 429, "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433,
        "mount_setattr": 442, "open_tree_attr": 467,
    },
    "aarch64": {
        "socket": 198, "socketpair": 199, "connect": 203, "bind": 200, "listen": 201, "accept": 202, "accept4": 242,
        "execve": 221, "execveat": 281, "ptrace": 117, "process_vm_readv": 270, "process_vm_writev": 271,
        "pidfd_open": 434, "pidfd_getfd": 438, "pidfd_send_signal": 424, "keyctl": 219, "add_key": 217,
        "request_key": 218, "mount": 40, "umount2": 39, "unshare": 97, "setns": 268, "bpf": 280,
        "perf_event_open": 241, "userfaultfd": 282, "io_uring_setup": 425, "seccomp": 277,
        "io_uring_enter": 426, "io_uring_register": 427, "sendmsg": 211, "sendmmsg": 269,
        "open_tree": 428, "move_mount": 429, "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433,
        "mount_setattr": 442, "open_tree_attr": 467,
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
        "fcntl": 72, "ioctl": 16, "clone": 56, "clone3": 435, "sendto": 44, "close_range": 436,
        "truncate": 76, "ftruncate": 77, "openat2": 437, "open": 2, "openat": 257, "personality": 135,
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
        "fcntl": 25, "ioctl": 29, "clone": 220, "clone3": 435, "sendto": 206, "close_range": 436,
        "truncate": 45, "ftruncate": 46, "openat2": 437, "openat": 56, "personality": 92,
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
_FS_MAKE_SOCK = 1 << 9
_FS_TRUNCATE = 1 << 14

_PR_SET_NO_NEW_PRIVS = 38
_SECCOMP_SET_MODE_FILTER, _SECCOMP_FILTER_FLAG_TSYNC = 1, 1
_BPF_LD_W_ABS, _BPF_JEQ_K, _BPF_JGE_K, _BPF_JSET_K, _BPF_RET_K = 0x20, 0x15, 0x35, 0x45, 0x06
_EPERM, _ENOSYS = 1, 38                      # Linux errno values (the kernel's, whatever the host Python says)
_RET_ALLOW, _RET_EPERM, _RET_ENOSYS = 0x7FFF0000, 0x00050000 | _EPERM, 0x00050000 | _ENOSYS
_RET_UNLISTED = _RET_EPERM  # the answer to every call the allow list does not name, as to every call refused by name
_OFF_NR, _OFF_ARCH = 0, 4                     # struct seccomp_data, little-endian
_OFF_ARG_LOW = (16, 24, 32)                   # the low word of arguments 0, 1 and 2
_OFF_ARG4 = (48, 52)                          # the low and high words of argument 4 (sendto's address)

# The checks of the record, in its order; each is True only when the kernel refused it.
RECORD_CHECKS = ("reads_outside", "home_canary", "program_start", "new_socket", "signal_other",
                 "signal_other_ways", "metadata_change", "kernel_surface")
# The kinds of change the probe tries behind the wall, in its order (the record's field ``writes``), each on the
# write file or in the write folder that the caller made for the purpose before the wall.
WRITE_KINDS = ("open_for_write", "truncate", "create_file", "create_folder", "make_fifo", "make_symlink",
               "make_link", "make_char_device", "make_block_device", "make_socket_node", "remove_file",
               "remove_folder")
# The outcome of one kind; only the first two are refusals of this wall, and proven needs one of them for every kind.
WRITE_OUTCOMES = ("refused_by_landlock", "refused_by_filter", "refused_elsewhere", "let_through", "error", "not_tried")
_WRITE_REFUSED = WRITE_OUTCOMES[:2]
# The kinds of change the wall claims to refuse, or leaves to the host, that the probe does NOT try, each with
# the reason (the record's field ``writes_not_tried``). 'proven' covers the kinds of ``WRITE_KINDS`` only: every
# other kind is named here, so that none is covered in silence.
WRITES_NOT_TRIED = {
    "device_ioctl": "a device's own commands need a device opened: the wall lets nothing new be opened outside "
                    "the paths it was given (reads_outside), and the probe opens no device and sends no command "
                    "to one",
    "move_between_folders": "the probe tries every kind in one folder, so it moves nothing into another; a move "
                            "or a link into another folder makes a name there, which Landlock asks as the make "
                            "rights that the make kinds show refused in the probe's folder",
    "rename_within_folder": "the probe renames nothing; a rename within one folder makes the new name and "
                            "removes the old one, which Landlock asks as the make and remove rights that the make "
                            "and remove kinds show refused",
    "change_owner": "the filter refuses every call that changes a file's owner, by path and by descriptor; the "
                    "probe tries one call of the filter's list of changes to a file's attributes, a change of "
                    "mode (metadata_change), and not this one",
    "change_times": "the filter refuses every call that changes a file's times, by path and by descriptor; the "
                    "probe tries one call of the filter's list of changes to a file's attributes, a change of "
                    "mode (metadata_change), and not this one",
    "change_extended_attributes": "the filter refuses every call that sets or removes a file's extended "
                                  "attributes, by path and by descriptor; the probe tries one call of the filter's "
                                  "list of changes to a file's attributes, a change of mode (metadata_change), and "
                                  "not this one",
    "write_through_open_descriptor": "Landlock checks a file when it is opened, so a file opened for writing "
                                     "before the wall can still be written through; the probe writes through none, "
                                     "and names each such file it finds open (descriptors_open_for_write) and each "
                                     "file mapped shared and writable (memory_maps_open_for_write), which a proven "
                                     "record holds empty",
    "write_beneath_other_folder": "the probe tries every kind in one folder only: a rule that granted write "
                                  "rights beneath another folder would not show here; apply_wall adds rules with "
                                  "read and execute rights only",
}
# The kinds of object a process can hold from before a wall and still use after it, of which apply_wall makes one of
# its own before it closes every descriptor not kept; probe_wall tries one harmless act through each (the record's
# field ``held_objects``). The wall's own definition, not a setting.
HELD_KINDS = ("ring", "datagram_socket", "stream_socket", "signal_owner", "shared_map", "namespace")
# The outcome of one kind: a proven record holds only the first two, or not_tried with its reason.
HELD_OUTCOMES = ("closed_by_wall", "refused_by_filter", "acted", "error", "not_made", "not_tried")
_HELD_HELD = HELD_OUTCOMES[:2]
# The kinds the wall does not make, each with the reason (the record's field ``held_objects_not_tried``).
HELD_NOT_TRIED = {
    "shared_map": "a map is not a descriptor, so the close step cannot reach it: the record names every map held shared and "
                  "writable, of a file or of memory alone (memory_maps_open_for_write), and is never proven with one, so the wall "
                  "makes none of its own",
    "namespace": "entering a namespace changes the whole process the host runs in, so the wall enters none of its "
                 "own; in a namespace entered before the wall the filter refuses unshare, setns, clone with a "
                 "namespace flag and every mount call by name (mount, umount2, open_tree, move_mount, fsopen, "
                 "fsconfig, fsmount, fspick, mount_setattr, open_tree_attr), and every call newer than the ones it "
                 "names",
}
_RING_OFF = ("the kernel made no ring of kernel calls for this process before the wall (rings turned off), so there "
             "was none to try")

# The file the probe opens for writing and cuts to its own length: made by prepare_write_folder itself in the write
# folder, with a mode only the user holds, and reached by the probe only through a descriptor opened without
# following a link. A file name, not a figure.
WRITE_FILE_NAME = "write-me"
_WRITE_FILE_TEXT = b"the probe's own file: opened and cut to its own length, never changed\n"
# The modes prepare_write_folder gives the folder and the file: only the user holds them. Part of the wall's
# definition, not a setting (a mode that let another user in would let that user change what the probe tries).
_WRITE_FOLDER_MODE, _WRITE_FILE_MODE = 0o700, 0o600
# Shared memory of no file (an anonymous shared map, System V memory, a memfd), as the maps list names it: no file
# of a person is behind it. Names of the kernel's interface, not figures.
_ANONYMOUS_SHARED = ("/dev/zero (deleted)", "/SYSV", "/memfd:")
_READ_CHUNK = 1 << 16  # how much of a kernel list one read takes: a buffer size, which no person would choose
# The kinds of file that a descriptor open for writing makes the record not proven for (the record's field
# ``descriptors_open_for_write``): through either, a write reaches what the wall is there to keep.
_DESCRIPTOR_KINDS = {stat.S_IFREG: "regular_file", stat.S_IFBLK: "block_device"}
# The kind of every descriptor, by the file type the kernel gives it (the record's field ``descriptors_unaccounted``);
# a descriptor of no file type (an eventfd, a ring of kernel calls, a timer) is "other". Names, not figures.
_FILE_KINDS = {**_DESCRIPTOR_KINDS, stat.S_IFCHR: "character_device", stat.S_IFIFO: "pipe",
               stat.S_IFSOCK: "socket", stat.S_IFDIR: "folder", stat.S_IFLNK: "link"}
_OTHER_KIND = "other"
# The names the probe uses in the write folder: the two that prepare_write_folder makes for the removals, and the
# ones the make kinds try to create (absent before the probe; whatever a weak wall let it make stays there for the
# caller to remove). File names, not figures.
WRITE_FOLDER_NAMES = {"remove_file": "remove-me", "remove_folder": "remove-me.d", "create_file": "made-file",
                      "create_folder": "made-folder", "make_fifo": "made-fifo", "make_symlink": "made-symlink",
                      "make_link": "made-link", "make_char_device": "made-char", "make_block_device": "made-block",
                      "make_socket_node": "made-socket"}
_LANDLOCK_RIGHT = {"open_for_write": _FS_WRITE_FILE, "truncate": _FS_TRUNCATE, "create_file": _FS_MAKE_REG,
                   "create_folder": _FS_MAKE_DIR, "make_fifo": _FS_MAKE_FIFO, "make_symlink": _FS_MAKE_SYM,
                   "make_link": _FS_MAKE_REG, "make_char_device": _FS_MAKE_CHAR, "make_block_device": _FS_MAKE_BLOCK,
                   "make_socket_node": _FS_MAKE_SOCK, "remove_file": _FS_REMOVE_FILE, "remove_folder": _FS_REMOVE_DIR}

# What a person reads when a wall is not proven: ONE table of words (why_not_proven puts them together). The
# entries named after a check or a kind say what was tried; the others are whole sentences.
WORDS = {
    "allow_list_refused": "The sandbox's list of the kernel calls it lets through ({path}) cannot be used: {problem}. "
                          "Without a whole list the sandbox cannot know which calls to let through, so it changed "
                          "nothing in this process and no plugin is loaded. Give the sandbox the list shipped beside it, "
                          "or mend the row named.",
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
    "kernel_surface": "kernel calls that only this sandbox refuses (among them a call it does not know and a call "
                      "its list does not name: it refuses every call it was not written to allow, so a newer "
                      "kernel's calls stay shut until its list names them)",
    "path_is_link": "A folder given to the sandbox ({path}) is a link to another place, or has a link in it. The "
                    "sandbox would let plugins read the place the link points to, not the folder named, so the plugin "
                    "host did not shut itself in and loads nothing. What you can do: give the sandbox the real path "
                    "of the folder, with every link in it resolved.",
    "open_for_write": "opening a test file for writing (nothing was written)",
    "truncate": "cutting a test file to its own length (nothing was cut off)",
    "create_file": "making a new file in a test folder",
    "create_folder": "making a new folder in a test folder",
    "make_fifo": "making a named pipe in a test folder",
    "make_symlink": "making a shortcut (a symbolic link) in a test folder",
    "make_link": "giving a test file a second name (a hard link)",
    "make_char_device": "making a character device file in a test folder",
    "make_block_device": "making a block device file in a test folder",
    "make_socket_node": "making a socket file in a test folder",
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
    "second_thread": "The plugin host was not alone in its process when it shut itself in ({threads} threads "
                     "ran), and the sandbox holds only a process that runs alone, so nothing was loaded. What you "
                     "can do: start the plugin host as its own program, as the daemon does.",
    "threads_unknown": "The plugin host could not count the threads of its process, so it could not show that it "
                       "was alone when it shut itself in, and nothing was loaded.",
    "descriptor_open": "A {kind} was held open for writing when the plugin host shut itself in. The sandbox checks "
                       "a file when it is opened, so it does not refuse writes through one opened before: the host "
                       "must close every such file before it shuts itself in.",
    "descriptors_unknown": "The plugin host could not see every file it held open, so it could not show that none "
                           "was open for writing.",
    "map_open": "A file or a piece of memory was mapped shared and writable when the plugin host shut itself in. "
                "The sandbox checks a file when it is opened, so writes through such a map still reach the file, "
                "and memory shared with another process lets that process act for the host outside the sandbox: "
                "the host must unmap every such map before it shuts itself in.",
    "child_process": "The plugin host had started another process before it shut itself in ({children} still "
                     "running). The sandbox holds the process that shuts itself in and what it starts afterwards, "
                     "never a process that already runs, which could act for the host outside the sandbox, so nothing "
                     "was loaded. What you can do: start the plugin host as its own program, as the daemon does, and "
                     "let it start no other process before it shuts itself in.",
    "children_unknown": "The plugin host could not see whether a process it had started before was still running when "
                        "it shut itself in, so it could not show that the sandbox holds everything that acts for it, "
                        "and nothing was loaded.",
    "maps_unknown": "The plugin host could not read the list of its memory maps, so it could not show that no file "
                    "was mapped for writing.",
    "descriptor_after_wall": "A {kind} reached the plugin host after it shut itself in and before it tested "
                             "itself (handed in over a connection it held). The sandbox checks a file only when it "
                             "is opened, and this one was opened outside it, so the test cannot show what it "
                             "reaches. What you can do: let the host receive nothing between shutting itself in "
                             "and testing itself.",
    "no_wall_here": "The plugin host did not shut itself in within this process, so this test shows nothing of a "
                    "sandbox of its own, and nothing was loaded. What you can do: start the plugin host as its own "
                    "program, as the daemon does, so that it shuts itself in before it tests itself.",
    "keep_not_named": "The plugin host did not say which of its open files and connections to keep when it shut "
                      "itself in, so the sandbox closed none of them, and one opened before could still act after "
                      "it. What you can do: start the plugin host as the daemon does, keeping its channel only.",
    "descriptor_not_closed": "A {kind} was still open after the plugin host shut itself in, though the host had not "
                             "kept it, so something opened before could still act after it.",
    "held_acted": "Something opened before the plugin host shut itself in could still act after it: {what}. The "
                  "sandbox is not shown to hold while it can.",
    "held_not_made": "Not tested, because the sandbox could not make one of its own before it shut itself in: {what}.",
    "keep_not_open": "A descriptor given to the sandbox to keep ({descriptor}) is not an open descriptor. What you can "
                     "do: keep only the plugin host's channel, by its descriptor number.",
    "keep_signal_owner": "A descriptor given to the sandbox to keep ({descriptor}) has a receiver of its signals set "
                         "(an owner, or signals on every change), so a signal could reach a process outside the sandbox. What you can "
                         "do: keep only the channel, with no receiver of its signals set.",
    "held_ring": "a queue of kernel calls opened before",
    "held_datagram_socket": "a connection that sends to an address of its choosing (a datagram socket)",
    "held_stream_socket": "a connection connected before (a stream socket)",
    "held_signal_owner": "a pipe set before to signal a process on every change",
    "held_shared_map": "a file mapped shared and writable",
    "held_namespace": "a namespace entered before",
    "regular_file": "file",
    "block_device": "disk or other block device",
    "character_device": "terminal or other character device",
    "pipe": "pipe",
    "socket": "network or local connection",
    "folder": "folder",
    "link": "link",
    "other": "kernel object that is not a file (a queue of kernel calls, a timer, an event counter)",
    "what_to_do": "What you can do: run the plugin host on Linux 5.13 or newer with Landlock turned on (most "
                  "distributions turn it on; otherwise add landlock to the kernel's lsm= boot setting), outside "
                  "any container or security tool that changes what a process may do with files, and start it "
                  "again. If it is still refused, keep this message: it says what got through.",
}

# How far apply_wall has gone in THIS process (a forked child inherits it): the steps that changed the
# process, the step that failed, and the Landlock scopes in force. A process with a failed step, or with any
# step, is never walled again.
_STATE: dict = {"applied": [], "failed": None, "scopes": [], "landlock_handled": 0, "filter_truncate": False}
# The folder and the file that prepare_write_folder made in THIS process, each as (device, inode): the probe
# refuses a folder or a file that is not the one it made.
_MADE: dict = {}
# The objects apply_wall made of each kind of HELD_KINDS before its close step, in THIS process: per kind the
# descriptor numbers and their identities at the making (or the reason a kind was not made).
_PLANTED: dict = {}


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


def _threads_now() -> int:
    """The threads of this process, counted in a way the wall does not refuse: the kernel gives the task folder
    two links plus one per thread (a stat, which Landlock does not mediate), and ``threading`` knows its own.
    0 when they cannot be counted (fail shut: never proven, never walled)."""
    try:
        tasks = os.stat("/proc/self/task").st_nlink - 2
    except OSError:
        return 0
    return max(tasks, threading.active_count()) if tasks >= 1 else 0


def _stat_state(pid) -> tuple:
    """(state, parent pid) of a process, from the kernel's ``/proc/<pid>/stat`` (its name may hold spaces)."""
    with open(f"/proc/{pid}/stat", "rb") as handle:
        fields = handle.read().rpartition(b")")[2].split()
    return fields[0].decode("ascii", "replace"), int(fields[1])


def _children() -> int:
    """The live child processes of this process (a zombie, which can no longer act, is not counted), from the kernel's
    own list of each task's children; where the kernel keeps no such list, from the parent of every process.
    ``wall_children_running`` with the words of ``children_unknown`` where neither can be read (fail shut)."""
    me, pids = os.getpid(), set()
    try:
        try:
            for task in os.listdir("/proc/self/task"):
                with open(f"/proc/self/task/{task}/children", "rb") as handle:
                    pids.update(int(pid) for pid in handle.read().split())
        except FileNotFoundError:  # a kernel without the children list
            for name in os.listdir("/proc"):
                if name.isdigit():
                    try:
                        if _stat_state(name)[1] == me:
                            pids.add(int(name))
                    except (OSError, ValueError, IndexError):
                        continue
    except (OSError, ValueError) as exc:
        raise WallRefused("wall_children_running", WORDS["children_unknown"]) from exc
    live = 0
    for pid in pids:
        try:
            live += _stat_state(pid)[0] != "Z"
        except (OSError, ValueError, IndexError):
            continue  # ended since: it can no longer act
    return live


def _alone(step: str, count) -> None:
    """``wall_partial`` naming ``step`` unless the process has one thread now: a thread here would sit outside
    Landlock, which limits only the calling thread."""
    threads = count()
    if threads != 1:
        _STATE["failed"] = step
        words = WORDS["second_thread"].format(threads=threads) if threads else WORDS["threads_unknown"]
        raise WallRefused("wall_partial", f"{words} The wall is not whole; applied before this step: "
                          f"{', '.join(_STATE['applied']) or 'nothing'}", step=step)


def _newest_call(arch: str) -> int:
    """The highest call number the wall's own tables name for ``arch``: read from the tables, never typed."""
    return max(dict(_NR[arch], **_NR_MORE[arch]).values())


def _required_check(call: str):
    """The check the filter holds for ``call``'s arguments (None where it is allowed whole): the wall's definition,
    which a row of the allow list must repeat and can never drop or change."""
    if call in CALLER_ONLY_CALLS:
        return "caller_only"
    if call in CALLER_ONLY_PRIORITY_CALLS:
        return "priority"
    if call in ("fcntl", "ioctl", "clone", "sendto"):
        return call
    if call in OLD_LANDLOCK_DENIED_CALLS or call in ("open", "openat"):
        return "landlock_truncate"
    return None


def _refused_by_name() -> set:
    """Every call the filter refuses by name, which no row of the allow list may name."""
    return set(DENIED_CALLS + DENIED_METADATA_CALLS + DENIED_SIGNAL_CALLS + DENIED_IPC_CALLS) | {"clone3"}


def _read_allow_list(path: str) -> tuple:
    """The rows of the allow list at ``path``, each checked; ``wall_allow_list_refused`` naming the first fault."""
    def refuse(problem):
        raise WallRefused("wall_allow_list_refused", WORDS["allow_list_refused"].format(path=path, problem=problem))
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        refuse(f"it cannot be read ({errno.errorcode.get(exc.errno or 0, exc.errno)})")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        refuse("it is not JSON text")
    if not isinstance(data, dict) or data.get("format") != _ALLOW_FORMAT or not isinstance(data.get("rows"), list) \
            or not data["rows"]:
        refuse(f"it is not a list of the wall's form (an object with format {_ALLOW_FORMAT!r} and a list of rows)")
    refused = _refused_by_name()
    taken: dict = {arch: {} for arch in _NR}
    by_name = {arch: dict(_NR[arch], **_NR_MORE[arch]) for arch in _NR}
    names = set()
    for index, row in enumerate(data["rows"], 1):
        where = f"row {index}"
        if not isinstance(row, dict) or set(row) - set(_ALLOW_ROW_KEYS):
            refuse(f"{where} is not a row of the wall's form (its keys: {', '.join(_ALLOW_ROW_KEYS)})")
        call = row.get("call")
        if not isinstance(call, str) or not call.isidentifier():
            refuse(f"{where} names no call")
        where = f"row {index} ({call})"
        reason = row.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            refuse(f"{where} carries no reason")
        recorded, beyond = row.get("recorded", []), row.get("beyond_recording")
        if not isinstance(recorded, list) or not all(isinstance(tag, str) and tag for tag in recorded):
            refuse(f"{where} names its recordings in a form the wall does not read")
        if not recorded and (not isinstance(beyond, str) or not beyond.strip()):
            refuse(f"{where} was not seen in the recording and carries no written reason to be on the list all the same")
        if call in names:
            refuse(f"{where} names a call a row above names already")
        if call in refused:
            refuse(f"{where} names a call the wall refuses by name, which no row can allow")
        if row.get("check") != _required_check(call):
            refuse(f"{where} must carry the check {_required_check(call)!r} that the wall holds for it, and carries "
                   f"{row.get('check')!r}")
        named = False
        for arch in _NR:
            if arch not in row:
                refuse(f"{where} gives no number for {arch} (null where the arch has no such call)")
            number, top = row[arch], _newest_call(arch)
            if number is None:
                continue
            if not isinstance(number, int) or isinstance(number, bool) or not 0 <= number <= top:
                refuse(f"{where} gives {arch} the number {number!r}, which the wall's tables do not reach (0 to {top}: "
                       "a call above them stays refused until the wall names it)")
            if number in {by_name[arch][name] for name in refused if name in by_name[arch]}:
                refuse(f"{where} gives {arch} the number of a call the wall refuses by name")
            if call in by_name[arch] and by_name[arch][call] != number:
                refuse(f"{where} gives {arch} the number {number}, and the wall's own table gives {by_name[arch][call]}")
            if number in taken[arch]:
                refuse(f"{where} gives {arch} the number of {taken[arch][number]}")
            taken[arch][number], named = call, True
        if not named:
            refuse(f"{where} gives no number on any arch")
        names.add(call)
    return tuple(data["rows"])


def load_allow_list(path=None) -> dict:
    """The allow list in force: ``rows`` (each checked), ``file`` (the path read), ``shipped`` (True when its rows are
    the rows of the list shipped beside the wall), ``added`` and ``removed`` (the calls whose rows differ from the
    shipped list's). ``path`` None reads the shipped list (``ALLOW_LIST_FILE``). The shipped list is read in every
    case, to name what differs. ``wall_allow_list_refused`` where either cannot be used, naming the fault."""
    shipped = _read_allow_list(ALLOW_LIST_FILE)
    file = ALLOW_LIST_FILE if path is None else os.fspath(path)
    rows = shipped if path is None else _read_allow_list(file)

    def key(row):
        return (row["call"], row["x86_64"], row["aarch64"], row.get("check"))
    ours, theirs = {key(row) for row in shipped}, {key(row) for row in rows}
    added = sorted({row["call"] for row in rows if key(row) not in ours})
    removed = sorted({row["call"] for row in shipped if key(row) not in theirs})
    return {"file": file, "rows": rows, "shipped": not added and not removed, "added": added, "removed": removed}


def _filter(arch: str, landlock_abi: int = _MIN_LANDLOCK_ABI, allow_rows=None) -> list[tuple[int, int, int, int]]:
    """The one seccomp program, as (code, jt, jf, k) rows. ``landlock_abi`` below 3 adds the truncation rules.
    ``allow_rows`` are the rows of the allow list (None: the shipped list). No pid number appears in it: the same
    program for every process."""
    numbers = dict(_NR[arch], **_NR_MORE[arch])
    if allow_rows is None:
        allow_rows = load_allow_list()["rows"]
    listed = {row["call"]: row for row in allow_rows if row.get(arch) is not None}
    rows: list[tuple] = [(_BPF_LD_W_ABS, 0, 0, _OFF_ARCH), (_BPF_JEQ_K, 1, 0, _AUDIT_ARCH[arch]),
                         (_BPF_RET_K, 0, 0, _RET_EPERM), (_BPF_LD_W_ABS, 0, 0, _OFF_NR)]
    if arch == "x86_64":
        rows.append((_BPF_JGE_K, "deny", 0, _X32_SYSCALL_BIT))
    rows.append((_BPF_JGE_K, "deny", 0, _newest_call(arch) + 1))  # a call the tables do not know: refused by default
    rows += [(_BPF_JEQ_K, "deny", 0, numbers[name]) for name in DENIED_CALLS]
    rows += [(_BPF_JEQ_K, "deny", 0, numbers[name]) for name in DENIED_METADATA_CALLS if name in numbers]
    rows += [(_BPF_JEQ_K, "deny", 0, numbers[name]) for name in DENIED_SIGNAL_CALLS + DENIED_IPC_CALLS]
    if landlock_abi < _TRUNCATE_ABI:
        rows += [(_BPF_JEQ_K, "deny", 0, numbers[name]) for name in OLD_LANDLOCK_DENIED_CALLS]
        rows += [(_BPF_JEQ_K, label, 0, numbers[name])
                 for name, label in (("open", "trunc1"), ("openat", "trunc2")) if name in numbers and name in listed]
    # a call allowed only in part is let through to its check only where the allow list names it
    rows += [(_BPF_JEQ_K, "caller", 0, numbers[name]) for name in CALLER_ONLY_CALLS if name in listed]
    rows += [(_BPF_JEQ_K, label, 0, numbers[name]) for name, label in
             (("setpriority", "prio"), ("ioprio_set", "ioprio"), ("fcntl", "fcntl"), ("ioctl", "ioctl"),
              ("clone", "clone"), ("sendto", "sendto")) if name in listed]
    rows.append((_BPF_JEQ_K, "enosys", 0, numbers["clone3"]))
    rows.append((_BPF_JGE_K, "listed", "listed", 0))  # always taken: on to the allow list, past the checks below
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
          *[(_BPF_JEQ_K, "deny", 0, value) for value in DENIED_FCNTL_COMMANDS.values()],
          (_BPF_JEQ_K, "setfl", 0, _F_SETFL), (_BPF_RET_K, 0, 0, _RET_ALLOW))
    block("setfl", (_BPF_LD_W_ABS, 0, 0, _OFF_ARG_LOW[2]), (_BPF_JSET_K, "deny", "allow", _O_ASYNC))
    # sendto with no address (send on a connected socket, as the C library's send is made) stays allowed; with one,
    # a datagram socket held from before the wall would reach an address of its choosing
    block("sendto", (_BPF_LD_W_ABS, 0, 0, _OFF_ARG4[0]), (_BPF_JEQ_K, 0, "deny", 0),
          (_BPF_LD_W_ABS, 0, 0, _OFF_ARG4[1]), (_BPF_JEQ_K, "allow", "deny", 0))
    block("ioctl", (_BPF_LD_W_ABS, 0, 0, _OFF_ARG_LOW[1]),
          *[(_BPF_JEQ_K, "deny", 0, value) for value in DENIED_IOCTL_COMMANDS.values()],
          *[(_BPF_JEQ_K, "deny", 0, value) for value in DENIED_TERMINAL_COMMANDS.values()],
          *[(_BPF_JEQ_K, "allow", 0, value) for value in ALLOWED_TERMINAL_COMMANDS.values()],
          (_BPF_JGE_K, "allow", 0, _TERMINAL_RANGE[1]), (_BPF_JGE_K, "deny", "allow", _TERMINAL_RANGE[0]))
    block("clone", (_BPF_LD_W_ABS, 0, 0, _OFF_ARG_LOW[0]), (_BPF_JSET_K, "deny", "allow", NAMESPACE_CLONE_FLAGS))
    block("trunc1", (_BPF_LD_W_ABS, 0, 0, _OFF_ARG_LOW[1]), (_BPF_JSET_K, "deny", "allow", _O_TRUNC))
    block("trunc2", (_BPF_LD_W_ABS, 0, 0, _OFF_ARG_LOW[2]), (_BPF_JSET_K, "deny", "allow", _O_TRUNC))
    block("enosys", (_BPF_RET_K, 0, 0, _RET_ENOSYS))
    block("allow", (_BPF_RET_K, 0, 0, _RET_ALLOW))
    block("deny", (_BPF_RET_K, 0, 0, _RET_EPERM))
    # the allow list: each call it names in full (or whose only check is the truncation rule above) is let through;
    # runs of comparisons, each ending in its own ALLOW, so that every jump stays within reach
    plain = [row[arch] for row in listed.values() if row.get("check") in (None, "landlock_truncate")]
    labels["listed"] = len(rows)
    for run in range(0, len(plain), _ALLOW_CHUNK):
        label = f"listed_{run}"
        rows.extend((_BPF_JEQ_K, label, 0, number) for number in plain[run:run + _ALLOW_CHUNK])
        rows.append((_BPF_JGE_K, 1, 1, 0))  # always taken: past this run's ALLOW, on to the next run
        labels[label] = len(rows)
        rows.append((_BPF_RET_K, 0, 0, _RET_ALLOW))
    rows.append((_BPF_RET_K, 0, 0, _RET_UNLISTED))  # every call the allow list does not name
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


def _load_seccomp_filter(libc, arch: str, landlock_abi: int = _MIN_LANDLOCK_ABI, allow_rows=None) -> None:
    rows = _filter(arch, landlock_abi, allow_rows)
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


def _open_through_no_link(entry) -> int:
    """An O_PATH descriptor of ``entry`` opened through no link in any part of it (openat2 with RESOLVE_NO_SYMLINKS,
    which every kernel with Landlock has); ``wall_path_is_link`` where a part is a link."""
    path = os.fspath(entry)
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError(f"a wall path must be an absolute path: {path!r}")
    how = ctypes.create_string_buffer(struct.pack("<QQQ", os.O_PATH | os.O_CLOEXEC, 0, _RESOLVE_NO_SYMLINKS), 24)
    name = ctypes.create_string_buffer(os.fsencode(path))
    libc = _libc()  # the C library directly: an open of a path only, which changes nothing
    ctypes.set_errno(0)
    fd = libc.syscall(ctypes.c_long(_NR_MORE[_arch()]["openat2"]), ctypes.c_long(-100), ctypes.c_long(ctypes.addressof(name)),
                      ctypes.c_long(ctypes.addressof(how)), ctypes.c_long(24))
    err = ctypes.get_errno()
    if fd < 0:
        if err == errno.ELOOP:
            raise WallRefused("wall_path_is_link", WORDS["path_is_link"].format(path=path))
        raise OSError(err, f"{path}: {os.strerror(err)}")
    return fd


def _open_paths(read_only_paths) -> list[tuple[int, int]]:
    """(fd, allowed rights) for each path, all opened before any change; nothing is left open on a raise."""
    opened: list[tuple[int, int]] = []
    try:
        for entry in read_only_paths:
            fd = _open_through_no_link(entry)
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
        _alone("threads_before_landlock", _thread_count)
        _step("landlock_restrict_self", lambda: _kernel("landlock_restrict_self", *_call(
            libc, _SYS_LANDLOCK_RESTRICT_SELF, ruleset, 0)))
        _STATE["scopes"] = scopes
        _STATE["landlock_handled"] = struct.unpack_from("<Q", data)[0]  # the rights the ruleset in force handles
    finally:
        os.close(ruleset)


def apply_wall(read_only_paths, keep_descriptors=None, allow_list=None) -> None:
    """Wall this process in: Landlock (read and map only ``read_only_paths``), then the one seccomp filter.

    ``read_only_paths`` is read whole into a list before the threads are counted, so that reading it cannot
    start a thread the count does not see. Refuses, before any change: ``wall_system_unsupported`` (not Linux;
    nothing is tried), ``wall_arch_unsupported`` (not x86_64 or aarch64), ``wall_threads_running`` (more than
    one thread), ``wall_children_running`` (a child process alive), ``landlock_unavailable`` (no Landlock, or an ABI below 1). A bad path raises ``ValueError`` or
    ``OSError`` before any change as well, and a path that is a link, or has a link in any part of it, is refused
    ``wall_path_is_link`` (the wall would grant the place the link points to, not the one named). A kernel failure at any step, more than one thread immediately before
    ``landlock_restrict_self`` (``threads_before_landlock``) or after the filter (``threads_after_filter``), and a
    second call in a process walled already in whole or in part, is refused ``wall_partial`` with ``step`` set.

    ``keep_descriptors`` names the descriptors the process keeps through the wall (its channel; empty keeps none):
    each must be open with no receiver of its signals set, else ``wall_keep_refused`` before any change. Before the
    first change of the wall every other descriptor is closed (step ``close_descriptors``), after one object of each
    kind of ``HELD_KINDS`` is made for the probe to try. None (left out) closes nothing, and the record is never proven.

    ``allow_list`` is the path of the allow list the filter lets through (None: the list shipped beside the wall,
    ``ALLOW_LIST_FILE``); it is read before any change, and a list that cannot be used is refused
    ``wall_allow_list_refused`` with the words of ``allow_list_refused``. A list other than the shipped one is named in
    the record by what it adds and removes.
    """
    libc = _libc()
    arch = _arch()
    if _STATE["failed"] or _STATE["applied"]:
        done = ", ".join(_STATE["applied"]) or "nothing"
        failed = f"; the step {_STATE['failed']} failed" if _STATE["failed"] else ""
        raise WallRefused("wall_partial", f"apply_wall has run in this process already (applied: {done}{failed}); "
                          "a wall is applied once, and a process walled in part must load nothing",
                          step=_STATE["failed"] or "already_walled")
    paths = list(read_only_paths)  # the whole argument, read before the threads are counted
    threads = _thread_count()
    if threads != 1:
        raise WallRefused("wall_threads_running", WORDS["second_thread"].format(threads=threads))
    children = _children()  # one thread: nothing but this call could start one before the wall
    if children:
        raise WallRefused("wall_children_running", WORDS["child_process"].format(children=children))
    abi = _landlock_abi(libc)
    if abi < _MIN_LANDLOCK_ABI:
        raise WallRefused("landlock_unavailable", "this kernel gives this process no Landlock (ABI 1, Linux 5.13, at least)")
    allow = load_allow_list(allow_list)  # refused before any change
    kept = None if keep_descriptors is None else _kept(keep_descriptors)  # refused before any change
    for entry in paths:  # a link in a path is refused before any change (and again where the path is opened)
        os.close(_open_through_no_link(entry))
    _plant_held()
    if kept is not None:
        _step("close_descriptors", lambda: _close_all_but(kept))
    _STATE["kept"] = kept
    lists = _open_kernel_lists()  # for reading, before the wall; both are closed before this call returns
    try:
        opened = _open_paths(paths)
        try:
            _apply_landlock(libc, abi, opened)
        finally:
            for fd, _ in opened:
                os.close(fd)
        _load_seccomp_filter(libc, arch, abi, allow["rows"])
        _STATE["allow_list"] = {key: allow[key] for key in ("file", "shipped", "added", "removed")}
        _STATE["allow_list"]["rows"] = len(allow["rows"])
        _alone("threads_after_filter", _threads_now)
        # what the kernel's own lists say after the wall (None for a list that could not be read: never proven)
        _STATE["seen_at_wall"] = _read_kernel_lists(lists)
    finally:
        for fd in lists.values():
            if fd is not None:
                os.close(fd)


def _identity(number: int):
    """What tells the object of descriptor ``number`` apart from any other: (device, inode, file type), as the kernel
    gives them (two sockets, two pipes differ by inode); None when the number is not open."""
    try:
        found = os.fstat(number)
    except OSError:
        return None
    return [found.st_dev, found.st_ino, stat.S_IFMT(found.st_mode)]


def _kept(keep_descriptors) -> list:
    """The descriptors the caller keeps through the wall, sorted, each open and with no receiver of its signals set (no
    owner, no O_ASYNC); else ``wall_keep_refused``, before any change."""
    import fcntl
    kept = []
    for number in list(keep_descriptors):
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise WallRefused("wall_keep_refused", WORDS["keep_not_open"].format(descriptor=repr(number)[:32]))
        try:
            flags = fcntl.fcntl(number, fcntl.F_GETFL)
            owner = struct.unpack("<ii", fcntl.fcntl(number, _F_GETOWN_EX, bytes(8)))[1]
        except OSError:
            raise WallRefused("wall_keep_refused", WORDS["keep_not_open"].format(descriptor=number)) from None
        if owner != 0 or flags & _O_ASYNC:
            raise WallRefused("wall_keep_refused", WORDS["keep_signal_owner"].format(descriptor=number))
        kept.append(number)
    return sorted(set(kept))


def _plant_held() -> None:
    """One object of the wall's own of each kind of ``HELD_KINDS`` that it can make, before the close step: a ring
    with one entry; an unconnected datagram socket and a receiver bound to an address the kernel picks (no file);
    a connected stream socket pair; a pipe whose read end signals this process itself with SIGURG (ignored unless
    asked for) on every byte. A kind that cannot be made is passed over (not_made); a ring the kernel does not make
    for this process is named with its reason."""
    import fcntl
    _PLANTED.clear()
    libc = _libc()

    def made(kind, numbers, **extra):
        _PLANTED[kind] = dict(extra, numbers=list(numbers), ids=[_identity(n) for n in numbers])
    params = ctypes.create_string_buffer(_RING_PARAMS)
    ring, err = _call(libc, _NR[_arch()]["io_uring_setup"], 1, ctypes.addressof(params))
    if ring >= 0:
        made("ring", [ring])
    elif err in (errno.EPERM, errno.ENOSYS):
        _PLANTED["ring"] = {"not_tried": _RING_OFF}
    try:
        receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        receiver.bind("")  # an address the kernel picks, in no folder
        address = receiver.getsockname()
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        made("datagram_socket", [sender.detach(), receiver.detach()],
             address=address if isinstance(address, bytes) else address.encode())
    except OSError:
        pass
    try:
        one, other = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        made("stream_socket", [one.detach(), other.detach()])
    except OSError:
        pass
    try:
        read_end, write_end = os.pipe()
        fcntl.fcntl(read_end, fcntl.F_SETOWN, os.getpid())
        fcntl.fcntl(read_end, DENIED_FCNTL_COMMANDS["F_SETSIG"], _SIGURG)
        fcntl.fcntl(read_end, fcntl.F_SETFL, fcntl.fcntl(read_end, fcntl.F_GETFL) | _O_ASYNC)
        made("signal_owner", [write_end, read_end])
    except OSError:
        pass


def _close_all_but(kept) -> None:
    """Close every descriptor of this process but ``kept`` (sorted): close_range over each gap between the kept numbers
    and above the last (where the kernel has no close_range, each number its list names); then each of 0, 1 and 2 that
    is not kept points at /dev/null, so that no file opened later takes a standard number."""
    libc, arch = _libc(), _arch()
    number = _NR_MORE[arch]["close_range"]
    edges = [-1] + list(kept)
    gaps = [(low + 1, high - 1) for low, high in zip(edges, edges[1:]) if high - low > 1]
    gaps.append((edges[-1] + 1, _CLOSE_RANGE_TOP))
    result, err = _call(libc, number, gaps[0][0], gaps[0][1], 0)
    if result < 0 and err == errno.ENOSYS:
        for name in os.listdir("/proc/self/fd"):
            if int(name) not in kept:
                try:
                    os.close(int(name))
                except OSError:
                    pass
    else:
        _kernel("close_range", result, err)
        for low, high in gaps[1:]:
            _kernel("close_range", *_call(libc, number, low, high, 0))
    standard = [n for n in range(3) if n not in kept]
    if standard:
        null = os.open("/dev/null", os.O_RDWR)
        for n in standard:
            if n != null:
                os.dup2(null, n)
        if null not in standard:
            os.close(null)


def _open_kernel_lists() -> dict:
    """The kernel's lists of this process's descriptors and maps, opened for reading (None where one cannot be)."""
    lists = {}
    for key, path, flags in (("descriptors", "/proc/self/fd", os.O_DIRECTORY), ("maps", "/proc/self/maps", 0)):
        try:
            lists[key] = os.open(path, os.O_RDONLY | os.O_CLOEXEC | flags)
        except OSError:
            lists[key] = None
    return lists


def _read_kernel_lists(lists: dict) -> dict:
    """What the two lists say now, read through the descriptors opened before the wall: the descriptors open for
    writing (named as ``_named_for_write`` names them) and the maps shared and writable of a file; None for a list
    that could not be read. The two descriptors of the lists themselves are not counted."""
    seen = {"descriptors": None, "maps": None, "numbers": None}
    if lists.get("descriptors") is not None:
        try:
            numbers = sorted(int(name) for name in os.listdir(lists["descriptors"]))
        except (OSError, ValueError):
            numbers = None
        if numbers is not None:
            ours = {fd for fd in lists.values() if fd is not None}
            seen["numbers"] = [number for number in numbers if number not in ours]
            seen["identities"] = {number: _identity(number) for number in seen["numbers"]}
            seen["descriptors"] = _named_for_write(seen["numbers"])
    if lists.get("maps") is not None:
        try:
            os.lseek(lists["maps"], 0, os.SEEK_SET)
            chunks = []
            while True:
                chunk = os.read(lists["maps"], _READ_CHUNK)
                if not chunk:
                    break
                chunks.append(chunk)
            seen["maps"] = _maps_shared_for_write(b"".join(chunks))
        except OSError:
            seen["maps"] = None
    return seen


def _maps_shared_for_write(text: bytes) -> list:
    """Each map that is shared and writable, of a file or of memory alone (an anonymous shared map, System V
    memory, a memfd), as ``{"map": n}`` (its line in the list, from 0): through it a write still reaches the file, or a
    process that shares the memory (one forked before the wall) acts on what the walled one writes there. Never its
    name, address or contents."""
    found = []
    for number, line in enumerate(text.decode("ascii", "replace").splitlines()):
        fields = line.split()
        if len(fields) >= 5 and len(fields[1]) == 4 and fields[1][1] == "w" and fields[1][3] == "s":
            found.append({"map": number})
    return found


def _maps_open_for_write():
    """The maps of this process shared and writable of a file, read now; None when the list cannot be read (a walled
    process cannot open it: after apply_wall the probe takes what apply_wall read instead)."""
    try:
        with open("/proc/self/maps", "rb") as handle:
            return _maps_shared_for_write(handle.read())
    except OSError:
        return None


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
    def on_a_pipe(command: int, argument: int):
        def act() -> None:
            read_end, write_end = os.pipe()
            try:
                _sys(libc, "fcntl", read_end, command, argument)
            finally:
                os.close(read_end)
                os.close(write_end)
        return act
    def async_by_ioctl() -> None:  # signals on every change, by the ioctl form, to no owner set here
        read_end, write_end = os.pipe()
        on = ctypes.c_int(1)
        try:
            _sys(libc, "ioctl", read_end, DENIED_IOCTL_COMMANDS["FIOASYNC"], ctypes.addressof(on))
        finally:
            os.close(read_end)
            os.close(write_end)
    _each_refused(lambda: _sys(libc, "rt_sigqueueinfo", target, 0, ctypes.addressof(info)),
                  owner_of_a_pipe,
                  on_a_pipe(DENIED_FCNTL_COMMANDS["F_SETSIG"], 0),        # the signal an owner gets: 0 is the default
                  on_a_pipe(DENIED_FCNTL_COMMANDS["F_NOTIFY"], 0),        # a pipe is no folder: ENOTDIR if allowed
                  on_a_pipe(_F_SETFL, _O_ASYNC),                          # signals on change, to no owner set here
                  async_by_ioctl,
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


def _open_tree_of_root(libc) -> None:
    """open_tree of the root folder with no flag (no clone, no mount): where it is allowed it gives a descriptor of a
    path only, closed at once; it needs no privilege, so a refusal is the filter's."""
    root = ctypes.create_string_buffer(b"/")
    os.close(_sys(libc, "open_tree", -100, ctypes.addressof(root), os.O_CLOEXEC))


def _kernel_surface() -> None:
    """Calls only this wall refuses, each harmless where it is allowed: io_uring_setup with no entries, the use of
    a ring and an addressed send on a descriptor that names nothing,
    unshare of nothing, a keyctl operation that does not exist, (x86_64) getpid by its x32 number, clone
    asking a new user namespace with flags the kernel itself rejects, msgctl IPC_STAT of the id -1,
    mq_getsetattr of the descriptor -1, signal 0 to this process itself by its own pid, and a lease on a
    pipe, open_tree of the root folder (the newer mount calls), a call number the kernel will never give a call
    (the filter refuses every number above the newest one its tables name), and personality asked only to read
    (a call the kernel has and the allow list does not name); then clone3, which this wall answers ENOSYS (the kernel's own answer to these arguments is EINVAL)."""
    libc, arch = _libc(), _arch()
    address = ctypes.create_string_buffer(16)  # a destination address of zeros, never sent: the descriptor is -1
    attempts = [lambda: _sys(libc, "io_uring_setup", 0, 0),
                lambda: _sys(libc, "io_uring_enter", -1, 0, 0, 0, 0, 0),     # no ring: EBADF if allowed
                lambda: _sys(libc, "io_uring_register", -1, 0, 0, 0),        # no ring: EBADF if allowed
                lambda: _sys(libc, "sendto", -1, 0, 0, 0, ctypes.addressof(address), 16),  # EBADF if allowed
                lambda: _sys(libc, "sendmsg", -1, 0, 0),                     # EBADF if allowed
                lambda: _sys(libc, "unshare", 0),
                lambda: _sys(libc, "keyctl", 0x7FFFFFFF, 0, 0, 0, 0),
                lambda: _sys(libc, "clone", 0x10000000 | 0x00000200, 0, 0, 0, 0),  # CLONE_NEWUSER|CLONE_FS: EINVAL
                lambda: _sys(libc, "msgctl", -1, 2, 0),                             # IPC_STAT of no queue: EINVAL
                lambda: _sys(libc, "mq_getsetattr", -1, 0, 0),                      # no descriptor: EBADF
                lambda: _sys(libc, "kill", os.getpid(), 0),                         # signal 0 to itself: ok
                lambda: _lease_on_a_pipe(libc),
                lambda: _open_tree_of_root(libc),                                   # a descriptor of / if allowed
                lambda: _kernel("a call the wall does not know", *_call(libc, _UNKNOWN_CALL)),  # ENOSYS if allowed
                lambda: _sys(libc, "personality", 0xFFFFFFFF)]  # off the allow list: reads the persona if allowed
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
    """Make, in ``write_folder`` (an EMPTY folder of this user that the caller made for the probe; it is opened
    without following a link and its mode is set so that only the user holds it), the file and the folder that the
    probe's removals try to remove (``WRITE_FOLDER_NAMES``) and the probe's own file (``WRITE_FILE_NAME``), which the
    probe opens for writing and cuts to its own length. The caller calls it BEFORE ``apply_wall``, and afterwards
    removes the whole folder with whatever a weak wall let the probe make in it."""
    folder = os.fspath(write_folder)
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        found = os.fstat(fd)
        if found.st_uid != os.geteuid():
            raise ValueError("the write folder must be this user's own: a refusal in another user's folder would "
                             "prove nothing")
        if os.listdir(fd):
            raise ValueError("the write folder must be empty: a name already there would make a refusal prove nothing")
        os.fchmod(fd, _WRITE_FOLDER_MODE)  # only the user may change what is in it from now on
        os.close(os.open(WRITE_FOLDER_NAMES["remove_file"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_CLOEXEC, _WRITE_FILE_MODE, dir_fd=fd))
        os.mkdir(WRITE_FOLDER_NAMES["remove_folder"], _WRITE_FOLDER_MODE, dir_fd=fd)
        made = os.open(WRITE_FILE_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                       _WRITE_FILE_MODE, dir_fd=fd)
        try:
            os.write(made, _WRITE_FILE_TEXT)
            mine = os.fstat(made)
        finally:
            os.close(made)
        _MADE.clear()
        _MADE.update(folder=(found.st_dev, found.st_ino), file=(mine.st_dev, mine.st_ino))
    finally:
        os.close(fd)


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


def _own_file_fd(folder_fd: int):
    """An O_PATH descriptor of the probe's own file in the write folder, opened without following a link, or None
    when the name is not the file prepare_write_folder made: a regular file of this user with one name only and the
    mode it was given (and, where it was made in this process, the same file). Everything the probe does to the file
    goes through this descriptor, so a name changed after this check changes nothing the probe does."""
    try:
        fd = os.open(WRITE_FILE_NAME, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=folder_fd)
    except OSError:
        return None
    try:
        found = os.fstat(fd)
        usable = (stat.S_ISREG(found.st_mode) and found.st_uid == os.geteuid() and found.st_nlink == 1
                  and stat.S_IMODE(found.st_mode) == _WRITE_FILE_MODE
                  and _MADE.get("file", (found.st_dev, found.st_ino)) == (found.st_dev, found.st_ino))
    except OSError:
        usable = False
    if not usable:
        os.close(fd)
        return None
    return fd


def _through(fd: int) -> str:
    """The name the kernel gives the file of descriptor ``fd`` itself (a link that does not follow any other name)."""
    return f"/proc/self/fd/{fd}"


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
                  and stat.S_IMODE(found.st_mode) == _WRITE_FOLDER_MODE
                  and _MADE.get("folder", (found.st_dev, found.st_ino)) == (found.st_dev, found.st_ino)
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
    the probe's own file (``WRITE_FILE_NAME`` in the folder, made by prepare_write_folder) is opened for writing and
    shut with nothing written, and cut to its own length, both only through a descriptor opened without following a
    link, never through a name; the folder gets new names the caller removes, and loses only the two names
    prepare_write_folder made for that. A kind whose folder is not given, or is not one the probe may use, is
    ``not_tried``. ``write_file`` is accepted for callers of the earlier form and is never opened, sized or written:
    the probe works only on the file it made itself."""
    outcomes = dict.fromkeys(WRITE_KINDS, "not_tried")
    fd = None if write_folder is None else _write_folder_fd(os.fspath(write_folder))
    if fd is None:
        return outcomes
    name = WRITE_FOLDER_NAMES
    try:
        own = _own_file_fd(fd)
        if own is not None:
            try:
                size = os.fstat(own).st_size
                outcomes["open_for_write"] = _attempt("open_for_write", lambda: os.close(
                    os.open(_through(own), os.O_WRONLY | os.O_NOCTTY | os.O_CLOEXEC)))
                outcomes["truncate"] = _attempt("truncate", lambda: os.truncate(_through(own), size))
            finally:
                os.close(own)

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
            # a socket file made by mknod is a name only: nothing listens on it
            "make_socket_node": (lambda: os.mknod(name["make_socket_node"], stat.S_IFSOCK | 0o600, dir_fd=fd),
                                 made("make_socket_node")),
            "remove_file": (lambda: os.unlink(name["remove_file"], dir_fd=fd), gone("remove_file")),
            "remove_folder": (lambda: os.rmdir(name["remove_folder"], dir_fd=fd), gone("remove_folder")),
        }
        for kind in WRITE_KINDS:
            if kind in attempts:
                outcomes[kind] = _attempt(kind, *attempts[kind])
    finally:
        os.close(fd)
    return outcomes


def _descriptors_open_for_write():
    """Each descriptor of this process open for writing on a regular file or a block device, as
    ``{"descriptor": n, "kind": ...}`` in its order, or None when not every descriptor could be seen: those held
    now, and, where apply_wall ran in this process, every one the kernel's list held after the wall."""
    numbers, seen = _descriptors_now(), _STATE.get("seen_at_wall")
    at_wall = seen.get("numbers") if seen else []
    if numbers is None or at_wall is None:
        return None
    return _named_for_write(sorted(set(numbers) | set(at_wall)))


def _walled_here() -> bool:
    """Whether apply_wall put the wall on in THIS process and read the kernel's lists after it."""
    return bool(_STATE.get("seen_at_wall"))


def _descriptors_unaccounted():
    """Each descriptor held now that the kernel's list read after the wall did not hold (one handed in since), or that
    it held as another object (received at the number of one closed since: its identity differs), as ``_named_kinds``
    names it; None where apply_wall did not run in this process or not every one could be seen."""
    seen = _STATE.get("seen_at_wall")
    at_wall = seen.get("numbers") if seen else None
    numbers = None if at_wall is None else _descriptors_now()
    if numbers is None:
        return None
    identities = seen.get("identities") or {}
    return _named_kinds(number for number in numbers
                        if number not in set(at_wall) or identities.get(number) != _identity(number))


def _kept_and_not_closed():
    """(descriptors_kept, descriptors_not_closed): the descriptors apply_wall kept (the caller's, and 0, 1 and 2, which
    it points at /dev/null where not kept), by kind, and each other descriptor the kernel's list held after the wall;
    (None, None) where no keep list was given or the list could not be read."""
    kept, seen = _STATE.get("kept"), _STATE.get("seen_at_wall")
    at_wall = seen.get("numbers") if seen else None
    if kept is None or at_wall is None:
        return None, None
    held = sorted(set(kept) | set(range(3)))
    return _named_kinds(held), _named_kinds(number for number in at_wall if number not in held)


def _held_act(libc, kind: str, made: dict) -> None:
    """One harmless act through an object apply_wall made: a submission of nothing to the ring, one byte sent to the
    wall's own receiver by address, one byte on the stream, one byte into the pipe (its signal goes to this process
    itself and is ignored)."""
    numbers = made["numbers"]
    if kind == "ring":
        _sys(libc, "io_uring_enter", numbers[0], 0, 0, 0, 0, 0)
    elif kind == "datagram_socket":
        address = struct.pack("<H", socket.AF_UNIX) + made["address"]
        data, target = ctypes.create_string_buffer(b"x", 1), ctypes.create_string_buffer(address, len(address))
        _sys(libc, "sendto", numbers[0], ctypes.addressof(data), 1, 0, ctypes.addressof(target), len(address))
    else:
        os.write(numbers[0], b"x")


def _held_objects() -> tuple:
    """(held_objects, held_objects_not_tried): per kind of ``HELD_KINDS`` its outcome after the wall, and the reason of
    each kind not tried. An object whose every descriptor is no longer the one apply_wall made was closed by the wall;
    else one act is tried through it (refused EPERM: refused_by_filter; done: acted) and the probe closes it."""
    outcomes, reasons = dict.fromkeys(HELD_KINDS, "not_made"), {}
    for kind, reason in HELD_NOT_TRIED.items():
        outcomes[kind], reasons[kind] = "not_tried", reason
    libc = _libc()
    for kind, made in list(_PLANTED.items()):
        if "not_tried" in made:
            outcomes[kind], reasons[kind] = "not_tried", made["not_tried"]
            continue
        still = [n for n, i in zip(made["numbers"], made["ids"]) if _identity(n) == i]
        if not still:
            outcomes[kind] = "closed_by_wall"
            continue
        try:
            _held_act(libc, kind, made)
        except PermissionError:
            outcomes[kind] = "refused_by_filter"
        except OSError:
            outcomes[kind] = "error"
        else:
            outcomes[kind] = "acted"
        for number in still:
            try:
                os.close(number)
            except OSError:
                pass
    _PLANTED.clear()
    return outcomes, reasons


def _descriptors_now():
    """The numbers of every descriptor this process holds now, in order, or None when not every one could be seen.

    The list of descriptors (/proc/self/fd) is outside what the wall lets the process read, so they are found by
    number with ``fstat`` and ``fcntl(F_GETFL)``, which the wall does not refuse: up to the count the kernel
    gives as the size of /proc/self/fd (a stat, from Linux 6.2), or, where that size is 0, up to the hard limit
    of open files (a descriptor numbered above a hard limit lowered after it was opened is then not seen; the
    probe adds the kernel's own list read after the wall for every descriptor held then)."""
    import fcntl
    import resource
    try:
        expected = os.stat("/proc/self/fd").st_size
    except OSError:
        expected = 0
    limit = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    if limit == resource.RLIM_INFINITY or limit < 0:
        return None
    found, open_numbers, number = 0, [], 0
    while number < limit and (expected <= 0 or found < expected):
        try:
            os.fstat(number)
            fcntl.fcntl(number, fcntl.F_GETFL)
        except OSError:
            number += 1
            continue
        found += 1
        open_numbers.append(number)
        number += 1
    if expected > 0 and found != expected:
        return None
    return open_numbers


def _named_kinds(numbers) -> list:
    """Each of the descriptors ``numbers`` that is still open, as ``{"descriptor": n, "kind": ...}`` in order, its
    kind by the file type the kernel gives it (``_FILE_KINDS``, else ``other``); never its name or contents."""
    named = []
    for number in numbers:
        try:
            kind = stat.S_IFMT(os.fstat(number).st_mode)
        except OSError:
            continue
        named.append({"descriptor": number, "kind": _FILE_KINDS.get(kind, _OTHER_KIND)})
    return named


def _named_for_write(numbers) -> list:
    """Of the descriptors ``numbers``, each open for writing on a regular file or a block device, as
    ``{"descriptor": n, "kind": ...}`` in order; a number no longer open is passed over."""
    import fcntl
    held = []
    for number in numbers:
        try:
            kind = stat.S_IFMT(os.fstat(number).st_mode)
            flags = fcntl.fcntl(number, fcntl.F_GETFL)
        except OSError:
            continue
        if kind in _DESCRIPTOR_KINDS and (flags & 3) in (os.O_WRONLY, os.O_RDWR):
            held.append({"descriptor": number, "kind": _DESCRIPTOR_KINDS[kind]})
    return held


def why_not_proven(record: dict) -> list:
    """The sentences a person reads when ``record`` is not proven, in order (an empty list when it is): that the
    plugin was not loaded, each check and each kind of write that did not hold, in plain words, and what they can
    do. Every word comes from ``WORDS``; the record is read as data, so a record of an older draft reads as not
    proven with its missing parts named."""
    if record.get("proven") is True:
        return []
    lines = [WORDS["not_loaded"]]
    threads = record.get("threads")
    if "threads" in record and threads != 1:
        known = isinstance(threads, int) and not isinstance(threads, bool) and threads > 1
        lines.append(WORDS["second_thread"].format(threads=threads) if known else WORDS["threads_unknown"])
    if "descriptors_open_for_write" in record:
        held = record["descriptors_open_for_write"]
        if not isinstance(held, list):
            lines.append(WORDS["descriptors_unknown"])
        else:
            for entry in held:
                kind = entry.get("kind") if isinstance(entry, dict) else None
                lines.append(WORDS["descriptor_open"].format(
                    kind=WORDS[kind] if kind in _DESCRIPTOR_KINDS.values() else WORDS["regular_file"]))
    if record.get("descriptors_seen_by") == "scan_by_number":
        lines.append(WORDS["no_wall_here"])
    if isinstance(record.get("descriptors_unaccounted"), list):
        for entry in record["descriptors_unaccounted"]:
            kind = entry.get("kind") if isinstance(entry, dict) else None
            known = kind in _FILE_KINDS.values() or kind == _OTHER_KIND
            lines.append(WORDS["descriptor_after_wall"].format(kind=WORDS[kind] if known else WORDS[_OTHER_KIND]))
    elif ("descriptors_unaccounted" in record and record.get("descriptors_seen_by") != "scan_by_number"
          and isinstance(record.get("descriptors_open_for_write"), list)):
        lines.append(WORDS["descriptors_unknown"])
    if ("descriptors_kept" in record and not isinstance(record["descriptors_kept"], list)
            and record.get("descriptors_seen_by") == "kernel_list_after_wall"):
        lines.append(WORDS["keep_not_named"])
    if isinstance(record.get("descriptors_not_closed"), list):
        for entry in record["descriptors_not_closed"]:
            kind = entry.get("kind") if isinstance(entry, dict) else None
            known = kind in _FILE_KINDS.values() or kind == _OTHER_KIND
            lines.append(WORDS["descriptor_not_closed"].format(kind=WORDS[kind] if known else WORDS[_OTHER_KIND]))
    if "held_objects" in record:
        held = record["held_objects"] if isinstance(record["held_objects"], dict) else {}
        reasons = record.get("held_objects_not_tried") if isinstance(record.get("held_objects_not_tried"), dict) else {}
        for kind in HELD_KINDS:
            outcome = held.get(kind, "not_made")
            if outcome in _HELD_HELD or (outcome == "not_tried" and reasons.get(kind)):
                continue
            key = {"acted": "held_acted", "error": "error"}.get(outcome, "held_not_made")
            lines.append(WORDS[key].format(what=WORDS["held_" + kind]))
    if "memory_maps_open_for_write" in record:
        maps = record["memory_maps_open_for_write"]
        if not isinstance(maps, list):
            lines.append(WORDS["maps_unknown"])
        else:
            lines.extend(WORDS["map_open"] for _ in maps)
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

    First the threads of the process are counted (``threads``) and every descriptor it holds is found, before the
    probe opens anything of its own: those open for writing on a regular file or a block device are named
    (``descriptors_open_for_write``; None when not every descriptor could be seen), and so is every descriptor
    that the kernel's list read after the wall did not hold (``descriptors_unaccounted``, by kind; None where no
    wall was applied in this process or not every descriptor could be seen). Eight checks are tried; each field is True only
    when the kernel refused it. Then each kind of
    ``WRITE_KINDS`` is tried on the probe's own file and in ``write_folder`` (a folder the caller made and gave to
    ``prepare_write_folder`` before the wall); ``writes`` holds the outcome of each, and ``writes_not_tried``
    names every kind the probe does not try, with its reason: ``proven`` covers the kinds in ``writes`` only.
    ``proven`` is True only when ``apply_wall`` ran in this process (``descriptors_seen_by`` is
    ``kernel_list_after_wall``) with a keep list, closing every descriptor it did not keep (``descriptors_kept``,
    ``descriptors_not_closed`` empty), each object of ``HELD_KINDS`` it made could not act after the wall
    (``held_objects``: closed by the wall or refused by the filter; a kind not tried named with its reason in
    ``held_objects_not_tried``), no descriptor is held that the wall's list did not hold, the process has one
    thread, no descriptor open for writing, all eight checks
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
    threads = _threads_now()  # before the probe opens anything of its own
    # every descriptor held at the probe's own moment, before it opens anything: the ones the kernel's list after the
    # wall did not hold (handed in since), and the ones open for writing, now or at the wall
    unaccounted, held, seen = _descriptors_unaccounted(), _descriptors_open_for_write(), _STATE.get("seen_at_wall")
    seen_by = "kernel_list_after_wall" if _walled_here() else "scan_by_number"
    kept, not_closed = _kept_and_not_closed()
    held_objects, held_not_tried = _held_objects()
    maps = seen["maps"] if seen else _maps_open_for_write()
    record = {
        "threads": threads,
        "descriptors_open_for_write": held,
        "memory_maps_open_for_write": maps,
        "descriptors_seen_by": seen_by,
        "descriptors_unaccounted": unaccounted,
        "descriptors_kept": kept,
        "descriptors_not_closed": not_closed,
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
        "held_objects": held_objects,
        "held_objects_not_tried": held_not_tried,
        "landlock_abi": _landlock_abi(libc),
        "landlock_scopes": list(_STATE["scopes"]),
        "arch": platform.machine() or "unknown",
    }
    record["proven"] = (seen_by == "kernel_list_after_wall" and unaccounted == []
                        and isinstance(kept, list) and not_closed == []
                        and all(held_objects[kind] in _HELD_HELD
                                or (held_objects[kind] == "not_tried" and held_not_tried.get(kind))
                                for kind in HELD_KINDS)
                        and threads == 1 and held == [] and maps == []
                        and all(record[name] for name in RECORD_CHECKS) and record["landlock_abi"] >= _MIN_LANDLOCK_ABI
                        and all(record["writes"][kind] in _WRITE_REFUSED for kind in WRITE_KINDS)
                        and record["arch"] in _NR and _STATE["failed"] is None)
    return record
