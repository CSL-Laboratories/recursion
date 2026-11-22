"""What a walled process can still reach: other processes, the metadata of files, an older kernel, new
namespaces, a half-applied wall, and whether the proof proves this wall.

Every walled case runs in a CHILD interpreter (``-I``, the runner's own ``-X`` options) with its own
temporary folder, stand-in files and stand-in processes (a 'daemon' and three victims) that the test starts
itself and ends; nothing here walls the test runner, and nothing is aimed at a process or file the test did
not make. Each walled case has an unwalled control in the same child code, so a test shows that the call
works without the wall and is refused with it. Cases that need only the module's own logic run in this
process with stand-ins and wall nothing.
"""
from __future__ import annotations

import json
import os
import platform
import signal
import socket
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

# The kernel's numbers for the calls these tests make by number, written here again so that the tests do
# not take them from the module they check (x86_64: syscall_64.tbl; aarch64: asm-generic/unistd.h).
NUM = {
    "x86_64": {"rt_sigqueueinfo": 129, "rt_tgsigqueueinfo": 297, "sched_setattr": 314, "ioprio_set": 251,
               "migrate_pages": 256, "move_pages": 279, "chmod": 90, "fchmod": 91, "chown": 92, "fchown": 93,
               "lchown": 94, "utime": 132, "utimes": 235, "futimesat": 261, "fchownat": 260, "fchmodat": 268,
               "fchmodat2": 452, "utimensat": 280, "setxattr": 188, "lsetxattr": 189, "fsetxattr": 190,
               "removexattr": 197, "lremovexattr": 198, "fremovexattr": 199, "setxattrat": 463,
               "removexattrat": 466, "file_setattr": 469, "truncate": 76, "openat2": 437, "clone": 56,
               "clone3": 435, "openat": 257, "open": 2, "fcntl": 72, "ioctl": 16, "prlimit64": 302,
               "setpriority": 141, "sched_setaffinity": 203, "sched_setscheduler": 144, "sched_setparam": 142,
               "read": 0, "getpid": 39},
    "aarch64": {"rt_sigqueueinfo": 138, "rt_tgsigqueueinfo": 240, "sched_setattr": 274, "ioprio_set": 30,
                "migrate_pages": 238, "move_pages": 239, "fchmod": 52, "fchown": 55, "fchownat": 54,
                "fchmodat": 53, "fchmodat2": 452, "utimensat": 88, "setxattr": 5, "lsetxattr": 6, "fsetxattr": 7,
                "removexattr": 14, "lremovexattr": 15, "fremovexattr": 16, "setxattrat": 463,
                "removexattrat": 466, "file_setattr": 469, "truncate": 45, "openat2": 437, "clone": 220,
                "clone3": 435, "openat": 56, "fcntl": 25, "ioctl": 29, "prlimit64": 261, "setpriority": 140,
                "sched_setaffinity": 122, "sched_setscheduler": 119, "sched_setparam": 118, "read": 63,
                "getpid": 172},
}
CLONE_NEW = {"NEWNS": 0x00020000, "NEWCGROUP": 0x02000000, "NEWUTS": 0x04000000, "NEWIPC": 0x08000000,
             "NEWUSER": 0x10000000, "NEWPID": 0x20000000, "NEWNET": 0x40000000}

