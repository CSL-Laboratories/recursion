"""Harvest: held-out tasks from merged commits of given repositories, after a not-before date.

    python -m eval.harvest hashes --repos repos.json
    python -m eval.harvest run --repos repos.json --not-before 2026-11-20 --suite pilot --out /abs/held-out/pilot

For every first-parent commit on each repository's branch with a committer date on or after the
not-before date (a merge commit is compared with its first parent, so a merged pull request is one
task): the commit must change at least one test file (by the repository's ``test_globs``) and at
least one other file. The task is the parent's tree; its prompt is the linked issue's text when an
issues file maps the commit to one, else the commit message without trailers; its hidden tests are
the commit's versions of the test files it changed; its reference solution is the rest of the
commit. It is kept only if the hidden tests fail on the parent and pass on the commit, on every one
of ``--reruns`` runs (eval/validate.py). Everything rejected is listed with its reason.

``hashes`` prints the SHA-256 of this script's files and of the repository list, to be published
BEFORE the freeze (PROTOCOL.md, "Held-out tasks"); the harvested suite records both.

repos.json (``recursion-eval-repos/1``):
    {"schema": "recursion-eval-repos/1",
     "repos": [{"name": "x", "path": "/abs/clone", "branch": "main",
                "test_globs": ["tests/**/test_*.py"],
                "test_command": ["{python}", "-B", "-m", "unittest", "{hidden_modules}"],
                "timeout_s": 1800, "grading_timeout_s": 600}]}
The clones are made beforehand, by the operator; this script does not use the network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from eval import PACKAGE_ROOT, __version__, files_digest, gitutil, heldout
from eval import scratch as scratch_mod
from eval import tasks as task_mod
from eval import validate as validate_mod

REPOS_SCHEMA = "recursion-eval-repos/1"
HARVEST_FILES = ("__init__.py", "harvest.py", "validate.py", "tasks.py", "txtar.py", "gitutil.py", "scratch.py",
                 "arms.py", "record.py", "heldout.py", "suite.py")
# What one candidate's own files can raise while its task is written, loaded and validated: its stored form
# refused (TaskError: a bad path, a start-tree entry that is not a file), its start archive unreadable
# (TarError), and a path that cannot be laid out as its tree says (a file where a folder must be, or the
# reverse). Not a GitError (git failing on a fresh scratch copy is the machine's: a permission, a full disk,
# a broken object store), not a plain ValueError, a RuntimeError or any other OSError: those are the
# caller's or the machine's and end the run. A candidate's paths that git or the task form cannot hold,
# including a source working-tree-encoding other than UTF-8, are refused before this point, in
# build_candidate (tree_files), as that candidate's rejection.
CANDIDATE_FAULTS = (task_mod.TaskError, tarfile.TarError, FileExistsError, IsADirectoryError, NotADirectoryError)
GRADING_FIELDS = ("test_command", "timeout_s", "grading_timeout_s")
TRAILER = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: .+$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class HarvestError(ValueError):
    pass


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and bool(value) and "\0" not in value


def _refuse_repository_shape(path: Path, repo: dict) -> None:
    """Name, path, branch and test_globs are the caller's. A wrong type is refused here, before any
    output, not as a traceback once a candidate is built. A string of globs would be read one character
    at a time and then look like a repository that changed no test."""
    name = repo.get("name")
    if not _nonempty_str(name):
        raise HarvestError(f"{path}: repository name must be a non-empty string, got {name!r}")
    for key in ("path", "branch"):
        if not _nonempty_str(repo.get(key)):
            raise HarvestError(f"{path}: repository {name!r}: {key} must be a non-empty string")
    globs = repo.get("test_globs")
    if not isinstance(globs, list) or not globs or not all(_nonempty_str(item) for item in globs):
        raise HarvestError(
            f"{path}: repository {name!r}: test_globs must be a non-empty list of non-empty strings")


def _refuse_caller_limits(path: Path, repo: dict) -> None:
    """A timeout must be a finite positive number, and a command argument must be passable to a process.

    NaN and Infinity pass a ``<= 0`` check (NaN compares false; Infinity is not ``<= 0``) and then
    publish a document no strict reader accepts, or wait with no deadline. An integer that does not
    fit in a float raises OverflowError while the task is built. A NUL in an argument cannot be executed.
    """
    name = repo["name"]
    command = repo.get("test_command")
    if isinstance(command, list) and any(isinstance(part, str) and "\0" in part for part in command):
        raise HarvestError(f"{path}: repository {name!r}: test_command must not contain a NUL byte")
    for key in ("timeout_s", "grading_timeout_s"):
        if key not in repo or repo[key] is None:
            continue
        value = repo[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue  # the shared task rule reports a wrong type; this only adds the finite check
        try:
            number = float(value)
        except OverflowError:
            raise HarvestError(
                f"{path}: repository {name!r}: {key} must be a finite positive number") from None
        if not math.isfinite(number) or number <= 0:
            raise HarvestError(f"{path}: repository {name!r}: {key} must be a finite positive number")


def _refuse_unwritable_path(commit: str, name: str) -> None:
    """A path git stored as UTF-8 can still be unwritable where the filesystem encoding is not UTF-8
    (``LC_ALL=C`` and UTF-8 mode off). That is this candidate's refusal, the same one a non-UTF-8
    byte path already gets, not the end of the harvest."""
    try:
        encoded = os.fsencode(name)
    except UnicodeEncodeError:
        encoded = None
    if encoded is None or os.fsdecode(encoded) != name:
        raise HarvestError(
            f"{commit[:12]}: the path {ascii(name)} cannot be written with this filesystem encoding; "
            "a task's paths are UTF-8 text")


def _refuse_worktree_encoding(commit: str, files: dict) -> None:
    """A source ``working-tree-encoding`` other than UTF-8 makes a later scratch ``git add`` fail on a
    healthy machine: git stored a UTF-8 blob and the attribute asks for another encoding. That is the
    candidate's own refusal. UTF-8 itself is the encoding the harness already writes."""
    marker = "working-tree-encoding="
    for path, (data, _mode) in files.items():
        if path != ".gitattributes" and not path.endswith("/.gitattributes"):
            continue
        for raw in data.splitlines():
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith("#"):
                continue
            for attr in line.split()[1:]:
                if not attr.startswith(marker):
                    continue
                value = attr[len(marker):].strip().strip('"').strip("'")
                if value.lower() not in ("utf-8", "utf8"):
                    raise HarvestError(
                        f"{commit[:12]}: unsupported working-tree-encoding {value!r} in {path}; "
                        "that source transformation is this candidate's refusal, not a machine failure")


