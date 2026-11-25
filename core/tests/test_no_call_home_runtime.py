"""The run-time proof that Recursion sends nothing home, watched by the interpreter's audit hook.

1. The daemon is started in a temporary folder with a configuration folder of its own that names no
   engine, left idle, asked for its status, and stopped. The audit hook (sys.addaudithook) records
   every socket, name lookup, program start and foreign-function call. Not one connection to a
   network address and not one name lookup may appear; the only sockets are the local doors (the
   control door's Unix socket in the temporary runtime folder, and the loopback browser view on a port
   the test lets the system choose); the only programs started are the test's own stand-ins for
   systemctl and loginctl, by their full path in the test's folder or by name under a PATH that holds
   only that folder; the stand-ins are unchanged after the run. The daemon refuses every interpreter
   but free-threaded CPython 3.14t; on any other CPython (the hosted gate runs 3.12) the test stands
   in for that one interpreter check, so that the same daemon code runs before every merge.
2. A stand-in engine is started through the product's own launch path with a base address the
   person configured: a listener of the test on the loopback address. The product itself opens no
   connection; the only address it hands the engine is that one, and the engine reaches exactly it.

What the hook cannot see, and what this proof therefore does not cover (NOT_COVERED below): what a
program started by the daemon reaches by itself (here only the test's own stand-ins run, and they
reach nothing); the traffic of a socket made from a descriptor (refused here when its family is not
named; with its family named it is seen only as a new socket, and this test hands the daemon no
descriptor); a call into the operating system through a foreign-function module (every ctypes event
is refused here, and the static proof refuses the modules); what the operating system does by itself.

Nothing leaves this machine: every address in this file is a loopback address or a path inside a
temporary folder the test made. The idle time is a setting: RECURSION_PROOF_IDLE_SECONDS (shipped
first value below). Run by itself: python3 core/tests/test_no_call_home_runtime.py
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

# Figures of this test, each a setting read from the environment with its shipped first value here.
IDLE_SECONDS = float(os.environ.get("RECURSION_PROOF_IDLE_SECONDS", "3"))
START_SECONDS = float(os.environ.get("RECURSION_PROOF_START_SECONDS", "30"))

FREE_THREADED_314 = (sys.implementation.name == "cpython" and sys.version_info[:2] == (3, 14)
                     and bool(sysconfig.get_config_var("Py_GIL_DISABLED")))
DAEMON_RUNS_HERE = sys.platform.startswith("linux") and sys.implementation.name == "cpython"

# What this proof does not cover, in the words a person reads (printed after a run of this file).
NOT_COVERED = (
    "what a program the daemon starts reaches by itself (only the test's own stand-ins run here, and "
    "they reach nothing; an engine's own traffic is the engine's setting)",
    "the traffic of a socket made from a descriptor with its family named (seen only as a new socket; "
    "the test hands the daemon no descriptor, and one with no family named is refused)",
    "a call into the operating system through a foreign-function module (every ctypes event is refused "
    "here; the static proof refuses the modules in shipped source)",
    "what the operating system does by itself (its own name lookups, time and package services)",
    "what the hook is not told: it sees only what Python code reports to it, so code that replaces a "
    "module's sys or calls into the operating system directly is not seen; this proof holds only "
    "together with the static proof (tools/network_reach.py)",
    "plugins: a plugin reaches a network only under a grant a person gave; that proof is the plugin "
    "wall's (core/recursion_core/plugin_security)",
    "the known ways past the proofs that are not built: the rows of tools/network_gaps.toml, named in the "
    "README; together with the static proof this proof holds for the kinds its tests plant",
)

# The audit events that can reach a network, look up a name, start a program or call foreign code.
WATCHED = ("socket.", "subprocess.", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
           "os.forkpty", "pty.", "urllib.", "http.", "ftplib.", "smtplib.", "imaplib.", "poplib.",
           "nntplib.", "telnetlib.", "webbrowser.", "ssl.", "ctypes.")
LOOKUPS = {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex", "socket.gethostbyaddr",
           "socket.getnameinfo", "socket.getfqdn"}

# The hook the daemon child installs before anything of the product is imported. With the environment
# setting RECURSION_PROOF_STAND_IN_INTERPRETER_CHECK=1 (set by this test only where the interpreter is
# not free-threaded 3.14t) the daemon's one interpreter check is stood in for before the daemon runs.
BOOT = r'''
import json, os, sys, threading
_out = open(sys.argv[1], "a", buffering=1, encoding="utf-8")
_lock = threading.Lock()
_watched = tuple(json.loads(sys.argv[2]))
def _plain(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    try:
        import socket as _s
        if isinstance(value, _s.socket):
            return {"socket_family": int(value.family)}
    except Exception:
        pass
    return type(value).__name__
def _hook(event, args):
    if event.startswith(_watched):
        try:
            plain = [_plain(a) for a in args[:3]]
            if event == "subprocess.Popen":
                env = args[3] if len(args) > 3 else None
                path = env.get("PATH") if hasattr(env, "get") else os.environ.get("PATH")
                plain.append({"PATH": path if isinstance(path, str) else _plain(path)})
            line = json.dumps([event, plain])
        except Exception as exc:
            line = json.dumps([event, ["unrecorded: " + type(exc).__name__]])
        with _lock:
            _out.write(line + "\n")
sys.addaudithook(_hook)
if os.environ.get("RECURSION_PROOF_STAND_IN_INTERPRETER_CHECK") == "1":
    import recursion_core.daemon as _daemon
    _daemon.require_free_threaded = lambda: None
sys.argv = ["recursion_core.daemon"] + sys.argv[3:]
import runpy
runpy.run_module("recursion_core.daemon", run_name="__main__", alter_sys=True)
'''


def _plain(value):
    """An audit argument as plain data: text, numbers and lists of them; anything else by its type."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    return type(value).__name__