CHILD = r'''
import ctypes, errno, fcntl, json, mmap, os, platform, resource, signal, socket, struct, sys, threading, _thread
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
    a1["setpriority_daemon"] = t(lambda: os.setpriority(os.PRIO_PROCESS, daemon, 19))
    a1["sched_setaffinity_daemon"] = t(lambda: os.sched_setaffinity(daemon, os.sched_getaffinity(0)))
    a1["sched_setscheduler_daemon"] = t(lambda: os.sched_setscheduler(daemon, os.SCHED_OTHER, os.sched_param(0)))
    a1["sched_setparam_daemon"] = t(lambda: os.sched_setparam(daemon, os.sched_param(0)))
    attr, pattr = buf(struct.pack("<IIQiIQQQ", 48, 0, 0, 19, 0, 0, 0, 0))  # nice 19: the control may set it unprivileged
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
    own["prlimit_self_pid"] = res("prlimit64", me, 0, 0, pold)
    own["getrlimit"] = t(lambda: resource.getrlimit(resource.RLIMIT_NOFILE))
    own["setpriority_self"] = t(lambda: os.setpriority(os.PRIO_PROCESS, 0, os.getpriority(os.PRIO_PROCESS, 0)))
    own["sched_setaffinity_self"] = t(lambda: os.sched_setaffinity(0, os.sched_getaffinity(0)))
    own["sched_setscheduler_self"] = t(lambda: os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0)))
    own["rt_sigqueueinfo_self_0"] = res("rt_sigqueueinfo", me, 0, pzero)
    own["move_pages_self"] = res("move_pages", 0, 0, 0, 0, 0, 0)
    own["kill_self_0"] = t(lambda: os.kill(me, 0))
    out["own"] = own
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
    out["record"] = w.probe_wall(spec["canary"], daemon)

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
    out["record"] = w.probe_wall(spec["canary"], daemon)
    out["reapply"] = apply(paths)
    out["proven_after_reapply"] = w.probe_wall(spec["canary"], daemon)["proven"]

elif mode == "twice":
    out["apply"] = apply(paths)
    out["reapply"] = apply(paths)
    out["proven"] = w.probe_wall(spec["canary"], daemon)["proven"]

elif mode == "weak":
    lc, arch = w._libc(), w._arch()
    opened = w._open_paths(paths)
    w._apply_landlock(lc, w._landlock_abi(lc), opened)
    for fd_, _ in opened:
        os.close(fd_)
    w.DENIED_CALLS = ("socket", "socketpair", "execve", "execveat")
    for name, empty in (("DENIED_METADATA_CALLS", ()), ("SELF_ONLY_QUEUE_CALLS", ()), ("SELF_OR_ZERO_CALLS", ()),
                        ("DENIED_FCNTL_COMMANDS", {}), ("DENIED_IOCTL_COMMANDS", {})):
        if hasattr(w, name):
            setattr(w, name, empty)
    w._load_seccomp_filter(lc, arch)
    out["record"] = w.probe_wall(spec["canary"], daemon)
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

print(json.dumps(out), flush=True)
'''


def _start(args, folder):
    return subprocess.Popen(_interpreter() + args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, cwd=folder, env={"TMPDIR": folder})


