"""Fetch one plugin file from a local bare git repository.

This is the first code in recursion that fetches a plugin. It uses the git
program already on the machine and no other package. The repository argument
is a directory path. An address with a scheme, or a ``git@`` path, is refused
before git is started.

What this refuses, before any byte is returned for the profile:

- a branch name, a tag name, or any commit that is not 40 lowercase hex
- a path that is absolute or contains ``..``
- a symbolic link whose target leaves the tree, and a symbolic link at the file
- an install script in the tree, or a manifest that names one

The scratch directory is mode 0700 and is removed on success and on failure.
Nothing in the fetched tree is executed. The file bytes are the git object,
read with ``git cat-file``. The work tree is not checked out, so a filter or
a hook in the tree has nothing to run. The scratch repository's HEAD is the
named commit, and a mismatch is refused.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import posixpath
import re
import secrets
import signal
import stat
import subprocess

__all__ = ["SourceRefused", "fetch_plugin_source"]

_GIT = "/usr/bin/git"
_COMMIT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
_ABS = re.compile(r"/[A-Za-z0-9._/-]+")
_INSTALL_SCRIPT_NAMES = frozenset({
    "install", "install.sh", "install.py", "install.bash", "install.cmd",
    "setup.sh", "setup.py", "setup.bash",
})
_MAX_BLOB = 8 * 1024 * 1024
_MAX_LISTING = 2 * 1024 * 1024
_MAX_STDERR = 64 * 1024


class SourceRefused(Exception):
    """The fetch did not return bytes. ``code`` is stable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def fetch_plugin_source(repository: str, commit: str, path: str, *, expected_sha256: str,
                        install_scripts: tuple = ()) -> bytes:
    """Return the file at ``commit``:``path`` after every check, or refuse.

    ``expected_sha256`` is the digest the profile would store. A different
    byte, including a one-byte change, refuses and returns nothing.
    """
    if install_scripts:
        raise SourceRefused("source_install_script")
    _require_commit(commit)
    _require_path(path)
    bare = _require_local(repository)
    if type(expected_sha256) is not str or _SHA256.fullmatch(expected_sha256) is None:
        raise SourceRefused("source_digest_mismatch")
    scratch = _make_scratch()
    try:
        return _fetch(scratch, bare, commit, path, expected_sha256, install_scripts)
    finally:
        _remove_tree(scratch)


def _require_commit(commit: object) -> None:
    """A full commit id only. A branch, a tag and a short id are the same refusal."""
    if type(commit) is not str or _COMMIT.fullmatch(commit) is None:
        raise SourceRefused("source_commit_refused")


def _require_path(path: object) -> None:
    if type(path) is not str or not path or len(path) > 512 or "\\" in path or path.startswith("/"):
        raise SourceRefused("source_path_escapes")
    parts = path.split("/")
    if any(_SEGMENT.fullmatch(part) is None or part in (".", "..") for part in parts):
        raise SourceRefused("source_path_escapes")


def _require_local(repository: object) -> Path:
    if type(repository) is not str or not repository or "\x00" in repository or "\n" in repository:
        raise SourceRefused("source_not_local")
    lowered = repository.lower()
    if ("://" in repository or lowered.startswith("git@") or lowered.startswith("ssh:")
            or lowered.startswith("http:") or lowered.startswith("https:")):
        raise SourceRefused("source_network_refused")
    if (not repository.startswith("/") or repository.startswith("-") or _ABS.fullmatch(repository) is None
            or "/../" in repository + "/" or "/./" in repository + "/"):
        raise SourceRefused("source_not_local")
    current = Path("/")
    for part in repository.split("/")[1:]:
        if part in ("", ".", ".."):
            raise SourceRefused("source_not_local")
        current = current / part
        if current.is_symlink():
            raise SourceRefused("source_not_local")
        if not current.exists():
            raise SourceRefused("source_unavailable")
    if not current.is_dir():
        raise SourceRefused("source_unavailable")
    return current


def _make_scratch() -> Path:
    raw = os.environ.get("TMPDIR") or "/tmp"
    parent = Path(raw)
    if parent.is_symlink() or not parent.is_dir():
        raise SourceRefused("source_unavailable")
    path = parent / f"plugin-fetch-{secrets.token_hex(8)}"
    os.mkdir(path, 0o700)
    os.chmod(path, 0o700)
    for name in ("home", "hooks", "template", "tmp", "repo"):
        child = path / name
        os.mkdir(child, 0o700)
        os.chmod(child, 0o700)
    return path


