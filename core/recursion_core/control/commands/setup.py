"""``recursiond install | upgrade | uninstall``: the per-user service unit and the daemon's stores (lane INSTALL).

One command family in the shape of seam S2 (``control/commands/__init__.py`` on SPINE's #369):
``NAMES``, ``parser(family)`` and ``dispatch(args, client) -> exit status``. ``recursiond`` runs it
today (``recursion_core._entrypoints``); nothing is registered in the command table until SPINE's
seams merge, and then lane INSTALL's ``setup/hooks.py`` registers this same family.

Each command prints one JSON object on stdout. Success is ``"code": "ok"`` (exit 0), with
``changed`` (whether a file was written or removed), ``unit`` (its path) and ``next`` (the
``systemctl --user`` commands that apply the change; none is run here); ``install`` and
``upgrade`` add ``stores``, one ``{"kind", "path", "created"}`` for the breaker store and one for
the revocation store. A refusal is ``"code": <kind>`` with ``detail`` (exit 1). A file-system error
on the way (a folder the user cannot write, a full disk) is the refusal ``filesystem_error``, its
detail naming the error and the path; files written before it stay written, so the answer does not
say that nothing changed. Off Linux the family refuses ``platform_unsupported`` before it reads
anything (``recursiond`` refuses there first; this holds the same once the family is registered in
the control CLI).

``install`` and ``upgrade`` point the unit at the interpreter running this command, and first ask
that interpreter, started as the unit will start it (``-I``, from ``/``), what it is: it must be
free-threaded CPython 3.14 (``interpreter_not_free_threaded``), and it must import this same
``recursion_core`` with no ``PYTHONPATH`` (``not_installed``), or the daemon could not start.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from recursion_core.daemon_install import InstallRefused, Roots, daemon_exec_args, install, uninstall, upgrade

NAMES = ("install", "upgrade", "uninstall")
EXIT_OK, EXIT_REFUSED = 0, 1
_HELP = {
    "install": "create the daemon's stores and write its per-user systemd unit (twice changes nothing)",
    "upgrade": "create any missing store and rewrite the unit for this interpreter and this version",
    "uninstall": "remove the unit `install` wrote; the daemon's state folder and its stores stay",
}

# Run by the interpreter the unit will run, as the unit runs it. Standard library only.
_PROBE = r"""
import json, sys, sysconfig
info = {"implementation": sys.implementation.name, "version": list(sys.version_info[:2]),
        "free_threaded_build": bool(sysconfig.get_config_var("Py_GIL_DISABLED")), "package": None}
try:
    import recursion_core
    info["package"] = recursion_core.__file__
except ImportError:
    pass
print(json.dumps(info))
"""


def parser(family) -> None:
    for name in NAMES:
        family.add_parser(name, help=_HELP[name], description=_HELP[name])


def dispatch(args, client) -> int:
    """S2's dispatch. ``client`` is unused: these commands work before any daemon exists."""

    import recursion_core

    out = getattr(args, "out", None) or sys.stdout
    if not sys.platform.startswith("linux"):
        _print(out, {"code": "platform_unsupported", "action": args.command,
                     "detail": f"the per-user service needs Linux with systemd; this system is {sys.platform}"})
        return EXIT_REFUSED
    return run(args.command, out=out, roots=default_roots(), interpreter=sys.executable,
               package=recursion_core.__file__)


def default_roots() -> Roots:
    """This user's home as the user manager sees it (``%h``): the password database, not ``$HOME``."""

    import pwd  # POSIX only; ``recursiond`` refuses every command elsewhere before this runs

    return Roots(home=pwd.getpwuid(os.geteuid()).pw_dir)


def run(action: str, *, out, roots: Roots, interpreter: str, package: str, probe=None) -> int:
    probe = probe_interpreter if probe is None else probe
    try:
        if action == "uninstall":
            plan = uninstall(roots)
        elif action in ("install", "upgrade"):
            require_daemon_interpreter(probe(interpreter), interpreter, package)
            step = install if action == "install" else upgrade
            plan = step(roots, daemon_exec_args(interpreter))
        else:
            raise InstallRefused("action", repr(action))
    except InstallRefused as exc:
        _print(out, {"code": exc.kind, "action": action, "detail": exc.detail})
        return EXIT_REFUSED
    except OSError as exc:
        _print(out, {"code": "filesystem_error", "action": action, "detail": _filesystem_detail(exc, roots)})
        return EXIT_REFUSED
    answer = {"code": "ok", "action": action, "changed": plan.changed, "unit": plan.unit,
              "next": [list(command) for command in plan.commands]}
    if action != "uninstall":
        answer["interpreter"] = interpreter
        answer["stores"] = [{"kind": kind, "path": path, "created": created} for kind, path, created in plan.stores]
    _print(out, answer)
    return EXIT_OK


def _filesystem_detail(exc: OSError, roots: Roots) -> str:
    """The error and the path. A call on an open file or folder (``fsync``, ``read``, ``write``) names
    no path, so the unit's folder, where every file these commands write or remove is, is named."""

    detail = f"{type(exc).__name__}: {exc}"
    if exc.filename is None:
        detail += f" (in {os.path.join(roots.home, '.config', 'systemd', 'user')})"
    return detail


def probe_interpreter(interpreter: str) -> dict:
    """What ``interpreter`` is, asked of it as the unit will start it: ``-I``, working folder ``/``."""

    try:
        done = subprocess.run([interpreter, "-I", "-c", _PROBE], cwd="/", capture_output=True, text=True,
                              timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallRefused("interpreter_unknown", f"{interpreter} could not be run: {type(exc).__name__}") from None
    try:
        info = json.loads(done.stdout) if done.returncode == 0 else None
    except ValueError:
        info = None
    if type(info) is not dict:
        raise InstallRefused("interpreter_unknown", f"{interpreter} did not answer the probe (exit {done.returncode})")
    return info


def require_daemon_interpreter(info: dict, interpreter: str, package: str) -> None:
    version = info.get("version")
    if (info.get("implementation") != "cpython" or version != [3, 14]
            or info.get("free_threaded_build") is not True):
        shown = ".".join(str(part) for part in version) if type(version) is list else "unknown"
        build = "free-threaded" if info.get("free_threaded_build") is True else "with the GIL"
        raise InstallRefused(
            "interpreter_not_free_threaded",
            f"the daemon needs free-threaded CPython 3.14 (python3.14t); {interpreter} is "
            f"{info.get('implementation')} {shown} {build}. Install csl-recursion into a python3.14t "
            "environment and run its recursiond.")
    found = info.get("package")
    if type(found) is not str:
        raise InstallRefused(
            "not_installed",
            f"{interpreter} cannot import recursion_core without PYTHONPATH, so the unit could not start "
            "the daemon. Install csl-recursion into that interpreter's environment and run its recursiond.")
    if os.path.realpath(os.path.dirname(found)) != os.path.realpath(os.path.dirname(package)):
        raise InstallRefused(
            "not_installed",
            f"{interpreter} imports recursion_core from {os.path.dirname(found)}, not the copy running this "
            f"command ({os.path.dirname(package)}). Run the recursiond installed beside that interpreter.")


def _print(out, answer: dict) -> None:
    print(json.dumps(answer, sort_keys=True), file=out)
