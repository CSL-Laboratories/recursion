"""A task: a start tree, a prompt, hidden tests, and the reference solution that validated it.

Two storage forms, one model:

* dev tasks: one ``.txtar`` file in the repository (eval/devset/tasks/), sections ``task.json``,
  ``prompt.md``, ``start/...``, ``hidden/...``, ``solution/...`` and, for the stand-in engine only,
  ``standin.json`` and ``wrong/...``;
* harvested (held-out) tasks: a folder OUTSIDE the repository with ``task.json``, ``prompt.md``,
  ``start.tar``, ``hidden/...`` and ``solution/...``.

The engine is given only the start tree and the prompt; hidden tests are written in after it finishes.
Keeping held-out folders unreadable is the operator's job (heldout.py). A same-uid engine is not sandboxed.
"""
from __future__ import annotations

import errno
import hashlib
import io
import json
import math
import os
import posixpath
import re
import stat
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from eval import txtar

# Tar names are POSIX even where os.path is the host module. On Linux os.path is posixpath, so a
# replacement of os.path.normpath must not change this comparison. The function object is bound here.
_posix_normpath = posixpath.normpath

TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
DEFAULT_TIMEOUT_S = 1800.0
DEFAULT_GRADING_TIMEOUT_S = 600.0


class TaskError(ValueError):
    pass


def safe_relpath(name: str) -> str:
    try:
        return txtar.check_name(name)
    except txtar.TxtarError as exc:
        raise TaskError(str(exc)) from None


