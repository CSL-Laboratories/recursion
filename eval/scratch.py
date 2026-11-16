"""Each attempt's own scratch runtime: a new TMPDIR and a new XDG_RUNTIME_DIR, removed afterwards.

Defaults (EVAL brief, item 3): XDG_RUNTIME_DIR under /dev/shm/csl-lanes-eval/<attempt> (tmpfs, as the
real runtime folder is) and TMPDIR under /var/tmp/csl-lanes-eval/<attempt> (outside every home: the
subscription-route guard refuses a route home inside the operator's home). The bases may be moved
for tests; they may never be inside the operator's home (TMPDIR) or be the real runtime folder.
The runner creates both folders, so it removes both; it removes nothing else.
DBUS_SESSION_BUS_ADDRESS is never passed on, so nothing an attempt starts reaches the user manager.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TMP_BASE = Path("/var/tmp/csl-lanes-eval")
DEFAULT_XDG_BASE = Path("/dev/shm/csl-lanes-eval")
ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


class ScratchError(RuntimeError):
    pass


@dataclass
class Scratch:
    attempt_id: str
    tmpdir: Path
    xdg_runtime_dir: Path
    removed: bool | None = None

    @property
    def home(self) -> Path:
        return self.tmpdir / "home"


def _real_runtime_dir() -> Path:
    return Path(f"/run/user/{os.getuid()}")


def check_bases(tmp_base: Path, xdg_base: Path) -> None:
    tmp_base, xdg_base = Path(tmp_base), Path(xdg_base)
    if not tmp_base.is_absolute() or not xdg_base.is_absolute():
        raise ScratchError("scratch bases must be absolute paths")
    home = Path(os.path.expanduser("~")).resolve()
    tmp_resolved = tmp_base.resolve()
    if tmp_resolved == home or home in tmp_resolved.parents:
        raise ScratchError(f"TMPDIR base {tmp_base} is inside the home folder {home}")
    runtime = _real_runtime_dir()
    xdg_resolved = xdg_base.resolve()
    if xdg_resolved == runtime or runtime in xdg_resolved.parents:
        raise ScratchError(f"XDG_RUNTIME_DIR base {xdg_base} is the real runtime folder {runtime}")


def _make_base(base: Path) -> None:
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = base.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ScratchError(f"{base} is not a folder this user owns")


def _rmtree(path: Path) -> None:
    def onexc(function, target, _exc):
        # A read-only folder the engine made: let the owner write it, then try once more.
        parent = os.path.dirname(target)
        for item in (parent, target):
            try:
                os.chmod(item, stat.S_IRWXU)
            except OSError:
                pass
        function(target)

    if path.is_symlink():
        path.unlink()
    elif path.exists():
        try:
            shutil.rmtree(path, onexc=onexc)
        except TypeError:  # Python 3.11 and older: onerror
            shutil.rmtree(path, onerror=onexc)


@contextmanager
def attempt(attempt_id: str, tmp_base: Path = DEFAULT_TMP_BASE, xdg_base: Path = DEFAULT_XDG_BASE):
    """Create both folders (never reusing one), yield them, remove both whatever happens."""
    if not ATTEMPT_ID.match(attempt_id):
        raise ScratchError(f"bad attempt id {attempt_id!r}")
    tmp_base, xdg_base = Path(tmp_base), Path(xdg_base)
    check_bases(tmp_base, xdg_base)
    _make_base(tmp_base)
    _make_base(xdg_base)
    tmpdir = tmp_base / attempt_id
    xdg = xdg_base / attempt_id
    created = []
    try:
        for folder in (tmpdir, xdg):
            try:
                folder.mkdir(mode=0o700)
            except FileExistsError:
                raise ScratchError(f"{folder} already exists: an attempt folder is never reused") from None
            created.append(folder)
        scratch = Scratch(attempt_id, tmpdir, xdg)
        scratch.home.mkdir(mode=0o700)
        yield scratch
    finally:
        for folder in created:
            _rmtree(folder)
        if len(created) == 2:
            scratch.removed = not tmpdir.exists() and not xdg.exists()


def environment(scratch: Scratch, *, home: Path | None = None, extra: dict | None = None) -> dict:
    """A clean environment for a process an attempt starts. Nothing is inherited but PATH.

    ``extra`` is applied FIRST and the attempt's own values after it, so nothing passed in can move
    TMPDIR, XDG_RUNTIME_DIR, HOME or PATH, or bring in DBUS_SESSION_BUS_ADDRESS."""
    env = dict(extra or {})
    env.update({
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "HOME": str(home if home is not None else scratch.home),
        "TMPDIR": str(scratch.tmpdir),
        "XDG_RUNTIME_DIR": str(scratch.xdg_runtime_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    return env
