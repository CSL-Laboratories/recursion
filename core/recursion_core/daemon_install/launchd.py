"""The per-user launchd agent: the plist, and the launchctl commands that would apply it.

This module writes the plist and returns a ``Plan``. It runs none of those commands: no
``bootstrap``, no ``bootout``, and no process at all. Whether an agent is already loaded is
answered by a callable the caller passes (a test's fake ``launchctl``); when that answer is
missing or unusable, uninstall refuses and leaves the file.
"""

from __future__ import annotations

import os
import plistlib
import re

from .errors import InstallRefused
from .install import Plan, ensure_stores
from .layout import (
    Roots,
    check_dirs,
    check_private_dir,
    ensure_dir,
    file_untrusted,
    held_regular,
    owned_paths,
    replace_regular,
    still_holds,
    still_names,
    unlink_regular,
    write_exclusive,
)
from .unit import MARKER, STATE_HOME, render_unit

# Until the release name is cleared, the label is the command name. One token: a slash or a
# ".." would leave ``~/Library/LaunchAgents``.
LABEL = "recursiond"
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PLIST_MODE = 0o644
_DIR_MODE = 0o700
_SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
_LOG_DIR = "~/Library/Logs/Recursion"
# The inode helpers this module reuses report ``unit_missing`` and ``unit_changed``. Those kinds
# stay: the helpers are shared with the unit installer, and this draft does not edit them.