def harvest_sha256() -> str:
    return files_digest(PACKAGE_ROOT, HARVEST_FILES)


def glob_regex(pattern: str) -> re.Pattern:
    """``**/`` is any number of folders, ``*`` and ``?`` stay inside one path part."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def load_repos(path: Path) -> tuple:
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("schema") != REPOS_SCHEMA:
        raise HarvestError(f"{path}: not a {REPOS_SCHEMA} file")
    repos = data.get("repos")
    if not isinstance(repos, list) or not repos:
        raise HarvestError(f"{path}: repos must be a non-empty list")
    names = set()
    for repo in repos:
        if not isinstance(repo, dict):
            raise HarvestError(f"{path}: a repository entry must be an object")
        for key in ("name", "path", "branch", "test_globs", "test_command"):
            if key not in repo:
                raise HarvestError(f"{path}: a repository entry lacks {key!r}")
        _refuse_repository_shape(path, repo)
        if not task_mod.TASK_ID.match(repo["name"]) or repo["name"] in names:
            raise HarvestError(f"{path}: bad or duplicate repository name {repo['name']!r}")
        names.add(repo["name"])
        try:   # the same rules a task's own task.json is held to (eval/tasks.py), checked once, here
            task_mod._meta(repo["name"], {"id": repo["name"], **{k: repo[k] for k in GRADING_FIELDS if k in repo}})
        except task_mod.TaskError as exc:
            raise HarvestError(f"{path}: repository {repo['name']!r}: {exc}") from None
        _refuse_caller_limits(path, repo)
        if not Path(repo["path"]).is_absolute():
            raise HarvestError(f"{path}: repository path must be absolute: {repo['path']!r}")
    return repos, hashlib.sha256(raw).hexdigest()


def prompt_from_message(message: str) -> str:
    """The commit message without its trailer block (Signed-off-by, Co-authored-by and the like)."""
    paragraphs = [p for p in re.split(r"\n\s*\n", message.strip()) if p.strip()]
    if len(paragraphs) > 1 and all(TRAILER.match(line.strip()) for line in paragraphs[-1].splitlines()):
        paragraphs = paragraphs[:-1]
    return "\n\n".join(paragraphs).strip() + "\n"


def _git(repo: Path, *args) -> bytes:
    return gitutil.git(list(args), repo)


def candidate_commits(repo: Path, branch: str, not_before: str) -> list:
    """The branch's first-parent commits, oldest first, whose committer date is on or after ``not_before``
    (00:00 UTC). The whole first-parent history is walked and each commit's own date compared:
    ``rev-list --since`` stops walking at the first older commit, so where committer dates run backward
    it left eligible commits out. (``--since-as-filter`` filters too, but only from git 2.37; this needs
    no particular git.)"""
    since = int(datetime.strptime(not_before, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    out = _git(repo, "rev-list", "--first-parent", "--reverse", "--timestamp", branch)
    commits = []
    for line in out.decode().splitlines():
        stamp, sha = line.split()
        if int(stamp) >= since:
            commits.append(sha)
    return commits


def tree_files(repo: Path, commit: str) -> dict:
    """path -> (bytes, mode) for every blob of the commit's tree. Submodules and symlinks refuse."""
    listing = _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    entries = []
    for item in listing.split(b"\0"):
        if not item:
            continue
        meta, path = item.split(b"\t", 1)
        mode, kind, sha = meta.decode().split()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise HarvestError(f"{commit[:12]}: {path.decode('utf-8', 'replace')} is a {kind} with mode {mode}")
        try:
            name = path.decode("utf-8")
        except UnicodeDecodeError:
            raise HarvestError(f"{commit[:12]}: the path {path.decode('utf-8', 'replace')!r} is not UTF-8; "
                               "a task's paths are UTF-8 text") from None
        _refuse_unwritable_path(commit, name)
        entries.append((name, sha, int(mode[-3:], 8)))
    blobs = _cat_blobs(repo, [sha for _, sha, _ in entries])
    files = {path: (blobs[sha], mode) for path, sha, mode in entries}
    _refuse_worktree_encoding(commit, files)
    return files


