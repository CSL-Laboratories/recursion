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
    result = subprocess.run(command, cwd=str(cwd), input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=git_env(), check=False)
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])} failed ({result.returncode}): "
                       f"{result.stderr.decode('utf-8', 'replace').strip()[:500]}")
    return result.stdout


def snapshot(work: Path, message: str = "task start") -> tuple:
    """Make ``work`` a new repository holding its files as one commit. Returns (commit, tree)."""
    git(["init", "-q", "--template=", "-b", "main"], work)
    git(["add", "-A"], work)
    git(["commit", "-q", "--allow-empty", "-m", message], work)
    commit = git(["rev-parse", "HEAD"], work).decode().strip()
    tree = git(["rev-parse", "HEAD^{tree}"], work).decode().strip()
    return commit, tree


def tree_of_files(work: Path) -> str:
    """The tree id the files under ``work`` would have, from a throwaway repository."""
    git(["init", "-q", "--template=", "-b", "main"], work)
    git(["add", "-A"], work)
    return git(["write-tree"], work).decode().strip()


def diff_from(work: Path, base_commit: str) -> tuple:
    """Everything that changed under ``work`` since ``base_commit``, new files included.

    Returns (binary diff bytes, sorted changed paths). Works when the engine made commits of its own.
    """
    git(["add", "-A"], work)
    diff = git(["diff", "--cached", "--binary", "--no-color", "--no-ext-diff", "--no-textconv", base_commit],
               work)
    names = git(["diff", "--cached", "--name-only", "-z", "--no-ext-diff", base_commit], work)
    paths = sorted(p for p in names.decode("utf-8", "surrogateescape").split("\0") if p)
    return diff, paths
