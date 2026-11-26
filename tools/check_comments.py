#!/usr/bin/env python3
"""Refuse NEW comments and docstrings in the Python files a change adds or modifies.

Comments come out of the code; reasoning lives in csl-research (owner, entries 70
and 78 item B9). A line is new when the diff from the merge base of --base and
--head adds it AND git blame hands it to a commit that is reachable from none of:
the commits recorded in tools/comment_cutoff.json, --base, origin/master. So
comments already on master or on a recorded open draft pass; deleting passes.

Allowed comments: a line-1 interpreter line, a line-1/2 encoding line, tool marks
alone (noqa, type: ignore, pragma: no cover/branch, fmt:, nosec, pylint: disable=,
isort:, pyright: ignore), an SPDX licence line on lines 1-3, and exactly
'# CSLR-D-NNN' or '# CSLR-I-NNN'. A docstring (every bare string statement)
stays only where this file reads its __doc__, for the plugin_security __all__
API, or when listed in DOCSTRINGS_READ_ELSEWHERE. Shell, JS and YAML comments are
not checked. Deterministic and provider-free; read-only git commands only.

    python -X utf8 -B tools/check_comments.py --base origin/master
    python -X utf8 -B tools/check_comments.py --base origin/master --vault ../csl-research
    python -X utf8 -B tools/check_comments.py --write-cutoff heads.txt

With no --base and no CI_BASE_SHA it prints SKIP and exits 0. --vault also checks
that every new pointer has submissions/<id>/record.json at origin/main of a local
csl-research clone; it never touches the network.
"""
from __future__ import annotations

import argparse
import ast
import codecs
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from datetime import datetime, timezone
from pathlib import Path

CUTOFF = Path("tools") / "comment_cutoff.json"
CUTOFF_FORMAT = "csl-comment-cutoff/v1"
REPOSITORY = "CSL-Laboratories/recursion"
CUTOFF_KEYS = {"format", "repository", "recorded_at", "commits"}
COMMIT = re.compile(r"[0-9a-f]{40}")
GIT_TIMEOUT_SECONDS = 300
MAX_SHOWN = 200
PLUGIN_API = "core/recursion_core/plugin_security/__init__.py"
PLUGIN_DIR = "core/recursion_core/plugin_security/"

DOCSTRINGS_READ_ELSEWHERE = frozenset({
    ("core/recursion_core/tui/protocol.py", ""),
    ("core/recursion_core/tui/sessions.py", ""),
    ("core/recursion_core/tui/sessions.py", "session_row"),
    ("core/recursion_core/tui/sessions.py", "page_problem"),
    ("core/recursion_core/tui/sessions.py", "Transcript"),
    ("core/recursion_core/tui/sessions.py", "Transcript.apply"),
    ("core/recursion_core/tui/app.py", "TuiApp._search"),
    ("core/recursion_core/tui/app.py", "TuiApp._transcript_page"),
    ("core/recursion_core/tui/app.py", "TuiApp._rebuild"),
    ("core/recursion_core/control/hooks.py", ""),
    ("core/recursion_core/events/db.py", ""),
    ("core/recursion_core/control_transport/__init__.py", ""),
    ("core/recursion_core/control_transport/windows.py", ""),
    ("core/recursion_core/control_transport/windows.py", "WindowsControlServer"),
    ("core/recursion_core/control_transport/windows.py", "WindowsControlClient"),
})

_MARK = (
    r"(?:noqa(?::\s*[A-Z]+[0-9]+(?:\s*,\s*[A-Z]+[0-9]+)*)?"
    r"|type:\s*ignore(?:\[[A-Za-z0-9_,\s-]+\])?"
    r"|pragma:\s*no\s+(?:cover|branch)"
    r"|fmt:\s*(?:off|on|skip)"
    r"|nosec(?:\s+B[0-9]+(?:\s*,\s*B[0-9]+)*)?"
    r"|pylint:\s*disable=[A-Za-z0-9_,-]+"
    r"|isort:\s*(?:skip_file|skip|off|on)"
    r"|pyright:\s*ignore(?:\[[A-Za-z0-9_,\s-]+\])?)"
)
TOOL_PREFIX = re.compile(r"#\s*" + _MARK + r"(?:\s+#\s*" + _MARK + r")*")
TOOL = re.compile(TOOL_PREFIX.pattern + r"\s*\Z")
CODING = re.compile(r"#\s*(?:-\*-\s*)?(?:en)?coding[:=]\s*[-\w.]+(?:\s*-\*-)?\s*\Z")
SPDX = re.compile(r"# SPDX-License-Identifier: [A-Za-z0-9.+:()\- ]+\Z")
POINTER = re.compile(r"# (CSLR-(?:D|I)-[0-9]{3,})\Z")
POINTER_LIKE = re.compile(r"#\s*CSLR", re.IGNORECASE)
OPEN_FIRST_LINE = re.compile(rb"[ \t\f]*(?:#|\r?\Z)")