def render_plist(exec_args: tuple[str, ...], label: str, *, state_home: str | None = None) -> bytes:
    """The LaunchAgent plist as bytes, through ``plistlib``.

    ``ProgramArguments`` is ``exec_args`` with systemd's ``%%`` written back to ``%`` (launchd does
    not treat ``%`` as a specifier). ``EnvironmentVariables`` holds only ``XDG_STATE_HOME`` and
    ``PATH``: nothing is copied from the process environment, so a credential there cannot reach
    the plist. With ``state_home`` omitted, ``XDG_STATE_HOME`` is ``~/.local/state`` (the folder
    the unit names with ``%h``). ``install_agent`` passes the absolute folder it prepared, because
    a tilde inside an environment value is not something this module claims launchd expands.
    """

    checked = _check_label(label)
    program = _program_arguments(exec_args)
    folder = _state_home_value(state_home)
    payload = {
        "RecursionInstaller": MARKER,
        "Label": checked,
        "ProgramArguments": program,
        "AbandonProcessGroup": True,
        "ProcessType": "Standard",
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "EnvironmentVariables": {
            "XDG_STATE_HOME": folder,
            "PATH": _explicit_path(program[0]),
        },
        "StandardOutPath": f"{_LOG_DIR}/daemon.log",
        "StandardErrorPath": f"{_LOG_DIR}/daemon.err.log",
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def plan_commands_darwin(
    action: str, uid: int, label: str, *, plist: str | None = None
) -> tuple[tuple[str, ...], ...]:
    """The launchctl argv for ``action``. Returned, never run.

    install is ``launchctl bootstrap gui/<uid> <plist>``. uninstall is
    ``launchctl bootout gui/<uid>/<label>``. upgrade is bootout, then bootstrap. When ``plist`` is
    omitted, the path is ``~/Library/LaunchAgents/<label>.plist`` (this signature has no home);
    ``install_agent`` passes the absolute path under the home it was given.
    """

    checked = _check_label(label)
    user = _check_uid(uid)
    if action not in ("install", "upgrade", "uninstall"):
        raise InstallRefused("action", repr(action))
    path = _contract_plist(checked) if plist is None else _check_plist_path(plist)
    bootstrap = ("launchctl", "bootstrap", f"gui/{user}", path)
    bootout = ("launchctl", "bootout", f"gui/{user}/{checked}")
    if action == "install":
        return (bootstrap,)
    if action == "uninstall":
        return (bootout,)
    return (bootout, bootstrap)


def install_agent(roots: Roots, exec_args: tuple[str, ...], uid: int, label: str = LABEL) -> Plan:
    """Create the stores and the route homes' custody root, then write the plist.

    The same bytes already there change nothing. Other bytes are ``plist_differs``. A plist another
    user could change is ``plist_untrusted``. Both are refused before anything is made. A plist
    found is held open until the answer, then checked against its bytes and its path
    (``still_holds``, then ``still_names``). A refusal after the stores are begun leaves what was
    made, as ``install`` does.
    """

    paths = owned_paths(roots)
    checked = _check_label(label)
    user = _check_uid(uid)
    folder = os.path.join(paths.home, *STATE_HOME.split("/"))
    blob = render_plist(exec_args, checked, state_home=folder)
    plist = _agent_plist(paths.home, checked)
    agents = os.path.dirname(plist)
    check_dirs(paths.home, agents)
    with held_regular(plist) if os.path.lexists(plist) else _nothing() as read:
        present = read is not None
        if present:
            current, info = read
            if current != blob:
                raise InstallRefused(
                    "plist_differs",
                    f"{plist} holds another plist; `recursiond upgrade` replaces it",
                )
            why = file_untrusted(plist, info)
            if why:
                raise InstallRefused(
                    "plist_untrusted",
                    f"{why}; `recursiond upgrade` writes it again, ours and 0644",
                )
        check_private_dir(paths.home, paths.homes)
        made = ensure_stores(paths)
        homes = ensure_dir(paths.home, paths.homes, mode=_DIR_MODE)
        check_private_dir(paths.home, paths.homes, required=True)
        if present:
            still_holds(plist, read)
            still_names(plist, info)
        else:
            ensure_dir(paths.home, agents, mode=_DIR_MODE)
            write_exclusive(plist, blob, _PLIST_MODE)
        changed = (not present) or _any_created(made) or homes
        return _plan("install", changed, plist, user, checked, made)


def upgrade_agent(roots: Roots, exec_args: tuple[str, ...], uid: int, label: str = LABEL) -> Plan:
    """Rewrite the plist to what ``install_agent`` would write, keeping the prepared state folder.

    A missing plist is ``plist_missing``. A plist whose ``XDG_STATE_HOME`` is not that folder (or
    that cannot be read) is ``plist_state_elsewhere``, before anything is made: replacing it would
    point the daemon at another state folder. The same bytes are not written again unless another
    user could change the file. A plist kept is held open until the answer, as in ``install_agent``.
    """

    paths = owned_paths(roots)
    checked = _check_label(label)
    user = _check_uid(uid)
    folder = os.path.join(paths.home, *STATE_HOME.split("/"))
    blob = render_plist(exec_args, checked, state_home=folder)
    plist = _agent_plist(paths.home, checked)
    agents = os.path.dirname(plist)
    check_dirs(paths.home, agents)
    if not os.path.lexists(plist):
        raise InstallRefused("plist_missing", f"no plist at {plist}; `recursiond install` writes it")
    with held_regular(plist) as read:
        data, info = read
        why = _state_elsewhere(data, folder)
        if why:
            raise InstallRefused(
                "plist_state_elsewhere",
                f"{plist} {why}: the daemon it starts may keep its state there, and the plist "
                f"`recursiond upgrade` writes puts it in {folder}",
            )
        current = data == blob and not file_untrusted(plist, info)
        check_private_dir(paths.home, paths.homes)
        made = ensure_stores(paths)
        homes = ensure_dir(paths.home, paths.homes, mode=_DIR_MODE)
        check_private_dir(paths.home, paths.homes, required=True)
        if current:
            still_holds(plist, read)
            still_names(plist, info)
        else:
            replace_regular(plist, blob, _PLIST_MODE)
        changed = (not current) or _any_created(made) or homes
        return _plan("upgrade", changed, plist, user, checked, made)


def uninstall_agent(roots: Roots, uid: int, label: str = LABEL, *, launchctl=None) -> Plan:
    """Remove the plist this installer wrote, once ``launchctl`` says the agent is not loaded.

    ``launchctl`` is ``fn(argv) -> int`` (0 from ``print`` means loaded) or a bool. This function
    calls it only as ``('launchctl', 'print', 'gui/<uid>/<label>')``. It never bootstraps or
    boots the agent out. No callable, a raised call, or an unreadable answer is ``agent_loaded``:
    a loaded agent and an agent that cannot be told apart are both left in place. A plist without
    the marker is ``plist_not_ours`` and is not removed.
    """

    paths = owned_paths(roots)
    checked = _check_label(label)
    user = _check_uid(uid)
    plist = _agent_plist(paths.home, checked)
    check_dirs(paths.home, os.path.dirname(plist))
    if _print_says_loaded(launchctl, user, checked):
        raise InstallRefused(
            "agent_loaded",
            f"bootout gui/{user}/{checked} first; the plist is left as it is",
        )
    if not os.path.lexists(plist):
        return _plan("uninstall", False, plist, user, checked)
    try:
        with held_regular(plist) as (data, _info):
            if not _is_ours(data):
                raise InstallRefused(
                    "plist_not_ours",
                    f"{plist} was not written by `recursiond install`; it is left as it is",
                )
            return _plan("uninstall", unlink_regular(plist), plist, user, checked)
    except InstallRefused as refusal:
        if refusal.kind != "unit_missing":
            raise
        return _plan("uninstall", False, plist, user, checked)


def leftovers_agent(roots: Roots, label: str = LABEL) -> tuple[str, ...]:
    """The plist ``uninstall_agent`` removes, if it is present. The stores stay."""

    paths = owned_paths(roots)
    checked = _check_label(label)
    plist = _agent_plist(paths.home, checked)
    return tuple(path for path in (plist,) if os.path.lexists(path))


def _plan(
    action: str,
    changed: bool,
    plist: str,
    uid: int,
    label: str,
    stores: tuple[tuple[str, str, bool], ...] = (),
) -> Plan:
    return Plan(
        action=action,
        changed=changed,
        unit=plist,
        commands=plan_commands_darwin(action, uid, label, plist=plist),
        stores=stores,
    )


def _program_arguments(exec_args: tuple[str, ...]) -> list[str]:
    render_unit(exec_args)  # the unit's token rules; a bad vector is refused the same way
    program = [arg.replace("%%", "%") for arg in exec_args]
    if not program[0].startswith("/"):
        raise InstallRefused("exec_arg", "the program is an absolute path")
    return program


def _explicit_path(program: str) -> str:
    """The interpreter's directory, then the system directories. Never the process ``PATH``."""

    folder = os.path.dirname(program)
    parts: list[str] = []
    if folder:
        parts.append(folder)
    for item in _SYSTEM_PATH:
        if item not in parts:
            parts.append(item)
    return ":".join(parts)


def _state_home_value(state_home: str | None) -> str:
    if state_home is None:
        return "~/.local/state"
    if (
        type(state_home) is not str
        or not state_home
        or "\x00" in state_home
        or "\n" in state_home
        or "\r" in state_home
        or not os.path.isabs(state_home)
    ):
        raise InstallRefused("state_home", "XDG_STATE_HOME is an absolute path on one line")
    return state_home


def _check_label(label: object) -> str:
    if type(label) is not str or not _LABEL.fullmatch(label) or ".." in label:
        raise InstallRefused("label_invalid", "a label is one token, with no '/' and no '..'")
    return label


def _check_uid(uid: object) -> int:
    if type(uid) is not int or uid < 0 or uid > 0xFFFFFFFF:
        raise InstallRefused("uid", "the user id is a non-negative integer")
    return uid


def _check_plist_path(plist: object) -> str:
    if type(plist) is not str or not plist or "\x00" in plist or "\n" in plist or "\r" in plist:
        raise InstallRefused("plist", "the plist path is one line")
    return plist


def _contract_plist(label: str) -> str:
    return f"~/Library/LaunchAgents/{label}.plist"


def _agent_plist(home: str, label: str) -> str:
    return os.path.join(home, "Library", "LaunchAgents", label + ".plist")


def _state_elsewhere(data: bytes, state_home: str) -> str | None:
    try:
        doc = plistlib.loads(data)
    except Exception:
        return "is not a plist this installer can read, so where it keeps the daemon's state cannot be told"
    if type(doc) is not dict:
        return "is not a plist dictionary, so where it keeps the daemon's state cannot be told"
    env = doc.get("EnvironmentVariables")
    if type(env) is not dict or "XDG_STATE_HOME" not in env:
        return "does not name XDG_STATE_HOME, so where it keeps the daemon's state cannot be told"
    if env.get("XDG_STATE_HOME") != state_home:
        return "names another XDG_STATE_HOME than the folder install prepared"
    return None


def _is_ours(data: bytes) -> bool:
    try:
        doc = plistlib.loads(data)
    except Exception:
        return False
    return type(doc) is dict and doc.get("RecursionInstaller") == MARKER


def _print_says_loaded(launchctl: object, uid: int, label: str) -> bool:
    if launchctl is None:
        raise InstallRefused(
            "agent_loaded",
            "launchctl print was not given, so whether the agent is loaded cannot be told; the plist is left as it is",
        )
    argv = ("launchctl", "print", f"gui/{uid}/{label}")
    try:
        result = launchctl(argv)  # type: ignore[operator]
    except InstallRefused:
        raise
    except Exception as exc:
        raise InstallRefused(
            "agent_loaded",
            f"launchctl print did not answer ({type(exc).__name__}); the plist is left as it is",
        ) from None
    if type(result) is bool:
        return result
    if type(result) is int:
        return result == 0
    raise InstallRefused(
        "agent_loaded",
        "launchctl print answered in a form that cannot be read; the plist is left as it is",
    )


def _any_created(made: tuple[tuple[str, str, bool], ...]) -> bool:
    return any(created for _, _, created in made)


class _nothing:
    """Stand-in for ``held_regular`` when no plist is there: the block sees ``None``."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> bool:
        return False