def read_events(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _program_allowed(args, allowed_programs):
    """A program start is allowed only for a stand-in named by its full path, or by its bare name under
    a PATH that is exactly the stand-in's own folder (a program of the same name elsewhere is refused)."""
    program = args[0] if args and args[0] else (args[1][0] if len(args) > 1 and args[1] else None)
    program = str(program)
    if program in allowed_programs:
        return True
    extra = args[3] if len(args) > 3 and isinstance(args[3], dict) else {}
    path = extra.get("PATH")
    if "/" not in program and isinstance(path, str) and os.pathsep not in path:
        return os.path.join(path, program) in allowed_programs
    return False


def judge(events, *, tmp: Path, allowed_programs=(), allowed_addresses=()):
    """Every event that breaks the promise, as words; an empty list means the promise held."""
    problems = []
    inside = str(tmp)
    allowed = {tuple(a) if isinstance(a, (list, tuple)) else a for a in allowed_addresses}
    for event, args in events:
        if event in LOOKUPS:
            problems.append(f"a name lookup was attempted: {event} {args}")
        elif event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
            address = args[1] if len(args) > 1 else None
            if isinstance(address, str) and address.startswith(inside):
                continue
            if isinstance(address, list) and tuple(address[:2]) in allowed:
                continue
            problems.append(f"a connection was attempted to an address nobody configured: {event} {address}")
        elif event == "socket.bind":
            address = args[1] if len(args) > 1 else None
            if isinstance(address, str) and address.startswith(inside):
                continue
            if isinstance(address, list) and address[0] == "127.0.0.1":
                continue
            problems.append(f"a socket was bound to a door that is not local: {address}")
        elif event == "socket.__new__":
            family = args[1] if len(args) > 1 else None
            if family == -1:
                problems.append("a socket was made from a descriptor with no family named: what it reaches "
                                "is not seen by the hook")
            elif family not in (int(socket.AF_UNIX), int(socket.AF_INET)):
                problems.append(f"a socket of family {family} was made")
        elif event == "subprocess.Popen":
            if not _program_allowed(args, allowed_programs):
                problems.append(f"a program was started that is not one of the test's stand-ins (what it "
                                f"reaches is not seen by the hook): {args[:2]}")
        elif event.startswith(("os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "pty.")):
            problems.append(f"a program was started outside the checked launch: {event} {args}")
        elif event.startswith("ctypes."):
            problems.append(f"a foreign-function call (what it reaches is not seen by the hook): {event}")
        elif event.startswith(("urllib.", "http.", "ftplib.", "smtplib.", "imaplib.", "poplib.", "nntplib.",
                               "telnetlib.", "webbrowser.", "ssl.")):
            problems.append(f"a network library was used: {event} {args}")
    return problems


def _stand_in_bin(tmp: Path) -> Path:
    bins = tmp / "bin"
    bins.mkdir()
    (bins / "loginctl").write_text('#!/bin/sh\n[ "$1" = show-user ] || exit 91\nprintf "yes\\n"\n')
    (bins / "systemctl").write_text('#!/bin/sh\n[ "$1" = --user ] || exit 92\nexit 0\n')
    for program in bins.iterdir():
        program.chmod(0o700)
    return bins


def _digest(folder: Path):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())}


def _hooked_child(code: str, events_path: Path, tmp: Path):
    """Run a small Python program of the test under the same hook (as data in a file of the test)."""
    hook = BOOT.split("if os.environ.get(")[0]
    script = tmp / "child.py"
    script.write_text(hook + "\n" + code, encoding="utf-8")
    run = subprocess.run([sys.executable, "-I", "-B", str(script), str(events_path), json.dumps(WATCHED)],
                         cwd=str(tmp), capture_output=True, text=True, timeout=START_SECONDS)
    return run


class JudgeTests(unittest.TestCase):
    """The judge is itself held: each kind of reach it must refuse is refused."""

    def test_the_judge_refuses_each_kind_of_reach(self):
        tmp = Path(tempfile.gettempdir()) / "judge-only-never-made"
        standin = str(tmp / "bin" / "systemctl")
        cases = (["socket.getaddrinfo", ["example.org", 443]],
                 ["socket.gethostbyaddr", ["127.0.0.1"]],
                 ["socket.connect", [{"socket_family": 2}, ["192.0.2.7", 443]]],
                 ["socket.connect", [{"socket_family": 1}, "/run/elsewhere/door"]],
                 ["socket.bind", [{"socket_family": 2}, ["0.0.0.0", 80]]],
                 ["socket.__new__", [{"socket_family": 10}, 10, 1]],
                 ["socket.__new__", [{"socket_family": -1}, -1, -1]],
                 ["subprocess.Popen", ["curl", ["curl", "x"]]],
                 ["subprocess.Popen", [None, ["systemctl", "--user"], None, {"PATH": "/usr/bin"}]],
                 ["subprocess.Popen", [None, ["systemctl"], None, {"PATH": str(tmp / "bin") + ":/usr/bin"}]],
                 ["subprocess.Popen", ["/usr/bin/systemctl", ["systemctl"], None, {"PATH": str(tmp / "bin")}]],
                 ["ctypes.dlopen", [None]],
                 ["ctypes.call_function", [1234, []]],
                 ["os.system", ["curl x"]],
                 ["urllib.Request", ["https://example.org", None]])
        for event in cases:
            with self.subTest(event=event):
                self.assertTrue(judge([event], tmp=tmp, allowed_programs=(standin,)), event)
        self.assertEqual(judge([["socket.connect", [{}, str(tmp / "control.sock")]],
                                ["socket.bind", [{}, ["127.0.0.1", 0]]],
                                ["subprocess.Popen", [None, ["systemctl", "--user"], None, {"PATH": str(tmp / "bin")}]],
                                ["subprocess.Popen", [standin, ["systemctl"], None, {"PATH": "/usr/bin"}]]],
                               tmp=tmp, allowed_programs=(standin,)), [])

    def test_the_routes_past_the_hook_are_seen_or_refused(self):
        # Each route runs for real in a small Python program of the test, under the same hook; nothing in it
        # reaches beyond this machine (a socket pair, a call of the C library's getpid, a program by path).
        with tempfile.TemporaryDirectory(prefix="rc-proof-") as name:
            tmp = Path(name)
            bins = _stand_in_bin(tmp)
            other = tmp / "elsewhere"
            other.mkdir()
            (other / "systemctl").write_text("#!/bin/sh\nexit 0\n")
            (other / "systemctl").chmod(0o700)
            routes = {
                "a foreign-function call": "import ctypes\nctypes.CDLL(None).getpid()\n",
                "a socket made from a descriptor": "import socket\na, b = socket.socketpair()\n"
                                                   "s = socket.socket(fileno=a.detach())\ns.close(); b.close()\n",
                "a program named like a stand-in, from another folder": (
                    "import subprocess\nsubprocess.run([%r, '--user'], env={'PATH': %r}, timeout=10)\n"
                    % (str(other / "systemctl"), str(other))),
                "a child Python process": "import subprocess, sys\n"
                                          "subprocess.run([sys.executable, '-I', '-c', 'pass'], timeout=10)\n",
                "a program that is not Python, by its bare name": (
                    "import subprocess\nsubprocess.run(['systemctl', '--user'], env={'PATH': %r}, timeout=10)\n"
                    % str(other)),
            }
            allowed = (str(bins / "systemctl"), str(bins / "loginctl"))
            for route, code in routes.items():
                with self.subTest(route=route):
                    events_path = tmp / ("events-%d.jsonl" % len(route))
                    run = _hooked_child(code, events_path, tmp)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    events = read_events(events_path)
                    self.assertTrue(events, route)  # the hook saw the route
                    self.assertTrue(judge(events, tmp=tmp, allowed_programs=allowed), (route, events))
            # The control: the stand-in itself, by name under its own folder, passes.
            events_path = tmp / "events-control.jsonl"
            run = _hooked_child("import subprocess\nsubprocess.run(['systemctl', '--user'], env={'PATH': %r}, "
                                "timeout=10)\n" % str(bins), events_path, tmp)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(judge(read_events(events_path), tmp=tmp, allowed_programs=allowed), [])


class SuiteHookTests(unittest.TestCase):
    """tools/network_audit.py, the hook that is run once over the whole suite, is itself held."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("network_audit", ROOT / "tools" / "network_audit.py")
        cls.audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.audit)

    def _run(self, code, *extra):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = self.audit.main([*extra, "--", sys.executable, "-B", "-c", code])
        return status, out.getvalue()

    def test_a_local_door_passes(self):
        status, out = self._run("import sys; sys.audit('socket.connect', None, ('127.0.0.1', 9)); "
                                "sys.audit('socket.getaddrinfo', 'localhost', 9, 0, 0, 0); "
                                "sys.audit('http.client.connect', None, '127.0.0.1', 9)")
        self.assertEqual(status, 0, out)
        self.assertIn("PROVEN", out)

    def test_a_planted_reach_is_refused(self):
        # The events are raised by hand with sys.audit: nothing is looked up and nothing is sent.
        for code in ("import sys; sys.audit('socket.getaddrinfo', 'reports.example.com', 443, 0, 0, 0)",
                     "import sys; sys.audit('socket.connect', None, ('192.0.2.7', 443))",
                     "import sys; sys.audit('urllib.Request', 'https://reports.example.com/', None, {}, 'GET')",
                     "import sys; sys.audit('http.client.connect', None, 'reports.example.com', 443)",
                     "import sys; sys.audit('ctypes.dlopen', None)",
                     "import sys; sys.audit('subprocess.Popen', None, ['curl', '-s', 'x'], None, None)"):
            with self.subTest(code=code):
                status, out = self._run(code)
                self.assertEqual(status, 1, out)
                self.assertIn("NOT PROVEN", out)

    def test_a_program_that_is_not_python_is_named_and_refused_unless_allowed(self):
        code = "import sys; sys.audit('subprocess.Popen', None, ['bash', '-c', 'true'], None, None)"
        status, out = self._run(code)
        self.assertEqual(status, 1, out)
        self.assertIn("bash", out)
        self.assertIn("not watched", out)
        status, out = self._run(code, "--allow-program", "bash")
        self.assertEqual(status, 0, out)
        self.assertIn("bash", out)  # an allowed program is still named, with what is not watched

    def test_a_hook_that_saw_nothing_is_refused(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = self.audit.main(["--", sys.executable, "-I", "-c", "pass"])
        self.assertEqual(status, 1)
        self.assertIn("NOT PROVEN", out.getvalue())

    def test_a_green_run_names_the_ways_it_does_not_watch(self):
        status, out = self._run("pass")
        self.assertEqual(status, 0, out)
        self.assertIn("tools/network_gaps.toml", out)

    def test_a_whole_suite_run_says_why_it_is_not_proven(self):
        unhooked = ("process 1: started the Python program 'p', which does not load the audit hook (it is "
                    "started with -I): what it reaches is not watched")
        self.assertIn("NOT PROVEN today on a clean checkout", self.audit.whole_suite_note([unhooked]))
        self.assertIn("NOT PROVEN today on a clean checkout",
                      self.audit.whole_suite_note(["2 Python process(es) did not load the audit hook"]))
        self.assertIsNone(self.audit.whole_suite_note(["process 1: socket.connect to ['192.0.2.7', 443]"]))

    def _starts(self, *argvs, allowed=(), audit=True):
        events = [(1, "subprocess.Popen", [a[0], list(a), None, {"PATH": None, "audit": audit}]) for a in argvs]
        return self.audit.judge(events, allowed_programs=allowed)

    def test_a_program_started_through_a_launcher_is_judged_by_the_program_it_starts(self):
        # env and unshare start any program: no row allows them; the program they start is judged.
        for argv in (["env", "bash", "-c", "true"], ["/usr/bin/env", "-u", "X", "A=1", "bash"],
                     ["unshare", "--", "bash", "-c", "true"], ["unshare", "-r", "-w", "/tmp", "bash"],
                     ["env", "unshare", "bash"]):
            with self.subTest(argv=argv):
                problems = self._starts(argv, allowed=("sleep",))
                self.assertTrue(any("'bash'" in p and "not watched" in p for p in problems), problems)
        for argv in (["env", "-S", "bash -c true"], ["env"], ["unshare", "-r"]):
            with self.subTest(argv=argv):
                self.assertTrue(any("cannot be told" in p for p in self._starts(argv)), argv)
        self.assertEqual(self._starts(["env", "A=1", "sleep", "1"], ["unshare", "-r", "--", "sleep", "1"],
                                      allowed=("sleep",)), [])
        self.assertNotIn("env", self.audit.load_programs(ROOT))
        self.assertNotIn("unshare", self.audit.load_programs(ROOT))

    def test_only_a_real_interpreter_is_taken_for_python(self):
        for prog in ("python-helper", "/tmp/x/python-anything", "pythonista", "./python3.12-wrapper"):
            with self.subTest(prog=prog):
                self.assertFalse(self.audit.is_python(prog))
                self.assertTrue(any("not watched" in p for p in self._starts([prog, "x"])), prog)
        for prog in ("python3", "python3.12", "/usr/bin/python3.14t", sys.executable):
            with self.subTest(prog=prog):
                self.assertTrue(self.audit.is_python(prog))
                self.assertEqual(self._starts([prog, "x.py"]), [])

    def test_a_python_process_that_does_not_load_the_hook_is_refused(self):
        for argv in ([sys.executable, "-I", "-c", "pass"], [sys.executable, "-S", "x.py"],
                     [sys.executable, "-BE", "x.py"], [sys.executable, "-X", "gil=0", "-I", "x.py"],
                     ["env", "-i", sys.executable, "x.py"]):
            with self.subTest(argv=argv):
                self.assertTrue(any("does not load the audit hook" in p for p in self._starts(argv)), argv)
        self.assertTrue(any("does not load the audit hook" in p
                            for p in self._starts([sys.executable, "x.py"], audit=False)))
        # A real run: a hooked child starts a Python grandchild with -I; the whole run is NOT PROVEN.
        status, out = self._run("import subprocess, sys; subprocess.run([sys.executable, '-I', '-c', 'pass'])")
        self.assertEqual(status, 1, out)
        self.assertIn("does not load the audit hook", out)


@unittest.skipUnless(DAEMON_RUNS_HERE, "the daemon runs only on Linux CPython")
class IdleDaemonTests(unittest.TestCase):
    def test_the_idle_daemon_reaches_no_address_and_looks_up_no_name(self):
        from recursion_core.control.client import SocketAPI

        with tempfile.TemporaryDirectory(prefix="rc-proof-") as name:
            tmp = Path(name)
            os.chmod(tmp, 0o700)
            bins = _stand_in_bin(tmp)
            before = _digest(bins)
            runtime = tmp / "runtime"
            runtime.mkdir(mode=0o700)
            (tmp / "config" / "recursion").mkdir(parents=True)  # the person's settings: no engine named
            events_path = tmp / "events.jsonl"
            env = {"PATH": str(bins), "HOME": str(tmp), "XDG_RUNTIME_DIR": str(runtime),
                   "XDG_STATE_HOME": str(tmp / "state"), "XDG_CONFIG_HOME": str(tmp / "config"),
                   "TMPDIR": str(tmp), "PYTHONPATH": str(CORE), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
            if not FREE_THREADED_314:
                env["RECURSION_PROOF_STAND_IN_INTERPRETER_CHECK"] = "1"
            log_path = tmp / "daemon.log"
            with log_path.open("w") as log:
                # close_fds (the default) and no pass_fds: the daemon is handed no descriptor but 0, 1 and 2.
                child = subprocess.Popen([sys.executable, "-X", "gil=0", "-B", "-c", BOOT, str(events_path),
                                          json.dumps(WATCHED), "--gui-port", "0", "--sweep-seconds", "0.05"],
                                         env=env, stdout=log, stderr=subprocess.STDOUT, cwd=str(tmp),
                                         close_fds=True)
                try:
                    api = SocketAPI(str(runtime / "recursion" / "control.sock"), timeout_s=1)
                    deadline = time.monotonic() + START_SECONDS
                    answer = None
                    while time.monotonic() < deadline and child.poll() is None:
                        answer = api.call("run.list")
                        if answer.get("code") == "ok":
                            break
                        time.sleep(0.05)
                    self.assertIsNone(child.poll(), log_path.read_text())
                    self.assertEqual((answer or {}).get("code"), "ok", (answer, log_path.read_text()))
                    time.sleep(IDLE_SECONDS)  # idle: sweeps and reconciles run on their own
                    for method in ("run.list", "plugin.list", "route.list"):
                        self.assertIn("code", api.call(method))
                    child.terminate()
                    self.assertEqual(child.wait(timeout=START_SECONDS), 0, log_path.read_text())
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=START_SECONDS)
            events = read_events(events_path)
            names = {e for e, _ in events}
            # A hook that saw nothing is a broken hook: the control door must have been seen.
            self.assertIn("socket.bind", names, events[:20])
            self.assertTrue(any(e == "socket.bind" and isinstance(a[1], str) and a[1].endswith("control.sock")
                                for e, a in events), events[:20])
            problems = judge(events, tmp=tmp, allowed_programs=(str(bins / "systemctl"), str(bins / "loginctl")))
            self.assertEqual(problems, [], "\n".join(problems))
            self.assertEqual(_digest(bins), before)  # the stand-ins that ran are the test's own, unchanged


class ReadmeWordsTests(unittest.TestCase):
    """The words a person reads first say what the commands print, what is proven and what is not."""

    def _section(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        start = text.index("## What Recursion connects to")
        end = min(i for i in (text.find("\n## ", start + 4), text.find("\n> ", start)) if i > 0)
        return text[start:end]

    def test_the_readme_names_the_commands_and_what_is_not_proven(self):
        section = self._section()
        for words in ("python3 tools/network_reach.py", "python3 core/tests/test_no_call_home_runtime.py",
                      "python3 tools/network_audit.py", "NOT PROVEN", "Proven before every merge",
                      "Not proven:", "the engines' own traffic", "under a grant you gave",
                      "what the operating system does by itself", "made from a descriptor",
                      "foreign-function module", "not part of the hosted gate", "Windows",
                      "a later change of a kind those tests plant", "a way the scan cannot see is not caught",
                      "is judged by the program it starts", "only a real interpreter is taken for Python",
                      "a Python process that does not load the hook",
                      "code that hides from the hook", "sees only what Python code reports to it",
                      "holds only together with the static proof", "a file path that reaches a network by itself"):
            with self.subTest(words=words):
                self.assertIn(words, " ".join(section.split()))

    def test_the_readme_claims_no_more_than_the_tests_hold(self):
        # Two sentences once claimed more than held: every later call home goes red, and the whole-suite
        # run names every program that is not Python. Neither may come back.
        section = " ".join(self._section().split())
        for claim in ("a later change that adds a call home, a module or a refused construct goes red",
                      "names every program that is not Python"):
            with self.subTest(claim=claim):
                self.assertNotIn(claim, section)

    def _gaps(self):
        import tomllib
        with open(ROOT / "tools" / "network_gaps.toml", "rb") as fh:
            return tomllib.load(fh)

    def test_every_known_way_past_the_proofs_is_named_with_its_reason(self):
        data = self._gaps()
        section = " ".join(self._section().split())
        self.assertTrue(data.get("gap"))
        self.assertIn("tools/network_gaps.toml", section)
        for row in data["gap"]:
            with self.subTest(row=row.get("id")):
                self.assertTrue(str(row.get("reason", "")).strip(), row)
                self.assertIn(row.get("proof"), ("static", "run-time", "both"), row)
                self.assertIn(" ".join(row["words"].split()), section)

    def test_no_sentence_of_the_claims_list_stands_in_the_readme(self):
        data = self._gaps()
        section = " ".join(self._section().split()).lower()
        self.assertTrue(data.get("claim"))
        for row in data["claim"]:
            with self.subTest(claim=row["text"]):
                self.assertTrue(str(row.get("reason", "")).strip(), row)
                self.assertNotIn(" ".join(row["text"].split()).lower(), section)

    def test_the_readme_says_the_whole_suite_run_is_not_proven_today(self):
        section = " ".join(self._section().split())
        for words in ("Proven today, by the second command", "There may be more",
                      "On a clean checkout today it prints NOT PROVEN", "is not a proof today"):
            with self.subTest(words=words):
                self.assertIn(words, section)

    def test_a_green_run_prints_no_refusal_meant_for_a_planted_case(self):
        # The second command's own planted cases keep their words to themselves: on a green run the
        # person reads no NOT PROVEN line of a hook that saw nothing.
        run = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "SuiteHookTests", "JudgeTests"],
                             capture_output=True, text=True, timeout=START_SECONDS * 4, cwd=str(ROOT))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotIn("the hook saw no process at all", run.stdout)
        self.assertIn("Not covered by this proof:", run.stdout)


class ConfiguredEngineTests(unittest.TestCase):
    def test_the_only_address_reached_is_the_one_the_person_configured(self):
        from recursion_core.child_env.launch import spawn
        from recursion_core.child_env.policy import ChildRoute, build_child_env

        with tempfile.TemporaryDirectory(prefix="rc-proof-") as name:
            tmp = Path(name)
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(("127.0.0.1", 0))
            listener.listen(4)
            listener.settimeout(START_SECONDS)
            port = listener.getsockname()[1]
            configured = f"http://127.0.0.1:{port}"
            engine = tmp / "standin_engine.py"
            engine.write_text(
                "import os, socket, sys\n"
                "from urllib.parse import urlsplit\n"
                "u = urlsplit(os.environ['ANTHROPIC_BASE_URL'])\n"
                "s = socket.create_connection((u.hostname, u.port), timeout=10)\n"
                "s.sendall(b'stand-in engine'); s.close()\n", encoding="utf-8")
            env = build_child_env("claude_code", home=str(tmp), auth_mode="api_key",
                                  route=ChildRoute(api_key="stand-in-not-a-key", base_url=configured),
                                  parent={"PATH": "/usr/bin:/bin"})
            urls = sorted(v for v in env.values() if "://" in v)
            self.assertEqual(urls, [configured])  # the only address handed to the engine
            events = []
            recording = threading.Event()

            def hook(event, args):
                if recording.is_set() and event.startswith(WATCHED):
                    events.append([event, [_plain(a) for a in args[:3]]])

            sys.addaudithook(hook)
            recording.set()
            try:
                child = spawn([sys.executable, "-I", "-B", str(engine)], env, cwd=str(tmp))
                self.assertEqual(child.wait(timeout=START_SECONDS), 0)
            finally:
                recording.clear()
            # Accepted only now, after the watch ended: the backlog held the engine's one connection.
            conn, _peer = listener.accept()
            with conn:
                self.assertEqual(conn.recv(64), b"stand-in engine")
            listener.setblocking(False)
            with self.assertRaises(BlockingIOError):
                listener.accept()  # and there was no second one
            # The product itself opened no socket and looked up no name; it started only the engine.
            problems = judge(events, tmp=tmp, allowed_programs=(sys.executable,))
            self.assertEqual(problems, [], "\n".join(problems))
            self.assertEqual([e for e, _ in events if e.startswith("socket.")], [])
            self.assertTrue(any(e == "subprocess.Popen" for e, _ in events), events)


def _report(result):
    """The words a person reads after a run of this file: what was proven, and what was not."""
    print()
    if result.wasSuccessful() and not any("IdleDaemon" in str(t) for t, _ in result.skipped):
        print("PROVEN (run-time): the idle daemon reached no address and looked up no name; the configured "
              "engine reached only the address the person configured.")
    else:
        print("NOT PROVEN (run-time): a test failed or the idle daemon test did not run here; read above.")
    print("Not covered by this proof:")
    for line in NOT_COVERED:
        print(f"  - {line}")


if __name__ == "__main__":
    program = unittest.main(exit=False)
    _report(program.result)
    raise SystemExit(0 if program.result.wasSuccessful() else 1)
