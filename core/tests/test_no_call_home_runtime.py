"""The run-time proof that Recursion sends nothing home, watched by the interpreter's audit hook.

1. The daemon is started in a temporary folder with a configuration folder of its own that names no
   engine, left idle, asked for its status, and stopped. The audit hook (sys.addaudithook) records
   every socket, name lookup and program start. Not one connection to a network address and not one
   name lookup may appear; the only sockets are the local doors (the control door's Unix socket in
   the temporary runtime folder, and the loopback browser view on a port the test lets the system
   choose); the only programs started are the test's own stand-ins for systemctl and loginctl.
2. A stand-in engine is started through the product's own launch path with a base address the
   person configured: a listener of the test on the loopback address. The product itself opens no
   connection; the only address it hands the engine is that one, and the engine reaches exactly it.

Nothing leaves this machine: every address in this file is a loopback address or a path inside a
temporary folder the test made. The idle time is a setting: RECURSION_PROOF_IDLE_SECONDS (shipped
first value below). Run by itself: python3.14t -X gil=0 core/tests/test_no_call_home_runtime.py
"""
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

REAL_DAEMON = (sys.platform.startswith("linux") and sys.implementation.name == "cpython"
               and sys.version_info[:2] >= (3, 14) and bool(sysconfig.get_config_var("Py_GIL_DISABLED")))

# The audit events that can reach a network, look up a name, or start a program.
WATCHED = ("socket.", "subprocess.", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
           "os.forkpty", "pty.", "urllib.", "http.", "ftplib.", "smtplib.", "imaplib.", "poplib.",
           "nntplib.", "telnetlib.", "webbrowser.", "ssl.", "ctypes.dlopen")
LOOKUPS = {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex", "socket.gethostbyaddr",
           "socket.getnameinfo", "socket.getfqdn"}

# The hook the daemon child installs before anything of the product is imported.
BOOT = r'''
import json, sys, threading
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
            line = json.dumps([event, [_plain(a) for a in args[:3]]])
        except Exception as exc:
            line = json.dumps([event, ["unrecorded: " + type(exc).__name__]])
        with _lock:
            _out.write(line + "\n")
sys.addaudithook(_hook)
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
            if family not in (int(socket.AF_UNIX), int(socket.AF_INET), -1):
                problems.append(f"a socket of family {family} was made")
        elif event == "subprocess.Popen":
            program = args[0] if args and args[0] else (args[1][0] if len(args) > 1 and args[1] else None)
            if not any(str(program) == p or str(program).endswith("/" + p) for p in allowed_programs):
                problems.append(f"a program was started that is not one of the test's stand-ins: {program}")
        elif event.startswith(("os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "pty.")):
            problems.append(f"a program was started outside the checked launch: {event} {args}")
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


class JudgeTests(unittest.TestCase):
    """The judge is itself held: each kind of reach it must refuse is refused."""

    def test_the_judge_refuses_each_kind_of_reach(self):
        tmp = Path(tempfile.gettempdir()) / "judge-only-never-made"
        cases = (["socket.getaddrinfo", ["example.org", 443]],
                 ["socket.gethostbyaddr", ["127.0.0.1"]],
                 ["socket.connect", [{"socket_family": 2}, ["192.0.2.7", 443]]],
                 ["socket.connect", [{"socket_family": 1}, "/run/elsewhere/door"]],
                 ["socket.bind", [{"socket_family": 2}, ["0.0.0.0", 80]]],
                 ["socket.__new__", [{"socket_family": 10}, 10, 1]],
                 ["subprocess.Popen", ["curl", ["curl", "x"]]],
                 ["os.system", ["curl x"]],
                 ["urllib.Request", ["https://example.org", None]])
        for event in cases:
            with self.subTest(event=event[0]):
                self.assertTrue(judge([event], tmp=tmp), event)
        self.assertEqual(judge([["socket.connect", [{}, str(tmp / "control.sock")]],
                                ["socket.bind", [{}, ["127.0.0.1", 0]]]], tmp=tmp), [])


class SuiteHookTests(unittest.TestCase):
    """tools/network_audit.py, the hook that is run once over the whole suite, is itself held."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("network_audit", ROOT / "tools" / "network_audit.py")
        cls.audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.audit)

    def _run(self, code):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = self.audit.main(["--", sys.executable, "-B", "-c", code])
        return status, out.getvalue()

    def test_a_local_door_passes(self):
        status, out = self._run("import sys; sys.audit('socket.connect', None, ('127.0.0.1', 9)); "
                                "sys.audit('socket.getaddrinfo', 'localhost', 9, 0, 0, 0)")
        self.assertEqual(status, 0, out)
        self.assertIn("PROVEN", out)

    def test_a_planted_reach_is_refused(self):
        # The events are raised by hand with sys.audit: nothing is looked up and nothing is sent.
        for code in ("import sys; sys.audit('socket.getaddrinfo', 'reports.example.com', 443, 0, 0, 0)",
                     "import sys; sys.audit('socket.connect', None, ('192.0.2.7', 443))",
                     "import sys; sys.audit('urllib.Request', 'https://reports.example.com/', None, {}, 'GET')"):
            with self.subTest(code=code):
                status, out = self._run(code)
                self.assertEqual(status, 1, out)
                self.assertIn("NOT PROVEN", out)

    def test_a_hook_that_saw_nothing_is_refused(self):
        status, out = self.audit.main(["--", sys.executable, "-I", "-c", "pass"]), None
        self.assertEqual(status, 1)


@unittest.skipUnless(REAL_DAEMON, "the daemon runs only on Linux free-threaded CPython 3.14")
class IdleDaemonTests(unittest.TestCase):
    def test_the_idle_daemon_reaches_no_address_and_looks_up_no_name(self):
        from recursion_core.control.client import SocketAPI

        with tempfile.TemporaryDirectory(prefix="rc-proof-") as name:
            tmp = Path(name)
            os.chmod(tmp, 0o700)
            bins = _stand_in_bin(tmp)
            runtime = tmp / "runtime"
            runtime.mkdir(mode=0o700)
            (tmp / "config" / "recursion").mkdir(parents=True)  # the person's settings: no engine named
            events_path = tmp / "events.jsonl"
            env = {"PATH": str(bins), "HOME": str(tmp), "XDG_RUNTIME_DIR": str(runtime),
                   "XDG_STATE_HOME": str(tmp / "state"), "XDG_CONFIG_HOME": str(tmp / "config"),
                   "TMPDIR": str(tmp), "PYTHONPATH": str(CORE), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
            log_path = tmp / "daemon.log"
            with log_path.open("w") as log:
                child = subprocess.Popen([sys.executable, "-X", "gil=0", "-B", "-c", BOOT, str(events_path),
                                          json.dumps(WATCHED), "--gui-port", "0", "--sweep-seconds", "0.05"],
                                         env=env, stdout=log, stderr=subprocess.STDOUT, cwd=str(tmp))
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
            problems = judge(events, tmp=tmp, allowed_programs=("systemctl", "loginctl"))
            self.assertEqual(problems, [], "\n".join(problems))


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


if __name__ == "__main__":
    unittest.main()
