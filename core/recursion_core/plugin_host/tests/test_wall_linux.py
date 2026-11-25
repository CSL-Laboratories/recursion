"""The wall a plugin host applies to itself: proved in CHILD interpreters, never in the test runner.

Every test starts a child Python (``-I``, the runner's own ``-X`` options, its own temporary folder, a
stand-in home holding a canary, a stand-in 'daemon' process that the test starts itself and ends) and
reads one JSON line back. A wall applied to the runner would cripple it, so nothing here calls
``apply_wall`` in this process. Without the wall the five escapes of the probe succeed; with it all five
are refused, ``proven`` is True, and the child still imports from its install, answers a pipe and takes
a socket handed over on its channel by ``recvmsg``.
"""
from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from recursion_core.plugin_host import wall_linux

CORE = Path(__file__).resolve().parents[3]
SCHEMA = CORE.parent / "schemas" / "plugin" / "v1" / "wall.v1.json"
CHILD_TIMEOUT_S = 60  # a bound on one child process of this test; test-side only, not a product figure

CHILD = r'''
import errno, json, os, platform, socket, sys, threading
spec = json.loads(sys.argv[1])
sys.path.insert(0, spec["core"])
from recursion_core.plugin_host import wall_linux as w

def status():
    with open("/proc/self/status") as handle:
        rows = dict(line.split(":", 1) for line in handle if ":" in line)
    return {key: rows[key].strip() for key in ("NoNewPrivs", "Seccomp") if key in rows}

def attempt(call):
    try:
        call()
    except w.WallRefused as exc:
        return exc.code
    except Exception as exc:
        return type(exc).__name__
    return "applied"

mode, out = spec["mode"], {}
chan = socket.socket(fileno=spec["chan"])
if mode == "nowall":
    out["record"] = w.probe_wall(spec["canary"], spec["daemon"], write_file=spec["canary"], write_folder=spec["writes"])
elif mode in ("wall", "wall_missing_canary"):
    out["apply"] = attempt(lambda: w.apply_wall(spec["paths"], keep_descriptors=w._descriptors_now()))
    out["record"] = w.probe_wall(spec["canary"], spec["daemon"], write_file=spec["canary"], write_folder=spec["writes"])
    if mode == "wall":
        def denied(call):
            try:
                call()
            except PermissionError:
                return True
            except Exception as exc:
                return type(exc).__name__
            return False
        out["more_denied"] = {
            "socketpair": denied(socket.socketpair),
            "pidfd_open": denied(lambda: os.close(os.pidfd_open(spec["daemon"]))),
            "unshare": denied(lambda: os.unshare(os.CLONE_NEWUSER)) if hasattr(os, "unshare") else True,
            "write_in_home": denied(lambda: open(os.path.join(os.path.dirname(spec["canary"]), "new"), "w").close()),
        }
        try:  # the filter's EPERM, not only Landlock's EACCES (an install outside /usr cannot map the loader)
            os.waitpid(os.posix_spawn(sys.executable, [sys.executable, "-I", "-S", "-c", ""], {}), 0)
            out["start_errno"] = "started"
        except OSError as exc:
            out["start_errno"] = errno.errorcode.get(exc.errno, str(exc.errno))
        out["self_signal"] = attempt(lambda: os.kill(os.getpid(), 0))
        fresh = [name for name in ("fractions", "decimal", "colorsys") if name not in sys.modules]
        import fractions, decimal, colorsys
        out["imported"] = fresh, str(fractions.Fraction(1, 3) + 1), str(decimal.Decimal("1.5") * 2), colorsys.rgb_to_hsv(1, 0, 0)[2]
        worker = threading.Thread(target=lambda: None)
        worker.start()
        worker.join()
        out["thread_after"] = True
        out["pipe"] = sys.stdin.readline().strip()
        message, fds, _flags, _addr = socket.recv_fds(chan, 64, 1)
        handed = socket.socket(fileno=fds[0])
        handed.sendall(b"answer:" + message)
        handed.close()
elif mode == "thread_then_apply":
    gate = threading.Event()
    waiting = threading.Thread(target=gate.wait)
    waiting.start()
    out["apply"] = attempt(lambda: w.apply_wall(spec["paths"], keep_descriptors=w._descriptors_now()))
    gate.set()
    waiting.join()
    out["status"] = status()
elif mode == "thread_then_filter":
    gate, seen = threading.Event(), {}
    def late():
        gate.wait()
        try:
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()
            seen["socket"] = "made"
        except PermissionError:
            seen["socket"] = "refused"
    waiting = threading.Thread(target=late)
    waiting.start()
    out["filter"] = attempt(lambda: w._load_seccomp_filter(w._libc(), w._arch()))
    gate.set()
    waiting.join()
    out["thread"] = seen.get("socket")
elif mode == "no_landlock":
    w._landlock_abi = lambda libc: 0
    out["apply"] = attempt(lambda: w.apply_wall(spec["paths"], keep_descriptors=w._descriptors_now()))
    out["status"] = status()
elif mode == "other_arch":
    platform.machine = lambda: "riscv64"
    out["apply"] = attempt(lambda: w.apply_wall(spec["paths"], keep_descriptors=w._descriptors_now()))
    out["status"] = status()
elif mode == "not_linux":
    def no_library(*args, **kwargs):
        raise AssertionError("the C library was reached on a system without the two calls")
    w.ctypes.CDLL = no_library
    sys.platform = "darwin"
    out["apply"] = attempt(lambda: w.apply_wall(spec["paths"], keep_descriptors=w._descriptors_now()))
    out["probe"] = attempt(lambda: w.probe_wall(spec["canary"], spec["daemon"], write_file=spec["canary"], write_folder=spec["writes"]))
    sys.platform = "linux"
    out["status"] = status()
print(json.dumps(out), flush=True)
'''


