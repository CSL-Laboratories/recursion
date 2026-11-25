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
Python starts. A real interpreter is taken for Python: by its name (python3, python3.N, python3.Nt), or by
its file whatever its name (this interpreter's own file, or a program file that holds the interpreter's
entry point; the hook looks at the file when the start happens). A hooked process still running when the
command ends makes the run NOT PROVEN. A program that is not Python is not watched at all: the report names each one, and
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
    _MAX_ARGS = int(os.environ.get("RECURSION_AUDIT_MAX_ARGS", "64"))
    _seen_files = {}
    _this = os.path.realpath(sys.executable)
    def _py(name, path, cwd):
        """Whether the file a start runs is a Python interpreter, whatever its name: this interpreter's own
        file, or a program file that holds the interpreter's entry point. None: it is there and cannot be read."""
        try:
            name = os.fsdecode(name)
        except Exception:
            return False
        if not name or "\x00" in name:
            return False
        if os.sep in name:
            base = os.fsdecode(cwd) if isinstance(cwd, (str, bytes, os.PathLike)) else os.getcwd()
            found = os.path.join(base, name)
            if not os.path.isfile(found):
                return False
        else:
            found = None
            for d in (path if path is not None else os.defpath).split(os.pathsep):
                c = os.path.join(d or ".", name)
                if os.path.isfile(c) and os.access(c, os.X_OK):
                    found = c
                    break
            if found is None:
                return False
        try:
            real = os.path.realpath(found)
            st = os.stat(real)
            key = (real, st.st_size, st.st_mtime_ns)
            if key in _seen_files:
                return _seen_files[key]
            answer = real == _this
            if not answer:
                import mmap
                with open(real, "rb") as fh:
                    if fh.read(4) == b"\x7fELF" and st.st_size:
                        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as m:
                            answer = m.find(b"Py_BytesMain") >= 0 or m.find(b"Py_Main") >= 0
            _seen_files[key] = answer
            return answer
        except Exception:
            return None
    def _hook(event, args):
        if event.startswith("ctypes."):
            if event in _once:
                return
            _once.add(event)
        elif not event.startswith(_watched):
            return
        try:
            plain = [_plain(a) for a in args[:3]]
            if event in ("subprocess.Popen", "os.posix_spawn", "os.exec"):
                popen = event == "subprocess.Popen"
                env = (args[3] if len(args) > 3 else None) if popen else (args[2] if len(args) > 2 else None)
                path = env.get("PATH") if hasattr(env, "get") else os.environ.get("PATH")
                path = path if isinstance(path, str) else None
                cwd = args[2] if popen and len(args) > 2 else None
                argv = list(args[1]) if len(args) > 1 and isinstance(args[1], (list, tuple)) else []
                exe = args[0] if args and args[0] else (argv[0] if argv else None)
                while len(plain) < 3:
                    plain.append(None)
                plain.append({"PATH": path,
                              "audit": (env.get("RECURSION_AUDIT_DIR") is not None) if hasattr(env, "get") else True,
                              "py_prog": _py(exe, path, cwd),
                              "py_args": [k for k in range(1, min(len(argv), _MAX_ARGS)) if _py(argv[k], path, cwd)]})
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
    is_env = os.path.basename(argv[0]) == "env"
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--":
            return argv[i + 1:] or None
        if a.startswith("--"):
            if is_env and (a == "--split-string" or a.startswith("--split-string=")):
                return None  # a whole command line in one argument: not judged, refused
            i += 2 if (a in value_options and "=" not in a) else 1
            continue
        if a == "-" and is_env:
            i += 1  # env's '-' is -i
            continue
        if a.startswith("-") and a != "-":
            letters = a[1:]
            if is_env and "S" in letters:
                return None  # -S inside a group (-iS, -vS) is the same split-string option: refused
            step = 1
            for k, letter in enumerate(letters):
                if "-" + letter in value_options:  # it takes the rest of the group, or the next argument
                    step = 2 if k == len(letters) - 1 else 1
                    break
            i += step
            continue
        if "=" in a and is_env:
            i += 1
            continue
        return argv[i:]
    return None