def _cat_blobs(repo: Path, shas: list) -> dict:
    if not shas:
        return {}
    data = gitutil.git(["cat-file", "--batch"], repo, input=("\n".join(shas) + "\n").encode())
    blobs = {}
    pos = 0
    for sha in shas:
        header_end = data.index(b"\n", pos)
        header = data[pos:header_end].decode().split()
        size = int(header[2])
        start = header_end + 1
        blobs[header[0]] = data[start:start + size]
        pos = start + size + 1
        if header[0] != sha:
            raise HarvestError(f"cat-file answered {header[0]} for {sha}")
    return blobs


def changed(repo: Path, parent: str, commit: str) -> list:
    out = _git(repo, "diff", "--no-renames", "--name-status", "-z", parent, commit)
    parts = [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]
    return [(parts[i], parts[i + 1]) for i in range(0, len(parts), 2)]


def build_candidate(repo_cfg: dict, commit: str, issues: dict) -> tuple:
    """(task folder contents, None) or (None, reason)."""
    repo = Path(repo_cfg["path"])
    parents = _git(repo, "rev-list", "--parents", "-n", "1", commit).decode().split()
    if len(parents) < 2:
        return None, "root commit"
    parent = parents[1]
    tests = [glob_regex(g) for g in repo_cfg["test_globs"]]
    is_test = lambda p: any(r.match(p) for r in tests)  # noqa: E731
    diff = changed(repo, parent, commit)
    hidden_paths = [p for status, p in diff if is_test(p) and status in ("A", "M")]
    deleted_tests = [p for status, p in diff if is_test(p) and status == "D"]
    sources = [(status, p) for status, p in diff if not is_test(p)]
    if not hidden_paths:
        return None, "changes no test file"
    if not sources:
        return None, "changes only tests"
    try:
        start = tree_files(repo, parent)
        after = tree_files(repo, commit)
    except HarvestError as exc:
        return None, str(exc)
    hidden = {p: after[p][0] for p in hidden_paths}
    solution = {p: (None if status == "D" else after[p][0]) for status, p in sources}
    for p in deleted_tests:   # the commit removed it: part of the reference solution, so the trees agree
        solution[p] = None
    modes = {p: "100755" for p in [*hidden, *(q for q, d in solution.items() if d is not None)]
             if after[p][1] == 0o755}
    commit_tree = _git(repo, "rev-parse", f"{commit}^{{tree}}").decode().strip()
    message = _git(repo, "log", "-1", "--format=%B", commit).decode("utf-8", "replace")
    created = _git(repo, "log", "-1", "--format=%cI", commit).decode().strip()
    task_id = "h-" + hashlib.sha256(f"{repo_cfg['name']}:{commit}".encode()).hexdigest()[:12]
    meta = {
        "id": task_id,
        "cluster": repo_cfg["name"],
        "created_at": created,
        "test_command": repo_cfg["test_command"],
        "timeout_s": repo_cfg.get("timeout_s", task_mod.DEFAULT_TIMEOUT_S),
        "grading_timeout_s": repo_cfg.get("grading_timeout_s", task_mod.DEFAULT_GRADING_TIMEOUT_S),
        "solution_deletes": sorted(p for p, d in solution.items() if d is None),
        "modes": modes,
        "commit_tree": commit_tree,
        "source": {"repo": repo_cfg["name"], "commit": commit, "parent": parent},
    }
    issue = issues.get(commit)
    prompt = issue if isinstance(issue, str) and issue.strip() else prompt_from_message(message)
    parent_tree = _git(repo, "rev-parse", f"{parent}^{{tree}}").decode().strip()
    return {"meta": meta, "prompt": prompt, "start": start, "hidden": hidden,
            "solution": {p: d for p, d in solution.items() if d is not None},
            "parent": parent, "commit": commit, "parent_tree": parent_tree, "commit_tree": commit_tree}, None