def run_reach_child(mode: str, **extra) -> dict:
    """One child in ``mode``; returns its JSON line plus what the parent saw afterwards ('_victims',
    '_files', '_answer', '_daemon_nice')."""
    with tempfile.TemporaryDirectory() as folder:
        home = os.path.join(folder, "home")
        os.mkdir(home)
        canary = os.path.join(home, "canary")
        with open(canary, "w", encoding="utf-8") as handle:
            handle.write("the daemon's secret, stand-in")
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
        victims = [_start(["-c", "import time; time.sleep(300)"], folder),
                   _start(["-c", "import time; time.sleep(300)"], folder),
                   _start(["-c", "while True: pass"], folder)]
        ours, theirs = socket.socketpair()
        handed_here, handed_there = socket.socketpair()
        metafd = os.open(files["metafd"], os.O_RDONLY)
        try:
            spec = dict({"core": str(CORE), "mode": mode, "canary": canary, "daemon": daemon.pid, "dir": folder,
                         "files": files, "ro": ro, "victims": [v.pid for v in victims], "chan": theirs.fileno(),
                         "metafd": metafd, "num": NUM, "paths": sorted({sys.base_prefix, sys.prefix})}, **extra)
            socket.send_fds(ours, [b"ping"], [handed_there.fileno()])
            handed_there.close()
            time.sleep(1.2)  # the spinning victim has used more than one second of CPU before any limit is set
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
            daemon_nice = os.getpriority(os.PRIO_PROCESS, daemon.pid)
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
    result.update(_victims=seen_victims, _files=seen_files, _answer=answer, _daemon_nice=daemon_nice)
    return result


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
        self.assertEqual(walled["_daemon_nice"], 0, "the stand-in daemon's priority is unchanged")
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

    def test_python_keeps_working_and_the_record_is_proven(self):
        walled = self.walled
        self.assertEqual(walled["keeps_working"], {"fresh_import": True, "pipe": "question"})
        self.assertEqual(walled["_answer"], "answer:ping", "the socket handed over by recvmsg answers")
        self.assertIs(walled["record"]["proven"], True)
        self.assertEqual(walled["status"], {"NoNewPrivs": "1", "Seccomp": "2"})


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


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class ProvenNeedsEveryCheckTests(unittest.TestCase):
    """In this process, with every check replaced by a stand-in: nothing is walled and no escape is tried."""

    def probe(self, let_through=None, **patches):
        stand_ins = {fn: (_let_through if name == let_through else _refuse) for name, fn in CHECK_FUNCTIONS.items()}
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
        return wall_linux._filter(arch, self.OWN, abi)

    def test_the_added_entries_are_the_walls_own_definition(self):
        self.assertEqual(wall_linux.DENIED_METADATA_CALLS, (
            "chmod", "fchmod", "fchmodat", "fchmodat2", "chown", "fchown", "lchown", "fchownat", "utime", "utimes",
            "futimesat", "utimensat", "setxattr", "lsetxattr", "fsetxattr", "setxattrat", "removexattr",
            "lremovexattr", "fremovexattr", "removexattrat", "file_setattr"))
        self.assertEqual(wall_linux.SELF_ONLY_QUEUE_CALLS, ("rt_sigqueueinfo", "rt_tgsigqueueinfo"))
        self.assertEqual(wall_linux.SELF_OR_ZERO_CALLS, ("prlimit64", "sched_setaffinity", "sched_setscheduler",
                                                         "sched_setparam", "sched_setattr", "migrate_pages", "move_pages"))
        self.assertEqual(wall_linux.SELF_PROCESS_CALLS, ("setpriority", "ioprio_set"))
        self.assertEqual(wall_linux.DENIED_FCNTL_COMMANDS, {"F_SETOWN": 8, "F_SETOWN_EX": 15})
        self.assertEqual(wall_linux.DENIED_IOCTL_COMMANDS, {"FIOSETOWN": 0x8901, "SIOCSPGRP": 0x8902,
                                                            "FS_IOC_SETFLAGS": 0x40086602, "FS_IOC_FSSETXATTR": 0x401C5820})
        self.assertEqual(wall_linux.NAMESPACE_CLONE_FLAGS, sum(CLONE_NEW.values()))
        self.assertEqual(wall_linux.OLD_LANDLOCK_DENIED_CALLS, ("truncate", "openat2"))
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
                for name in ("rt_sigqueueinfo", "rt_tgsigqueueinfo"):
                    self.assertEqual((run(name, self.OWN), run(name, self.OTHER), run(name, 0)), ("allow", "eperm", "eperm"))
                for name in ("prlimit64", "sched_setaffinity", "sched_setscheduler", "sched_setparam", "sched_setattr",
                             "migrate_pages", "move_pages"):
                    self.assertEqual((run(name, 0), run(name, self.OWN), run(name, self.OTHER)), ("allow", "allow", "eperm"), name)
                self.assertEqual([run("setpriority", which, who) for which, who in
                                  ((0, 0), (0, self.OWN), (0, self.OTHER), (1, 0), (2, 0))],
                                 ["allow", "allow", "eperm", "eperm", "eperm"], "PRIO_PROCESS only, for itself")
                self.assertEqual([run("ioprio_set", which, who) for which, who in
                                  ((1, 0), (1, self.OWN), (1, self.OTHER), (2, 0), (3, 0))],
                                 ["allow", "allow", "eperm", "eperm", "eperm"], "IOPRIO_WHO_PROCESS only, for itself")
                self.assertEqual([run("fcntl", 3, cmd) for cmd in (8, 15, 1, 3, 4, 10)],
                                 ["eperm", "eperm", "allow", "allow", "allow", "allow"], "F_SETOWN and F_SETOWN_EX only")
                self.assertEqual([run("ioctl", 3, cmd) for cmd in (0x8901, 0x8902, 0x40086602, 0x401C5820, 0x5401, 0x80086601)],
                                 ["eperm", "eperm", "eperm", "eperm", "allow", "allow"])

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
