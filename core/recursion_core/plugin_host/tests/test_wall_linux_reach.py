"""What a walled process can still reach: other processes, the metadata of files, an older kernel, new
namespaces, a half-applied wall, whether the proof proves this wall, the kernel's old IPC objects of other
processes, a process that later takes the host's old pid number, a lease, and the Landlock scopes.

Every walled case runs in a CHILD interpreter (``-I``, the runner's own ``-X`` options) with its own
temporary folder, stand-in files and stand-in processes (a 'daemon' and three victims) that the test starts
itself and ends; nothing here walls the test runner, and nothing is aimed at a process or file the test did
not make. Each walled case has an unwalled control in the same child code, so a test shows that the call
works without the wall and is refused with it. Cases that need only the module's own logic run in this
process with stand-ins and wall nothing.
"""
from __future__ import annotations

import errno
import json
import os
import platform
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from recursion_core.plugin_host import wall_linux
from recursion_core.plugin_host.tests.test_wall_linux import CORE, _interpreter, _why_not_here

CHILD_TIMEOUT_S = 90  # a bound on one child process of this test; test-side only, not a product figure
VICTIM_WAIT_S = 5     # how long a test waits to see a stand-in victim end; test-side only
# What these tests assume about the runner's own priority is read, never taken as a literal: a runner may start
# at any nice value. All of these are test-side values, not product figures.
NICE_TOP = 19                 # the largest nice value the kernel knows (setpriority(2)); a kernel number
DAEMON_NICE_STEP = 1          # how far above its starting nice the child tries to raise the stand-in daemon
SPINNER_CPU_MIN_S = 1.1       # the spinning victim must have used this much CPU before the child sets a 1 s limit
SPINNER_CPU_WAIT_S = 120      # the bound on that wait, on a busy host at a lowered priority
SPINNER_CPU_POLL_S = 0.05     # how often the wait looks
PRIORITY_RUNS = (0, 10, 19)   # the starting priorities the A1 tests are run at, one nested run each
PRIORITY_RUN_TIMEOUT_S = 600  # a bound on one nested run
PRIORITY_RUN_ENV = "RECURSION_REACH_PRIORITY_RUN"  # set in a nested run so that it does not start more

# The kernel's numbers for the calls these tests make by number, written here again so that the tests do
# not take them from the module they check (x86_64: syscall_64.tbl; aarch64: asm-generic/unistd.h).
NUM = {
    "x86_64": {"rt_sigqueueinfo": 129, "rt_tgsigqueueinfo": 297, "sched_setattr": 314, "ioprio_set": 251,
               "migrate_pages": 256, "move_pages": 279, "chmod": 90, "fchmod": 91, "chown": 92, "fchown": 93,
               "lchown": 94, "utime": 132, "utimes": 235, "futimesat": 261, "fchownat": 260, "fchmodat": 268,
               "fchmodat2": 452, "utimensat": 280, "setxattr": 188, "lsetxattr": 189, "fsetxattr": 190,
               "removexattr": 197, "lremovexattr": 198, "fremovexattr": 199, "setxattrat": 463,
               "removexattrat": 466, "file_setattr": 469, "truncate": 76, "ftruncate": 77, "openat2": 437, "clone": 56,
               "clone3": 435, "openat": 257, "open": 2, "fcntl": 72, "ioctl": 16, "prlimit64": 302,
               "setpriority": 141, "sched_setaffinity": 203, "sched_setscheduler": 144, "sched_setparam": 142,
               "read": 0, "getpid": 39, "kill": 62, "tkill": 200, "tgkill": 234,
               "shmget": 29, "shmat": 30, "shmdt": 67, "shmctl": 31, "semget": 64, "semop": 65, "semtimedop": 220,
               "semctl": 66, "msgget": 68, "msgsnd": 69, "msgrcv": 70, "msgctl": 71, "mq_open": 240,
               "mq_unlink": 241, "mq_timedsend": 242, "mq_timedreceive": 243, "mq_notify": 244,
               "mq_getsetattr": 245},
    "aarch64": {"rt_sigqueueinfo": 138, "rt_tgsigqueueinfo": 240, "sched_setattr": 274, "ioprio_set": 30,
                "migrate_pages": 238, "move_pages": 239, "fchmod": 52, "fchown": 55, "fchownat": 54,
                "fchmodat": 53, "fchmodat2": 452, "utimensat": 88, "setxattr": 5, "lsetxattr": 6, "fsetxattr": 7,
                "removexattr": 14, "lremovexattr": 15, "fremovexattr": 16, "setxattrat": 463,
                "removexattrat": 466, "file_setattr": 469, "truncate": 45, "ftruncate": 46, "openat2": 437, "clone": 220,
                "clone3": 435, "openat": 56, "fcntl": 25, "ioctl": 29, "prlimit64": 261, "setpriority": 140,
                "sched_setaffinity": 122, "sched_setscheduler": 119, "sched_setparam": 118, "read": 63,
                "getpid": 172, "kill": 129, "tkill": 130, "tgkill": 131,
                "shmget": 194, "shmat": 196, "shmdt": 197, "shmctl": 195, "semget": 190, "semop": 193,
                "semtimedop": 192, "semctl": 191, "msgget": 186, "msgsnd": 189, "msgrcv": 188, "msgctl": 187,
                "mq_open": 180, "mq_unlink": 181, "mq_timedsend": 182, "mq_timedreceive": 183, "mq_notify": 184,
                "mq_getsetattr": 185},
}
CLONE_NEW = {"NEWNS": 0x00020000, "NEWCGROUP": 0x02000000, "NEWUTS": 0x04000000, "NEWIPC": 0x08000000,
             "NEWUSER": 0x10000000, "NEWPID": 0x20000000, "NEWNET": 0x40000000}

