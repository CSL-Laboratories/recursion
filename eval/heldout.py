"""The held-out guard: held-out tasks open only for scoring, only from a path given at run time.

Rules (PROTOCOL.md, "Held-out tasks"):
1. Only a scoring run opens a held-out suite: ``open_suite(path, purpose="score")``. Any other
   purpose (``dev``, ``optimize``) is denied.
2. A process that has declared itself a dev or optimizer process (``declare("dev")`` or
   ``declare("optimize")``) can never open one afterwards, whatever purpose it then claims.
3. The path is given at run time and is absolute. It may not lie inside the repository, through a
   symlink or otherwise: there is no default and nothing in the repository points at it.
4. Only eval/runner.py calls this module, and only eval/heldout.py calls the suite module's held-out
   loader (eval/tests/tests_heldout.py checks both across every Python file in the repository).

What it does not do: every engine runs as the same operator, so a held-out folder the operator can
read, the engine can read too. The guard keeps the repository's own code paths out; keeping the
folder away from an engine during a run is the operator's job (PROTOCOL.md says how).
"""
from __future__ import annotations

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
    raw = Path(path)
    if not raw.is_absolute():
        raise HeldOutDenied(f"held-out path must be absolute, got {str(path)!r}")
    try:
        resolved = raw.resolve(strict=must_exist)
    except OSError as exc:
        raise HeldOutDenied(f"held-out path {str(path)!r} cannot be opened: {exc}") from None
    repo = REPO_ROOT.resolve()
    if _inside(resolved, repo) or _inside(raw.absolute(), repo):
        raise HeldOutDenied(f"held-out tasks never load from the repository ({resolved} is inside {repo})")
    return resolved


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
    return suite_mod._load_held_out(manifest)
