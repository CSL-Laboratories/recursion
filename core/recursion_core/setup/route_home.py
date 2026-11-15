"""``check(path, root)``: may the daemon hand an engine ``path`` as its HOME? (lane INSTALL; lane STOP calls it.)

A route home is the folder the launcher sets as an engine's HOME (``<state>/homes/<engine>``,
``daemon/launcher.py``). An engine signs in there and keeps its login in it, so another user must not
be able to read it or change what its path names, and it must never be the operator's own home,
whose logins and keys belong to the operator, not to a route (F6).

FROZEN INTERFACE (lane INSTALL, Nov 14, 2026). Lane STOP calls ``check`` in the launcher before
``private_dir`` makes the folder; a change to a name, a parameter or a code below is a request to the
keeper of this file:

    check(path, root) -> None        refuses with RouteHomeRefused, or returns
    RouteHomeRefused(code, detail)   .code is one of the codes below; .detail is never secret

    route_home_untrusted         path or root is not an absolute str, holds a NUL character or a
                                 ``..`` name, or cannot be written as a file name; the root or a name
                                 between it and path is a link, not a folder, not ours, or open to
                                 group or world at all (any bit of 0o077), or cannot be looked up; a
                                 folder above the root another user could change (each root's or
                                 ours, not writable by group or world unless sticky, each link root's
                                 or ours and followed, at most 40 links); a path, the root or the
                                 operator's home that cannot be followed to the end (a lookup or
                                 readlink that fails other than "missing", more than 40 links, a
                                 name below something that is not a folder); or the operator's home
                                 is unknown (no absolute $HOME and no password entry) or the
                                 password database cannot be read
    route_home_outside_root      path is the root itself, or not below it (a root of / has nothing below
                                 it: it is root's, never the operator's custodied root)
    route_home_is_operator_home  path, or the root, is the operator's own home (the password
                                 database's, or ``$HOME``), compared after links are followed
    route_home_missing           the root, a folder between it and path's parent, or a link above
                                 the root (removed while it was being followed) does not exist

``check`` raises nothing else for any path or root, however malformed.

A route home that does not exist yet is accepted when everything above it passes: the launcher's
``private_dir`` makes it 0700 right after the check. The root itself must already exist (a missing
root is ``route_home_missing``): it is made once, owner-only, before the first launch (by
``recursiond install`` or ``provision``, lane INSTALL), not by the check's caller on the way. A route home inside the operator's home is
accepted (the daemon's state folder is ``~/.local/state/recursion``), only the home itself is
refused. Nothing is created or opened: the file system is asked through ``lstat`` and ``readlink``
only (links are followed by hand, so a lookup that fails part-way is a refusal, never a partial
answer), and the password database through ``pwd.getpwuid`` for this user's home. POSIX ACLs
are not read. The answer holds for the moment of the check: code running as the operator can change
any of it afterwards, before ``private_dir`` makes the folder too (the B17 limit in
``plugin_security/authority.py``), which no path check can close. Linux only, as the launcher is.
"""

from __future__ import annotations

import os
import stat

__all__ = ["RouteHomeRefused", "check"]

_MAX_LINKS = 40  # Linux's MAXSYMLINKS: the links one path lookup follows before ELOOP


class RouteHomeRefused(Exception):
    """A typed refusal. ``code`` is stable; ``detail`` names the path part and is never secret."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


def check(path, root) -> None:
    """Refuse ``path`` unless it is (or may become) an owner-only folder below ``root``, reached
    without a link, that is not the operator's own home. See the module docstring for the codes."""

    path, root = _text(path, "path"), _text(root, "root")
    if not path.startswith(root + "/"):  # the root itself is not below it either; nor is anything below "/"
        raise RouteHomeRefused("route_home_outside_root", f"{path} is not below {root}")
    _check_above(root)
    homes = _operator_homes()
    if _resolve(root) in homes:
        raise RouteHomeRefused("route_home_is_operator_home", "the root is the operator's home")
    _owner_only(root, _lstat(root, missing_ok=False))
    names = path[len(root) + 1:].split("/")
    current = root
    for index, name in enumerate(names):
        current = os.path.join(current, name)
        info = _lstat(current, missing_ok=index == len(names) - 1)
        if info is None:
            break  # the route home itself, not made yet
        _owner_only(current, info)
    if _resolve(path) in homes:
        raise RouteHomeRefused("route_home_is_operator_home", path)


def _text(value: object, what: str) -> str:
    try:
        text = os.fspath(value)
    except TypeError:
        raise RouteHomeRefused("route_home_untrusted", f"the {what} is not a file-system path") from None
    if type(text) is not str or not text.startswith("/"):
        raise RouteHomeRefused("route_home_untrusted", f"the {what} is not absolute")
    if "\0" in text:
        raise RouteHomeRefused("route_home_untrusted", f"the {what} has a NUL character in it")
    try:
        os.fsencode(text)  # a lone surrogate no file name can hold would make os.lstat raise
    except UnicodeEncodeError:
        raise RouteHomeRefused("route_home_untrusted", f"the {what} cannot be written as a file name") from None
    parts = text.split("/")
    if ".." in parts:
        raise RouteHomeRefused("route_home_untrusted", f"the {what} has a '..' in it")
    return "/" + "/".join(part for part in parts if part not in ("", "."))