def write_task(folder: Path, candidate: dict) -> None:
    folder.mkdir(parents=True)
    (folder / "task.json").write_text(json.dumps(candidate["meta"], indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (folder / "prompt.md").write_text(candidate["prompt"], encoding="utf-8")
    (folder / "start.tar").write_bytes(task_mod.tar_of_files(candidate["start"]))
    for sub in ("hidden", "solution"):
        for name, data in candidate[sub].items():
            path = folder / sub / task_mod.safe_relpath(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


def harvest(repos_path: Path, not_before: str, out: Path, *, suite_id: str, version: str = "1",
            max_per_repo: int | None = None, reruns: int = 2, issues_path: Path | None = None,
            tmp_base: Path = scratch_mod.DEFAULT_TMP_BASE, xdg_base: Path = scratch_mod.DEFAULT_XDG_BASE,
            log=print) -> dict:
    if not DATE.match(not_before):
        raise HarvestError("--not-before is a date, YYYY-MM-DD")
    if not task_mod.TASK_ID.match(suite_id):
        raise HarvestError(f"bad suite id {suite_id!r}")
    # What would be refused for every candidate alike is the caller's, so it ends the run here, before
    # anything is written, and never reaches the per-candidate rejection below.
    if isinstance(reruns, bool) or not isinstance(reruns, int) or reruns < 1:
        raise HarvestError(f"--reruns must be an integer of at least 1, got {reruns!r}")
    if max_per_repo is not None and (isinstance(max_per_repo, bool) or not isinstance(max_per_repo, int)
                                     or max_per_repo < 1):
        raise HarvestError(f"--max-per-repo must be an integer of at least 1, got {max_per_repo!r}")
    try:
        scratch_mod.check_bases(tmp_base, xdg_base)
    except scratch_mod.ScratchError as exc:
        raise HarvestError(str(exc)) from None
    out = Path(out)
    heldout.check_path(out, must_exist=False)  # absolute, and never inside the repository
    if out.exists() and any(out.iterdir()):
        raise HarvestError(f"{out} is not empty: a harvest writes a new folder")
    repos, repos_sha = load_repos(repos_path)   # every repository's settings, before anything is written
    out.mkdir(parents=True, exist_ok=True)
    heldout.check_path(out)
    issues = json.loads(Path(issues_path).read_text(encoding="utf-8")) if issues_path else {}
    staging = out / ".staging"
    entries, rejects, heads = [], [], []
    for repo_cfg in repos:
        repo = Path(repo_cfg["path"])
        head = _git(repo, "rev-parse", repo_cfg["branch"]).decode().strip()
        heads.append({"name": repo_cfg["name"], "branch": repo_cfg["branch"], "head": head})
        kept = 0
        for commit in candidate_commits(repo, repo_cfg["branch"], not_before):
            if max_per_repo is not None and kept >= max_per_repo:
                break
            candidate, reason = build_candidate(repo_cfg, commit, issues)
            if candidate is None:
                rejects.append({"repo": repo_cfg["name"], "commit": commit, "reason": reason})
                continue
            task_id = candidate["meta"]["id"]
            folder = staging / task_id
            try:
                write_task(folder, candidate)
                task = task_mod.load_dir(folder)
                task.parent_tree = candidate["parent_tree"]
                checked = validate_mod.validate(task, reruns=reruns, tmp_base=tmp_base, xdg_base=xdg_base)
            except CANDIDATE_FAULTS as exc:
                # One candidate that cannot be laid out or checked is that candidate's rejection, not the
                # end of the harvest. Anything else (a caller's argument, the machine) ends the run.
                checked = validate_mod.Validation(
                    ok=False, reason=f"the task could not be built or checked: {type(exc).__name__}: {exc}"[:500])
            if not checked.ok:
                shutil.rmtree(folder, ignore_errors=True)
                rejects.append({"repo": repo_cfg["name"], "commit": commit, "reason": checked.reason})
                log(f"rejected {repo_cfg['name']} {commit[:12]}: {checked.reason}")
                continue
            final = out / task_id
            folder.rename(final)
            entries.append({"id": task_id, "dir": task_id, "sha256": task_mod.dir_digest(final),
                            "parent_tree": candidate["parent_tree"], "parent_commit": candidate["parent"],
                            "commit": candidate["commit"], "commit_tree": candidate["commit_tree"],
                            "cluster": repo_cfg["name"]})
            kept += 1
            log(f"kept {task_id} ({repo_cfg['name']} {commit[:12]})")
    if staging.exists():
        shutil.rmtree(staging)
    report = {
        "harvest_sha256": harvest_sha256(),
        "harvest_files": list(HARVEST_FILES),
        "runner_version": __version__,
        "repos_sha256": repos_sha,
        "not_before": not_before,
        "reruns": reruns,
        "repos": heads,
        "harvested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kept": len(entries),
        "rejected": rejects,
    }
    (out / "harvest-report.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    if entries:
        manifest = {
            "schema": "recursion-eval-suite/1", "id": suite_id, "version": version, "split": "held_out",
            "frozen": False, "runner": None, "evaluated_commit": None, "engines": {}, "arms": {},
            "harvest": {k: report[k] for k in ("harvest_sha256", "repos_sha256", "not_before", "reruns", "repos")},
            "tasks": entries,
        }
        (out / "suite.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.harvest", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    hashes = sub.add_parser("hashes", help="print the script's and the repository list's SHA-256")
    hashes.add_argument("--repos", type=Path, required=True)
    run = sub.add_parser("run", help="harvest a held-out suite into a new folder")
    run.add_argument("--repos", type=Path, required=True)
    run.add_argument("--not-before", required=True)
    run.add_argument("--suite", required=True)
    run.add_argument("--version", default="1")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--max-per-repo", type=int)
    run.add_argument("--reruns", type=int, default=2)
    run.add_argument("--issues", type=Path, help="JSON object: commit sha -> issue text")
    run.add_argument("--tmp-base", type=Path, default=scratch_mod.DEFAULT_TMP_BASE)
    run.add_argument("--xdg-base", type=Path, default=scratch_mod.DEFAULT_XDG_BASE)
    args = parser.parse_args(argv)
    try:
        if args.command == "hashes":
            _, repos_sha = load_repos(args.repos)
            print(f"harvest_sha256 {harvest_sha256()}")
            print(f"repos_sha256   {repos_sha}")
            return 0
        report = harvest(args.repos, args.not_before, args.out, suite_id=args.suite, version=args.version,
                         max_per_repo=args.max_per_repo, reruns=args.reruns, issues_path=args.issues,
                         tmp_base=args.tmp_base, xdg_base=args.xdg_base)
    except (HarvestError, heldout.HeldOutDenied, gitutil.GitError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"kept {report['kept']}, rejected {len(report['rejected'])}")
    return 0 if report["kept"] else 1


if __name__ == "__main__":
    sys.exit(main())