def _judge_start(pid, prog, argv, start, allowed_programs, launchers, problems, depth=0, base=0, pystarts=None):
    name = os.path.basename(prog) if prog else "an unnamed program"
    record = start if isinstance(start, dict) else {}
    by_file = record.get("py_prog") if base == 0 else (base in (record.get("py_args") or ()))
    if prog and (is_python(prog) or by_file):
        if pystarts is not None:
            pystarts.append(pid)
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
        # env -i clears the environment (also inside a group, -vi): a Python program it starts does not
        # carry the hook.
        before = [prog] + argv[1:]
        before = before[1:len(before) - len(inner)]
        cleared = name == "env" and any(a in ("--ignore-environment", "-") or (a.startswith("-") and not
                                        a.startswith("--") and "i" in a[1:]) for a in before)
        _judge_start(pid, inner[0], inner, dict(record, audit=False) if cleared else start, allowed_programs,
                     launchers, problems, depth + 1, base + len(before) + 1, pystarts)
        return
    if prog and record.get("py_prog", False) is None and base == 0:
        problems.append(f"process {pid}: started {name!r}, whose file could not be read when it started, so "
                        f"whether it is a Python interpreter cannot be told: refused")
        return
    if name not in allowed_programs:
        problems.append(f"process {pid}: started {name!r}, a program that is not Python and is not "
                        f"allowed: what it reaches is not watched (allow it by name only if it "
                        f"reaches no network)")


def judge(events, allowed_programs=(), launchers=None, pystarts=None):
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
            start = args[3] if len(args) > 3 and isinstance(args[3], dict) else None
            # os.exec keeps its process (and its events file): it is not a new Python start to count.
            _judge_start(pid, prog, argv, start, allowed_programs, launchers, problems,
                         pystarts=pystarts if event in ("subprocess.Popen", "os.posix_spawn") else None)
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


def whole_suite_note(problems):
    """The words a whole-suite run prints when Python children did not load the hook, or None."""
    n = sum(("does not load the audit hook" in p) or ("did not load the audit hook" in p) for p in problems)
    if not n:
        return None
    return (f"The whole suite is NOT PROVEN today on a clean checkout: the product's own tests start Python "
            f"children with -I or with a cleared environment, which the hook cannot reach ({n} of the "
            f"problems above). The README says so; this run names what it could not watch.")


def still_running(pids, events_dir):
    """The hooked processes that are still running (not ended, not a zombie) and are of this run: what they
    reach after the run is judged is not watched."""
    running = []
    for pid in sorted(pids):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except OSError:
            pass
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                if fh.read().rsplit(b")", 1)[-1].split()[:1] == [b"Z"]:
                    continue
            with open(f"/proc/{pid}/environ", "rb") as fh:
                if b"RECURSION_AUDIT_DIR=" + os.fsencode(str(events_dir)) + b"\x00" not in fh.read():
                    continue  # the number was given to another process since
        except OSError:
            pass  # cannot be told: it is taken as still running
        running.append(pid)
    return running


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    allowed = []
    while argv[:1] == ["--allow-program"] and len(argv) > 1:
        allowed.append(argv[1])
        argv = argv[2:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    command = argv
    suite = not command
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
        pystarts = []
        problems = judge(events, allowed, pystarts=pystarts)
        running = still_running(pids, events_dir)
        if running:
            problems.append(f"{len(running)} hooked process(es) still running when the command ended (pid "
                            f"{', '.join(str(p) for p in running[:10])}): what they reach later is not watched")
        # Every Python start must have a process that wrote an events file; fewer means some did not load it.
        starts = len(pystarts)
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
        note = whole_suite_note(problems) if suite else None
        if note:
            print(note)
        return 1
    print("PROVEN: every address reached or looked up by a watched process was a door on this machine; "
          "the programs that are not Python are named above and are not watched.")
    print("Not watched by this run: code that hides from the hook (it sees only what Python code reports; "
          "the tests themselves are not scanned), what a unit started by systemd-run runs, and the other "
          "run-time ways past it that are known and not built: the rows of tools/network_gaps.toml, named "
          "in the README.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