CHILD = r'''
import ctypes, errno, fcntl, json, mmap, os, platform, resource, signal, socket, struct, sys, threading, time, _thread
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w
ARCH = platform.machine()
NUM = spec["num"][ARCH]
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long

def sc(name_or_nr, *args):
    nr = NUM[name_or_nr] if isinstance(name_or_nr, str) else name_or_nr
    ctypes.set_errno(0)
    r = libc.syscall(ctypes.c_long(nr), *(ctypes.c_long(a) for a in args))
    return r, ctypes.get_errno()

def res(name_or_nr, *args):
    if isinstance(name_or_nr, str) and name_or_nr not in NUM:
        return "absent"
    r, e = sc(name_or_nr, *args)
    return "ok" if r >= 0 else errno.errorcode.get(e, str(e))

def t(call):
    try:
        call()
    except w.WallRefused as exc:
        return "refused:" + exc.code
    except OSError as exc:
        return errno.errorcode.get(exc.errno or 0, type(exc).__name__)
    except Exception as exc:
        return "exc:" + type(exc).__name__
    return "ok"

def buf(data):
    b = ctypes.create_string_buffer(data, max(len(data), 1))
    return b, ctypes.addressof(b)

libc.prctl.restype = ctypes.c_int

def status():  # by prctl, not /proc/self/status, which the wall's Landlock part refuses to read
    return {"NoNewPrivs": str(libc.prctl(39, 0, 0, 0, 0)), "Seccomp": str(libc.prctl(21, 0, 0, 0, 0))}

def apply(paths):
    try:
        w.apply_wall(paths)
    except w.WallRefused as exc:
        return {"code": exc.code, "step": getattr(exc, "step", None)}
    except Exception as exc:
        return {"code": type(exc).__name__, "step": None}
    return {"code": "applied", "step": None}

def forked(fn):
    pid = os.fork()
    if pid == 0:
        try:
            code = fn()
        except BaseException:
            code = 99
        os._exit(code if isinstance(code, int) else 98)
    _, st = os.waitpid(pid, 0)
    return ("exit:%d" % os.WEXITSTATUS(st)) if os.WIFEXITED(st) else ("signal:%d" % os.WTERMSIG(st))

def at_symbols(path):
    return buf(path.encode() + b"\0")

mode, out = spec["mode"], {}
D, F, me, daemon = spec["dir"], spec["files"], os.getpid(), spec["daemon"]
paths = spec["paths"] + ([spec["ro"]] if spec.get("ro") else [])
chan = socket.socket(fileno=spec["chan"])

if mode == "reach":
    if spec.get("abi_cap"):
        real_abi = w._landlock_abi
        w._landlock_abi = lambda lc, real_abi=real_abi: min(spec["abi_cap"], real_abi(lc))
    out["apply"] = apply(paths) if spec["wall"] else {"code": "unwalled", "step": None}
    out["status"] = status()
    V = spec["victims"]
    info, pinfo = buf(struct.pack("<iii", signal.SIGTERM, 0, -1) + bytes(116))
    zero_info, pzero = buf(struct.pack("<iii", 0, 0, -1) + bytes(116))
    a1 = {}
    a1["rt_sigqueueinfo_victim"] = res("rt_sigqueueinfo", V[0], signal.SIGTERM, pinfo)
    def owner_signal():
        r, wr = os.pipe()
        try:
            fcntl.fcntl(r, 8, V[1])                       # F_SETOWN
            fcntl.fcntl(r, 10, signal.SIGTERM)            # F_SETSIG
            fcntl.fcntl(r, fcntl.F_SETFL, fcntl.fcntl(r, fcntl.F_GETFL) | 0o20000)  # O_ASYNC
            os.write(wr, b"x")
        finally:
            os.close(r); os.close(wr)
    a1["f_setown_victim"] = t(owner_signal)
    a1["prlimit_cpu_victim"] = t(lambda: resource.prlimit(V[2], resource.RLIMIT_CPU, (1, 1)))
    a1["rt_tgsigqueueinfo_daemon"] = res("rt_tgsigqueueinfo", daemon, daemon, 0, pzero)
    def owner_ex():
        r, wr = os.pipe()
        try:
            fcntl.fcntl(r, 15, struct.pack("<ii", 1, daemon))   # F_SETOWN_EX, F_OWNER_PID
        finally:
            os.close(r); os.close(wr)
    a1["f_setown_ex_daemon"] = t(owner_ex)
    a1["fiosetown_daemon"] = t(lambda: fcntl.ioctl(chan.fileno(), 0x8901, struct.pack("i", daemon)))
    a1["siocspgrp_daemon"] = t(lambda: fcntl.ioctl(chan.fileno(), 0x8902, struct.pack("i", daemon)))
    a1["setpriority_daemon"] = t(lambda: os.setpriority(os.PRIO_PROCESS, daemon, spec["daemon_nice_set"]))
    a1["sched_setaffinity_daemon"] = t(lambda: os.sched_setaffinity(daemon, os.sched_getaffinity(0)))
    a1["sched_setscheduler_daemon"] = t(lambda: os.sched_setscheduler(daemon, os.SCHED_OTHER, os.sched_param(0)))
    a1["sched_setparam_daemon"] = t(lambda: os.sched_setparam(daemon, os.sched_param(0)))
    # the same nice value as above (one step above the daemon's start): an unprivileged process may raise it
    attr, pattr = buf(struct.pack("<IIQiIQQQ", 48, 0, 0, spec["daemon_nice_set"], 0, 0, 0, 0))
    a1["sched_setattr_daemon"] = res("sched_setattr", daemon, pattr, 0)
    a1["ioprio_set_daemon"] = res("ioprio_set", 1, daemon, (2 << 13) | 4)
    nodes, pnodes = buf(struct.pack("<Q", 1))
    a1["migrate_pages_daemon"] = res("migrate_pages", daemon, 64, pnodes, pnodes)
    a1["move_pages_daemon"] = res("move_pages", daemon, 0, 0, 0, 0, 0)
    old, pold = buf(bytes(16))
    a1["prlimit_read_daemon"] = res("prlimit64", daemon, 0, 0, pold)
    out["a1"] = a1
    own = {}
    own["prlimit_self_zero"] = res("prlimit64", 0, 0, 0, pold)
    own["getrlimit"] = t(lambda: resource.getrlimit(resource.RLIMIT_NOFILE))
    own["setpriority_self"] = t(lambda: os.setpriority(os.PRIO_PROCESS, 0, os.getpriority(os.PRIO_PROCESS, 0)))
    own["sched_setaffinity_self"] = t(lambda: os.sched_setaffinity(0, os.sched_getaffinity(0)))
    own["sched_setscheduler_self"] = t(lambda: os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0)))
    own["move_pages_self"] = res("move_pages", 0, 0, 0, 0, 0, 0)
    out["own"] = own
    signal.signal(signal.SIGUSR1, lambda *_a: None)
    by_pid = {}  # the process at itself by its own pid number, and every way to signal itself
    by_pid["prlimit_own_pid"] = res("prlimit64", me, 0, 0, pold)
    by_pid["setpriority_own_pid"] = t(lambda: os.setpriority(os.PRIO_PROCESS, me, os.getpriority(os.PRIO_PROCESS, 0)))
    by_pid["sched_setaffinity_own_pid"] = t(lambda: os.sched_setaffinity(me, os.sched_getaffinity(0)))
    by_pid["sched_setscheduler_own_pid"] = t(lambda: os.sched_setscheduler(me, os.SCHED_OTHER, os.sched_param(0)))
    by_pid["ioprio_set_own_pid"] = res("ioprio_set", 1, me, (2 << 13) | 4)
    by_pid["move_pages_own_pid"] = res("move_pages", me, 0, 0, 0, 0, 0)
    by_pid["kill_own_pid_0"] = t(lambda: os.kill(me, 0))
    by_pid["kill_own_group_0"] = t(lambda: os.kill(0, 0))
    by_pid["tkill_own_0"] = res("tkill", me, 0)
    by_pid["tgkill_own_0"] = res("tgkill", me, me, 0)
    by_pid["rt_sigqueueinfo_own_0"] = res("rt_sigqueueinfo", me, 0, pzero)
    by_pid["rt_tgsigqueueinfo_own_0"] = res("rt_tgsigqueueinfo", me, me, 0, pzero)
    by_pid["raise_signal"] = t(lambda: signal.raise_signal(signal.SIGUSR1))
    by_pid["pthread_kill_own_0"] = t(lambda: signal.pthread_kill(threading.get_ident(), 0))
    out["by_pid"] = by_pid
    arrives, rang = {}, []
    signal.signal(signal.SIGALRM, lambda *_a: rang.append(1))
    signal.setitimer(signal.ITIMER_REAL, 0.05)
    until = time.monotonic() + 5
    while not rang and time.monotonic() < until:
        time.sleep(0.01)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    arrives["itimer_real_handler"] = bool(rang)
    arrives["alarm_ends"] = forked(lambda: (signal.alarm(1), time.sleep(5), 0)[-1])
    arrives["abort"] = forked(lambda: (os.abort(), 97)[-1])
    out["arrives"] = arrives
    a2, P, fd = {}, F["meta"], spec["metafd"]
    p_, pp = at_symbols(P)
    uid, gid = os.getuid(), os.getgid()
    a2["chmod"] = res("chmod", pp, 0o604)
    a2["chown"] = res("chown", pp, uid, gid)
    a2["lchown"] = res("lchown", pp, uid, gid)
    times, ptimes = buf(struct.pack("<qq", 1000000, 1000000))
    a2["utime"] = res("utime", pp, ptimes)
    tv, ptv = buf(struct.pack("<qqqq", 1000000, 0, 1000000, 0))
    a2["utimes"] = res("utimes", pp, ptv)
    a2["futimesat"] = res("futimesat", -100, pp, ptv)
    a2["fchmodat"] = res("fchmodat", -100, pp, 0o604)
    a2["fchmodat2"] = res("fchmodat2", -100, pp, 0o604, 0)
    a2["fchownat"] = res("fchownat", -100, pp, uid, gid, 0)
    ts, pts = buf(struct.pack("<qqqq", 1000000, 0, 1000000, 0))
    a2["utimensat"] = res("utimensat", -100, pp, pts, 0)
    n1, pn1 = buf(b"user.wall\0")
    v1, pv1 = buf(b"1")
    a2["setxattr"] = res("setxattr", pp, pn1, pv1, 1, 0)
    a2["lsetxattr"] = res("lsetxattr", pp, pn1, pv1, 1, 0)
    nk, pnk = buf(b"user.keep\0")
    a2["removexattr"] = res("removexattr", pp, pnk)
    nk2, pnk2 = buf(b"user.keep2\0")
    a2["lremovexattr"] = res("lremovexattr", pp, pnk2)
    xargs, pxargs = buf(struct.pack("<QII", pv1, 1, 0))
    a2["setxattrat"] = res("setxattrat", -100, pp, 0, pn1, pxargs, 16)
    a2["removexattrat"] = res("removexattrat", -100, pp, 0, pnk)
    fattr, pfattr = buf(bytes(32))
    a2["file_setattr"] = res("file_setattr", -100, pp, pfattr, 32, 0)
    a2["fd_fchmod"] = res("fchmod", fd, 0o604)
    a2["fd_fchown"] = res("fchown", fd, uid, gid)
    a2["fd_futimens"] = res("utimensat", fd, 0, pts, 0)
    a2["fd_fsetxattr"] = res("fsetxattr", fd, pn1, pv1, 1, 0)
    a2["fd_fremovexattr"] = res("fremovexattr", fd, pnk)
    def setflags():
        got = fcntl.ioctl(fd, 0x80086601, bytes(8))          # FS_IOC_GETFLAGS
        fcntl.ioctl(fd, 0x40086602, got)                     # FS_IOC_SETFLAGS, the same flags
    a2["fd_fs_ioc_setflags"] = t(setflags)
    def setfsx():
        got = fcntl.ioctl(fd, 0x801C581F, bytes(28))         # FS_IOC_FSGETXATTR
        fcntl.ioctl(fd, 0x401C5820, got)                     # FS_IOC_FSSETXATTR, the same attributes
    a2["fd_fs_ioc_fssetxattr"] = t(setfsx)
    a2["fd_read_still_works"] = t(lambda: os.pread(fd, 4, 0))
    out["a2"] = a2
    a3 = {}
    tp, ptp = at_symbols(F["truncate"])
    a3["truncate_outside"] = res("truncate", ptp, 0)
    a3["o_trunc_beneath_ro"] = t(lambda: os.close(os.open(os.path.join(spec["ro"], "trunc"), os.O_RDONLY | os.O_TRUNC)))
    rp, prp = at_symbols(os.path.join(spec["ro"], "trunc"))
    how, phow = buf(struct.pack("<QQQ", os.O_RDONLY | os.O_TRUNC, 0, 0))
    a3["openat2_beneath_ro"] = res("openat2", -100, prp, phow, 24)
    a3["read_beneath_ro"] = t(lambda: open(os.path.join(spec["ro"], "inside"), "rb").read(4))
    a3["open_rdonly_beneath_ro"] = t(lambda: os.close(os.open(os.path.join(spec["ro"], "inside"), os.O_RDONLY)))
    a3["list_ro"] = t(lambda: os.listdir(spec["ro"]))
    a3["append_beneath_ro"] = t(lambda: open(os.path.join(spec["ro"], "inside"), "ab").close())
    a3["create_beneath_ro"] = t(lambda: open(os.path.join(spec["ro"], "new"), "x").close())
    a3["mkdir_beneath_ro"] = t(lambda: os.mkdir(os.path.join(spec["ro"], "newdir")))
    a3["unlink_beneath_ro"] = t(lambda: os.unlink(os.path.join(spec["ro"], "doomed")))
    a3["rename_beneath_ro"] = t(lambda: os.rename(os.path.join(spec["ro"], "doomed"), os.path.join(spec["ro"], "moved")))
    a3["chmod_beneath_ro"] = t(lambda: os.chmod(os.path.join(spec["ro"], "inside"), 0o600))
    out["a3"] = a3
    o2 = {}
    def clone_newuser():
        r, e = sc("clone", 0x10000000 | 17, 0, 0, 0, 0)
        if r == 0:
            os._exit(0)
        if r < 0:
            return errno.errorcode.get(e, str(e))
        os.waitpid(r, 0)
        return "child_made"
    o2["clone_newuser"] = clone_newuser()
    def clone3_newuser():
        args, pargs = buf(struct.pack("<QQQQQQQQ", 0x10000000, 0, 0, 0, 17, 0, 0, 0))
        r, e = sc("clone3", pargs, 64)
        if r == 0:
            os._exit(0)
        if r < 0:
            return errno.errorcode.get(e, str(e))
        os.waitpid(r, 0)
        return "child_made"
    o2["clone3_newuser"] = clone3_newuser()
    ran = []
    worker = threading.Thread(target=lambda: ran.append(1)); worker.start(); worker.join()
    o2["thread"] = "ran" if ran else "not run"
    def in_fork():
        seen = []
        th = threading.Thread(target=lambda: seen.append(1)); th.start(); th.join()
        return 0 if seen else 7
    o2["fork_then_thread"] = forked(in_fork)
    out["o2"] = o2
    a4 = {}
    if ARCH == "x86_64":
        def i386(nr):
            code = bytes([0x53, 0xb8]) + struct.pack("<I", nr) + b"\xbb\0\0\0\0\xb9\0\0\0\0\xba\0\0\0\0\xcd\x80\x5b\xc3"
            m = mmap.mmap(-1, 4096, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
            m.write(code)
            fn = ctypes.CFUNCTYPE(ctypes.c_int)(ctypes.addressof(ctypes.c_char.from_buffer(m)))
            rp_, wp_ = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.write(wp_, str(fn()).encode()); os._exit(0)
            os.close(wp_); os.waitpid(pid, 0); v = os.read(rp_, 32).decode(); os.close(rp_)
            return int(v) if v else None
        got = i386(20)                       # getpid on i386: a number the deny list does not hold
        a4["i386_getpid"] = "pid" if got and got > 0 else ("EPERM" if got == -errno.EPERM else str(got))
    out["a4"] = a4
    fresh = [n for n in ("fractions", "colorsys") if n not in sys.modules]
    import fractions, colorsys
    out["keeps_working"] = {"fresh_import": bool(fresh) and str(fractions.Fraction(1, 3) + 1) == "4/3",
                            "pipe": sys.stdin.readline().strip()}
    message, fds, _f, _a = socket.recv_fds(chan, 64, 1)
    handed = socket.socket(fileno=fds[0]); handed.sendall(b"answer:" + message); handed.close()
    out["record"] = w.probe_wall(spec["canary"], daemon, write_file=spec["canary"], write_folder=spec["writes"])

elif mode.startswith("half_"):
    point = mode[5:]
    real_call, real_nnp = w._call, w._no_new_privs
    seccomp_nr = {"x86_64": 317, "aarch64": 277}[ARCH]
    def fake_call(lc, number, *args):
        if (point == "create" and number == 444 and args[2] == 0) or (point == "addrule" and number == 445) \
           or (point == "restrict" and number == 446) or (point == "seccomp" and number == seccomp_nr):
            return -1, errno.ENOMEM
        if point == "after_load" and number == seccomp_nr:
            real_call(lc, number, *args)
            return -1, errno.EAGAIN
        return real_call(lc, number, *args)
    calls = {"n": 0}
    def fake_nnp(lc):
        calls["n"] += 1
        if (point == "nnp1" and calls["n"] == 1) or (point == "nnp2" and calls["n"] == 2):
            raise OSError(errno.EINVAL, "stand-in prctl failure")
        return real_nnp(lc)
    w._call, w._no_new_privs = fake_call, fake_nnp
    out["apply"] = apply(paths)
    w._call, w._no_new_privs = real_call, real_nnp
    out["status"] = status()
    out["record"] = w.probe_wall(spec["canary"], daemon, write_file=spec["canary"], write_folder=spec["writes"])
    out["reapply"] = apply(paths)
    out["proven_after_reapply"] = w.probe_wall(spec["canary"], daemon, write_file=spec["canary"], write_folder=spec["writes"])["proven"]

elif mode == "twice":
    out["apply"] = apply(paths)
    out["reapply"] = apply(paths)
    out["proven"] = w.probe_wall(spec["canary"], daemon, write_file=spec["canary"], write_folder=spec["writes"])["proven"]

elif mode == "weak":
    lc, arch = w._libc(), w._arch()
    opened = w._open_paths(paths)
    w._apply_landlock(lc, w._landlock_abi(lc), opened)
    for fd_, _ in opened:
        os.close(fd_)
    w.DENIED_CALLS = ("socket", "socketpair", "execve", "execveat")
    for name, empty in (("DENIED_METADATA_CALLS", ()), ("CALLER_ONLY_CALLS", ()), ("DENIED_IPC_CALLS", ()),
                        ("DENIED_SIGNAL_CALLS", ("kill",)), ("DENIED_FCNTL_COMMANDS", {}),
                        ("DENIED_IOCTL_COMMANDS", {})):
        if hasattr(w, name):
            setattr(w, name, empty)
    w._load_seccomp_filter(lc, arch)
    out["record"] = w.probe_wall(spec["canary"], daemon, write_file=spec["canary"], write_folder=spec["writes"])
    out["io_uring_setup"] = res(425 if ARCH == "x86_64" else 425, 0, 0)

elif mode == "threads_native":
    gate = _thread.allocate_lock(); gate.acquire()
    started = _thread.allocate_lock(); started.acquire()
    def native():
        started.release(); gate.acquire()
    _thread.start_new_thread(native, ())
    started.acquire()
    out["known_to_threading"] = threading.active_count()
    out["apply"] = apply(paths)
    gate.release()
    out["status"] = status()

elif mode == "probe_other_user":
    out["record"] = w.probe_wall(spec["canary"], 1)

elif mode == "ipc":
    out["apply"] = apply(paths) if spec["wall"] else {"code": "unwalled", "step": None}
    I, ipc = spec["ipc"], {}
    def shm_write():
        r, e = sc("shmat", I["shm_a"], 0, 0)
        if r == -1:
            return errno.errorcode.get(e, str(e))
        ctypes.memmove(r, b"WALLED!\0", 8)
        sc("shmdt", r)
        return "ok"
    ipc["shmat_write"] = shm_write()
    s0, ps0 = buf(struct.pack("<HhH", 0, -1, 0o4000))   # struct sembuf: take one, IPC_NOWAIT
    s1, ps1 = buf(struct.pack("<HhH", 1, -1, 0o4000))
    ipc["semop_take"] = res("semop", I["sem_a"], ps0, 1)
    ipc["semtimedop_take"] = res("semtimedop", I["sem_a"], ps1, 1, 0)
    m, pm = buf(struct.pack("<q", 1) + b"x")
    ipc["msgsnd_fill"] = res("msgsnd", I["msq_a"], pm, 1, 0o4000)
    rb, prb = buf(bytes(64))
    ipc["msgrcv_take"] = res("msgrcv", I["msq_c"], prb, 16, 0, 0o4000)
    ipc["shmctl_rmid"] = res("shmctl", I["shm_b"], 0, 0)
    ipc["semctl_rmid"] = res("semctl", I["sem_b"], 0, 0, 0)
    ipc["msgctl_rmid"] = res("msgctl", I["msq_b"], 0, 0)
    for kind, make, remove in (("shmget", ("shmget", 0, 4096, 0o600), lambda i: sc("shmctl", i, 0, 0)),
                               ("semget", ("semget", 0, 1, 0o600), lambda i: sc("semctl", i, 0, 0, 0)),
                               ("msgget", ("msgget", 0, 0o600), lambda i: sc("msgctl", i, 0, 0))):
        r, e = sc(*make)
        if r >= 0:
            remove(r)   # made by the unwalled control only, and removed at once
        ipc[kind + "_private"] = "ok" if r >= 0 else errno.errorcode.get(e, str(e))
    na, pna = buf(I["mq_a"].encode() + b"\0")
    nc, pnc = buf(I["mq_c"].encode() + b"\0")
    ipc["mq_unlink"] = res("mq_unlink", pna)
    r, e = sc("mq_open", pnc, os.O_RDWR, 0, 0)
    if r >= 0:
        os.close(r)
    ipc["mq_open"] = "ok" if r >= 0 else errno.errorcode.get(e, str(e))
    fd = I["mq_fd"]
    x, px = buf(b"x")
    rq, prq = buf(bytes(64))
    at, pat = buf(bytes(64))
    ipc["mq_timedsend"] = res("mq_timedsend", fd, px, 1, 0, 0)
    ipc["mq_timedreceive"] = res("mq_timedreceive", fd, prq, 16, 0, 0)
    ipc["mq_getsetattr"] = res("mq_getsetattr", fd, 0, pat)
    ipc["mq_notify"] = res("mq_notify", fd, 0)
    out["ipc"] = ipc

elif mode == "lease":
    signal.signal(signal.SIGIO, signal.SIG_IGN)   # a lease break is signalled to its holder
    out["apply"] = apply(paths) if spec["wall"] else {"code": "unwalled", "step": None}
    held = os.open(os.path.join(spec["ro"], "inside"), os.O_RDONLY)
    out["setlease"] = t(lambda: fcntl.fcntl(held, 1024, fcntl.F_RDLCK))   # F_SETLEASE
    out["getlease"] = t(lambda: fcntl.fcntl(held, 1025))                  # F_GETLEASE
    chan.sendall(b"h")
    chan.recv(1)
    os.close(held)

print(json.dumps(out), flush=True)
'''


