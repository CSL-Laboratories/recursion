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

from .errors import InstallRefused

UNIT_NAME = "recursion-daemon.service"
DAEMON_MODULE = "recursion_core.daemon"
# The unit's first line. ``uninstall`` removes only a unit that starts with it.
MARKER = "# Recursion per-user daemon, written by `recursiond install` (csl-recursion)."
STOP_TIMEOUT_S = 90  # systemd's own default, stated: finite, and long enough for a normal drain
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
        "[Unit]\n"
        "Description=Recursion per-user daemon\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "UnsetEnvironment=PYTHONPATH PYTHONHOME\n"
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
