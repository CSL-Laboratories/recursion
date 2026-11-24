"""What Linux and WSL2 do with links in a held-folder walk, with real links and a real swap.

These are the system facts the anchored-path module will rest on, run before the module exists.
Each case runs on its own system and skips elsewhere with the reason; no case passes where it did
not run. Every case uses real links, a real second thread or a real user namespace, never a mock,
and keeps every path inside one temporary folder it makes itself (W5 alone needs a folder on the
Windows side, see its case). What a case only records (numbers, the shape of today's walk under a
swap) it prints as one ``FACT {...}`` line on stderr and never asserts; FACTS.md beside the package
holds those lines per system and interpreter.

- L1: a walk that opens each part with ``O_NOFOLLOW|O_DIRECTORY`` through the held parent refuses a
  link in a middle part; one ``os.open`` of a multi-part name under the held folder does not
  (``O_NOFOLLOW`` guards the last name only).
- L2: ``openat2`` with ``RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS`` refuses a middle link (ELOOP) and a
  ``..`` escape (EXDEV) and opens a plain path; the EAGAIN count and the time per open against the
  per-part walk are recorded.
- L3: a second thread swaps a middle folder of a 5-part path with a link to an outside folder and
  back while this thread makes a file there 2,000 times: (i) the shape of today's walk (``islink`` on
  each prefix, then ``mkdir`` and an exclusive open by path) is recorded; (ii) the held walk lands
  no file outside (asserted).
- L4, L5: inside a user namespace (``unshare -r``, no root on the host), an ostree-shaped layout
  (``/home -> var/home``) passes the owner check above the folder, and a link above the folder that
  another uid owns refuses. The check is a copy of the trust store's ``_check_ancestors`` rule.
- W5: on WSL2 with interop, a junction made by ``cmd.exe /c mklink /J`` is a link to Linux ``lstat``
  and the per-part walk refuses it. It starts a Windows program, so it runs only when the person
  running the tests sets ``RECURSION_FACTS_WSL_INTEROP=1``.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

LINUX = os.name == "posix" and sys.platform.startswith("linux")
NOT_LINUX = (f"a Linux and WSL2 fact; this host is {sys.platform} (os.name {os.name}): "
             "the real Windows and macOS rows come from their own runs")
PARTS = ("p1", "p2", "p3", "p4", "p5")  # the 5-part name of L2 and L3; p3 is the middle part
L2_OPENS = 10_000
L3_ROUNDS = 2_000
W5_OPT_IN = "RECURSION_FACTS_WSL_INTEROP"
# openat2 has one number on every Linux machine that took the shared syscall table (kernel 5.6);
# a machine missing here is skipped with its name, never guessed.
OPENAT2_NUMBERS = {"x86_64": 437, "aarch64": 437, "arm64": 437, "armv7l": 437, "armv8l": 437, "i386": 437,
                   "i686": 437, "riscv64": 437, "ppc64le": 437, "ppc64": 437, "s390x": 437, "loongarch64": 437,
                   "mips64": 437}
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
RENAME_EXCHANGE = 2
AT_FDCWD = -100


def _fact(case: str, **values: object) -> None:
    """One recorded fact, never asserted: one line on stderr that FACTS.md is filled from."""
    row = {"case": case, "python": sys.version.split()[0], "gil": _gil(), "machine": os.uname().machine,
           "kernel": os.uname().release, **values}
    sys.stderr.write("FACT " + json.dumps(row, sort_keys=True) + "\n")
    sys.stderr.flush()


def _gil() -> str:
    check = getattr(sys, "_is_gil_enabled", None)
    return "on" if check is None or check() else "off"


def _walk_flags() -> int:
    # Read when called: a Windows interpreter has no O_NOFOLLOW or O_DIRECTORY, and the module must import there.
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _hold(path: object) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def held_walk(anchor: int, parts: tuple, *, make: bool = False) -> int:
    """Open each part through the held parent, one name at a time; a link in any part raises OSError.

    With ``make`` a missing part is made through the held parent first. Returns a new descriptor of
    the last folder; ``anchor`` stays open and is the caller's.
    """
    fd = os.dup(anchor)
    try:
        for part in parts:
            if make:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            following = os.open(part, _walk_flags(), dir_fd=fd)
            os.close(fd)
            fd = following
    except BaseException:
        os.close(fd)
        raise
    return fd


def _count_files(top: object) -> int:
    return sum(len(files) for _, _, files in os.walk(top))  # os.walk follows no link


# -- the owner check above the folder, copied from the trust store's _check_ancestors rule ----------
# A copy, not an import: the rule lives only in an open draft (plugin_security/stores.py,
# _check_ancestors, _lstat, _trusted_folder, at 80b832f5). The statements are kept line for line; the
# docstring is shortened and the error type is local.

_UNSAFE_BITS = stat.S_IWGRP | stat.S_IWOTH
_MAX_LINKS = 40  # Linux's MAXSYMLINKS: the links one path lookup follows before ELOOP


class Refused(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _check_ancestors(folder: str, prefix: str) -> None:
    """Refuse a path that another user could make name something else above the folder."""
    if os.name != "posix":
        return  # no POSIX owners here (Windows)
    trusted = (0, os.geteuid())
    names = [name for name in os.path.dirname(folder).split("/") if name]
    current, links = "/", 0
    _trusted_folder("/", _lstat("/", prefix), trusted, prefix)
    while names:
        # ``current`` holds no link, so ``current/..`` and ``current/.`` name what the kernel's own
        # resolution reaches there, and they are checked like any other name.
        candidate = os.path.join(current, names.pop(0))
        info = _lstat(candidate, prefix)
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid not in trusted:
                raise Refused(f"{prefix}_untrusted", f"{candidate} is a link another user owns")
            links += 1
            if links > _MAX_LINKS:
                raise Refused(f"{prefix}_untrusted", f"more than {_MAX_LINKS} links above the store's folder")
            try:
                target = os.readlink(candidate)
            except FileNotFoundError:  # removed since the lstat above
                raise Refused(f"{prefix}_missing", candidate) from None
            except OSError as exc:  # replaced by something that is not a link, or unreadable
                raise Refused(f"{prefix}_invalid", f"{candidate}: {type(exc).__name__}") from None
            if target.startswith("/"):
                current = "/"
            names[:0] = [part for part in target.split("/") if part]
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise Refused(f"{prefix}_untrusted", f"{candidate} is not a folder")
        _trusted_folder(candidate, info, trusted, prefix)
        current = candidate


def _lstat(path: str, prefix: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        raise Refused(f"{prefix}_missing", path) from None
    except OSError as exc:
        raise Refused(f"{prefix}_invalid", f"{path}: {type(exc).__name__}") from None


def _trusted_folder(path: str, info: os.stat_result, trusted: tuple, prefix: str) -> None:
    if info.st_uid not in trusted:
        raise Refused(f"{prefix}_untrusted", f"{path} is owned by another user")
    if info.st_mode & _UNSAFE_BITS and not info.st_mode & stat.S_ISVTX:
        raise Refused(f"{prefix}_untrusted", f"{path} is writable by group or world and not sticky")


# -- L1 -------------------------------------------------------------------------------------------

@unittest.skipUnless(LINUX, NOT_LINUX)
class L1PerPartWalkTests(unittest.TestCase):
    def test_l1_the_per_part_walk_refuses_a_middle_link_and_one_multi_part_open_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            held, outside = Path(tmp, "held"), Path(tmp, "outside")
            (held / "a").mkdir(parents=True)
            (outside / "c").mkdir(parents=True)
            os.symlink(outside, held / "a" / "link")
            anchor = _hold(held)
            try:
                with self.assertRaises(OSError) as caught:
                    os.close(held_walk(anchor, ("a", "link", "c")))
                walk_errno = caught.exception.errno
                self.assertIn(walk_errno, (errno.ELOOP, errno.ENOTDIR))
                one = os.open("a/link/c", _walk_flags(), dir_fd=anchor)
                try:
                    self.assertEqual(os.fstat(one).st_ino, os.stat(outside / "c").st_ino)
                finally:
                    os.close(one)
            finally:
                os.close(anchor)
            _fact("L1", per_part_walk=f"refused {errno.errorcode[walk_errno]}",
                  one_multi_part_open="opened the folder behind the link")


# -- L2 -------------------------------------------------------------------------------------------

class _OpenAt2:
    """openat2(2) through ctypes; raises unittest.SkipTest where the machine or the kernel has none."""

    def __init__(self) -> None:
        import ctypes  # here, not at import: the module must import on a Windows interpreter

        self.number = OPENAT2_NUMBERS.get(os.uname().machine)
        if self.number is None:
            raise unittest.SkipTest(f"no openat2 number known for the machine {os.uname().machine!r}")

        class OpenHow(ctypes.Structure):
            _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64), ("resolve", ctypes.c_uint64)]

        self._ctypes, self._how_type = ctypes, OpenHow
        self._syscall = ctypes.CDLL(None, use_errno=True).syscall
        self._syscall.restype = ctypes.c_long
        self._syscall.argtypes = [ctypes.c_long, ctypes.c_int, ctypes.c_char_p, ctypes.POINTER(OpenHow),
                                  ctypes.c_size_t]

    def open(self, dirfd: int, name: str, flags: int, resolve: int) -> int:
        how = self._how_type(flags=flags, mode=0, resolve=resolve)
        fd = self._syscall(self.number, dirfd, os.fsencode(name), self._ctypes.byref(how),
                           self._ctypes.sizeof(how))
        if fd < 0:
            code = self._ctypes.get_errno()
            raise OSError(code, os.strerror(code), name)
        return int(fd)


@unittest.skipUnless(LINUX, NOT_LINUX)
class L2OpenAt2Tests(unittest.TestCase):
    def test_l2_openat2_refuses_a_middle_link_and_an_escape_and_opens_a_plain_path(self) -> None:
        call = _OpenAt2()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        resolve = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS
        with tempfile.TemporaryDirectory() as tmp:
            held, outside = Path(tmp, "held"), Path(tmp, "outside")
            held.joinpath(*PARTS).mkdir(parents=True)
            (outside / "c").mkdir(parents=True)
            (held / "a").mkdir()
            os.symlink(outside, held / "a" / "link")
            anchor = _hold(held)
            try:
                try:
                    os.close(call.open(anchor, "/".join(PARTS), flags, resolve))
                except OSError as exc:
                    if exc.errno in (errno.ENOSYS, errno.EPERM):
                        raise unittest.SkipTest(f"openat2 answered {errno.errorcode[exc.errno]} on kernel "
                                                f"{os.uname().release}") from None
                    raise
                with self.assertRaises(OSError) as link:
                    os.close(call.open(anchor, "a/link/c", flags, resolve))
                self.assertEqual(errno.ELOOP, link.exception.errno)
                with self.assertRaises(OSError) as escape:
                    os.close(call.open(anchor, "../outside", flags, resolve))
                self.assertEqual(errno.EXDEV, escape.exception.errno)
                plain = call.open(anchor, "/".join(PARTS), flags, resolve)
                try:
                    self.assertEqual(os.fstat(plain).st_ino, os.stat(held.joinpath(*PARTS)).st_ino)
                finally:
                    os.close(plain)
                # Recorded, never asserted: EAGAIN answers and the time per open, against the per-part walk.
                eagain, other, openat2_ns, walk_ns = 0, {}, [], []
                for _ in range(L2_OPENS):
                    start = time.perf_counter_ns()
                    try:
                        fd = call.open(anchor, "/".join(PARTS), flags, resolve)
                    except OSError as exc:
                        if exc.errno == errno.EAGAIN:
                            eagain += 1
                        else:
                            other[errno.errorcode.get(exc.errno, str(exc.errno))] = \
                                other.get(errno.errorcode.get(exc.errno, str(exc.errno)), 0) + 1
                        continue
                    openat2_ns.append(time.perf_counter_ns() - start)
                    os.close(fd)
                for _ in range(L2_OPENS):
                    start = time.perf_counter_ns()
                    fd = held_walk(anchor, PARTS)
                    walk_ns.append(time.perf_counter_ns() - start)
                    os.close(fd)
            finally:
                os.close(anchor)
            _fact("L2", openat2_number=call.number, middle_link="ELOOP", dotdot_escape="EXDEV", plain="opened",
                  opens=L2_OPENS, eagain=eagain, other_errors=other,
                  median_ns_openat2=statistics.median(openat2_ns) if openat2_ns else None,
                  median_ns_per_part_walk=statistics.median(walk_ns), parts=len(PARTS))


# -- L3 -------------------------------------------------------------------------------------------

def _exchanger():
    """renameat2(RENAME_EXCHANGE) from the C library; raises unittest.SkipTest where there is none."""
    import ctypes  # here, not at import: the module must import on a Windows interpreter

    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    if function is None:
        raise unittest.SkipTest("this C library has no renameat2, so the swap cannot be atomic")
    function.restype = ctypes.c_int
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]

    def exchange(first: Path, second: Path) -> None:
        if function(AT_FDCWD, os.fsencode(first), AT_FDCWD, os.fsencode(second), RENAME_EXCHANGE) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(first))

    return exchange


def _make_by_path(base: Path, index: int) -> bool:
    """The shape the walk of today has: islink on each prefix, then mkdir and an exclusive open by path."""
    prefix = base
    for part in PARTS:
        prefix = prefix / part
        if os.path.islink(prefix):
            return False
    folder = base.joinpath(*PARTS)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd = os.open(folder / f"f{index}", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, b"x")
    finally:
        os.close(fd)
    return True


def _make_by_held_walk(base: Path, index: int, anchor: int) -> bool:
    """Each part through the held parent; the file is made relative to the held folder."""
    folder = held_walk(anchor, PARTS, make=True)
    try:
        fd = os.open(f"f{index}", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                     dir_fd=folder)
    finally:
        os.close(folder)
    try:
        os.write(fd, b"x")
    finally:
        os.close(fd)
    return True


def _swap_run(maker) -> dict:
    """Run ``maker`` L3_ROUNDS times while a second thread swaps p3 with a link to an outside folder."""
    exchange = _exchanger()
    with tempfile.TemporaryDirectory() as tmp:
        base, outside = Path(tmp, "base"), Path(tmp, "outside")
        base.joinpath(*PARTS).mkdir(parents=True)
        outside.joinpath(*PARTS[3:]).mkdir(parents=True)  # the link's target holds p4/p5, so a path through it resolves
        middle = base.joinpath(*PARTS[:3])
        spare = middle.with_name(middle.name + ".swap")
        os.symlink(outside, spare)
        stop, swaps, swap_errors = threading.Event(), [0], [0]

        def swapper() -> None:
            while not stop.is_set():
                try:
                    exchange(middle, spare)
                    swaps[0] += 1
                except OSError:
                    swap_errors[0] += 1

        anchor = _hold(base)
        made = refused = failed = 0
        thread = threading.Thread(target=swapper, name="l3-swapper")
        thread.start()
        try:
            for index in range(L3_ROUNDS):
                try:
                    if maker(base, index, anchor):
                        made += 1
                    else:
                        refused += 1
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):  # the held walk met the link and refused
                        refused += 1
                    else:
                        failed += 1
        finally:
            stop.set()
            thread.join()
            os.close(anchor)
        if os.path.islink(middle):  # leave the real folder at its own name
            exchange(middle, spare)
        outside_files = _count_files(outside)
        inside_files = _count_files(base)
        return {"rounds": L3_ROUNDS, "made": made, "refused": refused, "errors": failed, "swaps": swaps[0],
                "swap_errors": swap_errors[0], "landed_outside": outside_files, "landed_inside": inside_files,
                "middle_is_folder_after": middle.is_dir() and not middle.is_symlink()}


@unittest.skipUnless(LINUX, NOT_LINUX)
class L3SwapTests(unittest.TestCase):
    def test_l3_ii_the_held_walk_lands_no_file_outside_while_a_second_thread_swaps(self) -> None:
        result = _swap_run(lambda base, index, anchor: _make_by_held_walk(base, index, anchor))
        _fact("L3-ii", shape="held walk", **result)
        self.assertTrue(result["middle_is_folder_after"])
        self.assertGreater(result["swaps"], 0)
        self.assertEqual(0, result["landed_outside"])

    def test_l3_i_the_shape_of_today_is_recorded_as_it_comes(self) -> None:
        result = _swap_run(lambda base, index, anchor: _make_by_path(base, index))
        note = "the gap was not shown on this host" if result["landed_outside"] == 0 else "files landed outside"
        _fact("L3-i", shape="islink on each prefix, then mkdir and an exclusive open by path", note=note, **result)
        self.assertTrue(result["middle_is_folder_after"])  # the run's own bookkeeping; the count is only recorded
        self.assertEqual(L3_ROUNDS, result["made"] + result["refused"] + result["errors"])


# -- L4, L5 ---------------------------------------------------------------------------------------

# Runs inside `unshare -r`: it builds an ostree-shaped root, chroots into it (the user namespace gives
# CAP_SYS_CHROOT over it) and runs the copied check from there. The module is imported before the chroot.
_NAMESPACE_CHILD = r"""
import errno, json, os, sys
sys.path.insert(0, sys.argv[1])
from recursion_core.anchored.tests import test_system_facts as facts
root, case = sys.argv[2], sys.argv[3]
os.umask(0o022)
out = {"euid": os.geteuid(), "uid_map": " ".join(open("/proc/self/uid_map").read().split())}
os.makedirs(os.path.join(root, "var", "home", "u", "data"), mode=0o755)
os.symlink("var/home", os.path.join(root, "home"))
if case == "L5":
    try:
        os.lchown(os.path.join(root, "home"), 65534, -1)
    except OSError as exc:
        out["lchown_65534"] = errno.errorcode.get(exc.errno, str(exc.errno))
        print(json.dumps(out)); raise SystemExit(0)
    out["lchown_65534"] = "done"
