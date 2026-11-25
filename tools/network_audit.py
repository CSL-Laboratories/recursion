"""Run a command with the interpreter's audit hook in every Python process it starts, and judge
every network address those processes reached.

Usage: python3 tools/network_audit.py [--allow-program NAME ...] [--] [command ...]
       (no command: the product's whole test suite, python -m unittest discover over core/recursion_core,
       with the programs of tools/network_audit.toml allowed)

A temporary folder holds a sitecustomize module that each Python process of the command loads at
start (through PYTHONPATH); it records every socket connection, bind, name lookup, program start and
foreign-function load into the folder. After the command ends, each reached address must be a door on
this machine: a Unix socket path, or a loopback address. A Python process started with -I, -S, -E or a
cleared environment does not load the hook, and is refused; so is a run with fewer hooked processes than
Python starts. Only a real interpreter (python3, python3.N, python3.Nt, or this one by its real path) is
taken for Python. A program that is not Python is not watched at all: the report names each one, and
each must be allowed by name (--allow-program, or the list file for the suite); a program that starts
another (a [[launcher]] row: env, unshare) is judged by the program it starts. Exit status 1
when an address that is not local was reached or looked up, a program not allowed was started, a
foreign-function module was loaded, or no process was seen at all (a hook that saw nothing is a broken
hook).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python before 3.11 is not supported
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]
PROGRAMS_FILE = Path("tools/network_audit.toml")

HOOK = r'''
import json, os, sys, threading
_dir = os.environ.get("RECURSION_AUDIT_DIR")
if _dir:
    _lock = threading.Lock()
    _path = os.path.join(_dir, "events-%d.jsonl" % os.getpid())
    _out = open(_path, "a", buffering=1, encoding="utf-8")  # the file says the process loaded the hook
    _once = set()
    def _plain(v):
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v
        if isinstance(v, bytes):
            return v.decode("utf-8", "replace")
        if isinstance(v, (tuple, list)):
            return [_plain(x) for x in v]
        if isinstance(v, os.PathLike):
            return os.fspath(v)
        return type(v).__name__
    _watched = ("socket.connect", "socket.sendto", "socket.sendmsg", "socket.bind", "socket.getaddrinfo",
                "socket.gethostbyname", "socket.gethostbyname_ex", "socket.gethostbyaddr", "socket.getnameinfo",
                "subprocess.Popen", "urllib.Request", "http.client.connect", "os.system", "os.posix_spawn",
                "os.exec")
    def _hook(event, args):
        if event.startswith("ctypes."):
            if event in _once:
                return
            _once.add(event)
        elif not event.startswith(_watched):
            return
        try:
            plain = [_plain(a) for a in args[:3]]
            if event == "subprocess.Popen":
                env = args[3] if len(args) > 3 else None
                path = env.get("PATH") if hasattr(env, "get") else os.environ.get("PATH")
                plain.append({"PATH": path if isinstance(path, str) else None,
                              "audit": (env.get("RECURSION_AUDIT_DIR") is not None) if hasattr(env, "get") else True})
            line = json.dumps([event, plain])
        except Exception:
            line = json.dumps([event, ["unrecorded"]])
        with _lock:
            _out.write(line + "\n")
    sys.addaudithook(_hook)
'''

LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _is_local_host(host) -> bool:
    if not isinstance(host, str):
        return host is None
    bare = host.strip("[]").split("%", 1)[0]
    if bare.lower() in LOCAL_NAMES or bare == "":
        return True
    try:
        address = ipaddress.ip_address(bare)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback or (mapped or address).is_unspecified


def program_of(event, args):
    """The program a start names: its last path part (the hook records the call, not the file run)."""
    if event == "subprocess.Popen":
        prog = args[0] if args and args[0] else (args[1][0] if len(args) > 1 and isinstance(args[1], list)
                                                 and args[1] else args[1] if len(args) > 1 else None)
    elif event == "os.system":
        return "a shell (os.system)"
    else:
        prog = args[0] if args else None
    return os.path.basename(str(prog)) if prog is not None else "an unnamed program"


# A Python interpreter by its file name (python, python3, python3.12, python3.14t) or by its real path.
PYTHON_NAME = re.compile(r"python(3(\.[0-9]+)?t?)?")
# Options of a Python start that keep it from loading the hook (no site, no PYTHONPATH).
PYTHON_UNHOOKED = set("ISE")


def is_python(prog: str) -> bool:
    """True only for a real Python interpreter: a name like python3.12 or the real path of this one."""
    prog = str(prog)
    if PYTHON_NAME.fullmatch(os.path.basename(prog)):
        return True
    return os.path.sep in prog and os.path.realpath(prog) == os.path.realpath(sys.executable)


def _argv(event, args):
    """The program and its arguments of a start, as far as the hook recorded them."""
    if event == "subprocess.Popen":
        argv = args[1] if len(args) > 1 and isinstance(args[1], list) else []
        prog = args[0] if args and args[0] else (argv[0] if argv else None)
        return (str(prog) if prog is not None else None), [str(x) for x in argv]
    if event.startswith(("os.posix_spawn", "os.exec")):
        argv = args[1] if len(args) > 1 and isinstance(args[1], list) else []
        return (str(args[0]) if args else None), [str(x) for x in argv]
    return None, []


def _python_unhooked(argv, start):
    """Why a Python start does not load the hook, or None."""
    i = 1
    while i < len(argv):
        opt = argv[i]
        if not opt.startswith("-") or opt in ("-c", "-m", "-"):
            break
        if opt in ("-X", "-W"):  # an option that takes the next argument as its value
            i += 2
            continue
        if not opt.startswith("--") and not opt.startswith(("-X", "-W")) and PYTHON_UNHOOKED & set(opt[1:]):
            return f"it is started with {opt}"
        i += 1
    if isinstance(start, dict) and start.get("audit") is False:
        return "its environment does not carry the hook"
    return None


def _inner(argv, value_options):
    """The program a launcher (env, unshare) starts: the first argument that is not an option, a value of
    an option, or (for env) an assignment. None when it cannot be told."""
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--":
            return argv[i + 1:] or None
        if a.startswith("-") and a != "-":
            if a in ("-S", "--split-string") and "env" in os.path.basename(argv[0]):
                return None  # a whole command line in one argument: not judged, refused
            if a in value_options:
                i += 2
                continue
            i += 1
            continue
        if "=" in a and "env" == os.path.basename(argv[0]):
            i += 1
            continue
        return argv[i:]
    return None


def _judge_start(pid, prog, argv, start, allowed_programs, launchers, problems, depth=0):
    name = os.path.basename(prog) if prog else "an unnamed program"
    if prog and is_python(prog):
        why = _python_unhooked([prog] + argv[1:], start)
        if why:
            problems.append(f"process {pid}: started the Python program {prog!r}, which does not load the "
                            f"audit hook ({why}): what it reaches is not watched")
        return
    if name in launchers:
        inner = _inner([prog] + argv[1:], launchers[name])
        if not inner or depth > 8:
            problems.append(f"process {pid}: started {name!r}, which starts another program, and which one "
                            f"cannot be told from {argv!r}: refused")
            return
        # env -i clears the environment: a Python program started by it does not carry the hook.
        cleared = name == "env" and any(a in ("-i", "--ignore-environment") or a == "-" for a in argv[1:argv.index(inner[0]) if inner[0] in argv else None])
        _judge_start(pid, inner[0], inner, {"audit": False} if cleared else start, allowed_programs, launchers,
                     problems, depth + 1)
        return
    if name not in allowed_programs:
        problems.append(f"process {pid}: started {name!r}, a program that is not Python and is not "
                        f"allowed: what it reaches is not watched (allow it by name only if it "
                        f"reaches no network)")


def judge(events, allowed_programs=(), launchers=None):
    """Every reach that is not a door on this machine, and every program that is not watched and not
    allowed, as words. A launcher (a program that starts another, such as env or unshare) is judged by
    the program it starts."""
    launchers = load_launchers(ROOT) if launchers is None else launchers
    problems = []
    for pid, event, args in events:
        if event in ("socket.connect", "socket.sendto", "socket.sendmsg", "socket.bind"):
            address = args[1] if len(args) > 1 else None
            if isinstance(address, str) or address is None:
                continue  # a Unix socket path (or an unnamed one)
            if isinstance(address, list) and address and _is_local_host(address[0]):
                continue
            problems.append(f"process {pid}: {event} to {address}, which is not a door on this machine")
        elif event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
                       "socket.gethostbyaddr"):
            if not (args and _is_local_host(args[0])):
                problems.append(f"process {pid}: {event} looked up {args[:1]}, which is not this machine")
        elif event == "socket.getnameinfo":
            sockaddr = args[0] if args else None
            if not (isinstance(sockaddr, list) and sockaddr and _is_local_host(sockaddr[0])):
                problems.append(f"process {pid}: {event} of {sockaddr}")
        elif event in ("urllib.Request", "http.client.connect"):
            # http.client.connect is raised with (connection, host, port); urllib.Request with (url, ...).
            target = (args[1] if len(args) > 1 else None) if event == "http.client.connect" else (
                args[0] if args else None)
            host = target if event == "http.client.connect" else None
            if host is None and isinstance(target, str):
                from urllib.parse import urlsplit
                host = urlsplit(target).hostname
            if not _is_local_host(host):
                problems.append(f"process {pid}: {event} {target}")
        elif event == "os.system":
            if "a shell (os.system)" not in allowed_programs:
                problems.append(f"process {pid}: started a shell (os.system): what it reaches is not watched")
        elif event.startswith(("subprocess.Popen", "os.posix_spawn", "os.exec")):
            prog, argv = _argv(event, args)
            start = args[3] if event == "subprocess.Popen" and len(args) > 3 else None
            _judge_start(pid, prog, argv, start, allowed_programs, launchers, problems)
        elif event.startswith("ctypes."):
            problems.append(f"process {pid}: {event}: a foreign-function call; what it reaches is not watched")
    return problems


def read(folder: Path):
    events, pids = [], set()
    for path in sorted(folder.glob("events-*.jsonl")):
        pid = int(path.stem.split("-", 1)[1])
        pids.add(pid)
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                event, args = json.loads(line)
                events.append((pid, event, args))
    return events, pids


def load_programs(root: Path = ROOT):
    """The programs the whole suite may start that are not Python, each with its reason."""
    with open(root / PROGRAMS_FILE, "rb") as fh:
        rows = tomllib.load(fh).get("program", [])
    return {row["name"]: row.get("reason", "") for row in rows if str(row.get("reason", "")).strip()}


def load_launchers(root: Path = ROOT):
    """The programs that start another program, each with the options that take a value: no row allows
    one of them by name; each is judged by the program it starts."""
    try:
        with open(root / PROGRAMS_FILE, "rb") as fh:
            rows = tomllib.load(fh).get("launcher", [])
    except OSError:
        return {}
    return {row["name"]: set(row.get("value_options", [])) for row in rows}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    allowed = []
    while argv[:1] == ["--allow-program"] and len(argv) > 1:
        allowed.append(argv[1])
        argv = argv[2:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    command = argv
    if not command:
        command = [sys.executable, "-B", "-m", "unittest", "discover", "-s", "core/recursion_core",
                   "-t", "core", "-p", "test_*.py"]
        allowed.extend(load_programs(ROOT))
    with tempfile.TemporaryDirectory(prefix="rc-audit-") as name:
        folder = Path(name)
        hook_dir = folder / "hook"
        hook_dir.mkdir()
        (hook_dir / "sitecustomize.py").write_text(HOOK, encoding="utf-8")
        events_dir = folder / "events"
        events_dir.mkdir()
        env = dict(os.environ)
        env["RECURSION_AUDIT_DIR"] = str(events_dir)
        env["PYTHONPATH"] = os.pathsep.join([str(hook_dir), str(ROOT / "core")] +
                                            ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        done = subprocess.run(command, cwd=str(ROOT), env=env)
        events, pids = read(events_dir)
        problems = judge(events, allowed)
        # Every Python start must have a process that wrote an events file; fewer means some did not load it.
        starts = 0
        for _pid, event, args in events:
            if event in ("subprocess.Popen", "os.posix_spawn"):
                prog, _a = _argv(event, args)
                starts += bool(prog and is_python(prog))
        expected = starts + (1 if command and is_python(command[0]) else 0)
        if len(pids) < expected:
            problems.append(f"{expected - len(pids)} Python process(es) did not load the audit hook "
                            f"({expected} started, {len(pids)} seen): what they reached is not watched")
    kinds, hosts, programs = {}, set(), {}
    for _pid, event, args in events:
        kinds[event] = kinds.get(event, 0) + 1
        if event in ("socket.connect", "socket.sendto", "socket.bind") and len(args) > 1 \
                and isinstance(args[1], list) and args[1]:
            hosts.add(str(args[1][0]))
        elif event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr") and args:
            hosts.add(str(args[0]))
        elif event == "http.client.connect" and len(args) > 1:
            hosts.add(str(args[1]))
        if event.startswith(("subprocess.Popen", "os.system", "os.posix_spawn", "os.exec")):
            p = program_of(event, args)
            programs[p] = programs.get(p, 0) + 1
    print(f"command exit status {done.returncode}; Python processes seen by the hook: {len(pids)}; "
          f"events: {', '.join(f'{k} {v}' for k, v in sorted(kinds.items())) or 'none'}")
    print(f"hosts and addresses seen: {', '.join(sorted(hosts)) or 'none'} (Unix socket paths not shown)")
    other = {p: n for p, n in programs.items() if not is_python(p)}
    print(f"programs started that are not Python (what they reach is not watched): "
          f"{', '.join(f'{p} {n}' for p, n in sorted(other.items())) or 'none'}")
    for p in problems:
        print(p)
    if not pids:
        print("NOT PROVEN: the hook saw no process at all, so it saw nothing (a broken hook).")
        return 1
    if problems:
        print(f"NOT PROVEN: {len(problems)} problem(s): a reach of an address that is not on this machine, "
              f"a program that is not watched and not allowed, or a foreign-function call.")
        return 1
    print("PROVEN: every address reached or looked up by a watched process was a door on this machine; "
          "the programs that are not Python are named above and are not watched.")
    print("Not watched by this run: code that hides from the hook (it sees only what Python code reports; "
          "the tests themselves are not scanned), and what a unit started by systemd-run runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
