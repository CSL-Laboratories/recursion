"""The systemd user unit for the per-user daemon. The text is data: nothing here starts systemd.

``Restart=on-failure`` and ``WantedBy=default.target`` are the directives in systemd.service(5) and
systemd.unit(5): the unit comes back after a crash, and ``enable`` attaches it to the user manager's
default target. ``KillMode=control-group`` ends only this unit's own processes: runs live in their
own transient ``recursion-run-*.service`` units, outside this unit's cgroup.

The stop timeout is the external backstop for a stalled drain (the requirement Codex wrote in its
read of plan version 3.3, applied as written): a stop that has not finished after
``TimeoutStopSec`` is ended with SIGKILL (``SendSIGKILL=yes``). That expiry is a daemon crash, not a
clean stop: the run units stay alive, and durable intents and ambiguous attempts are recovered at
the next start under seam S1. The timeout never lets the daemon's own close return, raise or close
a resource still in use; the kill ends the process instead (seam S13's shutdown rule).
"""

from __future__ import annotations

import os
import re
import shlex

from .errors import InstallRefused

UNIT_NAME = "recursion-daemon.service"
DAEMON_MODULE = "recursion_core.daemon"
# The unit's first line. ``uninstall`` removes only a unit that starts with it.
MARKER = "# Recursion per-user daemon, written by `recursiond install` (csl-recursion)."
STOP_TIMEOUT_S = 90  # systemd's own default, stated: finite, and long enough for a normal drain
# The daemon keeps its state in $XDG_STATE_HOME/recursion, or <home>/.local/state/recursion when the
# variable is unset (daemon/__main__.py). The unit sets the variable to that default under the user
# manager's %h, the folder `install` creates the stores in, so a value in the user manager's own
# environment cannot point the daemon at another state folder than the one install prepared.
STATE_HOME = ".local/state"  # relative to the home
MAX_ARGS = 16
MAX_ARG_LEN = 256
_ARG = re.compile(r"[A-Za-z0-9_./:%@=+-]+")


def daemon_exec_args(interpreter: str) -> tuple[str, ...]:
    """The ``ExecStart`` vector for the daemon under ``interpreter``, an installed Python's path.

    ``-I`` keeps the daemon from reading ``PYTHONPATH``, the user site folder or the working
    folder, so it runs the package installed beside that interpreter and nothing else. ``-X gil=0``
    turns the GIL off on the free-threaded build the daemon requires. The path is kept as given,
    not resolved: a virtual environment's ``bin/python`` is a link whose target is outside it. Its
    ``%`` signs are written ``%%``: systemd reads ``%`` in ``ExecStart`` as a specifier, and
    ``%%`` is its literal ``%``, so the unit runs this exact path.
    """

    if type(interpreter) is not str or not os.path.isabs(interpreter):
        raise InstallRefused("exec_arg", "the interpreter path is not absolute")
    return (interpreter.replace("%", "%%"), "-I", "-X", "gil=0", "-m", DAEMON_MODULE)