def _why_not_here() -> str | None:
    if not sys.platform.startswith("linux"):
        return "the wall is Linux-only; other systems are answered wall_system_unsupported"
    if platform.machine() not in ("x86_64", "aarch64"):
        return f"the wall knows x86_64 and aarch64 only, not {platform.machine()}"
    if wall_linux._landlock_abi(wall_linux._libc()) < 1:  # a query only: nothing is applied here
        return "this kernel gives this process no Landlock"
    return None


def _install_paths() -> list[str]:
    return sorted({sys.base_prefix, sys.prefix})


def _interpreter() -> list[str]:
    args = [sys.executable, "-I", "-B"]
    for key, value in sys._xoptions.items():
        args += ["-X", key if value is True else f"{key}={value}"]
    return args


def run_child(mode: str, *, canary_exists: bool = True) -> tuple[dict, str]:
    """(the child's JSON line, what came back on the socket handed to it) for one child in ``mode``."""
    with tempfile.TemporaryDirectory() as folder:
        home = os.path.join(folder, "home")
        os.mkdir(home)
        canary = os.path.join(home, "canary")
        if canary_exists:
            with open(canary, "w", encoding="utf-8") as handle:
                handle.write("the daemon's secret, stand-in")
        writes = os.path.join(folder, "write-probe")  # the folder made for the probe's write kinds, before any wall
        os.mkdir(writes)
        wall_linux.prepare_write_folder(writes)
        daemon = subprocess.Popen(_interpreter() + ["-c", "import time; time.sleep(120)"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, cwd=folder,
                                  env={"HOME": home, "TMPDIR": folder})
        ours, theirs = socket.socketpair()
        handed_here, handed_there = socket.socketpair()
        try:
            spec = {"core": str(CORE), "mode": mode, "canary": canary, "daemon": daemon.pid,
                    "chan": theirs.fileno(), "paths": _install_paths(), "writes": writes}
            socket.send_fds(ours, [b"ping"], [handed_there.fileno()])
            handed_there.close()
            child = subprocess.run(_interpreter() + ["-c", CHILD, json.dumps(spec)], input="question\n",
                                   capture_output=True, text=True, timeout=CHILD_TIMEOUT_S, cwd=folder,
                                   pass_fds=[theirs.fileno()], env={"HOME": home, "TMPDIR": folder})
            handed_here.settimeout(1)
            try:
                answer = handed_here.recv(64).decode()
            except OSError:
                answer = ""
        finally:
            for end in (ours, theirs, handed_here):
                end.close()
            daemon.kill()
            daemon.wait()
    lines = child.stdout.strip().splitlines()
    if child.returncode != 0 or not lines:
        raise AssertionError(f"the child ({mode}) ended {child.returncode}: {child.stderr[-1500:]}")
    return json.loads(lines[-1]), answer


def _interpret(rows, arch: str, number: int, arg0: int, audit_arch: int | None = None) -> str:
    """Run the seccomp program on one call, as the kernel's classic BPF would: 'allow' or 'eperm'."""
    data = {0: number, 4: wall_linux._AUDIT_ARCH[arch] if audit_arch is None else audit_arch, 16: arg0 & 0xFFFFFFFF}
    acc, pc = 0, 0
    while True:
        code, jt, jf, k = rows[pc]
        if code == wall_linux._BPF_LD_W_ABS:
            acc, pc = data[k], pc + 1
        elif code in (wall_linux._BPF_JEQ_K, wall_linux._BPF_JGE_K):
            hit = acc == k if code == wall_linux._BPF_JEQ_K else acc >= k
            pc += 1 + (jt if hit else jf)
        elif code == wall_linux._BPF_RET_K:
            return {wall_linux._RET_ALLOW: "allow", wall_linux._RET_EPERM: "eperm"}[k]
        else:
            raise AssertionError(f"an instruction this program should not hold: {code:#x}")


@unittest.skipIf(_why_not_here(), _why_not_here() or "")
class WallInAChildTests(unittest.TestCase):

    def test_without_the_wall_every_escape_of_the_probe_succeeds(self):
        result, _ = run_child("nowall")
        record = result["record"]
        self.assertEqual({name: record[name] for name in wall_linux.RECORD_CHECKS},
                         dict.fromkeys(wall_linux.RECORD_CHECKS, False), "the probe is not vacuous: unwalled, all five escape")
        self.assertIs(record["proven"], False)

    def test_with_the_wall_all_five_are_refused_and_python_keeps_working(self):
        result, answer = run_child("wall")
        self.assertEqual(result["apply"], "applied")
        record = result["record"]
        self.assertEqual({name: record[name] for name in wall_linux.RECORD_CHECKS},
                         dict.fromkeys(wall_linux.RECORD_CHECKS, True))
        self.assertIs(record["proven"], True)
        self.assertGreaterEqual(record["landlock_abi"], 1)
        self.assertEqual(record["arch"], platform.machine())
        self.assertEqual(result["more_denied"], {"socketpair": True, "pidfd_open": True, "unshare": True,
                                                 "write_in_home": True})
        self.assertEqual(result["start_errno"], "EPERM", "starting a program is refused by the filter itself")
        self.assertEqual(result["self_signal"], "PermissionError", "no signal passes, not even to its own pid")
        fresh, fraction, number, value = result["imported"]
        self.assertTrue(fresh, "at least one stdlib module was imported for the first time inside the wall")
        self.assertEqual((fraction, number, value), ("4/3", "3.0", 1))
        self.assertIs(result["thread_after"], True)
        self.assertEqual(result["pipe"], "question")
        self.assertEqual(answer, "answer:ping", "the socket handed over by recvmsg answers")

    def test_a_check_that_raises_is_not_counted_as_refused(self):
        result, _ = run_child("wall_missing_canary", canary_exists=False)
        self.assertEqual(result["apply"], "applied")
        record = result["record"]
        self.assertIs(record["home_canary"], False, "a canary that is not there proves nothing")
        self.assertIs(record["proven"], False)
        self.assertTrue(all(record[name] for name in wall_linux.RECORD_CHECKS if name != "home_canary"))

    def test_a_second_thread_is_refused_before_any_change(self):
        result, _ = run_child("thread_then_apply")
        self.assertEqual(result["apply"], "wall_threads_running")
        self.assertEqual(result["status"], {"NoNewPrivs": "0", "Seccomp": "0"})

    def test_the_filter_reaches_a_thread_started_before_it(self):
        result, _ = run_child("thread_then_filter")
        self.assertEqual(result["filter"], "applied")
        self.assertEqual(result["thread"], "refused", "TSYNC: the earlier thread is filtered too")

    def test_no_landlock_is_refused_before_any_change(self):
        result, _ = run_child("no_landlock")
        self.assertEqual(result["apply"], "landlock_unavailable")
        self.assertEqual(result["status"], {"NoNewPrivs": "0", "Seccomp": "0"})

    def test_another_arch_is_refused_before_any_change(self):
        result, _ = run_child("other_arch")
        self.assertEqual(result["apply"], "wall_arch_unsupported")
        self.assertEqual(result["status"], {"NoNewPrivs": "0", "Seccomp": "0"})

    def test_without_the_two_calls_the_module_answers_unsupported_without_trying(self):
        result, _ = run_child("not_linux")
        self.assertEqual((result["apply"], result["probe"]), ("wall_system_unsupported", "wall_system_unsupported"))
        self.assertEqual(result["status"], {"NoNewPrivs": "0", "Seccomp": "0"})


class WallDefinitionTests(unittest.TestCase):

    def test_the_deny_list_is_the_walls_own_definition(self):
        self.assertEqual(wall_linux.DENIED_CALLS, (
            "socket", "socketpair", "connect", "bind", "listen", "accept", "accept4", "execve", "execveat",
            "ptrace", "process_vm_readv", "process_vm_writev", "pidfd_open", "pidfd_getfd", "pidfd_send_signal",
            "keyctl", "add_key", "request_key", "mount", "umount2", "open_tree", "move_mount", "fsopen", "fsconfig",
            "fsmount", "fspick", "mount_setattr", "open_tree_attr", "unshare", "setns", "bpf", "perf_event_open",
            "userfaultfd", "io_uring_setup", "io_uring_enter", "io_uring_register", "sendmsg", "sendmmsg"))
        self.assertEqual(wall_linux.DENIED_SIGNAL_CALLS, ("kill", "tkill", "tgkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo"))
        for arch, numbers in wall_linux._NR.items():
            with self.subTest(arch=arch):
                names = wall_linux.DENIED_CALLS + ("seccomp",)
                self.assertEqual(sorted(numbers), sorted(names))
                self.assertEqual(len(set(numbers.values())), len(numbers))

    def test_every_listed_call_is_answered_eperm_by_the_program(self):
        for arch in wall_linux._NR:
            rows = wall_linux._filter(arch, 3)
            with self.subTest(arch=arch):
                self.assertEqual([_interpret(rows, arch, wall_linux._NR[arch][name], 0) for name in wall_linux.DENIED_CALLS],
                                 ["eperm"] * len(wall_linux.DENIED_CALLS))
                for name in wall_linux.DENIED_SIGNAL_CALLS:
                    for target in (4242, 4243, 0, -1):
                        self.assertEqual(_interpret(rows, arch, wall_linux._NR_MORE[arch][name], target), "eperm")
                self.assertEqual(_interpret(rows, arch, {"x86_64": 0, "aarch64": 63}[arch], 0), "allow")
                self.assertEqual(_interpret(rows, arch, wall_linux._NR[arch]["socket"], 0, audit_arch=0x40000003), "eperm")
                if arch == "x86_64":  # the x32 calls share the arch value: denied whole
                    self.assertEqual(_interpret(rows, arch, 0x40000000 | 0, 0), "eperm")

    def test_the_record_has_exactly_the_fields_of_its_schema(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        fields = set(wall_linux.RECORD_CHECKS) | {"writes", "writes_not_tried", "landlock_abi", "landlock_scopes",
                                                  "arch", "proven"}
        later = {"threads", "descriptors_open_for_write", "memory_maps_open_for_write", "descriptors_seen_by",
                 "descriptors_unaccounted", "descriptors_kept", "descriptors_not_closed", "held_objects",
                 "held_objects_not_tried", "allow_list", "proven_over", "shared_waits_allowed",
                 "limits_of_proven", "descriptors_counted_by"}  # required of a proven record only
        self.assertEqual(set(schema["required"]), fields)
        self.assertEqual(set(schema["then"]["required"]), later)
        self.assertEqual(set(schema["properties"]), fields | later)

    def test_on_a_system_without_the_two_calls_nothing_is_tried(self):
        if sys.platform.startswith("linux"):
            self.skipTest("asserted on Linux in a child with a simulated system (WallInAChildTests)")
        for call in (lambda: wall_linux.apply_wall([sys.prefix]), lambda: wall_linux.probe_wall("canary", 1)):
            with self.assertRaises(wall_linux.WallRefused) as caught:
                call()
            self.assertEqual(caught.exception.code, "wall_system_unsupported")


if __name__ == "__main__":
    unittest.main()