try:  # before the chroot: the host's own root as this namespace sees it (recorded only)
    facts._check_ancestors(os.path.join(root, "var", "home", "u", "data"), "store")
    out["host_root_check"] = "passed"
except facts.Refused as exc:
    out["host_root_check"] = str(exc)
out["host_root_uid"] = os.lstat("/").st_uid
os.chroot(root)
os.chdir("/")
try:
    facts._check_ancestors("/home/u/data", "store")
    out["result"] = "passed"
except facts.Refused as exc:
    out["result"], out["code"], out["detail"] = "refused", exc.code, exc.detail
print(json.dumps(out))
"""


def _in_namespace(case: str) -> dict:
    """Run the namespace child for ``case``; raises unittest.SkipTest when no user namespace can be made."""
    unshare = shutil.which("unshare")
    if unshare is None:
        raise unittest.SkipTest("no unshare program on this host, so no user namespace without root")
    import recursion_core  # the child imports this module from the package the tests were found in

    core = str(Path(recursion_core.__file__).resolve().parents[1])
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "root")
        os.mkdir(root, 0o755)
        proc = subprocess.run([unshare, "-r", sys.executable, "-X", "utf8", "-B", "-I", "-c", _NAMESPACE_CHILD,
                               core, root, case], capture_output=True, text=True, timeout=120, check=False)
        lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
        if proc.returncode != 0 and not lines and proc.stderr.startswith("unshare:"):
            raise unittest.SkipTest(f"unshare -r refused for this user: {proc.stderr.strip()[:200]}")
        if proc.returncode != 0 or not lines:
            raise AssertionError(f"namespace child exit {proc.returncode}: {proc.stderr[-2000:]}")
        return json.loads(lines[-1])


@unittest.skipUnless(LINUX, NOT_LINUX)
class L4L5NamespaceTests(unittest.TestCase):
    def test_l4_an_ostree_shaped_home_passes_the_owner_check_above_the_folder(self) -> None:
        result = _in_namespace("L4")
        _fact("L4", how="unshare -r, chroot into an ostree-shaped root", **result)
        self.assertEqual("passed", result["result"])

    def test_l5_a_link_above_the_folder_that_another_uid_owns_refuses(self) -> None:
        result = _in_namespace("L5")
        if result.get("lchown_65534") != "done":
            _fact("L5", how="unshare -r", not_run=True, **result)
            raise unittest.SkipTest(
                f"chown -h 65534 answered {result.get('lchown_65534')} inside unshare -r: the namespace maps one uid "
                f"({result.get('uid_map')}), and mapping more needs newuidmap, which is "
                f"{'present' if shutil.which('newuidmap') else 'absent'} here")
        _fact("L5", how="unshare -r, chroot, /home a link owned by uid 65534", **result)
        self.assertEqual("refused", result["result"])
        self.assertIn("is a link another user owns", result["detail"])


# -- W5 -------------------------------------------------------------------------------------------

def _w5_skip_reason() -> str | None:
    if not LINUX:
        return NOT_LINUX
    if "microsoft" not in os.uname().release.lower():
        return f"not a WSL2 kernel ({os.uname().release})"
    if os.environ.get(W5_OPT_IN) != "1":
        return (f"W5 starts cmd.exe on the Windows side of the host; it runs only when the person running the "
                f"tests sets {W5_OPT_IN}=1")
    interop = Path("/proc/sys/fs/binfmt_misc")
    if not any((interop / name).exists() for name in ("WSLInterop", "WSLInterop-late")):
        return "WSL interop is off (no WSLInterop entry in binfmt_misc)"
    if shutil.which("cmd.exe") is None or shutil.which("wslpath") is None:
        return "cmd.exe or wslpath is not on PATH"
    return None


@unittest.skipIf(_w5_skip_reason() is not None, _w5_skip_reason() or "")
class W5JunctionTests(unittest.TestCase):
    def test_w5_a_junction_is_a_link_to_lstat_and_the_per_part_walk_refuses_it(self) -> None:
        cmd, wslpath = shutil.which("cmd.exe"), shutil.which("wslpath")

        def windows(*args: str) -> str:
            return subprocess.run([cmd, "/d", "/c", *args], capture_output=True, text=True, timeout=60,
                                  check=True, cwd=os.path.dirname(cmd)).stdout.strip()

        def convert(flag: str, path: str) -> str:
            return subprocess.run([wslpath, flag, path], capture_output=True, text=True, timeout=30,
                                  check=True).stdout.strip()

        windows_temp = convert("-u", windows("echo", "%TEMP%"))
        if not os.path.isdir(windows_temp):
            self.skipTest(f"the Windows temp folder is not reachable from Linux ({windows_temp})")
        scratch = Path(tempfile.mkdtemp(prefix="anchored-w5-", dir=windows_temp))
        junction = scratch / "junction"
        try:
            (scratch / "target" / "inner").mkdir(parents=True)
            windows("mklink", "/J", convert("-w", str(junction)), convert("-w", str(scratch / "target")))
            is_link = stat.S_ISLNK(os.lstat(junction).st_mode)
            anchor = _hold(scratch)
            try:
                with self.assertRaises(OSError) as caught:
                    os.close(held_walk(anchor, ("junction", "inner")))
            finally:
                os.close(anchor)
            _fact("W5", junction_s_islnk=is_link, per_part_walk=errno.errorcode.get(caught.exception.errno))
        finally:
            if os.path.lexists(junction):
                windows("rmdir", convert("-w", str(junction)))  # removes the junction, never its target
            shutil.rmtree(scratch)
        self.assertFalse(os.path.lexists(scratch))


# -- W1 to W4, W6: real Windows ------------------------------------------------------------------
# These run only on a real Windows interpreter (``sys.getwindowsversion`` exists only there; the
# simulated-Windows harness sets ``os.name`` and ``sys.platform`` but has no such function) and skip
# elsewhere with the reason. Every call into the system goes through ctypes; the junctions come from
# ``_winapi.CreateJunction``, which needs no special right. Every junction, link, folder and file is
# made inside one ``tempfile.TemporaryDirectory``, every junction and link points at a folder inside
# that same directory, and each one is removed by name (the reparse point itself, never its target) in
# a ``finally`` before the directory goes. A recorded path names the directory as ``<tmp>``.

REAL_WINDOWS = os.name == "nt" and hasattr(sys, "getwindowsversion")
NOT_REAL_WINDOWS = (f"a real Windows fact; this host is {sys.platform} (os.name {os.name}) without "
                    "sys.getwindowsversion: the Windows rows come from a run on a real Windows host")
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
IO_REPARSE_TAG_SYMLINK = 0xA000000C
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
ERROR_PRIVILEGE_NOT_HELD = 1314


def _wfact(case: str, **values: object) -> None:
    """One recorded fact on real Windows, never asserted: the Windows build instead of a kernel."""
    import platform

    version = sys.getwindowsversion()
    row = {"case": case, "python": sys.version.split()[0], "gil": _gil(), "machine": platform.machine(),
           "windows_build": f"{version.major}.{version.minor}.{version.build}", **values}
    sys.stderr.write("FACT " + json.dumps(row, sort_keys=True) + "\n")
    sys.stderr.flush()


def _masked(path: str, tmp: str) -> str:
    """``path`` with the test's own directory shown as ``<tmp>`` (no host path goes into a record)."""
    plain = path[4:] if path.startswith("\\\\?\\") else path
    real = os.path.realpath(tmp)
    if os.path.normcase(plain).startswith(os.path.normcase(real)):
        return "<tmp>" + plain[len(real):]
    return "<outside the test's directory>"


