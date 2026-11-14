"""The search tool's worker. Runs as its own process (``python -I <this file>``), standard library only.

It reads one JSON request on standard input and writes one JSON answer on standard output. It runs in a child
process so that a pattern that backtracks without end, or a huge tree, can be stopped (the parent kills it at
its deadline or when the turn is interrupted) without stopping the loop.

Files: inside a git work tree, ``git ls-files --cached --others --exclude-standard`` (tracked files and
untracked files that .gitignore does not exclude); elsewhere a walk that skips version-control and cache
folders. Symbolic links that lead outside the workspace, binary files and files larger than the size limit are
skipped and counted.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", ".tox", ".mypy_cache",
             ".pytest_cache", ".ruff_cache"}


def _inside(root: str, real: str) -> bool:
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


def _git_files(base: str) -> list[str] | None:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        inside = subprocess.run(["git", "-C", base, "rev-parse", "--is-inside-work-tree"], capture_output=True,
                                env=env, check=False)
    except OSError:
        return None
    if inside.returncode != 0 or inside.stdout.strip() != b"true":
        return None
    listed = subprocess.run(["git", "-C", base, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                             "--", "."], capture_output=True, env=env, check=False)
    if listed.returncode != 0:
        return None
    names = sorted({n.decode("utf-8", "surrogateescape") for n in listed.stdout.split(b"\x00") if n})
    return names


def _walk_files(base: str) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        rel_dir = os.path.relpath(dirpath, base)
        for name in sorted(filenames):
            out.append(name if rel_dir == "." else os.path.join(rel_dir, name))
    return out


def search(req: dict) -> dict:
    root = req["root"]
    base = req["base"]
    flags = re.IGNORECASE if req.get("ignore_case") else 0
    pattern = re.escape(req["pattern"]) if req.get("literal") else req["pattern"]
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        return {"error": f"the pattern is not a valid regular expression: {exc}"}
    glob = req.get("glob")
    max_results = req["max_results"]
    max_line = req["max_line_chars"]
    max_file = req["max_file_bytes"]
    files_only = req.get("files_only", False)
    if os.path.isfile(base):
        candidates = [os.path.basename(base)]
        base_dir = os.path.dirname(base)
        rel_prefix = os.path.relpath(base_dir, root)
    else:
        base_dir = base
        rel_prefix = os.path.relpath(base, root)
        listed = _git_files(base) if req.get("use_git", True) else None
        candidates = listed if listed is not None else _walk_files(base)
    matches: list[list] = []
    files: list[str] = []
    scanned = skipped_binary = skipped_large = skipped_outside = 0
    more = False
    for rel in candidates:
        full = os.path.join(base_dir, rel)
        shown = rel if rel_prefix == "." else os.path.join(rel_prefix, rel)
        shown = shown.replace(os.sep, "/")
        if glob and not (fnmatch.fnmatch(shown, glob) or fnmatch.fnmatch(os.path.basename(rel), glob)):
            continue
        real = os.path.realpath(full)
        if not _inside(root, real):
            skipped_outside += 1
            continue
        try:
            if not os.path.isfile(real):
                continue
            if os.path.getsize(real) > max_file:
                skipped_large += 1
                continue
            with open(real, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            skipped_binary += 1
            continue
        scanned += 1
        text = data.decode("utf-8", "replace")
        hit = False
        for no, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hit = True
                if files_only:
                    break
                if len(matches) >= max_results:
                    more = True
                    break
                cut = line if len(line) <= max_line else line[:max_line] + " [line cut]"
                matches.append([shown, no, cut])
        if hit:
            if files_only and len(files) >= max_results:
                more = True
                break
            files.append(shown)
        if more and not files_only:
            break
    return {"matches": matches, "files": files, "more": more, "scanned": scanned,
            "skipped_binary": skipped_binary, "skipped_large": skipped_large, "skipped_outside": skipped_outside}


def main() -> int:
    req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    sys.stdout.write(json.dumps(search(req), ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
