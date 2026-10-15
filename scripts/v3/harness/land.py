"""Merge an accepted run branch. A dirty tree or a conflict is refused."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from scripts.v3.harness.errors import HarnessError
from scripts.v3.harness.state import load_run, save_run


class _Conflict(Exception):
    def __init__(self, paths: list[str]) -> None:
        self.paths = paths


def land_run(state_dir, run_id: str, *, repo: Path, into: str | None = None) -> dict:
    """Merge the accepted run branch into ``into`` or the current branch.

    The merge is ``--no-ff``. A dirty worktree is refused before git writes.
    A conflict aborts the merge and leaves the tree as it was.
    """

    root = Path(repo)
    record = load_run(state_dir, run_id)
    if record.get("accepted") is not True or record.get("status") != "completed":
        raise HarnessError("run is not accepted")
    branch = _run_branch(root, record, run_id)
    if _dirty(root):
        _remember(state_dir, run_id, branch=branch, commit=None, status="refused", error="working tree is dirty")
        raise HarnessError("working tree is dirty")
    target = _target(root, into)
    message = f"recursion: land {run_id}"
    try:
        commit = _merge(root, target, branch, message)
    except _Conflict as exc:
        _remember(
            state_dir,
            run_id,
            branch=target,
            commit=None,
            status="conflict",
            error="merge conflict",
            paths=exc.paths,
        )
        raise HarnessError("merge conflict") from exc
    _remember(state_dir, run_id, branch=target, commit=commit, status="merged", error=None)
    return load_run(state_dir, run_id)


def _run_branch(repo: Path, record: dict, run_id: str) -> str:
    recorded = record.get("run_branch")
    if isinstance(recorded, str) and recorded:
        branch = _check_name(repo, recorded)
    else:
        branch = f"recursion/run/{run_id}"
        _check_name(repo, branch)
    if not _ref_exists(repo, branch):
        raise HarnessError("run branch is missing")
    return branch


def _remember(
    state_dir,
    run_id: str,
    *,
    branch: str,
    commit: str | None,
    status: str,
    error: str | None,
    paths: list[str] | None = None,
) -> None:
    current = load_run(state_dir, run_id)
    current["land"] = {
        "branch": branch,
        "commit": commit,
        "error": error,
        "paths": list(paths or []),
        "status": status,
    }
    save_run(state_dir, current)


def _target(repo: Path, into: str | None) -> str:
    if into is None:
        current = _current_branch(repo)
        if current is None:
            raise HarnessError("pass --into when HEAD is detached")
        return current
    return _check_name(repo, into, must_exist=True)


def _check_name(repo: Path, name: str, *, must_exist: bool = False) -> str:
    if not isinstance(name, str) or name != name.strip() or not name:
        raise HarnessError("invalid branch name")
    if "\n" in name or "\r" in name or name.startswith("-") or " " in name:
        raise HarnessError("invalid branch name")
    checked = _run(repo, "check-ref-format", "--branch", name)
    if checked.returncode != 0:
        raise HarnessError("invalid branch name")
    if must_exist and not _ref_exists(repo, name):
        raise HarnessError(f"unknown branch {name}")
    return name


def _merge(repo: Path, target: str, run_branch: str, message: str) -> str:
    if _current_branch(repo) != target:
        raise HarnessError("pass a checkout of the target branch; this slice does not switch branches")
    env = os.environ.copy()
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        env.pop(key, None)
    before = _run(repo, "rev-parse", "HEAD").stdout.strip()
    completed = _run(repo, *_author_config(repo), "merge", "--no-ff", "-m", message, run_branch, env=env)
    if completed.returncode != 0:
        listed = _run(repo, "diff", "--name-only", "--diff-filter=U")
        paths = [line for line in listed.stdout.splitlines() if line.strip()][:20]
        _run(repo, "merge", "--abort")
        if paths:
            raise _Conflict(paths)
        raise HarnessError("merge failed")
    shown = _run(repo, "rev-parse", "HEAD")
    if shown.returncode != 0:
        raise HarnessError("git rev-parse failed")
    if shown.stdout.strip() == before:
        raise HarnessError("run branch is already merged")
    return shown.stdout.strip()


def _author_config(repo: Path) -> tuple[str, ...]:
    args = ["-c", "commit.gpgsign=false"]
    if not _setting(repo, "user.name"):
        args.extend(["-c", "user.name=recursion"])
    if not _setting(repo, "user.email"):
        args.extend(["-c", "user.email=recursion@localhost"])
    return tuple(args)


def _setting(repo: Path, key: str) -> str:
    completed = _run(repo, "config", "--get", key)
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _dirty(repo: Path) -> bool:
    completed = _run(repo, "status", "--porcelain")
    if completed.returncode != 0:
        raise HarnessError("git status failed")
    return bool(completed.stdout.strip())


def _current_branch(repo: Path) -> str | None:
    completed = _run(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if completed.returncode != 0:
        raise HarnessError("git rev-parse failed")
    name = completed.stdout.strip()
    if not name or name == "HEAD":
        return None
    return name


def _ref_exists(repo: Path, branch: str) -> bool:
    completed = _run(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    return completed.returncode == 0


def _run(cwd: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    kwargs = {
        "cwd": cwd,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "check": False,
    }
    if env is not None:
        kwargs["env"] = env
    return subprocess.run(["git", "-c", "core.longpaths=true", *args], **kwargs)