class _Win:
    """kernel32 and ntdll through ctypes, loaded only when a Windows case runs."""

    GENERIC_READ = 0x80000000
    FILE_READ_ATTRIBUTES = 0x80
    FILE_LIST_DIRECTORY = 0x1
    FILE_TRAVERSE = 0x20
    SYNCHRONIZE = 0x100000
    SHARE_ALL = 0x7
    FILE_SHARE_READ = 0x1
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_OPEN = 1
    FILE_DIRECTORY_FILE = 0x1
    FILE_SYNCHRONOUS_IO_NONALERT = 0x20
    FILE_OPEN_REPARSE_POINT = 0x00200000
    OBJ_CASE_INSENSITIVE = 0x40
    OBJ_DONT_REPARSE = 0x1000
    FILE_ATTRIBUTE_TAG_INFO = 9

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes, self.wintypes = ctypes, wintypes
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll")
        self.INVALID = wintypes.HANDLE(-1).value

        class UnicodeString(ctypes.Structure):
            _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                        ("Buffer", wintypes.LPWSTR)]

        class ObjectAttributes(ctypes.Structure):
            _fields_ = [("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE),
                        ("ObjectName", ctypes.POINTER(UnicodeString)), ("Attributes", wintypes.ULONG),
                        ("SecurityDescriptor", ctypes.c_void_p), ("SecurityQualityOfService", ctypes.c_void_p)]

        class IoStatusBlock(ctypes.Structure):
            _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

        class TagInfo(ctypes.Structure):
            _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

        self.UnicodeString, self.ObjectAttributes = UnicodeString, ObjectAttributes
        self.IoStatusBlock, self.TagInfo = IoStatusBlock, TagInfo
        k32 = self.k32
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        k32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        k32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        k32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.ntdll.NtCreateFile.restype = ctypes.c_ulong  # NTSTATUS, read unsigned
        self.ntdll.NtCreateFile.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                            ctypes.POINTER(ObjectAttributes), ctypes.POINTER(IoStatusBlock),
                                            ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG, wintypes.ULONG,
                                            wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG]

    def create_file(self, path: str, access: int, share: int, flags: int) -> int:
        handle = self.k32.CreateFileW(path, access, share, None, self.OPEN_EXISTING, flags, None)
        if handle is None or handle == self.INVALID:
            code = self.ctypes.get_last_error()
            raise OSError(None, self.ctypes.FormatError(code), path, code)
        return handle

    def close(self, handle: int) -> None:
        self.k32.CloseHandle(handle)

    def final_path(self, handle: int) -> str:
        buffer = self.ctypes.create_unicode_buffer(32_768)
        if not self.k32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0):
            code = self.ctypes.get_last_error()
            raise OSError(None, self.ctypes.FormatError(code), None, code)
        return buffer.value

    def tag(self, handle: int) -> tuple:
        """(attributes, reparse tag) of an open handle."""
        info = self.TagInfo()
        if not self.k32.GetFileInformationByHandleEx(handle, self.FILE_ATTRIBUTE_TAG_INFO,
                                                     self.ctypes.byref(info), self.ctypes.sizeof(info)):
            code = self.ctypes.get_last_error()
            raise OSError(None, self.ctypes.FormatError(code), None, code)
        return info.FileAttributes, info.ReparseTag

    def nt_open(self, root: int, name: str, *, attributes: int, options: int) -> tuple:
        """NtCreateFile of ``name`` relative to the folder handle ``root``: (status, handle or None)."""
        ctypes, wintypes = self.ctypes, self.wintypes
        buffer = ctypes.create_unicode_buffer(name)
        text = self.UnicodeString(len(name) * 2, len(name) * 2 + 2, ctypes.cast(buffer, wintypes.LPWSTR))
        objects = self.ObjectAttributes(ctypes.sizeof(self.ObjectAttributes), root, ctypes.pointer(text),
                                        attributes, None, None)
        handle, status_block = wintypes.HANDLE(), self.IoStatusBlock()
        status = self.ntdll.NtCreateFile(
            ctypes.byref(handle), self.FILE_READ_ATTRIBUTES | self.FILE_LIST_DIRECTORY | self.FILE_TRAVERSE
            | self.SYNCHRONIZE, ctypes.byref(objects), ctypes.byref(status_block), None, 0, self.SHARE_ALL,
            self.FILE_OPEN, options, None, 0)
        return status, (handle.value if status < 0x80000000 else None)

    def hold_folder(self, path: str, share: int | None = None) -> int:
        return self.create_file(path, self.FILE_READ_ATTRIBUTES | self.FILE_LIST_DIRECTORY | self.FILE_TRAVERSE
                                | self.SYNCHRONIZE, self.SHARE_ALL if share is None else share,
                                self.FILE_FLAG_BACKUP_SEMANTICS)

    def nt_walk(self, held: int, parts: tuple) -> dict:
        """Open each part relative to the held parent with FILE_OPEN_REPARSE_POINT; refuse a reparse point.

        Returns what happened: the part that refused, its status and its tag, or "opened".
        """
        current, owned = held, False
        try:
            for index, part in enumerate(parts):
                status, handle = self.nt_open(current, part, attributes=self.OBJ_CASE_INSENSITIVE,
                                              options=self.FILE_DIRECTORY_FILE | self.FILE_SYNCHRONOUS_IO_NONALERT
                                              | self.FILE_OPEN_REPARSE_POINT)
                if handle is None:
                    return {"result": "status", "part": index, "status": f"0x{status:08X}"}
                attributes, tag = self.tag(handle)
                if owned:
                    self.close(current)
                current, owned = handle, True
                if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                    return {"result": "refused", "part": index, "status": f"0x{status:08X}", "tag": tag}
            return {"result": "opened", "part": len(parts)}
        finally:
            if owned:
                self.close(current)