def _start(args, folder):
    return subprocess.Popen(_interpreter() + args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, cwd=folder, env={"TMPDIR": folder})


def _daemon_reading(pid: int) -> dict:
    """The priority, the CPU set and the scheduling policy of one stand-in process, as the parent sees them."""
    return {"nice": os.getpriority(os.PRIO_PROCESS, pid), "affinity": sorted(os.sched_getaffinity(pid)),
            "policy": os.sched_getscheduler(pid)}


def _cpu_used_s(pid: int) -> float:
    """The CPU seconds one stand-in process has used so far (user and system), from its stat line."""
    with open(f"/proc/{pid}/stat", "rb") as handle:
        after_name = handle.read().rsplit(b")", 1)[1].split()   # field 3 (state) comes first
    return (int(after_name[11]) + int(after_name[12])) / os.sysconf("SC_CLK_TCK")


def _wait_for_cpu(pid: int) -> None:
    """Wait, bounded, until the spinning stand-in has used more CPU than the limit the child will set: how long
    that takes depends on the runner's priority and the host's load, so no fixed sleep stands in for it."""
    end = time.monotonic() + SPINNER_CPU_WAIT_S
    while _cpu_used_s(pid) < SPINNER_CPU_MIN_S:
        if time.monotonic() > end:
            raise AssertionError(f"the spinning stand-in used less than {SPINNER_CPU_MIN_S} s of CPU in "
                                 f"{SPINNER_CPU_WAIT_S} s: the host is too busy to show a CPU limit")
        time.sleep(SPINNER_CPU_POLL_S)


