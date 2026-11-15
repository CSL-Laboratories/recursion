"""Real temporary git repositories for this lane's tests, made by git itself: its hooks off, no credential
helper, and no configuration read but the repository's own (not the operator's global or system files)."""
from __future__ import annotations

import os
import pathlib
import subprocess

GIT = '/usr/bin/git'
HAVE_GIT = os.access(GIT, os.X_OK)
FLAGS = ('-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'credential.helper=')
WHO = ('-c', 'user.name=Workspace Test', '-c', 'user.email=workspace-test@example.invalid', '-c', 'commit.gpgsign=false')
ENVIRONMENT = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/nonexistent/recursion-workspace-test-home',
               'XDG_CONFIG_HOME': '/nonexistent/recursion-workspace-test-home', 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C'}


def git(folder, *args: str, check: bool = True) -> bytes:
    """``git -C folder args``; its standard output. A failure raises ``AssertionError`` with git's message."""
    done = subprocess.run([GIT, '-C', os.fspath(folder), *FLAGS, *args], stdin=subprocess.DEVNULL,
                          capture_output=True, env=ENVIRONMENT, timeout=120, check=False)
    if check and done.returncode != 0:
        raise AssertionError(f'git {" ".join(args)}: {done.stderr.decode("utf-8", "replace").strip()}')
    return done.stdout


def commit(top: pathlib.Path, message: str = 'change') -> str:
    git(top, 'add', '-A')
    git(top, *WHO, 'commit', '-q', '--allow-empty', '-m', message)
    return git(top, 'rev-parse', 'HEAD').decode('ascii').strip()


def make_repo(top: pathlib.Path, files: dict | None = None) -> str:
    """A repository at ``top`` on branch main with ``files`` committed; its HEAD commit."""
    top.mkdir(parents=True, exist_ok=True)
    git(top, 'init', '-q', '-b', 'main')
    for name, text in (files or {'README.md': 'hello\n'}).items():
        path = top / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode('utf-8'))
    return commit(top, 'first')


def worktrees(top: pathlib.Path) -> list:
    """The worktree folders git lists for the repository at ``top``."""
    return [line[len('worktree '):] for line in git(top, 'worktree', 'list', '--porcelain').decode().splitlines()
            if line.startswith('worktree ')]
