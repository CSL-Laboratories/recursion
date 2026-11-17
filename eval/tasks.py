"""A task: a start tree, a prompt, hidden tests, and the reference solution that validated it.

Two storage forms, one model:

* dev tasks: one ``.txtar`` file in the repository (eval/devset/tasks/), sections ``task.json``,
  ``prompt.md``, ``start/...``, ``hidden/...``, ``solution/...`` and, for the stand-in engine only,
  ``standin.json`` and ``wrong/...``;
* harvested (held-out) tasks: a folder OUTSIDE the repository with ``task.json``, ``prompt.md``,
  ``start.tar``, ``hidden/...`` and ``solution/...``.

The engine never sees anything but the start tree and the prompt. Hidden tests are written into the
work tree only after the engine has finished, over whatever the engine left at those paths.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from eval import txtar

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
        there; TaskError when the entry is not a regular file or the start tree cannot be read."""
        if self.start_files is not None:
            return self.start_files.get(name)
        if self.start_tar is None:
            raise TaskError(f"task {self.id}: no start tree")
        try:
            with tarfile.open(self.start_tar) as archive:
                for member in archive.getmembers():
                    if os.path.normpath(member.name) != name:
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
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(self.modes.get(name, 0o644))

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
            "solution": {n: (None if d is None else d.decode("utf-8")) for n, d in self.solution.items()},
            "wrong": {n: d.decode("utf-8") for n, d in self.wrong.items()},
        }


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        import shutil
        shutil.rmtree(path)


def _plain_folders_above(work: Path, path: Path) -> bool:
    """True when every folder between ``work`` and ``path`` is a real folder (no file, no link)."""
    here = work
    for part in path.relative_to(work).parts[:-1]:
        here = here / part
        if here.is_symlink() or not here.is_dir():
            return False
    return True


def _clear_the_way(work: Path, path: Path) -> None:
    """Remove what stands where ``path`` is to be written: a file or link where one of its folders must be
    (a file replaced by a folder), and a folder or link at the path itself (a folder replaced by a file).
    A link is removed, never followed, so nothing is written outside ``work``."""
    here = work
    for part in path.relative_to(work).parts[:-1]:
        here = here / part
        if here.is_symlink() or (here.exists() and not here.is_dir()):
            _remove(here)
    if path.is_symlink() or path.is_dir():
        _remove(path)


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
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        path.chmod((modes or {}).get(name, 0o644))


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
        value = meta.get(key)
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0):
            raise TaskError(f"{task_id}: {key} must be a positive number")
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
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {rel}\n")
    return hashlib.sha256("".join(sorted(lines)).encode("utf-8")).hexdigest()


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
