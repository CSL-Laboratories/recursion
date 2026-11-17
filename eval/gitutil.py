"""git, run apart from the operator's own configuration, with fixed identities and dates.

Every repository this package makes (a task's fresh copy, a harvest check) is made with no global or
system configuration, no template (so no hooks), a fixed author, committer and date. The same files
then always give the same commit, and a task's start tree can be checked against its pinned tree id.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

FIXED_DATE = "2026-01-01T00:00:00+0000"
IDENTITY = ("recursion-eval", "eval@invalid")
# Small repositories this package builds finish well under this. A git that does not finish is a GitError.
GIT_TIMEOUT_S = 120


class GitError(RuntimeError):
    pass


def git_env(extra: dict | None = None) -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": IDENTITY[0],
        "GIT_AUTHOR_EMAIL": IDENTITY[1],
        "GIT_COMMITTER_NAME": IDENTITY[0],
        "GIT_COMMITTER_EMAIL": IDENTITY[1],
        "GIT_AUTHOR_DATE": FIXED_DATE,
        "GIT_COMMITTER_DATE": FIXED_DATE,
        "LC_ALL": "C",
        "HOME": os.devnull,
    }
    if extra:
        env.update(extra)
    return env


def git(args: list, cwd: Path, *, input: bytes | None = None, check: bool = True) -> bytes:
    command = ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.quotePath=false",
               *args]
    try:
        result = subprocess.run(command, cwd=str(cwd), input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=git_env(), check=False, timeout=GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args[:3])} did not finish within {GIT_TIMEOUT_S:g} s") from exc
    except NotADirectoryError as exc:
        raise GitError(f"git {' '.join(args[:3])} cannot run in {cwd}: {exc}") from exc
    except FileNotFoundError as exc:
        # A missing work folder raises FileNotFoundError from the process call. A missing git binary
        # raises the same type while the folder is present, and that one stays FileNotFoundError.
        if not Path(cwd).is_dir():
            raise GitError(f"git {' '.join(args[:3])} cannot run in {cwd}: {exc}") from exc
        raise
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])} failed ({result.returncode}): "
                       f"{result.stderr.decode('utf-8', 'replace').strip()[:500]}")
    return result.stdout


def stage_task_files(work: Path) -> None:
    """Put every file under ``work`` into its index, those its own .gitignore matches included.

    Called only where ``work`` holds exactly a task's stored files: a fresh copy of a start tree, with or
    without the reference solution and the hidden tests laid over it, before anything has run in it. Every
    such file is a path the source commit tracks, and a source repository may track a file its .gitignore
    matches (added with ``git add --force``, or tracked before the rule was written); a plain ``git add -A``
    would leave it out and give another tree than the source commit's. ``--force`` adds nothing more here,
    because nothing else is in the folder. After an engine has run, use :func:`diff_from`, which does not
    force: the files a run leaves that the task never tracked and its .gitignore matches stay out.
    """
    git(["add", "--all", "--force"], work)


def snapshot(work: Path, message: str = "task start") -> tuple:
    """Make ``work`` a new repository holding its files as one commit. Returns (commit, tree)."""
    git(["init", "-q", "--template=", "-b", "main"], work)
    stage_task_files(work)
    git(["commit", "-q", "--allow-empty", "-m", message], work)
    commit = git(["rev-parse", "HEAD"], work).decode().strip()
    tree = git(["rev-parse", "HEAD^{tree}"], work).decode().strip()
    return commit, tree


def tree_of_files(work: Path) -> str:
    """The tree id the files under ``work`` would have, from a throwaway repository."""
    git(["init", "-q", "--template=", "-b", "main"], work)
    stage_task_files(work)
    return git(["write-tree"], work).decode().strip()


def diff_from(work: Path, base_commit: str) -> tuple:
    """Everything that changed under ``work`` since ``base_commit``, new files included.

    Returns (binary diff bytes, sorted changed paths). Works when the engine made commits of its own.
    Not forced: a path the task tracks is in the index since :func:`snapshot`, so its changes and deletion
    are staged even when .gitignore matches it, and a new file that .gitignore matches stays out.
    """
    git(["add", "-A"], work)
    diff = git(["diff", "--cached", "--binary", "--no-color", "--no-ext-diff", "--no-textconv", base_commit],
               work)
    names = git(["diff", "--cached", "--name-only", "-z", "--no-ext-diff", base_commit], work)
    paths = sorted(p for p in names.decode("utf-8", "surrogateescape").split("\0") if p)
    return diff, paths
