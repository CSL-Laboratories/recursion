"""Systemd user-unit text for the per-user daemon (CSL-758).

The text is data. Nothing in this module starts systemd or opens a socket.
``Restart=on-failure`` and ``WantedBy=default.target`` are the directives in
systemd.service(5) and systemd.unit(5): the unit comes back after a crash, and
``enable`` attaches it to the user session's login target. A leftover token
file is not an input.
"""

from __future__ import annotations

import re

from .errors import InstallRefused

UNIT_NAME = "recursion-daemon.service"
MAX_ARGS = 16
MAX_ARG_LEN = 256
_ARG = re.compile(r"[A-Za-z0-9_./:%@=+-]+")


def render_unit(exec_args: tuple[str, ...]) -> str:
    """Return the unit file. ``exec_args`` is the ``ExecStart`` argument vector.

    Each argument is one token (no spaces, no quotes, no ``..`` segment) so the
    line needs no shell and no quoting. The first token is an absolute path or
    a systemd specifier such as ``%h``.
    """

    if type(exec_args) is not tuple or not exec_args or len(exec_args) > MAX_ARGS:
        raise InstallRefused("exec_args")
    for arg in exec_args:
        if type(arg) is not str or not _ARG.fullmatch(arg) or len(arg) > MAX_ARG_LEN:
            raise InstallRefused("exec_arg")
        if ".." in arg.split("/"):
            raise InstallRefused("exec_arg")
    if not (exec_args[0].startswith("/") or exec_args[0].startswith("%")):
        raise InstallRefused("exec_arg")
    text = (
        "# Recursion per-user daemon (CSL-758, candidate).\n"
        "# Login start is WantedBy=default.target. A crash restart is\n"
        "# Restart=on-failure. This file does not consult a token file.\n"
        "[Unit]\n"
        "Description=Recursion per-user daemon\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={' '.join(exec_args)}\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "UMask=0077\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    fields = service_fields(text)
    if fields.get("Service.Restart") != "on-failure":
        raise InstallRefused("unit_restart")
    if fields.get("Install.WantedBy") != "default.target":
        raise InstallRefused("unit_wanted_by")
    if "token" in fields.get("Service.ExecStart", ""):
        raise InstallRefused("unit_token_gate")
    return text


def service_fields(unit: str) -> dict[str, str]:
    """Map ``Section.Key`` to the last value in ``unit``. Comments are ignored.

    Duplicate keys keep the last value. This is a reader for the small unit
    this package writes, not a systemd parser.
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


def crash_restart(unit: str, *, token_file_exists: bool) -> str:
    """What a crash does to this unit, including when a token file was left behind.

    ``restarted`` means ``Restart=on-failure`` applies and no ExecStart,
    ExecStartPre, or ExecCondition line names ``token``. ``refused_stale_token``
    means one of those lines names ``token`` and the file exists, which is the
    serve-fake refusal this unit does not use. ``stayed_down`` means the unit
    does not restart on failure.

    ``scripts/v3`` serve-fake is not imported or changed here. Its ``serve-fake``
    path returns ``token_file_exists`` when the token file is already present
    (``scripts/v3/control_api/__main__.py``).
    """

    if type(token_file_exists) is not bool:
        raise InstallRefused("restart_args")
    fields = service_fields(unit)
    if fields.get("Service.Restart") != "on-failure":
        return "stayed_down"
    for key in ("Service.ExecStart", "Service.ExecStartPre", "Service.ExecCondition"):
        if "token" in fields.get(key, ""):
            return "refused_stale_token" if token_file_exists else "restarted"
    return "restarted"