def _junction(target: Path, junction: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(junction))


def _remove_reparse_point(path: Path) -> None:
    """Remove a junction or a folder link by its own name; never follows it, never empties its target."""
    if os.path.lexists(path):
        os.rmdir(path)


@unittest.skipUnless(REAL_WINDOWS, NOT_REAL_WINDOWS)
class W1ToW4AndW6WindowsTests(unittest.TestCase):
    def _layout(self, tmp: str) -> tuple:
        held, outside = Path(tmp, "held"), Path(tmp, "outside")
        (held / "a").mkdir(parents=True)
        (outside / "c").mkdir(parents=True)
        return held, outside

    def test_w1_a_junction_is_no_link_to_islink_and_one_multi_part_open_follows_it(self) -> None:
        win = _Win()
        with tempfile.TemporaryDirectory() as tmp:
            held, outside = self._layout(tmp)
            junction = held / "a" / "j"
            try:
                _junction(outside, junction)
                islink, isjunction = os.path.islink(junction), os.path.isjunction(junction)
                handle = win.create_file(str(junction / "c"), win.FILE_READ_ATTRIBUTES, win.SHARE_ALL,
                                         win.FILE_FLAG_OPEN_REPARSE_POINT | win.FILE_FLAG_BACKUP_SEMANTICS)
                try:
                    final = win.final_path(handle)
                finally:
                    win.close(handle)
            finally:
                _remove_reparse_point(junction)
            self.assertFalse(islink)
            self.assertTrue(isjunction)
            self.assertEqual("<tmp>\\outside\\c", _masked(final, tmp))
            _wfact("W1", made_by="_winapi.CreateJunction", islink=islink, isjunction=isjunction,
                   createfile="FILE_FLAG_OPEN_REPARSE_POINT|FILE_FLAG_BACKUP_SEMANTICS of held\\a\\j\\c",
                   final_path=_masked(final, tmp))

    def test_w2_the_nt_walk_opens_the_junction_part_as_itself_and_refuses_it_by_its_tag(self) -> None:
        win = _Win()
        with tempfile.TemporaryDirectory() as tmp:
            held, outside = self._layout(tmp)
            junction = held / "a" / "j"
            try:
                _junction(outside, junction)
                anchor = win.hold_folder(str(held))
                try:
                    through = win.nt_walk(anchor, ("a", "j", "c"))
                    plain = win.nt_walk(anchor, ("a",))
                finally:
                    win.close(anchor)
            finally:
                _remove_reparse_point(junction)
            self.assertEqual("refused", through["result"])
            self.assertEqual(1, through["part"])
            self.assertEqual(IO_REPARSE_TAG_MOUNT_POINT, through["tag"])
            self.assertEqual("opened", plain["result"])
            _wfact("W2", call="NtCreateFile, RootDirectory = the held parent, one part at a time, "
                   "FILE_OPEN_REPARSE_POINT|FILE_DIRECTORY_FILE", junction_part=through["part"],
                   junction_status=through["status"], junction_tag=f"0x{through['tag']:08X}",
                   plain_part="opened")

    def test_w3_obj_dont_reparse_on_a_relative_multi_part_name_is_recorded(self) -> None:
        win = _Win()
        options = win.FILE_DIRECTORY_FILE | win.FILE_SYNCHRONOUS_IO_NONALERT
        attributes = win.OBJ_CASE_INSENSITIVE | win.OBJ_DONT_REPARSE
        with tempfile.TemporaryDirectory() as tmp:
            held, outside = self._layout(tmp)
            (held / "a" / "b" / "c").mkdir(parents=True)
            junction = held / "a" / "j"
            try:
                _junction(outside, junction)
                anchor = win.hold_folder(str(held))
                try:
                    through_status, through = win.nt_open(anchor, "a\\j\\c", attributes=attributes, options=options)
                    if through is not None:
                        through_final = _masked(win.final_path(through), tmp)
                        win.close(through)
                    plain_status, plain = win.nt_open(anchor, "a\\b\\c", attributes=attributes, options=options)
                    if plain is not None:
                        win.close(plain)
                finally:
                    win.close(anchor)
            finally:
                _remove_reparse_point(junction)
        # Recorded, never asserted: the status for a middle junction and for a plain name on this build.
        _wfact("W3", call="NtCreateFile, RootDirectory = the held folder, OBJ_DONT_REPARSE|OBJ_CASE_INSENSITIVE, "
               "a relative multi-part name", middle_junction_name="a\\j\\c",
               middle_junction_status=f"0x{through_status:08X}",
               middle_junction_opened=through is not None and through_final,
               plain_name="a\\b\\c", plain_status=f"0x{plain_status:08X}", plain_opened=plain is not None)

    def test_w4_a_folder_held_with_share_read_only_cannot_be_renamed_or_deleted_by_another_process(self) -> None:
        win = _Win()
        child = (
            "import json, os, shutil, sys\n"
            "folder, parent = sys.argv[1], sys.argv[2]\n"
            "out = {}\n"
            "for key, call in (('rename_folder', lambda: os.rename(folder, folder + '.moved')),\n"
            "                  ('delete_folder', lambda: os.rmdir(folder)),\n"
            "                  ('rename_parent', lambda: os.rename(parent, parent + '.moved')),\n"
            "                  ('delete_parent', lambda: shutil.rmtree(parent))):\n"
            "    try:\n"
            "        call(); out[key] = 'done'\n"
            "    except OSError as exc:\n"
            "        out[key] = getattr(exc, 'winerror', None) or exc.errno\n"
            "print(json.dumps(out))\n")
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp, "parent")
            folder = parent / "held"
            folder.mkdir(parents=True)
            handle = win.hold_folder(str(folder), share=win.FILE_SHARE_READ)
            try:
                proc = subprocess.run([sys.executable, "-X", "utf8", "-B", "-I", "-c", child, str(folder), str(parent)],
                                      capture_output=True, text=True, timeout=120, check=False)
            finally:
                win.close(handle)
            still = folder.is_dir() and parent.is_dir()
        lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
        self.assertEqual(0, proc.returncode, proc.stderr[-2000:])
        result = json.loads(lines[-1])
        _wfact("W4", hold="CreateFileW FILE_SHARE_READ only, FILE_FLAG_BACKUP_SEMANTICS",
               second_process="a new interpreter process", winerrors=result, both_still_there=still)
        for key in ("rename_folder", "delete_folder", "rename_parent", "delete_parent"):
            self.assertNotEqual("done", result[key], key)
        self.assertTrue(still)

    def test_w6_a_symbolic_link_in_a_middle_part_only_if_this_account_can_make_one(self) -> None:
        win = _Win()
        with tempfile.TemporaryDirectory() as tmp:
            held, outside = self._layout(tmp)
            link = held / "a" / "s"
            try:
                try:
                    os.symlink(outside, link, target_is_directory=True)
                except OSError as exc:
                    if getattr(exc, "winerror", None) == ERROR_PRIVILEGE_NOT_HELD:
                        _wfact("W6", not_run=True, symlink_winerror=ERROR_PRIVILEGE_NOT_HELD)
                        raise unittest.SkipTest(
                            "this account cannot make a symbolic link (ERROR_PRIVILEGE_NOT_HELD, 1314): it needs "
                            "Developer Mode or the symbolic-link right, a system setting this test never changes")
                    raise
                islink, isjunction = os.path.islink(link), os.path.isjunction(link)
                anchor = win.hold_folder(str(held))
                try:
                    through = win.nt_walk(anchor, ("a", "s", "c"))
                finally:
                    win.close(anchor)
            finally:
                _remove_reparse_point(link)
            self.assertTrue(islink)
            self.assertFalse(isjunction)
            self.assertEqual("refused", through["result"])
            self.assertEqual(IO_REPARSE_TAG_SYMLINK, through["tag"])
            _wfact("W6", islink=islink, isjunction=isjunction, link_part=through["part"],
                   link_tag=f"0x{through['tag']:08X}")