@dataclass
class Task:
    id: str
    prompt: str
    test_command: list
    hidden: dict                      # path -> bytes, written after the engine finishes
    solution: dict                    # path -> bytes, or None for a deletion (reference solution)
    sha256: str                       # over the task's stored form
    cluster: str | None = None
    created_at: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    grading_timeout_s: float = DEFAULT_GRADING_TIMEOUT_S
    start_files: dict | None = None   # path -> bytes (dev form)
    start_tar: Path | None = None     # harvested form
    standin: dict = field(default_factory=dict)
    wrong: dict = field(default_factory=dict)
    parent_tree: str | None = None    # pinned by the suite manifest
    parent_commit: str | None = None
    commit: str | None = None
    commit_tree: str | None = None    # the tree of start + solution + hidden; validation checks it
    modes: dict = field(default_factory=dict)   # path -> mode for solution and hidden files (default 0o644)

    def materialize_start(self, dest: Path) -> None:
        """Write the start tree into the empty folder ``dest``."""
        if self.start_files is not None:
            for name, data in self.start_files.items():
                path = dest / safe_relpath(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            return
        if self.start_tar is None:
            raise TaskError(f"task {self.id}: no start tree")
        with tarfile.open(self.start_tar) as archive:
            for member in archive.getmembers():
                safe_relpath(member.name)
                if not (member.isfile() or member.isdir() or member.issym()):
                    raise TaskError(f"task {self.id}: {member.name} is not a file, folder or symlink")
            archive.extractall(dest, filter="data")

    def start_file(self, name: str) -> bytes | None:
        """The bytes of the start tree's file ``name`` (a relative path), None when the start tree has no entry
        there; TaskError when the entry is not a regular file or the start tree cannot be read.

        ``name`` is normalised the same way as a member name, so ``src/./value.txt`` is the entry
        ``src/value.txt`` when that entry is in the tree.
        """
        wanted = _posix_normpath(name)
        if self.start_files is not None:
            return self.start_files.get(wanted)
        if self.start_tar is None:
            raise TaskError(f"task {self.id}: no start tree")
        try:
            with tarfile.open(self.start_tar) as archive:
                for member in archive.getmembers():
                    if _posix_normpath(member.name) != wanted:
                        continue
                    if not member.isfile():
                        raise TaskError(f"task {self.id}: {name} in the start tree is not a regular file")
                    return archive.extractfile(member).read()
        except (OSError, tarfile.TarError) as exc:
            raise TaskError(f"task {self.id}: start tree cannot be read: {exc}") from None
        return None

    def write_hidden(self, work: Path) -> None:
        for name, data in self.hidden.items():
            path = work / safe_relpath(name)
            _clear_the_way(work, path)
            _make_parent(path)
            _release_shared_name(path)
            _write_private(path, data, self.modes.get(name, 0o644))

    def apply_solution(self, work: Path) -> None:
        """The reference solution, for validation and for the stand-in engine only."""
        apply_files(work, self.solution, self.modes)

    def command(self) -> list:
        """The hidden-test command with its placeholders filled."""
        modules = [n[:-3].replace("/", ".") for n in sorted(self.hidden) if n.endswith(".py")]
        argv: list = []
        for part in self.test_command:
            if part == "{python}":
                argv.append(sys.executable)
            elif part == "{hidden}":
                argv.extend(sorted(self.hidden))
            elif part == "{hidden_modules}":
                argv.extend(modules)
            else:
                argv.append(part)
        return argv

    def grader_sha256(self) -> str:
        body = json.dumps({"test_command": self.test_command,
                           "hidden": {n: hashlib.sha256(d).hexdigest() for n, d in sorted(self.hidden.items())}},
                          sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def standin_plan(self) -> dict:
        """What the stand-in engine is told for this task (it is an oracle, and labelled one)."""
        return {
            "task": self.id,
            "outcomes": list(self.standin.get("outcomes") or ["solve"]),
            "usage": self.standin.get("usage"),
            "solution": {n: _plan_file(d, self.modes.get(n)) for n, d in self.solution.items()},
            "wrong": {n: _plan_file(d, self.modes.get(n)) for n, d in self.wrong.items()},
        }


def _plan_file(data, mode):
    """A plan entry the stand-in can write back.

    A UTF-8 file with the ordinary mode stays a string, which is what existing plans already are.
    Bytes that are not UTF-8, and an executable mode, travel as base64 with the mode named.
    """
    if data is None:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None and mode in (None, 0o644):
        return text
    import base64
    # Any mode the task carries, not only 0755. 0644 is the string form above. A third mode used to
    # be written back as 0644, so the stand-in and the reference disagreed.
    mode_text = "100644" if mode is None else f"100{mode:03o}"
    return {"encoding": "base64", "data": base64.b64encode(data).decode("ascii"), "mode": mode_text}


def _remove(path: Path) -> None:
    """Remove ``path`` without following a link.

    A real directory is removed as a tree. Anything else at the name (a file, a link, a FIFO or a
    socket) is unlinked. ``unlink`` removes the name and does not follow a link.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        import shutil
        shutil.rmtree(path)
        return
    path.unlink()


def _plain_folders_above(work: Path, path: Path) -> bool:
    """True when every folder between ``work`` and ``path`` is a real folder (no file, no link)."""
    here = work
    for part in path.relative_to(work).parts[:-1]:
        here = here / part
        if here.is_symlink() or not here.is_dir():
            return False
    return True


def _clear_the_way(work: Path, path: Path) -> None:
    """Remove what stands where ``path`` is to be written: anything that is not a real folder where one
    of its folders must be (a file, a link, a FIFO or a socket), and a folder or link at the path
    itself. A link is removed, never followed, so nothing is written outside ``work``."""
    here = work
    for part in path.relative_to(work).parts[:-1]:
        here = here / part
        if here.is_symlink() or (here.exists() and not here.is_dir()):
            _remove(here)
    if path.is_symlink() or path.is_dir():
        _remove(path)


def _make_parent(path: Path) -> None:
    """Make the folders above ``path``. A name that is still in the way is a TaskError, not a raw OSError."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TaskError(f"cannot write {path.name}: {exc.strerror}") from None


def apply_files(work: Path, files: dict, modes: dict | None = None) -> None:
    """Deletions first, then the files written: a commit that replaces a file with a folder (or a folder
    with a file) is laid out whatever the order of ``files``."""
    for name, data in files.items():
        if data is not None:
            continue
        path = work / safe_relpath(name)
        if _plain_folders_above(work, path) and (path.exists() or path.is_symlink()):
            _remove(path)
    for name, data in files.items():
        if data is None:
            continue
        path = work / safe_relpath(name)
        _clear_the_way(work, path)
        _make_parent(path)
        _release_shared_name(path)
        payload = data if isinstance(data, bytes) else data.encode("utf-8")
        _write_private(path, payload, (modes or {}).get(name, 0o644))


_WRITE_ATTEMPTS = 8
_TAKEN = {
    errno.EEXIST,
    errno.ELOOP,
    errno.ENXIO,
    errno.EISDIR,
    errno.ENOTDIR,
    errno.ENOTEMPTY,
}


def _unlink_name(path: Path) -> None:
    """Remove ``path`` without following it. A real directory is removed as a tree."""
    try:
        info = path.lstat()
    except OSError:
        return
    if stat.S_ISDIR(info.st_mode):
        import shutil
        shutil.rmtree(path, ignore_errors=True)
        return
    try:
        path.unlink()
    except OSError:
        pass


def _write_private(path: Path, data: bytes, mode: int) -> None:
    """Create a new private regular file and write ``data`` through that descriptor.

    ``O_NOFOLLOW`` refuses a symlink. ``O_EXCL`` refuses a name that already exists, including a
    FIFO, so the open does not wait for another process. ``O_NONBLOCK`` is the same refusal if a
    FIFO is opened anyway. A taken name is removed and the create is retried. The descriptor is
    kept only when ``fstat`` shows a regular file with one link, and its mode is set with
    ``fchmod`` on that descriptor.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    last = "the name stayed taken"
    for _ in range(_WRITE_ATTEMPTS):
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            if exc.errno in _TAKEN:
                _unlink_name(path)
                last = exc.strerror or last
                continue
            raise TaskError(f"cannot write {path.name}: {exc.strerror}") from None
        discard = False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                discard = True
                last = "not a private regular file"
                continue
            os.fchmod(fd, mode & 0o777)
            view = memoryview(data)
            while len(view):
                wrote = os.write(fd, view)
                if wrote <= 0:
                    raise TaskError(f"cannot write {path.name}")
                view = view[wrote:]
            return
        except TaskError:
            raise
        except OSError as exc:
            raise TaskError(f"cannot write {path.name}: {exc.strerror}") from None
        finally:
            os.close(fd)
            if discard:
                _unlink_name(path)
    raise TaskError(f"cannot write {path.name}: {last}")


def _release_shared_name(path: Path) -> None:
    """Unlink a name that must not be opened for writing.

    Opening a FIFO or a socket for write waits for another process, and that wait is not the grading
    deadline. A hard-linked regular file shares its inode with another name, which may lie outside the
    work tree. This only removes a name that is already the wrong kind of object. The bytes are written
    by ``_write_private``, which creates a new file with ``O_NOFOLLOW`` and ``O_EXCL`` so a name planted
    after this returns is not followed and is not waited on.
    """
    try:
        info = path.lstat()
    except OSError:
        return
    if stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode):
        return
    if stat.S_ISREG(info.st_mode) and info.st_nlink <= 1:
        return
    path.unlink()


def _positive_limit(task_id: str, key: str, value) -> None:
    """Refuse a time limit that is not a finite positive number of seconds.

    NaN and Infinity pass a ``<= 0`` check (NaN compares false; Infinity is not ``<= 0``) and then
    ``process.wait`` has no deadline. An integer that does not fit in a float (``10**400``) would raise
    OverflowError while the task is built.
    """
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TaskError(f"{task_id}: {key} must be a positive number")
    try:
        number = float(value)
    except OverflowError:
        raise TaskError(f"{task_id}: {key} must be a finite positive number") from None
    if not math.isfinite(number) or number <= 0:
        raise TaskError(f"{task_id}: {key} must be a finite positive number")


def _modes(task_id: str, meta: dict) -> dict:
    raw = meta.get("modes") or {}
    if not isinstance(raw, dict):
        raise TaskError(f"{task_id}: modes must be an object of path -> \"100644\" or \"100755\"")
    out = {}
    for name, mode in raw.items():
        if mode not in ("100644", "100755"):
            raise TaskError(f"{task_id}: mode {mode!r} for {name!r} is not 100644 or 100755")
        out[safe_relpath(name)] = int(mode[-3:], 8)
    return out


def _meta(task_id_hint: str, meta: dict) -> dict:
    if not isinstance(meta, dict):
        raise TaskError(f"{task_id_hint}: task.json must be an object")
    task_id = meta.get("id")
    if not isinstance(task_id, str) or not TASK_ID.match(task_id):
        raise TaskError(f"{task_id_hint}: bad task id {task_id!r}")
    command = meta.get("test_command")
    if not isinstance(command, list) or not command or not all(isinstance(p, str) and p for p in command):
        raise TaskError(f"{task_id}: test_command must be a non-empty list of strings")
    for key in ("timeout_s", "grading_timeout_s"):
        _positive_limit(task_id, key, meta.get(key))
    for key in ("cluster", "created_at"):
        if key not in meta or meta[key] is None:
            continue
        value = meta[key]
        if not isinstance(value, str) or not value:
            raise TaskError(f"{task_id}: {key} must be a non-empty string or be absent")
    return meta


def load_txtar(path: Path) -> Task:
    data = path.read_bytes()
    _, files = txtar.parse(data.decode("utf-8"))
    if "task.json" not in files or "prompt.md" not in files:
        raise TaskError(f"{path.name}: needs task.json and prompt.md sections")
    meta = _meta(path.name, json.loads(files["task.json"]))
    start, hidden, solution, wrong = {}, {}, {}, {}
    deletions = meta.get("solution_deletes") or []
    for name, content in files.items():
        for prefix, target in (("start/", start), ("hidden/", hidden), ("solution/", solution), ("wrong/", wrong)):
            if name.startswith(prefix):
                target[safe_relpath(name[len(prefix):])] = content.encode("utf-8")
    for name in deletions:
        solution[safe_relpath(name)] = None
    if not hidden:
        raise TaskError(f"{meta['id']}: no hidden tests")
    standin = json.loads(files["standin.json"]) if "standin.json" in files else {}
    return Task(
        id=meta["id"], prompt=files["prompt.md"], test_command=meta["test_command"], hidden=hidden,
        solution=solution, sha256=hashlib.sha256(data).hexdigest(), cluster=meta.get("cluster"),
        created_at=meta.get("created_at"), timeout_s=float(meta.get("timeout_s") or DEFAULT_TIMEOUT_S),
        grading_timeout_s=float(meta.get("grading_timeout_s") or DEFAULT_GRADING_TIMEOUT_S),
        start_files=start, standin=standin, wrong=wrong, modes=_modes(meta["id"], meta),
        commit_tree=meta.get("commit_tree"),
    )


def _refuse_links(root: Path) -> None:
    """A task folder holds plain files and folders only: a link could make the loader read something the
    folder does not hold (eval/heldout.py refuses the same)."""
    if root.is_symlink():
        raise TaskError(f"{root} is a link; a task folder holds plain files and folders only")
    for dirpath, dirs, names in os.walk(root):
        for name in dirs + names:
            if (Path(dirpath) / name).is_symlink():
                raise TaskError(f"{Path(dirpath) / name} is a link; a task folder holds plain files and folders only")


def _read_tree(root: Path) -> dict:
    out = {}
    if not root.is_dir():
        return out
    for dirpath, _, names in os.walk(root):
        for name in names:
            path = Path(dirpath) / name
            out[safe_relpath(path.relative_to(root).as_posix())] = path.read_bytes()
    return out


def dir_digest(root: Path) -> str:
    lines = []
    for dirpath, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            # A newline in a name must not become another row. Ordinary names are unchanged.
            shown = rel.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {shown}\n")
    # surrogateescape keeps a name that is not UTF-8 in the digest. Backslash, newline and CR are
    # already escaped above, so two different names still cannot share a row. Callers get a digest,
    # not UnicodeEncodeError.
    return hashlib.sha256("".join(sorted(lines)).encode("utf-8", "surrogateescape")).hexdigest()


def load_dir(path: Path) -> Task:
    _refuse_links(path)
    meta = _meta(path.name, json.loads((path / "task.json").read_text(encoding="utf-8")))
    hidden = _read_tree(path / "hidden")
    if not hidden:
        raise TaskError(f"{meta['id']}: no hidden tests")
    solution = _read_tree(path / "solution")
    for name in meta.get("solution_deletes") or []:
        solution[safe_relpath(name)] = None
    return Task(
        id=meta["id"], prompt=(path / "prompt.md").read_text(encoding="utf-8"), test_command=meta["test_command"],
        hidden=hidden, solution=solution, sha256=dir_digest(path), cluster=meta.get("cluster"),
        created_at=meta.get("created_at"), timeout_s=float(meta.get("timeout_s") or DEFAULT_TIMEOUT_S),
        grading_timeout_s=float(meta.get("grading_timeout_s") or DEFAULT_GRADING_TIMEOUT_S),
        start_tar=path / "start.tar", modes=_modes(meta["id"], meta), commit_tree=meta.get("commit_tree"),
    )


def tar_of_files(files: dict) -> bytes:
    """A deterministic tar of path -> (bytes, mode) or path -> bytes (mode 0o644)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(files):
            value = files[name]
            data, mode = value if isinstance(value, tuple) else (value, 0o644)
            info = tarfile.TarInfo(safe_relpath(name))
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()
