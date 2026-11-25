"""Fetch one plugin file, or a registry's tree, from a git repository.

This is the code in recursion that fetches. It uses the git program already
on the machine and no other package. A plugin source is a local bare
repository: an address with a scheme, or a ``git@`` path, is refused before
git is started. A registry (``registry_checkout``) is a local bare repository,
a ``file://`` address of one, or an ``https://`` address; every other scheme is
refused before git is started, and git is allowed that one protocol only.

What this refuses, before any byte is returned for the profile:

- a branch name, a tag name, or any commit that is not 40 lowercase hex
- a path that is absolute or contains ``..``
- a symbolic link whose target leaves the tree, and a symbolic link at the file
- an install script in the tree, or a manifest that names one
- for a registry: a symbolic link or a submodule anywhere in the tree

The scratch directory is mode 0700 and is removed on success and on failure.
Nothing in the fetched tree is executed. The file bytes are the git object,
read with ``git cat-file``. The work tree is not checked out, so a filter or
a hook in the tree has nothing to run. The scratch repository's HEAD is the
named commit, and a mismatch is refused. A plugin fetch and a registry fetch
pin the commit by the same steps (``_pin``).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path
import posixpath
import re
import secrets
import shlex
import signal
import stat
import subprocess

__all__ = ["SourceRefused", "fetch_plugin_source", "registry_address", "registry_checkout"]

_GIT = "/usr/bin/git"
_COMMIT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
_ABS = re.compile(r"/[A-Za-z0-9._/-]+")
_HTTPS = re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._-]+)+")
_INSTALL_SCRIPT_NAMES = frozenset({
    "install", "install.sh", "install.py", "install.bash", "install.cmd",
    "setup.sh", "setup.py", "setup.bash",
})
# Each figure below becomes a row of the person's settings file once that file is on this base; until
# then it stands here, in one place, with its present value as the shipped first value.
_MAX_BLOB = 8 * 1024 * 1024
_MAX_LISTING = 2 * 1024 * 1024
_MAX_STDERR = 64 * 1024
# Seconds one git step may take before it is ended and the fetch is refused.
_STEP_SECONDS = 30
# Seconds to wait for an ended git step to go.
_END_WAIT_SECONDS = 5
# A registry tree: the most files, and the most bytes in all, its registry/ and examples/ areas may hold.
_MAX_REGISTRY_FILES = 10000
_MAX_REGISTRY_BYTES = 256 * 1024 * 1024
# The folders of a registry's tree that the registry check reads. Nothing else of the tree is written.
_REGISTRY_AREAS = ("registry", "examples")


class SourceRefused(Exception):
    """The fetch did not return bytes. ``code`` is stable."""

    def __init__(self, code: str, path: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        # For a registry: the file in the fetched tree the refusal is about, when there is one.
        self.path = path


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
    fetched = False
    try:
        blob = _fetch(scratch, bare, commit, path, expected_sha256, install_scripts)
        fetched = True
    finally:
        # A removal that fails while a refusal is being raised never replaces it.
        try:
            _remove_tree(scratch)
        except OSError:
            if fetched:
                raise SourceRefused("source_unavailable") from None
    return blob


def registry_address(address: object) -> tuple[str, str]:
    """``(kind, value)`` for a registry address: ``local`` with a folder path, or ``https`` with the URL.

    A ``file://`` address is its folder path. Every other scheme, a ``git@`` path, a URL with a user,
    a query or a fragment, and a relative path refuse ``registry_address_refused``.
    """
    if type(address) is not str or not address or "\x00" in address or "\n" in address:
        raise SourceRefused("registry_address_refused")
    lowered = address.lower()
    if lowered.startswith("file://"):
        return "local", address[len("file://"):]
    if lowered.startswith("https://"):
        if address.startswith("https://") and _HTTPS.fullmatch(address) is not None:
            return "https", address
        raise SourceRefused("registry_address_refused")
    # Every other scheme, a git@ form and a relative path: none starts with a slash.
    if not address.startswith("/"):
        raise SourceRefused("registry_address_refused")
    return "local", address


@contextlib.contextmanager
def registry_checkout(address: str, commit: str):
    """Yield a folder holding the registry's ``registry/`` and ``examples/`` at ``commit``, then remove it.

    The folder is written from the git objects of that one commit: no history, no ref and no file outside
    those two areas is written. A symbolic link or a submodule anywhere in the commit's tree refuses
    ``registry_link_refused`` before any file is written; an install script by name in those areas refuses
    ``source_install_script``; a tree over the file or byte figures refuses ``registry_too_large``.
    """
    _require_commit(commit)
    kind, value = registry_address(address)
    source = str(_require_local(value)) if kind == "local" else value
    scratch = _make_scratch()
    try:
        records = _pin(scratch, source, commit, local=kind == "local")
        tree = scratch / "tree"
        _write_registry_tree(scratch, tree, records)
        yield tree
    finally:
        _remove_tree(scratch)


def _write_registry_tree(scratch: Path, tree: Path, records: dict) -> None:
    for name, (mode, _sha) in sorted(records.items()):
        if mode in ("120000", "160000"):
            raise SourceRefused("registry_link_refused", name)
    chosen = {name: record for name, record in records.items()
              if name.split("/", 1)[0] in _REGISTRY_AREAS and "/" in name}
    if len(chosen) > _MAX_REGISTRY_FILES:
        raise SourceRefused("registry_too_large")
    _refuse_scripts(chosen, ())
    os.mkdir(tree, 0o700)
    total = 0
    for name, (mode, _sha) in sorted(chosen.items()):
        try:
            _require_path(name)
        except SourceRefused:
            raise SourceRefused("source_path_escapes", name) from None
        if mode not in ("100644", "100755"):
            raise SourceRefused("source_not_regular", name)
        blob = _git(scratch, "cat-file", "blob", f"HEAD:{name}", limit=_MAX_BLOB)
        total += len(blob)
        if total > _MAX_REGISTRY_BYTES:
            raise SourceRefused("registry_too_large")
        folder = tree
        for part in name.split("/")[:-1]:
            folder = folder / part
            if not os.path.lexists(folder):
                os.mkdir(folder, 0o700)
        fd = os.open(folder / name.rsplit("/", 1)[-1],
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            view = memoryview(blob)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)


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
    try:
        os.mkdir(path, 0o700)
    except OSError:
        raise SourceRefused("source_unavailable") from None
    try:
        os.chmod(path, 0o700)
        for name in ("home", "hooks", "template", "tmp", "repo"):
            child = path / name
            os.mkdir(child, 0o700)
            os.chmod(child, 0o700)
    except OSError:
        try:
            _remove_tree(path)
        except OSError:
            pass
        raise SourceRefused("source_unavailable") from None
    return path


def _env(scratch: Path, protocol: str | None = None) -> dict:
    """A git environment with no user config and no prompt. Nothing is inherited."""
    env = {
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
    if protocol is not None:
        # A registry fetch: git may use this one protocol and no other, redirects included.
        env["GIT_ALLOW_PROTOCOL"] = protocol
    return env


def _run(args: list[str], scratch: Path, *, cwd: Path | None = None, limit: int = _MAX_LISTING,
         protocol: str | None = None) -> bytes:
    try:
        proc = subprocess.Popen(
            args, cwd=str(cwd or scratch / "repo"), env=_env(scratch, protocol), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError:
        raise SourceRefused("source_unavailable") from None
    try:
        out, err = proc.communicate(timeout=_STEP_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        _kill(proc)
        raise SourceRefused("source_unavailable") from None
    if proc.returncode != 0 or len(err) > _MAX_STDERR or len(out) > limit:
        raise SourceRefused("source_unavailable")
    return out


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.communicate(timeout=_END_WAIT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _git(scratch: Path, *args: str, limit: int = _MAX_LISTING, protocol: str | None = None) -> bytes:
    hooks = str(scratch / "hooks")
    return _run([
        _GIT, "-c", "maintenance.auto=false", "-c", f"core.hooksPath={hooks}",
        "-c", "core.fsmonitor=", "-c", "core.sshCommand=", "-c", "core.autocrlf=false",
        *args,
    ], scratch, limit=limit, protocol=protocol)


def _pin(scratch: Path, source: str, commit: str, *, local: bool = True) -> dict:
    """Fetch ``commit`` alone from ``source`` into the scratch repository, prove HEAD is it, return its listing."""
    hooks = str(scratch / "hooks")
    _git(scratch, "init", "--quiet", "--template", str(scratch / "template"), "-b", "scratch")
    if local:
        bare_text = _git(scratch, "--git-dir", source, "rev-parse", "--is-bare-repository")
        if bare_text != b"true\n":
            raise SourceRefused("source_not_local")
        # git runs --upload-pack through a shell. Every part is quoted, so a hooks
        # path that contains a semicolon, a space, a quote or a dollar sign stays data.
        upload = " ".join(shlex.quote(part) for part in (
            _GIT, "-c", f"core.hooksPath={hooks}", "-c", "core.fsmonitor=",
            "-c", "core.sshCommand=", "upload-pack"))
        _git(scratch, "fetch", "--upload-pack", upload, "--no-tags", "--no-recurse-submodules", "--", source,
             commit)
    else:
        _git(scratch, "-c", "http.followRedirects=false", "fetch", "--no-tags", "--no-recurse-submodules",
             "--depth", "1", "--", source, commit, protocol="https")
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
    return _records(listing)


def _fetch(scratch: Path, bare: Path, commit: str, path: str, expected: str, install_scripts: tuple) -> bytes:
    records = _pin(scratch, str(bare), commit)
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
            raise SourceRefused("source_install_script", name)


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