# -- W7, W8: a junction made with the system's own command, and one swapped in after the check ------
# Real Windows only, like W1 to W4 and W6 (the same skip and the same clean-up habits). W7 makes the
# junction with the command every Windows install has (``cmd.exe /c mklink /J``; no administrator
# right and no Developer Mode) and shows the held walk refuses it. W8 checks a plain folder part
# through the held parent, then swaps a junction in under the same name, and shows that the part
# already held is not redirected and that a new walk refuses the junction; a by-path use after the
# same swap is only recorded. Every junction points inside the test's own directory and is removed
# by its own name in a ``finally``.

# Seconds one ``cmd.exe`` call of W7 and W8 may take before the case fails. A first value: it becomes
# a row of the person's settings file once that file is at the base.
W_COMMAND_SECONDS = 60


def _system_command() -> str | None:
    """The system's own command interpreter, from ``ComSpec`` or the system folder; None when absent."""
    for candidate in (os.environ.get("ComSpec"),
                      os.path.join(os.environ.get("SystemRoot", ""), "System32", "cmd.exe")):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _mklink_junction(target: Path, junction: Path) -> str:
    """Make ``junction`` -> ``target`` with ``cmd.exe /c mklink /J``; returns the command's exit code."""
    cmd = _system_command()
    if cmd is None:
        raise unittest.SkipTest("no cmd.exe on this Windows host (ComSpec and the system folder have none)")
    proc = subprocess.run([cmd, "/d", "/c", "mklink", "/J", junction.name, str(target)], cwd=str(junction.parent),
                          capture_output=True, text=True, timeout=W_COMMAND_SECONDS, check=False)
    if proc.returncode != 0 or not os.path.lexists(junction):
        raise AssertionError(f"mklink /J answered {proc.returncode}: {(proc.stdout + proc.stderr).strip()[-500:]}")
    return str(proc.returncode)