COMMENT_ADVICE = (
    "  delete it. A rule worth keeping: write a test that fails without it; if no test can hold it, "
    "file a csl-research record and leave only '# CSLR-I-NNN'"
)
POINTER_ADVICE = "  pointer must be exactly '# CSLR-D-NNN' or '# CSLR-I-NNN'"
DOCSTRING_ADVICE = (
    "  delete it. Allowed only where this file reads its __doc__, for plugin_security's __all__, "
    "or when listed in tools/check_comments.py DOCSTRINGS_READ_ELSEWHERE"
)
STRING_ADVICE = "  delete it. A bare string statement is a comment by another name; " + COMMENT_ADVICE.strip()


class CheckError(Exception):
    pass


def _printable(text: object) -> str:
    return str(text).encode("utf-8", "backslashreplace").decode("utf-8").replace("\0", "\\x00")


def _shown(text: str, limit: int = 100) -> str:
    text = text if len(text) <= limit else text[: limit - 3] + "..."
    return repr(text)


def git(root: Path, *args: str, stdin: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "-c", "core.quotePath=false", *args],
            input=stdin,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"git {args[0]} timed out after {GIT_TIMEOUT_SECONDS} seconds") from exc
    except OSError as exc:
        raise CheckError(f"cannot run git: {exc}") from exc
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise CheckError(f"git {' '.join(args[:2])} failed: {detail}")
    return result


def classify_comment(text: str, line: int, prefix: str, first_line_open: bool) -> tuple[str, str | None]:
    if line == 1 and not prefix and text.startswith("#!"):
        return "allowed", None
    own_line = not prefix.strip(" \t\f")
    if own_line and (line == 1 or (line == 2 and first_line_open)) and CODING.match(text):
        return "allowed", None
    if line <= 3 and SPDX.match(text):
        return "allowed", None
    if TOOL.match(text):
        return "allowed", None
    pointer = POINTER.match(text)
    if pointer:
        return "pointer", pointer.group(1)
    if POINTER_LIKE.match(text):
        return "bad-pointer", None
    prefix = TOOL_PREFIX.match(text)
    if prefix:
        return "tool-text", prefix.group(0).rstrip()
    return "comment", None


def _is_string_statement(node: ast.stmt) -> bool:
    if not isinstance(node, ast.Expr):
        return False
    value = node.value
    return (isinstance(value, ast.Constant) and isinstance(value.value, str)) or isinstance(value, ast.JoinedStr)


def string_statements(tree: ast.Module) -> list[tuple[str | None, str, int, int]]:
    found: list[tuple[str | None, str, int, int]] = []

    def visit(node: ast.AST, qualname: str, kind: str) -> None:
        body = getattr(node, "body", None)
        first = body[0] if isinstance(body, list) and body else None
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt) and _is_string_statement(child):
                is_doc = (
                    child is first
                    and kind in ("module", "class", "function")
                    and isinstance(child.value, ast.Constant)
                )
                found.append((qualname if is_doc else None, kind if is_doc else qualname or "module",
                              child.lineno, child.end_lineno or child.lineno))
            if isinstance(child, ast.ClassDef):
                visit(child, f"{qualname}.{child.name}" if qualname else child.name, "class")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, f"{qualname}.{child.name}" if qualname else child.name, "function")
            elif not isinstance(child, ast.Lambda):
                visit(child, qualname, "block")

    visit(tree, "", "module")
    return found