def run_reach_child(mode: str, **extra) -> dict:
    """One child in ``mode``; returns its JSON line plus what the parent saw afterwards ('_victims',
    '_files', '_answer', '_daemon_nice') and what it read of the stand-in daemon right after starting it
    ('_daemon_start') and after the child ended ('_daemon_after'): priority, CPU set and scheduling policy."""
    with tempfile.TemporaryDirectory() as folder:
        home = os.path.join(folder, "home")
        os.mkdir(home)
        canary = os.path.join(home, "canary")
        with open(canary, "w", encoding="utf-8") as handle:
            handle.write("the daemon's secret, stand-in")
        writes = os.path.join(folder, "write-probe")  # the folder made for the probe's write kinds, before any wall
        os.mkdir(writes)
        wall_linux.prepare_write_folder(writes)
        files = {}
        for name in ("read", "truncate", "meta", "metafd"):
            files[name] = os.path.join(folder, "outside-" + name)
            with open(files[name], "w", encoding="utf-8") as handle:
                handle.write("stand-in content")
            os.chmod(files[name], 0o644)
            os.utime(files[name], (2000000, 2000000))
        for name in ("meta", "metafd"):
            os.setxattr(files[name], "user.keep", b"1")
            os.setxattr(files[name], "user.keep2", b"1")
        ro = os.path.join(folder, "ro")
        os.mkdir(ro)
        for name in ("inside", "trunc", "doomed"):
            with open(os.path.join(ro, name), "w", encoding="utf-8") as handle:
                handle.write("read-only stand-in")
        daemon = _start(["-c", "import time; time.sleep(300)"], folder)
        daemon_start = _daemon_reading(daemon.pid)   # before the child runs: what the later reading is compared with
        daemon_nice_set = min(daemon_start["nice"] + DAEMON_NICE_STEP, NICE_TOP)
        victims = [_start(["-c", "import time; time.sleep(300)"], folder),
                   _start(["-c", "import time; time.sleep(300)"], folder),
                   _start(["-c", "while True: pass"], folder)]
        ours, theirs = socket.socketpair()
        handed_here, handed_there = socket.socketpair()
        metafd = os.open(files["metafd"], os.O_RDONLY)
        try:
            spec = dict({"core": str(CORE), "mode": mode, "canary": canary, "daemon": daemon.pid, "dir": folder,
                         "files": files, "ro": ro, "victims": [v.pid for v in victims], "chan": theirs.fileno(),
                         "metafd": metafd, "num": NUM, "daemon_nice_set": daemon_nice_set, "paths": sorted({sys.base_prefix, sys.prefix}),
                         "writes": writes}, **extra)
            socket.send_fds(ours, [b"ping"], [handed_there.fileno()])
            handed_there.close()
            _wait_for_cpu(victims[2].pid)  # the spinning victim has used more than the limit's CPU before it is set
            child = subprocess.run(_interpreter() + ["-c", CHILD, json.dumps(spec)], input="question\n",
                                   capture_output=True, text=True, timeout=CHILD_TIMEOUT_S, cwd=folder,
                                   pass_fds=[theirs.fileno(), metafd], env={"HOME": home, "TMPDIR": folder})
            handed_here.settimeout(1)
            try:
                answer = handed_here.recv(64).decode()
            except OSError:
                answer = ""
            end = time.monotonic() + VICTIM_WAIT_S
            while time.monotonic() < end and any(v.poll() is None for v in victims):
                time.sleep(0.1)
            seen_victims = [v.poll() for v in victims]
            daemon_after = _daemon_reading(daemon.pid)
            seen_files = {}
            for name, path in list(files.items()) + [("ro_trunc", os.path.join(ro, "trunc")),
                                                     ("ro_doomed", os.path.join(ro, "doomed"))]:
                if not os.path.exists(path):
                    seen_files[name] = None
                    continue
                st = os.stat(path)
                seen_files[name] = {"size": st.st_size, "mode": st.st_mode & 0o7777, "mtime": int(st.st_mtime),
                                    "xattrs": sorted(os.listxattr(path))}
        finally:
            os.close(metafd)
            for end_ in (ours, theirs, handed_here):
                end_.close()
            for process in [daemon] + victims:
                if process.poll() is None:
                    process.kill()
                process.wait()
    lines = child.stdout.strip().splitlines()
    if child.returncode != 0 or not lines:
        raise AssertionError(f"the child ({mode}) ended {child.returncode}: {child.stderr[-2000:]}")
    result = json.loads(lines[-1])
    result.update(_victims=seen_victims, _files=seen_files, _answer=answer, _daemon_nice=daemon_after["nice"],
                  _daemon_start=daemon_start, _daemon_after=daemon_after, _daemon_nice_set=daemon_nice_set)
    return result


def _sys(name, *args):
    """One call by number in the test process (stand-ins it makes and removes); (result, errno)."""
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    ctypes.set_errno(0)
    result = libc.syscall(ctypes.c_long(NUM[platform.machine()][name]), *(ctypes.c_long(a) for a in args))
    return result, ctypes.get_errno()


def run_ipc_child(wall: bool) -> dict:
    """One child that tries the kernel's old IPC on stand-ins this test makes and removes: System V shared
    memory, semaphores and message queues, and POSIX message queues. Returns its JSON line plus what the
    parent saw afterwards ('_seen')."""
    import ctypes
    tag = f"cw3-{os.getpid()}-{'w' if wall else 'c'}"
    made = {"shm": [], "sem": [], "msq": [], "mq": [], "fd": []}
    keep = []

    def ptr(data):
        b = ctypes.create_string_buffer(data, max(len(data), 1))
        keep.append(b)
        return b, ctypes.addressof(b)

    def make(kind, *args):
        r, e = _sys({"shm": "shmget", "sem": "semget", "msq": "msgget"}[kind], 0, *args)  # IPC_PRIVATE: only this test knows it
        if r < 0:
            raise OSError(e, f"{kind}get")
        made[kind].append(r)
        return r
    try:
        shm_a, shm_b = make("shm", 4096, 0o600), make("shm", 4096, 0o600)
        addr, _ = _sys("shmat", shm_a, 0, 0)
        ctypes.memmove(addr, b"stand-in", 8)
        _sys("shmdt", addr)
        sem_a, sem_b = make("sem", 2, 0o600), make("sem", 1, 0o600)
        for n in (0, 1):
            _sys("semctl", sem_a, n, 16, 1)                      # SETVAL 1
        msq_a, msq_b, msq_c = make("msq", 0o600), make("msq", 0o600), make("msq", 0o600)
        _, pm = ptr(struct.pack("<q", 1) + b"y")
        _sys("msgsnd", msq_c, pm, 1, 0o4000)
        _, pattr = ptr(struct.pack("<qqqq", 0, 4, 16, 0) + bytes(32))         # struct mq_attr: 4 messages of 16 bytes
        names = {}
        for key in ("a", "c"):
            names[key] = f"{tag}-{key}"
            _, pname = ptr(names[key].encode() + b"\0")
            fd, e = _sys("mq_open", pname, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NONBLOCK, 0o600, pattr)
            if fd < 0:
                raise OSError(e, "mq_open")
            made["mq"].append(pname)
            made["fd"].append(fd)
        spec_ipc = {"shm_a": shm_a, "shm_b": shm_b, "sem_a": sem_a, "sem_b": sem_b, "msq_a": msq_a, "msq_b": msq_b,
                    "msq_c": msq_c, "mq_a": names["a"], "mq_c": names["c"], "mq_fd": made["fd"][1]}
        ours, theirs = socket.socketpair()
        with tempfile.TemporaryDirectory() as folder:
            spec = {"core": str(CORE), "mode": "ipc", "wall": wall, "canary": "", "daemon": 0, "dir": folder,
                    "files": {}, "chan": theirs.fileno(), "num": NUM, "ipc": spec_ipc,
                    "paths": sorted({sys.base_prefix, sys.prefix})}
            try:
                child = subprocess.run(_interpreter() + ["-c", CHILD, json.dumps(spec)], capture_output=True, text=True,
                                       timeout=CHILD_TIMEOUT_S, cwd=folder, pass_fds=[theirs.fileno(), made["fd"][1]],
                                       env={"TMPDIR": folder})
            finally:
                ours.close()
                theirs.close()
        seen = {}
        addr, _ = _sys("shmat", shm_a, 0, 0)
        seen["shm_a"] = ctypes.string_at(addr, 8).rstrip(b"\0").decode()
        _sys("shmdt", addr)
        seen["sem_a"] = [_sys("semctl", sem_a, n, 12, 0)[0] for n in (0, 1)]   # GETVAL
        _, pr = ptr(bytes(64))
        seen["msq_a_messages"] = sum(1 for _ in range(4) if _sys("msgrcv", msq_a, pr, 16, 0, 0o4000)[0] >= 0)
        seen["msq_c_messages"] = sum(1 for _ in range(4) if _sys("msgrcv", msq_c, pr, 16, 0, 0o4000)[0] >= 0)
        _, pstat = ptr(bytes(256))
        seen["shm_b_exists"] = _sys("shmctl", shm_b, 2, pstat)[0] >= 0               # IPC_STAT
        seen["sem_b_exists"] = _sys("semctl", sem_b, 0, 12, 0)[0] >= 0
        seen["msq_b_exists"] = _sys("msgctl", msq_b, 2, pstat)[0] >= 0
        fd, _ = _sys("mq_open", made["mq"][0], os.O_RDWR, 0, 0)
        seen["mq_a_exists"] = fd >= 0
        if fd >= 0:
            os.close(fd)
    finally:
        for i in made["shm"]:
            _sys("shmctl", i, 0, 0)
        for i in made["sem"]:
            _sys("semctl", i, 0, 0, 0)
        for i in made["msq"]:
            _sys("msgctl", i, 0, 0)
        for pname in made["mq"]:
            _sys("mq_unlink", pname)
        for fd in made["fd"]:
            os.close(fd)
    lines = child.stdout.strip().splitlines()
    if child.returncode != 0 or not lines:
        raise AssertionError(f"the child (ipc) ended {child.returncode}: {child.stderr[-2000:]}")
    result = json.loads(lines[-1])
    result["_seen"] = seen
    return result