def _env(scratch: Path) -> dict:
    """A git environment with no user config and no prompt. Nothing is inherited."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(scratch / "home"),
        "TMPDIR": str(scratch / "tmp"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_COUNT": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "LC_ALL": "C",
        "LANG": "C",
    }


def _run(args: list[str], scratch: Path, *, cwd: Path | None = None, limit: int = _MAX_LISTING) -> bytes:
    try:
        proc = subprocess.Popen(
            args, cwd=str(cwd or scratch / "repo"), env=_env(scratch), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError:
        raise SourceRefused("source_unavailable") from None
    try:
        out, err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        _kill(proc)
        raise SourceRefused("source_unavailable") from None
    if proc.returncode != 0 or len(err) > _MAX_STDERR or len(out) > limit:
        raise SourceRefused("source_unavailable")
    return out


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _git(scratch: Path, *args: str, limit: int = _MAX_LISTING) -> bytes:
    hooks = str(scratch / "hooks")
    return _run([
        _GIT, "-c", "maintenance.auto=false", "-c", f"core.hooksPath={hooks}",
        "-c", "core.fsmonitor=", "-c", "core.sshCommand=", "-c", "core.autocrlf=false",
        *args,
    ], scratch, limit=limit)


def _fetch(scratch: Path, bare: Path, commit: str, path: str, expected: str, install_scripts: tuple) -> bytes:
    hooks = str(scratch / "hooks")
    _git(scratch, "init", "--quiet", "--template", str(scratch / "template"), "-b", "scratch")
    bare_text = _git(scratch, "--git-dir", str(bare), "rev-parse", "--is-bare-repository")
    if bare_text != b"true\n":
        raise SourceRefused("source_not_local")
    upload = f"{_GIT} -c core.hooksPath={hooks} -c core.fsmonitor= -c core.sshCommand= upload-pack"
    _git(scratch, "fetch", "--upload-pack", upload, "--no-tags", "--no-recurse-submodules", "--", str(bare), commit)
    fetched = _git(scratch, "rev-parse", "--verify", "FETCH_HEAD")
    if fetched != (commit + "\n").encode("ascii"):
        raise SourceRefused("source_commit_mismatch")
    _git(scratch, "update-ref", "refs/pin/plugin", "FETCH_HEAD")
    _git(scratch, "symbolic-ref", "HEAD", "refs/pin/plugin")
    head = _git(scratch, "rev-parse", "--verify", "HEAD")
    if head != (commit + "\n").encode("ascii"):
        raise SourceRefused("source_commit_mismatch")
    kind = _git(scratch, "cat-file", "-t", "HEAD")
    if kind != b"commit\n":
        raise SourceRefused("source_commit_mismatch")
    listing = _git(scratch, "ls-tree", "-r", "-z", "HEAD")
    records = _records(listing)
    _refuse_scripts(records, install_scripts)
    _refuse_symlinks(scratch, records)
    found = records.get(path)
    if found is None:
        raise SourceRefused("source_unavailable")
    mode, _sha = found
    if mode == "120000" or _symlink_record_escapes(path, mode, scratch):
        raise SourceRefused("source_symlink_escapes")
    if mode not in ("100644", "100755"):
        raise SourceRefused("source_not_regular")
    size_text = _git(scratch, "cat-file", "-s", f"HEAD:{path}")
    try:
        size = int(size_text.strip())
    except ValueError:
        raise SourceRefused("source_unavailable") from None
    if size > _MAX_BLOB or size < 0:
        raise SourceRefused("source_unavailable")
    blob = _git(scratch, "cat-file", "blob", f"HEAD:{path}", limit=_MAX_BLOB + 1)
    if len(blob) != size or hashlib.sha256(blob).hexdigest() != expected:
        raise SourceRefused("source_digest_mismatch")
    return blob


def _records(listing: bytes) -> dict:
    found = {}
    for record in listing.split(b"\0"):
        if not record:
            continue
        try:
            text = record.decode("utf-8")
            mode, rest = text.split(" ", 1)
            _kind, rest = rest.split(" ", 1)
            _sha, name = rest.split("\t", 1)
        except (UnicodeDecodeError, ValueError):
            raise SourceRefused("source_unavailable") from None
        if name in found:
            raise SourceRefused("source_unavailable")
        found[name] = (mode, _sha)
    return found


def _refuse_scripts(records: dict, install_scripts: tuple) -> None:
    named = {posixpath.normpath(script) for script in install_scripts if type(script) is str}
    for name in records:
        base = posixpath.basename(name)
        if base in _INSTALL_SCRIPT_NAMES or name in named or posixpath.normpath(name) in named:
            raise SourceRefused("source_install_script")


def _refuse_symlinks(scratch: Path, records: dict) -> None:
    for name, (mode, _sha) in records.items():
        if mode != "120000":
            continue
        if _symlink_record_escapes(name, mode, scratch):
            raise SourceRefused("source_symlink_escapes")


def _symlink_record_escapes(name: str, mode: str, scratch: Path) -> bool:
    if mode != "120000":
        return False
    target = _git(scratch, "cat-file", "blob", f"HEAD:{name}", limit=4096)
    if b"\x00" in target or target.startswith(b"/") or target.startswith(b"\\"):
        return True
    try:
        text = target.decode("utf-8")
    except UnicodeDecodeError:
        return True
    if text.startswith("/") or text.startswith("\\"):
        return True
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(name), text))
    return joined == ".." or joined.startswith("../") or joined.startswith("/")


def _remove_tree(path: Path) -> None:
    """Remove a scratch tree. A symlink is unlinked and not followed."""
    if not os.path.lexists(path):
        return
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        os.unlink(path)
        return
    for name in os.listdir(path):
        _remove_tree(path / name)
    os.rmdir(path)