def doc_readers(tree: ast.Module) -> set[str]:
    top = {node.name for node in tree.body
           if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
    allowed: set[str] = set()

    def visit(node: ast.AST, path: str, scope: str) -> None:
        if isinstance(node, ast.Name) and node.id == "__doc__" and isinstance(node.ctx, ast.Load):
            allowed.add(scope)
        elif isinstance(node, ast.Attribute) and node.attr == "__doc__" and isinstance(node.ctx, ast.Load):
            parts = []
            value = node.value
            while isinstance(value, ast.Attribute):
                parts.append(value.attr)
                value = value.value
            if isinstance(value, ast.Name) and value.id in top and len(parts) <= 1:
                allowed.add(".".join([value.id, *reversed(parts)]))
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            inner = f"{path}.{node.name}" if path else node.name
            body = {id(child) for child in node.body}
            own = inner if isinstance(node, ast.ClassDef) else ""
            for child in ast.iter_child_nodes(node):
                if id(child) in body:
                    visit(child, inner, own)
                else:
                    visit(child, path, scope)
        elif isinstance(node, ast.Lambda):
            visit(node.args, path, scope)
            visit(node.body, path, "")
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            first = node.generators[0]
            for child in ast.iter_child_nodes(node):
                if child is first:
                    for part in ast.iter_child_nodes(child):
                        visit(part, path, scope if part is first.iter else "")
                else:
                    visit(child, path, "")
        else:
            for child in ast.iter_child_nodes(node):
                visit(child, path, scope)

    visit(tree, "", "")
    return allowed


def plugin_api(source: bytes | None) -> dict[str, set[str]]:
    if source is None:
        return {}
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    exported: set[str] = set()
    origin: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                exported |= {e.value for e in node.value.elts
                             if isinstance(e, ast.Constant) and isinstance(e.value, str)}
        elif isinstance(node, ast.ImportFrom) and node.level == 1 and node.module and "." not in node.module:
            for alias in node.names:
                origin[alias.asname or alias.name] = (node.module, alias.name)
    api: dict[str, set[str]] = {PLUGIN_API: {""}}
    for name in exported:
        module, defined = origin.get(name, (None, name))
        path = f"{PLUGIN_DIR}{module}.py" if module else PLUGIN_API
        api.setdefault(path, set()).add(defined)
    return api


def plugin_allowed(tree: ast.Module, names: set[str]) -> set[str]:
    allowed = set(names)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in names:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and not item.name.startswith("_"):
                    allowed.add(f"{node.name}.{item.name}")
    return allowed


def candidates(path: str, source: bytes, api: dict[str, set[str]]) -> list[dict]:
    try:
        tokens = list(tokenize.tokenize(io.BytesIO(source).readline))
        tree = ast.parse(source)
    except (tokenize.TokenError, SyntaxError, ValueError, UnicodeDecodeError) as exc:
        raise CheckError(f"cannot read {path} at HEAD: {exc}") from exc
    first_line = source.split(b"\n", 1)[0]
    if first_line.startswith(codecs.BOM_UTF8):
        first_line = first_line[len(codecs.BOM_UTF8):]
    first_line_open = OPEN_FIRST_LINE.match(first_line) is not None
    out = []
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        verdict, extra = classify_comment(token.string, token.start[0], token.line[: token.start[1]],
                                          first_line_open)
        if verdict != "allowed":
            out.append({"kind": verdict, "first": token.start[0], "last": token.start[0],
                        "text": token.string, "extra": extra})
    allowed = doc_readers(tree)
    allowed |= {name for listed, name in DOCSTRINGS_READ_ELSEWHERE if listed == path}
    if path in api:
        allowed |= plugin_allowed(tree, api[path])
    for qualname, kind, first, last in string_statements(tree):
        if qualname is not None:
            if qualname in allowed:
                continue
            out.append({"kind": "docstring", "first": first, "last": last, "text": "",
                        "extra": (kind, qualname)})
        else:
            out.append({"kind": "string", "first": first, "last": last, "text": "", "extra": kind})
    return out


def added_lines(root: Path, merge_base: str, head: str) -> dict[str, set[int]]:
    spec = ("--no-color", "--no-ext-diff", "--no-renames", "--diff-filter=AM", merge_base, head, "--", "*.py")
    names = [name for name in git(root, "diff", "--name-only", "-z", *spec).stdout.decode("utf-8", "replace")
             .split("\0") if name]
    diff = git(root, "diff", "-U0", *spec).stdout.decode("utf-8", "replace")
    sections = re.split(r"^diff --git [^\n]*\n?", diff, flags=re.MULTILINE)[1:]
    if len(sections) != len(names):
        raise CheckError(f"git diff gave {len(sections)} file sections for {len(names)} changed paths")
    added: dict[str, set[int]] = {}
    for path, section in zip(names, sections):
        for line in section.splitlines():
            if line.startswith(("Binary files ", "GIT binary patch")):
                raise CheckError(f"cannot read {path} at HEAD: git diff treats it as binary")
            if line.startswith("@@"):
                match = re.search(r"\+(\d+)(?:,(\d+))?", line)
                start, count = int(match.group(1)), int(match.group(2) or 1)
                if count:
                    added.setdefault(path, set()).update(range(start, start + count))
    return added


def read_cutoff(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckError(f"cannot read cutoff {path}: {exc}") from exc
    if not isinstance(data, dict) or set(data) != CUTOFF_KEYS:
        raise CheckError(f"cutoff {path} must be an object with keys {', '.join(sorted(CUTOFF_KEYS))}")
    if data["format"] != CUTOFF_FORMAT:
        raise CheckError(f"cutoff {path}: format must be {CUTOFF_FORMAT!r}")
    commits = data["commits"]
    if not isinstance(commits, list) or not all(isinstance(c, str) and COMMIT.fullmatch(c) for c in commits):
        raise CheckError(f"cutoff {path}: commits must be a list of 40-hex commit ids")
    if commits != sorted(set(commits)):
        raise CheckError(f"cutoff {path}: commits must be sorted and unique")
    return commits


def present_commits(root: Path, commits: list[str]) -> list[str]:
    if not commits:
        return []
    out = git(root, "cat-file", "--batch-check=%(objectname) %(objecttype)",
              stdin="".join(f"{c}\n" for c in commits).encode()).stdout.decode()
    return [parts[0] for parts in (line.split() for line in out.splitlines())
            if len(parts) == 2 and parts[1] == "commit"]


def commit_of(root: Path, rev: str) -> str:
    result = git(root, "rev-parse", "-q", "--verify", f"{rev}^{{commit}}", check=False)
    if result.returncode:
        raise CheckError(f"no commit named {rev!r}")
    return result.stdout.decode().strip()


def blame_commits(root: Path, head: str, path: str, lines: set[int]) -> dict[int, str]:
    ranges: list[str] = []
    ordered = sorted(lines)
    start = prev = ordered[0]
    for line in ordered[1:] + [None]:
        if line is not None and line == prev + 1:
            prev = line
            continue
        ranges += ["-L", f"{start},{prev}"]
        if line is not None:
            start = prev = line
    out = git(root, "blame", "--porcelain", *ranges, head, "--", path).stdout.decode("utf-8", "replace")
    commits: dict[int, str] = {}
    for line in out.splitlines():
        match = re.match(r"([0-9a-f]{40}) \d+ (\d+)", line)
        if match:
            commits[int(match.group(2))] = match.group(1)
    return commits


def vault_has(vault: Path, ident: str) -> bool:
    spec = f"origin/main:submissions/{ident}/record.json"
    try:
        result = subprocess.run(["git", "-C", str(vault), "cat-file", "-e", spec],
                                capture_output=True, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def describe(path: str, cand: dict, line: int, vault: Path | None) -> tuple[str, str]:
    kind = cand["kind"]
    where = f"{path}:{line}"
    if kind == "comment":
        return f"{where}: comment: {_shown(cand['text'])}", COMMENT_ADVICE
    if kind == "tool-text":
        return f"{where}: tool mark with text: {_shown(cand['text'])}", f"  keep the mark only: {cand['extra']!r}"
    if kind == "bad-pointer":
        return f"{where}: pointer: {_shown(cand['text'])}", POINTER_ADVICE
    if kind == "pointer":
        return (f"{where}: pointer: {_shown(cand['text'])}",
                f"  no record submissions/{cand['extra']}/record.json at {vault} origin/main")
    span = f"lines {cand['first']}-{cand['last']}"
    if kind == "docstring":
        owner, qualname = cand["extra"]
        label = "module" if owner == "module" else f"{owner} {qualname!r}"
        return f"{path}:{cand['first']}: docstring of {label} ({span})", DOCSTRING_ADVICE
    inside = "module" if cand["extra"] == "module" else repr(cand["extra"])
    return f"{path}:{cand['first']}: string statement in {inside} ({span})", STRING_ADVICE


def run(root: Path, base: str, head: str, cutoff: Path, vault: Path | None) -> tuple[int, list[str]]:
    try:
        return _run(root, base, head, cutoff, vault)
    except CheckError as exc:
        return 1, [f"FAIL: {exc}"]


def _run(root: Path, base: str, head: str, cutoff: Path, vault: Path | None) -> tuple[int, list[str]]:
    head_sha = commit_of(root, head)
    base_sha = commit_of(root, base)
    merge_base = git(root, "merge-base", base_sha, head_sha).stdout.decode().strip()
    recorded = read_cutoff(cutoff)
    present = present_commits(root, recorded)
    anchors = present + [base_sha]
    master = git(root, "rev-parse", "-q", "--verify", "refs/remotes/origin/master^{commit}", check=False)
    if master.returncode == 0:
        anchors.append(master.stdout.decode().strip())
    stdin = (head_sha + "\n" + "".join(f"^{a}\n" for a in anchors)).encode()
    new_commits = set(git(root, "rev-list", "--stdin", stdin=stdin).stdout.decode().split())

    added = added_lines(root, merge_base, head_sha)
    api_source = git(root, "show", f"{head_sha}:{PLUGIN_API}", check=False)
    api = plugin_api(api_source.stdout if api_source.returncode == 0 else None)
    refusals: list[tuple[str, int, str, str]] = []
    checked_lines = sum(len(lines) for lines in added.values())
    for path in sorted(added):
        lines = added[path]
        source = git(root, "show", f"{head_sha}:{path}").stdout
        touched = []
        for cand in candidates(path, source, api):
            if cand["kind"] == "pointer" and vault is None:
                continue
            hit = sorted(set(range(cand["first"], cand["last"] + 1)) & lines)
            if hit:
                touched.append((cand, hit))
        if not touched or not new_commits:
            continue
        blamed = blame_commits(root, head_sha, path, {n for _, hit in touched for n in hit})
        for cand, hit in touched:
            fresh = [n for n in hit if blamed.get(n) in new_commits]
            if not fresh:
                continue
            if cand["kind"] == "pointer" and vault_has(vault, cand["extra"]):
                continue
            first, advice = describe(path, cand, fresh[0], vault)
            refusals.append((path, cand["first"], first, advice))

    missing = len(recorded) - len(present)
    summary = (f"base {base_sha[:8]}, cutoff {cutoff.as_posix()}: {len(recorded)} commits, "
               f"{missing} not present locally; {len(added)} files, {checked_lines} added lines checked")
    if not refusals:
        return 0, [f"PASS: no new comments or docstrings ({len(added)} files, {checked_lines} added lines checked)",
                   summary]
    out = [f"FAIL: {len(refusals)} new comment or docstring line(s) (no new comments; docstrings only for "
           f"run time, the plugin API or command-line help)"]
    for _, _, first, advice in refusals[:MAX_SHOWN]:
        out += [first, advice]
    if len(refusals) > MAX_SHOWN:
        out.append(f"... and {len(refusals) - MAX_SHOWN} more")
    out.append(summary)
    return 1, out


def write_cutoff(source: Path, cutoff: Path) -> tuple[int, list[str]]:
    try:
        ids = source.read_text(encoding="utf-8").split()
    except (OSError, UnicodeDecodeError) as exc:
        return 1, [f"FAIL: cannot read {source}: {exc}"]
    bad = [ident for ident in ids if not COMMIT.fullmatch(ident)]
    if bad:
        return 1, [f"FAIL: not a 40-hex commit id: {_shown(ident)}" for ident in bad]
    commits = sorted(set(ids))
    data = {"format": CUTOFF_FORMAT, "repository": REPOSITORY,
            "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "commits": commits}
    try:
        cutoff.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        return 1, [f"FAIL: cannot write {cutoff}: {exc}"]
    return 0, [f"PASS: wrote {cutoff.as_posix()} with {len(commits)} commits"]


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="base revision (default: the CI_BASE_SHA environment value)")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--cutoff", type=Path, default=CUTOFF)
    parser.add_argument("--vault", type=Path, help="local csl-research clone to check pointer records")
    parser.add_argument("--write-cutoff", type=Path, metavar="FILE", help="write the cutoff from the ids in FILE")
    args = parser.parse_args(argv)
    root = Path.cwd() if root is None else root
    cutoff = args.cutoff if args.cutoff.is_absolute() else root / args.cutoff
    if args.write_cutoff is not None:
        code, lines = write_cutoff(args.write_cutoff, cutoff)
    else:
        base = args.base or os.environ.get("CI_BASE_SHA", "")
        if not base:
            print("SKIP check_comments (no CI_BASE_SHA; pass --base origin/master)")
            return 0
        code, lines = run(root, base, args.head, cutoff, args.vault)
    for line in lines:
        print(_printable(line))
    return code


if __name__ == "__main__":
    sys.exit(main())
