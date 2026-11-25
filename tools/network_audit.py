"""Run a command with the interpreter's audit hook in every Python process it starts, and judge
every network address those processes reached.

Usage: python3 tools/network_audit.py [--allow-program NAME ...] [--] [command ...]
       (no command: the product's whole test suite, python -m unittest discover over core/recursion_core,
       with the programs of tools/network_audit.toml allowed)

A temporary folder holds a sitecustomize module that each Python process of the command loads at
start (through PYTHONPATH); it records every socket connection, bind, name lookup, program start and
foreign-function load into the folder. After the command ends, each reached address must be a door on
this machine: a Unix socket path, or a loopback address. A Python process started with -I, -S or a
cleared environment does not load the hook; the report counts the processes it saw so that the
coverage is plain. A program that is not Python is not watched at all: the report names each one,
and each must be allowed by name (--allow-program, or the list file for the suite). Exit status 1
when an address that is not local was reached or looked up, a program not allowed was started, a
foreign-function module was loaded, or no process was seen at all (a hook that saw nothing is a broken
hook).
"""
from __future__ import annotations

import ipaddress
import json
import os
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
                plain.append({"PATH": path if isinstance(path, str) else None})
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


def is_python(name: str) -> bool:
    return name.startswith("python") or name == os.path.basename(sys.executable)


def judge(events, allowed_programs=()):
    """Every reach that is not a door on this machine, and every program that is not watched and not
    allowed, as words."""
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
        elif event.startswith(("subprocess.Popen", "os.system", "os.posix_spawn", "os.exec")):
            name = program_of(event, args)
            if not is_python(name) and name not in allowed_programs:
                problems.append(f"process {pid}: started {name!r}, a program that is not Python and is not "
                                f"allowed: what it reaches is not watched (allow it by name only if it "
                                f"reaches no network)")
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
    print("Not watched by this run: a program started through another listed program (env, unshare), a "
          "program whose name begins with python, a Python process that does not load the hook (-I, -S, a "
          "cleared environment), and code that hides from the hook (it sees only what Python code reports; "
          "the tests themselves are not scanned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