def run_lease_child(wall: bool) -> dict:
    """One child that holds a lease on a file it may read while this test opens the file for writing."""
    with tempfile.TemporaryDirectory() as folder:
        ro = os.path.join(folder, "ro")
        os.mkdir(ro)
        with open(os.path.join(ro, "inside"), "w", encoding="utf-8") as handle:
            handle.write("read-only stand-in")
        ours, theirs = socket.socketpair()
        spec = {"core": str(CORE), "mode": "lease", "wall": wall, "canary": "", "daemon": 0, "dir": folder,
                "files": {}, "ro": ro, "chan": theirs.fileno(), "num": NUM, "paths": sorted({sys.base_prefix, sys.prefix})}
        child = subprocess.Popen(_interpreter() + ["-c", CHILD, json.dumps(spec)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=folder,
                                 pass_fds=[theirs.fileno()], env={"TMPDIR": folder})
        theirs.close()
        try:
            ours.settimeout(CHILD_TIMEOUT_S)
            ours.recv(1)
            try:
                os.close(os.open(os.path.join(ro, "inside"), os.O_WRONLY | os.O_NONBLOCK))
                opened = "ok"
            except OSError as exc:
                opened = errno.errorcode.get(exc.errno, str(exc.errno))
            ours.sendall(b"g")
            stdout, stderr = child.communicate(timeout=CHILD_TIMEOUT_S)
        finally:
            ours.close()
            if child.poll() is None:
                child.kill()
                child.wait()
    lines = stdout.strip().splitlines()
    if child.returncode != 0 or not lines:
        raise AssertionError(f"the child (lease) ended {child.returncode}: {stderr[-2000:]}")
    result = json.loads(lines[-1])
    result["_writer"] = opened
    return result


PIDNS = r'''
import ctypes, json, os, select, signal, subprocess, sys, time
a = json.loads(sys.argv[1])
uid, gid = os.getuid(), os.getgid()
try:
    os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWPID)
    for name, text in (("setgroups", "deny"), ("uid_map", f"0 {uid} 1"), ("gid_map", f"0 {gid} 1")):
        with open(f"/proc/self/{name}", "w") as handle:
            handle.write(text)
except (OSError, AttributeError) as exc:
    print(json.dumps({"skip": f"no user and pid namespace here: {exc}"}), flush=True)
    sys.exit(0)
first = os.fork()
if first:                                        # outside the namespace: wait for its pid 1
    os.waitpid(first, 0)
    sys.exit(0)
# pid 1 of a pid namespace made for this test: every process below is ours, and all end when it ends
libc = ctypes.CDLL(None, use_errno=True)
libc.prctl(1, signal.SIGKILL)                    # PR_SET_PDEATHSIG: it ends if the outer process ends
signal.signal(signal.SIGALRM, lambda *_a: os._exit(3))
signal.alarm(a["bound"])
HOST = """
import json, os, sys
a = json.loads(sys.argv[1]); sys.path.insert(0, a["core"])
from recursion_core.plugin_host import wall_linux as w
out = {"host_pid": os.getpid()}
if a["wall"]:
    w.apply_wall(a["paths"])
    out["apply"] = "applied"
pid = os.fork()
if pid == 0:                                     # the leftover child: walled like its host
    os.close(1)
    os.read(a["go"], 1)
    try:
        os.kill(out["host_pid"], 15)
        r = "ok"
    except OSError as exc:
        r = "errno:%d" % exc.errno
    os.write(a["res"], r.encode())
    os._exit(0)
print(json.dumps(out), flush=True)
os._exit(0)                                      # the host ends; its walled child lives on
"""
go_r, go_w = os.pipe()
res_r, res_w = os.pipe()
spec = dict(a, go=go_r, res=res_w)
host = subprocess.Popen(a["interp"] + ["-c", HOST, json.dumps(spec)], stdout=subprocess.PIPE, text=True,
                        pass_fds=(go_r, res_w))
info = json.loads(host.communicate()[0].strip().splitlines()[-1])
out = {"host": info}
with open("/proc/sys/kernel/ns_last_pid", "w") as handle:  # this namespace's own counter
    handle.write(str(info["host_pid"] - 1))
victim = subprocess.Popen(a["interp"] + ["-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL)
out["victim_has_the_hosts_old_pid"] = victim.pid == info["host_pid"]
os.write(go_w, b"g")
ready, _, _ = select.select([res_r], [], [], 20)
out["leftover_kill"] = os.read(res_r, 64).decode() if ready else "no answer"
time.sleep(1)
out["victim"] = victim.poll()
print(json.dumps(out), flush=True)
os._exit(0)                                      # pid 1 ends: the kernel ends every process of the namespace
'''


def run_pid_reuse(wall: bool) -> dict:
    """A host (walled or not) forks a child and ends; in a pid namespace made for the test, a stand-in victim
    is given the host's old pid number; the leftover child then signals that number."""
    spec = {"core": str(CORE), "wall": wall, "paths": sorted({sys.base_prefix, sys.prefix}),
            "interp": _interpreter(), "bound": CHILD_TIMEOUT_S}
    with tempfile.TemporaryDirectory() as folder:
        child = subprocess.run(_interpreter() + ["-c", PIDNS, json.dumps(spec)], capture_output=True, text=True,
                               timeout=CHILD_TIMEOUT_S, cwd=folder, env={"TMPDIR": folder})
    lines = child.stdout.strip().splitlines()
    if child.returncode != 0 or not lines:
        raise AssertionError(f"the pid-namespace child ended {child.returncode}: {child.stderr[-2000:]}")
    return json.loads(lines[-1])


A1_CALLS = ("rt_sigqueueinfo_victim", "f_setown_victim", "prlimit_cpu_victim", "rt_tgsigqueueinfo_daemon",
            "f_setown_ex_daemon", "fiosetown_daemon", "siocspgrp_daemon", "setpriority_daemon",
            "sched_setaffinity_daemon", "sched_setscheduler_daemon", "sched_setparam_daemon", "sched_setattr_daemon",
            "ioprio_set_daemon", "migrate_pages_daemon", "move_pages_daemon", "prlimit_read_daemon")
A2_CALLS = ("chmod", "chown", "lchown", "utime", "utimes", "futimesat", "fchmodat", "fchmodat2", "fchownat",
            "utimensat", "setxattr", "lsetxattr", "removexattr", "lremovexattr", "setxattrat", "removexattrat",
            "file_setattr", "fd_fchmod", "fd_fchown", "fd_futimens", "fd_fsetxattr", "fd_fremovexattr",
            "fd_fs_ioc_setflags", "fd_fs_ioc_fssetxattr")
# Newer than some kernels: the unwalled control may answer ENOSYS (or EOPNOTSUPP where a filesystem lacks it);
# walled, the filter refuses them before the kernel looks.
A2_MAY_BE_ABSENT = ("setxattrat", "removexattrat", "file_setattr", "fd_fs_ioc_setflags", "fd_fs_ioc_fssetxattr")


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class ReachTests(unittest.TestCase):
    """One unwalled control and one walled child, each running the same battery."""

    @classmethod
    def setUpClass(cls):
        cls.control = run_reach_child("reach", wall=False)
        cls.walled = run_reach_child("reach", wall=True)

    def test_a1_no_other_process_is_signalled_limited_slowed_or_moved(self):
        control, walled = self.control, self.walled
        self.assertEqual(control["_victims"], [-signal.SIGTERM, -signal.SIGTERM, control["_victims"][2]],
                         "unwalled, a queued signal and a pipe's owner signal each end a stand-in victim")
        self.assertIsNotNone(control["_victims"][2], "unwalled, a CPU limit set on a spinning stand-in ends it")
        self.assertEqual(walled["apply"]["code"], "applied")
        self.assertEqual(walled["_victims"], [None, None, None], "walled, all three stand-in victims live")
        self.assertEqual({name: walled["a1"][name] for name in A1_CALLS}, dict.fromkeys(A1_CALLS, "EPERM"))
        start, after = walled["_daemon_start"], walled["_daemon_after"]
        self.assertEqual(walled["_daemon_nice"], start["nice"], "the stand-in daemon's priority is unchanged")
        self.assertEqual((after["affinity"], after["policy"]), (start["affinity"], start["policy"]),
                         "the stand-in daemon's CPU set and scheduling policy are unchanged")
        if walled["_daemon_nice_set"] != start["nice"]:
            self.assertEqual((control["_daemon_start"]["nice"], control["_daemon_nice"]),
                             (start["nice"], control["_daemon_nice_set"]),
                             "unwalled, the raised nice reaches the daemon, so the walled comparison can bite")
        with self.subTest("the priority comparison"):
            if walled["_daemon_nice_set"] == start["nice"]:
                self.skipTest(f"the runner starts at nice {start['nice']}, the top of the range: no raised value "
                              "differs from it, so the priority comparison cannot bite; every other check ran")
        for name in ("rt_tgsigqueueinfo_daemon", "f_setown_ex_daemon", "fiosetown_daemon", "setpriority_daemon",
                     "sched_setaffinity_daemon", "sched_setscheduler_daemon", "sched_setparam_daemon",
                     "sched_setattr_daemon", "ioprio_set_daemon", "move_pages_daemon", "prlimit_read_daemon"):
            self.assertEqual(control["a1"][name], "ok", f"unwalled control of {name}")

    def test_a1_the_process_may_still_act_on_itself(self):
        self.assertEqual(self.walled["own"], dict.fromkeys(self.walled["own"], "ok"))

    def test_a2_no_metadata_of_any_file_changes_by_path_or_descriptor(self):
        control, walled = self.control, self.walled
        arch = platform.machine()
        expected = {name: ("absent" if name in ("chmod", "chown", "lchown", "utime", "utimes", "futimesat")
                           and arch != "x86_64" else "EPERM") for name in A2_CALLS}
        self.assertEqual({name: walled["a2"][name] for name in A2_CALLS}, expected)
        self.assertEqual(walled["a2"]["fd_read_still_works"], "ok", "the handed-over descriptor itself still works")
        for name in A2_CALLS:
            if expected[name] == "EPERM" and name not in A2_MAY_BE_ABSENT:
                self.assertEqual(control["a2"][name], "ok", f"unwalled control of {name}")
        for name in ("meta", "metafd"):
            self.assertEqual(walled["_files"][name], {"size": 16, "mode": 0o644, "mtime": 2000000,
                                                      "xattrs": ["user.keep", "user.keep2"]}, name)
        self.assertNotEqual(control["_files"]["meta"]["mode"], 0o644, "unwalled, the mode did change")

    def test_a3_a4_a_folder_among_the_read_only_paths_is_read_only(self):
        walled = self.walled["a3"]
        for name in ("read_beneath_ro", "open_rdonly_beneath_ro", "list_ro"):
            self.assertEqual(walled[name], "ok", name)
        for name in ("append_beneath_ro", "create_beneath_ro", "mkdir_beneath_ro", "unlink_beneath_ro",
                     "rename_beneath_ro", "o_trunc_beneath_ro", "truncate_outside"):
            self.assertIn(walled[name], ("EACCES", "EPERM"), name)
        self.assertEqual(walled["chmod_beneath_ro"], "EPERM")
        files = self.walled["_files"]
        self.assertEqual((files["ro_trunc"]["size"], files["truncate"]["size"]), (18, 16))
        self.assertIsNotNone(files["ro_doomed"])

    def test_a4_the_arch_check_refuses_a_32_bit_call_the_list_does_not_name(self):
        if platform.machine() != "x86_64":
            self.skipTest("the 32-bit door is x86_64's")
        if self.control["a4"]["i386_getpid"] != "pid":
            self.skipTest("this kernel runs no 32-bit calls, so the control cannot show the door")
        self.assertEqual(self.walled["a4"]["i386_getpid"], "EPERM")

    def test_o2_no_new_namespace_and_threads_and_fork_still_work(self):
        control, walled = self.control, self.walled
        self.assertEqual((control["o2"]["clone_newuser"], control["o2"]["clone3_newuser"]), ("child_made", "child_made"),
                         "unwalled, clone and clone3 make a child in a new user namespace")
        self.assertEqual((walled["o2"]["clone_newuser"], walled["o2"]["clone3_newuser"]), ("EPERM", "ENOSYS"))
        self.assertEqual((walled["o2"]["thread"], walled["o2"]["fork_then_thread"]), ("ran", "exit:0"),
                         "walled, a thread starts, and a forked child starts a thread of its own")

    def test_b2_itself_is_never_a_pid_number(self):
        control, walled = self.control, self.walled
        self.assertEqual(control["by_pid"], dict.fromkeys(control["by_pid"], "ok"),
                         "unwalled, each call at its own pid (and each signal to itself) goes through")
        self.assertEqual(walled["by_pid"], dict.fromkeys(control["by_pid"], "EPERM"),
                         "walled, no call passes because its pid is the process's own, and no signal passes at all")

    def test_b2_timers_alarms_and_abort_still_end_or_reach_the_process(self):
        control, walled = self.control, self.walled
        self.assertIs(walled["arrives"]["itimer_real_handler"], True, "a timer the kernel delivers still arrives")
        self.assertEqual((control["arrives"]["alarm_ends"], walled["arrives"]["alarm_ends"]),
                         (f"signal:{signal.SIGALRM}", f"signal:{signal.SIGALRM}"), "an alarm still ends the process")
        self.assertEqual(control["arrives"]["abort"], f"signal:{signal.SIGABRT}")
        # glibc: abort cannot raise SIGABRT, so its abort instruction ends the process (SIGSEGV on x86_64),
        # or, where there is none, _exit(127). It never returns.
        self.assertIn(walled["arrives"]["abort"], (f"signal:{signal.SIGABRT}", f"signal:{signal.SIGSEGV}",
                                                   f"signal:{signal.SIGILL}", f"signal:{signal.SIGTRAP}", "exit:127"))

    def test_b2plus_the_record_names_the_scopes_in_force(self):
        record = self.walled["record"]
        expected = ["abstract_unix_socket", "signal"] if record["landlock_abi"] >= 6 else []
        self.assertEqual(record["landlock_scopes"], expected)
        self.assertEqual(self.control["record"]["landlock_scopes"], [], "no wall, no scope")

    def test_python_keeps_working_and_the_record_is_proven(self):
        walled = self.walled
        self.assertEqual(walled["keeps_working"], {"fresh_import": True, "pipe": "question"})
        self.assertEqual(walled["_answer"], "answer:ping", "the socket handed over by recvmsg answers")
        self.assertIs(walled["record"]["proven"], True)
        self.assertEqual(walled["status"], {"NoNewPrivs": "1", "Seccomp": "2"})


A1_TESTS = ("test_a1_no_other_process_is_signalled_limited_slowed_or_moved",
            "test_a1_the_process_may_still_act_on_itself")
PRIORITY_RUN_CODE = r'''
import os, sys, unittest
os.nice(int(sys.argv[1]))
sys.path.insert(0, sys.argv[2])
suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[3:])
result = unittest.TextTestRunner(verbosity=0, stream=sys.stderr).run(suite)
print(os.nice(0), result.testsRun, len(result.failures) + len(result.errors), len(result.skipped), flush=True)
sys.exit(0 if result.wasSuccessful() else 1)
'''


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
@unittest.skipIf(os.environ.get(PRIORITY_RUN_ENV), "this is one of the priority runs already")
class StartingPriorityTests(unittest.TestCase):
    """The A1 tests, each in a process of its own that starts at a given nice value, one run after another."""

    def test_a1_holds_at_every_starting_priority(self):
        here = os.nice(0)
        names = [f"recursion_core.plugin_host.tests.test_wall_linux_reach.ReachTests.{name}" for name in A1_TESTS]
        for target in PRIORITY_RUNS:
            with self.subTest(nice=target):
                if target < here:
                    self.skipTest(f"the runner is at nice {here}: an unprivileged process cannot lower it to {target}")
                with tempfile.TemporaryDirectory() as folder:
                    run = subprocess.run(_interpreter() + ["-c", PRIORITY_RUN_CODE, str(target - here), str(CORE)] + names,
                                         stdin=subprocess.DEVNULL, capture_output=True, text=True, cwd=folder,
                                         timeout=PRIORITY_RUN_TIMEOUT_S, env={"TMPDIR": folder, PRIORITY_RUN_ENV: "1"})
                self.assertEqual(run.returncode, 0, run.stderr[-2000:])
                nice, ran, bad, skipped = (int(word) for word in run.stdout.split()[-4:])
                self.assertEqual((nice, ran, bad), (target, len(A1_TESTS), 0), run.stderr[-2000:])
                # only the priority comparison may be unable to bite, and only where the start is the top of the range
                self.assertEqual(skipped, 1 if target == NICE_TOP else 0, run.stderr[-2000:])


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class OlderKernelTests(unittest.TestCase):

    def test_a3_below_abi_3_no_file_is_cut_and_the_record_says_which_abi(self):
        result = run_reach_child("reach", wall=True, abi_cap=2)
        self.assertEqual(result["apply"]["code"], "applied")
        a3 = result["a3"]
        self.assertEqual((a3["truncate_outside"], a3["o_trunc_beneath_ro"], a3["openat2_beneath_ro"]),
                         ("EPERM", "EPERM", "EPERM"), "the filter refuses what Landlock below ABI 3 cannot")
        self.assertEqual((result["_files"]["truncate"]["size"], result["_files"]["ro_trunc"]["size"]), (16, 18))
        self.assertEqual(a3["read_beneath_ro"], "ok")
        self.assertEqual(result["record"]["landlock_abi"], 2)
        self.assertIs(result["record"]["proven"], True)
        self.assertEqual(result["keeps_working"], {"fresh_import": True, "pipe": "question"})
        self.assertEqual(result["_answer"], "answer:ping")
        self.assertEqual((result["o2"]["thread"], result["o2"]["fork_then_thread"]), ("ran", "exit:0"))


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class OldIpcTests(unittest.TestCase):
    """B1: System V IPC and POSIX message queues of other processes, on stand-ins this test makes."""

    @classmethod
    def setUpClass(cls):
        cls.control = run_ipc_child(wall=False)
        cls.walled = run_ipc_child(wall=True)

    def test_b1_no_ipc_object_of_another_process_can_be_touched(self):
        control, walled = self.control, self.walled
        self.assertEqual(control["ipc"], dict.fromkeys(control["ipc"], "ok"), "unwalled, every call goes through")
        self.assertEqual(control["_seen"], {"shm_a": "WALLED!", "sem_a": [0, 0], "msq_a_messages": 1,
                                            "msq_c_messages": 0, "shm_b_exists": False, "sem_b_exists": False,
                                            "msq_b_exists": False, "mq_a_exists": False},
                         "unwalled, each stand-in changed: written, taken, filled, emptied, deleted")
        self.assertEqual(walled["apply"]["code"], "applied")
        self.assertEqual(walled["ipc"], dict.fromkeys(control["ipc"], "EPERM"))
        self.assertEqual(walled["_seen"], {"shm_a": "stand-in", "sem_a": [1, 1], "msq_a_messages": 0,
                                           "msq_c_messages": 1, "shm_b_exists": True, "sem_b_exists": True,
                                           "msq_b_exists": True, "mq_a_exists": True},
                         "walled, every stand-in stays whole")


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class LeaseTests(unittest.TestCase):

    def test_b3_no_lease_so_a_writer_opens_at_once(self):
        control, walled = run_lease_child(wall=False), run_lease_child(wall=True)
        self.assertEqual(control["setlease"], "ok")
        self.assertIn(control["_writer"], ("EAGAIN", "EWOULDBLOCK"),
                         "unwalled, a lease on a readable file makes another process's write-open wait")
        self.assertEqual(walled["apply"]["code"], "applied")
        self.assertEqual((walled["setlease"], walled["getlease"], walled["_writer"]), ("EPERM", "ok", "ok"))


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class PidReuseTests(unittest.TestCase):

    def test_b2_a_process_given_the_hosts_old_pid_is_not_reached_by_its_leftover_child(self):
        control = run_pid_reuse(wall=False)
        if "skip" in control:
            self.skipTest(control["skip"])
        walled = run_pid_reuse(wall=True)
        for result in (control, walled):
            self.assertIs(result["victim_has_the_hosts_old_pid"], True)
        self.assertEqual((control["leftover_kill"], control["victim"]), ("ok", -signal.SIGTERM),
                         "unwalled, the leftover child ends whatever process took the host's pid")
        self.assertEqual(walled["host"].get("apply"), "applied")
        self.assertEqual((walled["leftover_kill"], walled["victim"]), (f"errno:{errno.EPERM}", None),
                         "walled, the stand-in that took the host's old pid lives")


class LandlockScopeTests(unittest.TestCase):

    def test_b2plus_the_ruleset_asks_both_scopes_from_abi_6_and_none_below(self):
        self.assertEqual(wall_linux.LANDLOCK_SCOPES, {"abstract_unix_socket": 1, "signal": 2})
        for abi in (1, 2, 3, 4, 5):
            data, scopes = wall_linux._ruleset_attr(abi)
            self.assertEqual((len(data), scopes), (8, []), abi)
        for abi in (6, 7):
            data, scopes = wall_linux._ruleset_attr(abi)
            self.assertEqual(len(data), 24, abi)
            self.assertEqual(struct.unpack("<QQQ", data)[1:], (0, 3), "no network right handled; both scopes")
            self.assertEqual(scopes, ["abstract_unix_socket", "signal"])

    @unittest.skipIf(_why_not_here(), _why_not_here() or "")
    def test_b2plus_apply_wall_asks_the_scopes_and_the_record_names_them(self):
        """A stand-in ABI in this process: the kernel calls are stand-ins too, so nothing is walled here."""
        import ctypes
        for abi, expected, size in ((3, [], 8), (6, ["abstract_unix_socket", "signal"], 24)):
            seen = {}

            def fake_call(libc, number, *args):
                if number == 444:
                    seen["attr"] = ctypes.string_at(args[0], args[1])
                    return os.open(os.devnull, os.O_RDONLY), 0   # a stand-in ruleset descriptor; apply_wall closes it
                if number in (445, 446):
                    return 0, 0
                raise AssertionError(f"an unexpected call {number}")
            stand_ins = dict({fn: _refuse for fn in CHECK_FUNCTIONS.values()}, _write_outcomes=_writes_refused)
            with self.subTest(abi=abi), mock.patch.multiple(
                    wall_linux, _call=fake_call, _landlock_abi=lambda libc: abi, _thread_count=lambda: 1,
                    _threads_now=lambda: 1, _descriptors_open_for_write=lambda: [], _descriptors_unaccounted=lambda: [],
                    _read_kernel_lists=lambda lists: {"descriptors": [], "maps": [], "numbers": []},
                    _no_new_privs=lambda libc: None, _load_seccomp_filter=lambda *a, **k: None,
                    _STATE={"applied": [], "failed": None, "scopes": []}, **stand_ins):
                wall_linux.apply_wall([sys.prefix])
                record = wall_linux.probe_wall("/nonexistent/canary", 1)
                self.assertEqual(len(seen["attr"]), size)
                self.assertEqual(record["landlock_scopes"], expected)
                self.assertIs(record["proven"], True)


HALF_POINTS = {"create": "landlock_create_ruleset", "addrule": "landlock_add_rule", "nnp1": "no_new_privs",
               "restrict": "landlock_restrict_self", "nnp2": "no_new_privs_before_filter", "seccomp": "seccomp_filter",
               "after_load": "seccomp_filter"}


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class HalfAppliedTests(unittest.TestCase):

    def test_a6_each_failing_step_is_refused_wall_partial_and_never_proven(self):
        for point, step in HALF_POINTS.items():
            with self.subTest(point=point):
                result = run_reach_child("half_" + point)
                self.assertEqual(result["apply"], {"code": "wall_partial", "step": step})
                self.assertIs(result["record"]["proven"], False)
                self.assertEqual(result["reapply"], {"code": "wall_partial", "step": step},
                                 "a second apply_wall after a failed step names that step")
                self.assertIs(result["proven_after_reapply"], False)
                if point == "after_load":  # the filter did load: only the failed step keeps it from proven
                    self.assertTrue(all(result["record"][name] for name in wall_linux.RECORD_CHECKS))

    def test_a6_a_second_apply_in_a_walled_process_names_the_cause(self):
        result = run_reach_child("twice")
        self.assertEqual(result["apply"]["code"], "applied")
        self.assertEqual(result["reapply"], {"code": "wall_partial", "step": "already_walled"})
        self.assertIs(result["proven"], True, "the first, whole wall still stands")


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class ProofTests(unittest.TestCase):

    def test_a5_a_weaker_filter_without_apply_wall_is_not_proven(self):
        result = run_reach_child("weak")
        record = result["record"]
        self.assertTrue(all(record[name] for name in
                            ("reads_outside", "home_canary", "program_start", "new_socket", "signal_other")),
                        "the five first checks alone would read this weaker wall as whole")
        self.assertEqual((record.get("signal_other_ways"), record.get("kernel_surface")), (False, False))
        self.assertIs(record["proven"], False)

    def test_a5_a8_a_pid_of_another_user_proves_nothing(self):
        record = run_reach_child("probe_other_user")["record"]
        self.assertEqual((record["signal_other"], record.get("signal_other_ways")), (False, False))
        self.assertIs(record["proven"], False)

    def test_a7_a_native_thread_the_threading_module_does_not_know_is_refused(self):
        result = run_reach_child("threads_native")
        self.assertEqual(result["known_to_threading"], 1, "the stand-in thread is unknown to threading")
        self.assertEqual(result["apply"]["code"], "wall_threads_running")
        self.assertEqual(result["status"], {"NoNewPrivs": "0", "Seccomp": "0"})


CHECK_FUNCTIONS = {"reads_outside": "_read_root", "home_canary": "_read_canary", "program_start": "_start_program",
                   "new_socket": "_open_sockets", "signal_other": "_signal_other",
                   "signal_other_ways": "_reach_other", "metadata_change": "_change_mode",
                   "kernel_surface": "_kernel_surface"}


def _refuse(*_args, **_kwargs):
    raise PermissionError("a stand-in refusal")


def _let_through(*_args, **_kwargs):
    return None


def _writes_refused(*_args, **_kwargs):
    """A stand-in for the write kinds: each refused by Landlock (nothing is tried in this process)."""
    return dict.fromkeys(wall_linux.WRITE_KINDS, "refused_by_landlock")


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class ProvenNeedsEveryCheckTests(unittest.TestCase):
    """In this process, with every check replaced by a stand-in: nothing is walled and no escape is tried."""

    def probe(self, let_through=None, **patches):
        stand_ins = {fn: (_let_through if name == let_through else _refuse) for name, fn in CHECK_FUNCTIONS.items()}
        stand_ins["_write_outcomes"] = _writes_refused
        stand_ins.update(_threads_now=lambda: 1, _descriptors_open_for_write=lambda: [],
                         _maps_open_for_write=lambda: [], _walled_here=lambda: True,
                         _descriptors_unaccounted=lambda: [])  # the runner's own are not judged; as if walled here
        with mock.patch.multiple(wall_linux, create=True, **stand_ins, **patches):
            return wall_linux.probe_wall("/nonexistent/canary", 1)

    def test_the_record_holds_the_eight_checks(self):
        self.assertEqual(wall_linux.RECORD_CHECKS, tuple(CHECK_FUNCTIONS))

    def test_all_refused_is_proven(self):
        self.assertIs(self.probe()["proven"], True)

    def test_a4_any_one_check_not_refused_is_not_proven(self):
        for name in CHECK_FUNCTIONS:
            with self.subTest(check=name):
                record = self.probe(let_through=name)
                self.assertEqual([check for check in CHECK_FUNCTIONS if not record[check]], [name])
                self.assertIs(record["proven"], False)

    def test_a4_no_landlock_or_another_arch_is_not_proven(self):
        self.assertIs(self.probe(_landlock_abi=lambda libc: 0)["proven"], False)
        with mock.patch.object(wall_linux.platform, "machine", lambda: "riscv64"):
            self.assertIs(self.probe()["proven"], False)

    def test_a7_new_socket_needs_both_families_refused(self):
        def fake_socket_module(refused):
            def make(family, kind):
                if family in refused:
                    raise PermissionError("stand-in")
                return types.SimpleNamespace(close=lambda: None)
            return types.SimpleNamespace(AF_INET=socket.AF_INET, AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM,
                                         socket=make)
        for refused, expected in (({socket.AF_INET}, False), ({socket.AF_UNIX}, False),
                                  ({socket.AF_INET, socket.AF_UNIX}, True)):
            with self.subTest(refused=sorted(refused)), mock.patch.object(wall_linux, "socket", fake_socket_module(refused)):
                self.assertIs(wall_linux._refused(wall_linux._open_sockets), expected)


def _run_program(rows, number, args=(0, 0, 0), audit_arch=None, arch="x86_64"):
    """Run the seccomp program on one call as the kernel's classic BPF would: 'allow', 'eperm' or 'enosys'."""
    data = {0: number, 4: wall_linux._AUDIT_ARCH[arch] if audit_arch is None else audit_arch}
    for i, value in enumerate(args):
        data[16 + 8 * i] = value & 0xFFFFFFFF
        data[20 + 8 * i] = (value >> 32) & 0xFFFFFFFF
    answers = {0x7FFF0000: "allow", 0x00050000 | 1: "eperm", 0x00050000 | 38: "enosys"}
    acc, pc = 0, 0
    for _ in range(len(rows) + 1):
        code, jt, jf, k = rows[pc]
        if code == 0x20:
            acc, pc = data[k], pc + 1
        elif code in (0x15, 0x35, 0x45):
            hit = acc == k if code == 0x15 else (acc >= k if code == 0x35 else bool(acc & k))
            pc += 1 + (jt if hit else jf)
        elif code == 0x06:
            return answers[k]
        else:
            raise AssertionError(f"an instruction this program should not hold: {code:#x}")
    raise AssertionError("the program did not end")


class FilterProgramTests(unittest.TestCase):
    """The program itself, for both arches, read by a small interpreter: nothing is loaded."""

    OWN, OTHER = 4242, 4243

    def program(self, arch, abi=3):
        return wall_linux._filter(arch, abi)

    def test_the_added_entries_are_the_walls_own_definition(self):
        self.assertEqual(wall_linux.DENIED_METADATA_CALLS, (
            "chmod", "fchmod", "fchmodat", "fchmodat2", "chown", "fchown", "lchown", "fchownat", "utime", "utimes",
            "futimesat", "utimensat", "setxattr", "lsetxattr", "fsetxattr", "setxattrat", "removexattr",
            "lremovexattr", "fremovexattr", "removexattrat", "file_setattr"))
        self.assertEqual(wall_linux.DENIED_SIGNAL_CALLS, ("kill", "tkill", "tgkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo"))
        self.assertEqual(wall_linux.DENIED_IPC_CALLS, (
            "shmget", "shmat", "shmdt", "shmctl", "semget", "semop", "semtimedop", "semctl", "msgget", "msgsnd",
            "msgrcv", "msgctl", "mq_open", "mq_unlink", "mq_timedsend", "mq_timedreceive", "mq_notify", "mq_getsetattr"))
        self.assertEqual(wall_linux.CALLER_ONLY_CALLS, ("prlimit64", "sched_setaffinity", "sched_setscheduler",
                                                        "sched_setparam", "sched_setattr", "migrate_pages", "move_pages"))
        self.assertEqual(wall_linux.CALLER_ONLY_PRIORITY_CALLS, ("setpriority", "ioprio_set"))
        self.assertEqual(wall_linux.DENIED_FCNTL_COMMANDS, {"F_SETOWN": 8, "F_SETOWN_EX": 15, "F_SETLEASE": 1024,
                                                            "F_SETSIG": 10, "F_NOTIFY": 1026})
        self.assertEqual(wall_linux.DENIED_IOCTL_COMMANDS, {"FIOSETOWN": 0x8901, "SIOCSPGRP": 0x8902,
                                                            "FS_IOC_SETFLAGS": 0x40086602, "FS_IOC_FSSETXATTR": 0x401C5820})
        self.assertEqual(wall_linux.NAMESPACE_CLONE_FLAGS, sum(CLONE_NEW.values()))
        self.assertEqual(wall_linux.OLD_LANDLOCK_DENIED_CALLS, ("truncate", "ftruncate", "openat2"))
        for arch in ("x86_64", "aarch64"):
            with self.subTest(arch=arch):
                for name, number in NUM[arch].items():
                    if name in wall_linux._NR_MORE[arch]:
                        self.assertEqual(wall_linux._NR_MORE[arch][name], number, name)

    def test_a1_signals_limits_priorities_and_owners_only_for_itself(self):
        for arch in ("x86_64", "aarch64"):
            rows, n = self.program(arch), NUM[arch]
            with self.subTest(arch=arch):
                run = lambda name, *args: _run_program(rows, n[name], args + (0,) * (3 - len(args)), arch=arch)
                for name in ("kill", "tkill", "tgkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo"):
                    self.assertEqual([run(name, pid) for pid in (self.OWN, self.OTHER, 0, -1)], ["eperm"] * 4, name)
                for name in ("prlimit64", "sched_setaffinity", "sched_setscheduler", "sched_setparam", "sched_setattr",
                             "migrate_pages", "move_pages"):
                    self.assertEqual((run(name, 0), run(name, self.OWN), run(name, self.OTHER)), ("allow", "eperm", "eperm"), name)
                self.assertEqual([run("setpriority", which, who) for which, who in
                                  ((0, 0), (0, self.OWN), (0, self.OTHER), (1, 0), (2, 0))],
                                 ["allow", "eperm", "eperm", "eperm", "eperm"], "PRIO_PROCESS only, by 0 alone")
                self.assertEqual([run("ioprio_set", which, who) for which, who in
                                  ((1, 0), (1, self.OWN), (1, self.OTHER), (2, 0), (3, 0))],
                                 ["allow", "eperm", "eperm", "eperm", "eperm"], "IOPRIO_WHO_PROCESS only, by 0 alone")
                self.assertEqual([run("fcntl", 3, cmd) for cmd in (8, 15, 1024, 1, 3, 4, 10, 1025)],
                                 ["eperm"] * 3 + ["allow"] * 3 + ["eperm", "allow"],
                                 "F_SETOWN, F_SETOWN_EX, F_SETLEASE and F_SETSIG only; F_SETFL without O_ASYNC stays")
                self.assertEqual((run("fcntl", 3, 1026), run("fcntl", 3, 4, 0o20000), run("fcntl", 3, 4, 0o4000)),
                                 ("eperm", "eperm", "allow"), "F_NOTIFY, and F_SETFL asking O_ASYNC, are refused")
                self.assertEqual([run("ioctl", 3, cmd) for cmd in (0x8901, 0x8902, 0x40086602, 0x401C5820, 0x5401, 0x80086601)],
                                 ["eperm", "eperm", "eperm", "eperm", "allow", "allow"])

    def test_b1_b2_every_ipc_and_signal_call_is_refused_whatever_its_arguments(self):
        for arch in ("x86_64", "aarch64"):
            rows, n = self.program(arch), NUM[arch]
            with self.subTest(arch=arch):
                for name in wall_linux.DENIED_IPC_CALLS + wall_linux.DENIED_SIGNAL_CALLS:
                    for first in (0, self.OWN, self.OTHER, -1):
                        self.assertEqual(_run_program(rows, n[name], (first, 0, 0), arch=arch), "eperm", name)

    def test_o2_clone_with_a_namespace_flag_is_refused_and_clone3_falls_back(self):
        for arch in ("x86_64", "aarch64"):
            rows, n = self.program(arch), NUM[arch]
            with self.subTest(arch=arch):
                for flag, value in CLONE_NEW.items():
                    self.assertEqual(_run_program(rows, n["clone"], (value | 17, 0, 0), arch=arch), "eperm", flag)
                thread_flags, fork_flags = 0x003D0F00, 0x01200011
                self.assertEqual(_run_program(rows, n["clone"], (thread_flags, 0, 0), arch=arch), "allow")
                self.assertEqual(_run_program(rows, n["clone"], (fork_flags, 0, 0), arch=arch), "allow")
                self.assertEqual(_run_program(rows, n["clone3"], arch=arch), "enosys")

    def test_a2_every_metadata_call_is_refused(self):
        for arch in ("x86_64", "aarch64"):
            rows = self.program(arch)
            with self.subTest(arch=arch):
                for name in wall_linux.DENIED_METADATA_CALLS:
                    if name in NUM[arch]:
                        self.assertEqual(_run_program(rows, NUM[arch][name], arch=arch), "eperm", name)

    def test_a3_below_abi_3_truncation_is_refused_and_from_abi_3_landlock_does_it(self):
        o_trunc = 0o1000
        for arch in ("x86_64", "aarch64"):
            n = NUM[arch]
            with self.subTest(arch=arch):
                old, new = self.program(arch, 2), self.program(arch, 3)
                self.assertEqual([_run_program(old, n["truncate"], arch=arch), _run_program(old, n["openat2"], arch=arch),
                                  _run_program(old, n["openat"], (0, 0, o_trunc), arch=arch),
                                  _run_program(old, n["openat"], (0, 0, 0), arch=arch)], ["eperm", "eperm", "eperm", "allow"])
                if "open" in n:
                    self.assertEqual([_run_program(old, n["open"], (0, o_trunc), arch=arch),
                                      _run_program(old, n["open"], (0, 0), arch=arch)], ["eperm", "allow"])
                self.assertEqual([_run_program(new, n["truncate"], arch=arch),
                                  _run_program(new, n["openat"], (0, 0, o_trunc), arch=arch)], ["allow", "allow"])

    def test_a4_another_arch_is_refused_for_a_number_the_list_does_not_hold(self):
        for arch in ("x86_64", "aarch64"):
            rows = self.program(arch)
            with self.subTest(arch=arch):
                self.assertEqual(_run_program(rows, NUM[arch]["read"], arch=arch), "allow")
                self.assertEqual(_run_program(rows, NUM[arch]["read"], audit_arch=0x40000003, arch=arch), "eperm")
                self.assertEqual(_run_program(rows, NUM[arch]["read"], audit_arch=0x40000028, arch=arch), "eperm")
                if arch == "x86_64":
                    self.assertEqual(_run_program(rows, 0x40000000 | NUM[arch]["getpid"], arch=arch), "eperm")


if __name__ == "__main__":
    unittest.main()
