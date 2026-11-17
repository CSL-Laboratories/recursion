"""The held-out guard: held-out tasks open only for scoring, only from a path given at run time.

Rules (PROTOCOL.md, "Held-out tasks"):
1. Only a scoring run opens a held-out suite: ``open_suite(path, purpose="score")``. Any other
   purpose (``dev``, ``optimize``) is denied.
2. A process that has declared itself a dev or optimizer process (``declare("dev")`` or
   ``declare("optimize")``) can never open one afterwards, whatever purpose it then claims.
3. The path is given at run time and is absolute. It may not lie inside the repository, through a
   symlink or otherwise: there is no default and nothing in the repository points at it. The same
   holds for every input the loader reads (the manifest, each task file or folder): each must resolve
   inside the held-out folder and outside the repository and be reached through no link, and a task
   folder holds no link at any depth (the loader refuses links too).
4. Only eval/runner.py calls this module, and only eval/heldout.py calls the suite module's held-out
   loader (eval/tests/tests_heldout.py checks both across every Python file in the repository).

What it does not do: every engine runs as the same operator, so a held-out folder the operator can
read, the engine can read too. The guard keeps the repository's own code paths out; keeping the
folder away from an engine during a run is the operator's job (PROTOCOL.md says how).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from eval import REPO_ROOT
from eval import suite as suite_mod

PURPOSES = ("score", "dev", "optimize")
_lock = threading.Lock()
_declared: str | None = None


class HeldOutDenied(PermissionError):
    pass


def declare(mode: str) -> None:
    """Declare what this process is. ``dev`` and ``optimize`` are final: they cannot become ``score``."""
    global _declared
    if mode not in PURPOSES:
        raise ValueError(f"unknown mode {mode!r}")
    with _lock:
        if _declared in ("dev", "optimize") and mode != _declared:
            raise HeldOutDenied(f"this process declared itself {_declared!r}; it cannot become {mode!r}")
        _declared = mode


def declared() -> str | None:
    return _declared


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def check_path(path, *, must_exist: bool = True) -> Path:
    """The resolved held-out folder, or HeldOutDenied."""
    try:
        raw = Path(path)
    except (TypeError, ValueError) as exc:
        raise HeldOutDenied(f"held-out path {path!r} is not a filesystem path: {exc}") from None
    if not raw.is_absolute():
        raise HeldOutDenied(f"held-out path must be absolute, got {str(path)!r}")
    try:
        resolved = raw.resolve(strict=must_exist)
    except (OSError, ValueError) as exc:
        raise HeldOutDenied(f"held-out path {str(path)!r} cannot be opened: {exc}") from None
    repo = REPO_ROOT.resolve()
    if _inside(resolved, repo) or _inside(raw.absolute(), repo):
        raise HeldOutDenied(f"held-out tasks never load from the repository ({resolved} is inside {repo})")
    return resolved


def _plain_input(path: Path, folder: Path, what: str) -> None:
    """``path`` exists and is reached from the held-out folder through no link at all. The folder itself is
    outside the repository (check_path) and task paths hold no '..', so such a path lies inside the
    held-out folder and outside the repository."""
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise HeldOutDenied(f"{what} cannot be opened: {exc}") from None
    if resolved != Path(os.path.normpath(path)):
        if _inside(resolved, REPO_ROOT.resolve()):
            where = ", inside the repository"
        elif not _inside(resolved, folder):
            where = ", outside the held-out folder"
        else:
            where = ""
        raise HeldOutDenied(f"{what} is reached through a link: it leads to {resolved}{where}; held-out inputs "
                            "are plain files and folders inside the held-out folder")


def check_sources(folder: Path, manifest: Path) -> None:
    """Every input the held-out loader will read: the manifest and each task file or folder are reached
    through no link, and a task folder holds no link at any depth (the loader refuses one too, so what is
    checked is exactly what is read)."""
    _plain_input(manifest, folder, "suite.json")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return  # the loader refuses it with its own message
    entries = data.get("tasks") if isinstance(data, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        rel = entry.get("file", entry.get("dir"))
        if not isinstance(rel, str):
            continue
        what = f"task {entry.get('id')!r} ({rel})"
        source = folder / rel
        _plain_input(source, folder, what)
        if source.is_dir():
            for dirpath, dirs, files in os.walk(source):
                for name in dirs + files:
                    item = Path(dirpath) / name
                    if item.is_symlink():
                        raise HeldOutDenied(f"{what}: {item.relative_to(source)} is a link; a held-out task folder "
                                            "holds plain files and folders only")


def open_suite(path, *, purpose: str):
    """Open the held-out suite in folder ``path`` (its suite.json) for scoring."""
    if purpose != "score":
        raise HeldOutDenied(f"held-out tasks open only for scoring, not for {purpose!r}")
    with _lock:
        if _declared in ("dev", "optimize"):
            raise HeldOutDenied(f"this process declared itself {_declared!r}: held-out tasks stay closed")
    folder = check_path(path)
    manifest = folder / "suite.json"
    if not manifest.is_file():
        raise HeldOutDenied(f"no suite.json in {folder}")
    check_sources(folder, manifest)
    return suite_mod._load_held_out(manifest)