def _lstat(path: str, *, missing_ok: bool) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise RouteHomeRefused("route_home_missing", path) from None
    except OSError as exc:
        raise RouteHomeRefused("route_home_untrusted", f"{path}: {type(exc).__name__}") from None


def _owner_only(path: str, info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise RouteHomeRefused("route_home_untrusted", f"{path} is a link")
    if not stat.S_ISDIR(info.st_mode):
        raise RouteHomeRefused("route_home_untrusted", f"{path} is not a folder")
    if info.st_uid != os.geteuid():
        raise RouteHomeRefused("route_home_untrusted", f"{path} is owned by another user")
    if info.st_mode & 0o077:
        raise RouteHomeRefused("route_home_untrusted", f"{path} is open to group or world")


def _check_above(root: str) -> None:
    """The folders above ``root``, from ``/``, as the kernel follows them: each root's or ours, not
    writable by group or world unless sticky; each link root's or ours, followed, its target held to
    the same rule, at most ``_MAX_LINKS`` links (the rule ``plugin_security.stores`` holds a store's
    folders to)."""

    trusted = (0, os.geteuid())
    names = [name for name in os.path.dirname(root).split("/") if name]
    current, links = "/", 0
    _trusted_folder("/", _lstat("/", missing_ok=False), trusted)
    while names:
        # ``current`` holds no link, so ``current/..`` names what the kernel reaches there.
        candidate = os.path.join(current, names.pop(0))
        info = _lstat(candidate, missing_ok=False)
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid not in trusted:
                raise RouteHomeRefused("route_home_untrusted", f"{candidate} is a link another user owns")
            links += 1
            if links > _MAX_LINKS:
                raise RouteHomeRefused("route_home_untrusted", f"more than {_MAX_LINKS} links above the root")
            try:
                target = os.readlink(candidate)
            except FileNotFoundError:  # removed since the lstat above
                raise RouteHomeRefused("route_home_missing", candidate) from None
            except OSError as exc:  # replaced by something that is not a link
                raise RouteHomeRefused("route_home_untrusted", f"{candidate}: {type(exc).__name__}") from None
            if target.startswith("/"):
                current = "/"
            names[:0] = [part for part in target.split("/") if part]
            continue
        # Not a folder: the next name cannot be under it, and _resolve(root) refuses it so.
        _trusted_folder(candidate, info, trusted)
        current = candidate


def _resolve(path: str) -> str:
    """``path`` with every link followed as the kernel would, ``.`` and ``..`` taken from where the
    resolution is; from the first name that does not exist, the rest is kept as written (a folder
    not made yet still has a name to compare). Any other failure refuses ``route_home_untrusted``:
    a resolution that stopped part-way must never stand in for a path's identity."""

    names = [name for name in path.split("/") if name]
    current, links = "/", 0
    while names:
        name = names.pop(0)
        if name == ".":
            continue
        if name == "..":
            current = os.path.dirname(current)  # current holds no link, so this is its parent
            continue
        candidate = os.path.join(current, name)
        try:
            info = os.lstat(candidate)
            if stat.S_ISLNK(info.st_mode):
                links += 1
                if links > _MAX_LINKS:
                    raise RouteHomeRefused("route_home_untrusted", f"more than {_MAX_LINKS} links in {path}")
                target = os.readlink(candidate)
                if target.startswith("/"):
                    current = "/"
                names[:0] = [part for part in target.split("/") if part]
                continue
        except FileNotFoundError:  # not made yet (or removed while being followed)
            return os.path.join(candidate, *(n for n in names if n != "."))
        except OSError as exc:
            raise RouteHomeRefused("route_home_untrusted", f"{candidate} cannot be resolved: {type(exc).__name__}") from None
        if names and not stat.S_ISDIR(info.st_mode):
            raise RouteHomeRefused("route_home_untrusted", f"{candidate} is not a folder")
        current = candidate
    return current


def _trusted_folder(path: str, info: os.stat_result, trusted: tuple) -> None:
    if info.st_uid not in trusted:
        raise RouteHomeRefused("route_home_untrusted", f"{path} is owned by another user")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not info.st_mode & stat.S_ISVTX:
        raise RouteHomeRefused("route_home_untrusted", f"{path} is writable by group or world and not sticky")


def _operator_homes() -> set[str]:
    """The operator's home, as the password database and ``$HOME`` name it, links followed.

    Refuses when neither names one (no absolute ``$HOME`` and no password entry for this user), or
    when the password database cannot be read: the rule must not pass for want of a home to compare.
    """

    import pwd  # Linux only, as the launcher is

    homes = [os.environ.get("HOME")]
    try:
        homes.append(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError:  # no entry for this user: $HOME alone names the home
        pass
    except OSError as exc:  # the name service (NSS, LDAP) did not answer
        raise RouteHomeRefused("route_home_untrusted",
                               f"the password database cannot be read: {type(exc).__name__}") from None
    found = {_resolve(home) for home in homes if isinstance(home, str) and home.startswith("/")}
    if not found:
        raise RouteHomeRefused("route_home_untrusted", "the operator's home is unknown: no absolute HOME and "
                               "no password entry")
    return found