@unittest.skipUnless(REAL_WINDOWS, NOT_REAL_WINDOWS)
class W7W8BuiltInJunctionTests(unittest.TestCase):
    def test_w7_a_junction_made_by_mklink_is_refused_by_the_held_walk(self) -> None:
        win = _Win()
        with tempfile.TemporaryDirectory() as tmp:
            held, outside = Path(tmp, "held"), Path(tmp, "outside")
            (held / "a").mkdir(parents=True)
            (outside / "c").mkdir(parents=True)
            junction = held / "a" / "j"
            try:
                exit_code = _mklink_junction(outside, junction)
                islink, isjunction = os.path.islink(junction), os.path.isjunction(junction)
                anchor = win.hold_folder(str(held))
                try:
                    through = win.nt_walk(anchor, ("a", "j", "c"))
                    plain = win.nt_walk(anchor, ("a",))
                finally:
                    win.close(anchor)
            finally:
                _remove_reparse_point(junction)
            left = sorted(os.listdir(held / "a")) + sorted(os.listdir(outside))
        self.assertEqual("refused", through["result"])
        self.assertEqual(1, through["part"])
        self.assertEqual(IO_REPARSE_TAG_MOUNT_POINT, through["tag"])
        self.assertEqual("opened", plain["result"])
        self.assertEqual(["c"], left)  # the junction is gone and its target was not emptied
        _wfact("W7", made_by="cmd.exe /d /c mklink /J", mklink_exit=exit_code, islink=islink,
               isjunction=isjunction, walk="NtCreateFile per part relative to the held parent, FILE_OPEN_REPARSE_POINT",
               junction_part=through["part"], junction_status=through["status"],
               junction_tag=f"0x{through['tag']:08X}", plain_part="opened")

    def test_w8_a_junction_swapped_in_after_the_check_is_refused_and_the_held_part_is_not_redirected(self) -> None:
        win = _Win()
        options = win.FILE_DIRECTORY_FILE | win.FILE_SYNCHRONOUS_IO_NONALERT | win.FILE_OPEN_REPARSE_POINT
        with tempfile.TemporaryDirectory() as tmp:
            held, outside = Path(tmp, "held"), Path(tmp, "outside")
            (held / "a" / "c").mkdir(parents=True)
            (outside / "c").mkdir(parents=True)
            part, moved = held / "a", held / "a-moved"
            handles = []
            try:
                anchor = win.hold_folder(str(held))
                handles.append(anchor)
                # The check: the part is opened as itself through the held parent and is no reparse point.
                status, checked = win.nt_open(anchor, "a", attributes=win.OBJ_CASE_INSENSITIVE, options=options)
                self.assertIsNotNone(checked, f"0x{status:08X}")
                handles.append(checked)
                checked_attributes, _ = win.tag(checked)
                # The swap, after the check: the folder moves away and a junction takes its name.
                os.rename(part, moved)
                exit_code = _mklink_junction(outside, part)
                # The use through the part already held: it stays on the folder that was checked.
                use_status, used = win.nt_open(checked, "c", attributes=win.OBJ_CASE_INSENSITIVE, options=options)
                self.assertIsNotNone(used, f"0x{use_status:08X}")
                handles.append(used)
                held_use = _masked(win.final_path(used), tmp)
                # A new walk of the same name meets the junction and refuses it.
                again = win.nt_walk(anchor, ("a", "c"))
                # Recorded only: a use of the same path by its text after the same swap.
                by_path = win.create_file(str(part / "c"), win.FILE_READ_ATTRIBUTES, win.SHARE_ALL,
                                          win.FILE_FLAG_BACKUP_SEMANTICS)
                handles.append(by_path)
                by_path_use = _masked(win.final_path(by_path), tmp)
            finally:
                for handle in reversed(handles):
                    win.close(handle)
                _remove_reparse_point(part)
            left = sorted(os.listdir(held)) + sorted(os.listdir(outside))
        self.assertFalse(checked_attributes & FILE_ATTRIBUTE_REPARSE_POINT)
        self.assertEqual("<tmp>\\held\\a-moved\\c", held_use)
        self.assertEqual("refused", again["result"])
        self.assertEqual(0, again["part"])
        self.assertEqual(IO_REPARSE_TAG_MOUNT_POINT, again["tag"])
        self.assertEqual(["a-moved", "c"], left)  # the junction is gone and its target was not emptied
        _wfact("W8", swap="rename of the checked folder, then cmd.exe /d /c mklink /J under its name",
               mklink_exit=exit_code, held_part_use=held_use, new_walk=again["result"], new_walk_part=again["part"],
               new_walk_tag=f"0x{again['tag']:08X}", by_path_use_recorded=by_path_use)

if __name__ == "__main__":
    unittest.main()