def render_unit(exec_args: tuple[str, ...]) -> str:
    """Return the unit file. ``exec_args`` is the ``ExecStart`` argument vector.

    Each argument is one token (no spaces, no quotes, no ``..`` segment), so the line needs no
    shell and no quoting. The first token is an absolute path or a systemd specifier such as ``%h``.
    A ``%`` is written as given, so systemd expands it: an argument meant literally has its ``%``
    written ``%%`` before it gets here (``daemon_exec_args`` does that for the interpreter's path).
    A path with a space in it is refused (``exec_arg``) rather than quoted.
    """

    if type(exec_args) is not tuple or not exec_args or len(exec_args) > MAX_ARGS:
        raise InstallRefused("exec_args")
    for arg in exec_args:
        if type(arg) is not str or not _ARG.fullmatch(arg) or len(arg) > MAX_ARG_LEN:
            raise InstallRefused("exec_arg", "each argument is one token of letters, digits and ./:%@=+-_")
        if ".." in arg.split("/"):
            raise InstallRefused("exec_arg", "no '..' segment")
    if not (exec_args[0].startswith("/") or exec_args[0].startswith("%")):
        raise InstallRefused("exec_arg", "the program is an absolute path")
    text = (
        f"{MARKER}\n"
        "# `recursiond upgrade` rewrites this file and `recursiond uninstall` removes it.\n"
        "# Runs live in their own transient recursion-run-*.service units, not in this\n"
        "# unit's cgroup, so stopping or killing this unit never stops a run.\n"
        f"# A stop still running after {STOP_TIMEOUT_S} s is ended with SIGKILL: a daemon crash,\n"
        "# with the run units left alive and their state recovered at the next start.\n"
        f"# The daemon's state, with the stores `recursiond install` creates, is in %h/{STATE_HOME}/recursion.\n"
        "[Unit]\n"
        "Description=Recursion per-user daemon\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "UnsetEnvironment=PYTHONPATH PYTHONHOME\n"
        f"Environment=XDG_STATE_HOME=%h/{STATE_HOME}\n"
        f"ExecStart={' '.join(exec_args)}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "UMask=0077\n"
        "KillMode=control-group\n"
        f"TimeoutStopSec={STOP_TIMEOUT_S}\n"
        "SendSIGKILL=yes\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    return text


def state_elsewhere(unit: bytes, home: str) -> str | None:
    """Why the daemon a unit starts may keep its state in another folder than the one ``render_unit``
    names, or ``None``: what ``upgrade`` checks in a unit before it replaces it (the S6 sweep's F1 on
    #410: a hand-placed unit's own ``XDG_STATE_HOME`` was dropped, and the daemon's revocations with it).

    Its ``[Service]`` section is read as systemd reads it, near enough: an ``Environment=`` line is a
    list of assignments, each in double or single quotes or none, an empty one drops those before it,
    and the last ``XDG_STATE_HOME`` counts; a value naming ``home``'s ``.local/state`` (``%h`` is
    ``home``) is this unit's own. ``EnvironmentFile=`` can set it (its file is not read here), and
    ``PassEnvironment=XDG_STATE_HOME`` gives the daemon the user manager's own value; ``UnsetEnvironment``
    naming it leaves the default, which is ours. A unit that cannot be read so is answered too: then
    where it keeps the state cannot be told. A value only in the user manager's own environment
    (``environment.d``, ``systemctl --user set-environment``) is not seen: this package starts no
    process to ask it.
    """

    try:
        text = unit.decode("utf-8")
    except UnicodeDecodeError:
        return "is not UTF-8 text, so where it keeps the daemon's state cannot be told"
    section = ""
    assigned: dict[str, str] = {}
    files: list[str] = []
    passed = unset = False
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        while line.endswith("\\") and i < len(lines):  # a continued line
            line = line[:-1] + " " + lines[i].strip()
            i += 1
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if section != "Service" or key not in ("Environment", "EnvironmentFile", "PassEnvironment",
                                                "UnsetEnvironment"):
            continue
        if key == "EnvironmentFile":
            files = files + [value] if value else []
            continue
        try:
            words = shlex.split(value)
        except ValueError:
            return f"has {key}={value}, which cannot be read, so where it keeps the daemon's state cannot be told"
        if key == "Environment":
            if not words:
                assigned = {}
            for word in words:
                name, has, setting = word.partition("=")
                if has:
                    assigned[name] = setting
        elif key == "PassEnvironment":
            passed = False if not words else passed or "XDG_STATE_HOME" in words
        else:  # UnsetEnvironment; an empty one drops those before it
            named = [word.partition("=")[0] for word in words]
            unset = False if not words else unset or "XDG_STATE_HOME" in named
    if unset:  # applied after Environment, EnvironmentFile and PassEnvironment: the default, ours
        return None
    if files:
        return f"reads EnvironmentFile={files[0]}, which can set XDG_STATE_HOME"
    if passed:
        return "passes the user manager's own XDG_STATE_HOME to the daemon (PassEnvironment)"
    value = assigned.get("XDG_STATE_HOME")
    if value is None:
        return None
    named = home + value[2:] if value.startswith("%h") else value
    ours = os.path.join(home, *STATE_HOME.split("/"))
    if os.path.isabs(named) and os.path.realpath(named) == os.path.realpath(ours):
        return None
    return f"sets XDG_STATE_HOME={value}"


def service_fields(unit: str) -> dict[str, str]:
    """Map ``Section.Key`` to the last value in ``unit``. Comments are ignored.

    Duplicate keys keep the last value. This is a reader for the small unit this package writes,
    not a systemd parser.
    """

    if type(unit) is not str:
        raise InstallRefused("unit")
    section = ""
    out: dict[str, str] = {}
    for raw in unit.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]") and len(line) > 2:
            section = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        if sep != "=" or not section or not key:
            raise InstallRefused("unit")
        out[f"{section}.{key}"] = value
    return out
