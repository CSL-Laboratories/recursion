#!/usr/bin/env python3
"""Bounded durable control surface for one project pilot per Telegram topic."""

from __future__ import annotations

import argparse
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses the mkdir mutex below.
    fcntl = None  # type: ignore[assignment]
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from transition_receipt import (
    TransitionReceiptError, authenticated_transition, commit_event, commit_transition,
    json_mutation, resume_transition, raw_text_mutation, text_mutation,
)
try:
    from engine_contract import alias_role_ok, default_registry_path, pilot_engine_ok
except Exception as exc:
    module_name = getattr(exc, "name", None) or "engine_contract"
    if __name__ == "__main__":
        print(f"project-pilot: first-party module '{module_name}' is unavailable or malformed",
              file=sys.stderr)
        raise SystemExit(95)
    raise
from pilot_instruction import require_affirmative_command
from seed_contract import SeedContractError, inspect_repository, validate_contract
from project_authority import (
    AUTHORITY_V2_RELATIONSHIPS, AUTHORITY_V2_SCHEMA, OID, AuthorityError,
    authenticated_remote_head, observe_clean_source, prove_current_remote_lineage,
)
from remote_evidence import RemoteEvidenceError, read_events_for_run


IDENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
ALLOWED_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PILOT_VERBS = {
    "pilot-plan-propose", "pilot-task-create", "pilot-worker-request",
    "pilot-verifier-request", "pilot-block", "pilot-escalate", "pilot-goal-verify",
}
PHASE8_HEAD_VERBS = {"pilot-plan-approve", "pilot-window-open", "pilot-window-close"}
PILOT_ROUTES = PILOT_VERBS | {"status", "context", "repo", "logs", "evidence"}
GATE_PROFILE_SELECTION_SCHEMA = "recursion-project-gate-profile-selection/v1"
GATE_PROFILE_PROMOTION_SCHEMA = "recursion-project-gate-profile-promotion/v1"
LEGACY_AUTHORITY_KEYS = {
    "charter_sha256", "default_branch", "required_non_goals", "remote_head", "source_head",
}
PROMOTED_AUTHORITY_KEYS = LEGACY_AUTHORITY_KEYS | {"schema", "remote_relationship"}
TERMINAL_GOAL_STATUSES = {"achieved", "blocked", "abandoned", "budget-exhausted"}
HEAD_ONLY = {
    "priority-set", "reprioritize", "portfolio-add", "register", "policy-set", "spending",
    "credentials", "deploy", "merge", "pause", "resume", "model-policy", "control-plane",
}
MAX_PILOT_JSON_BYTES = 256 * 1024


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _parse_json_arg(raw: str) -> object:
    if len(raw.encode("utf-8")) > MAX_PILOT_JSON_BYTES:
        raise ValueError("pilot JSON argument exceeds the 256 KiB bound")
    return json.loads(raw, parse_constant=_reject_json_constant)


def _json_copy(value: object) -> object:
    try:
        return json.loads(json.dumps(value, allow_nan=False),
                          parse_constant=_reject_json_constant)
    except (TypeError, ValueError, OverflowError, RecursionError):
        fail("input is not canonical JSON")


def root() -> Path:
    return Path(os.environ.get("RECURSION_ROOT") or Path(__file__).resolve().parent.parent)


def fail(message: str, code: int = 4) -> None:
    # Caller-facing diagnostics are deliberately path-free.  State paths are controller details,
    # and raw OS exceptions can disclose both those paths and host-specific ACL information.
    safe = re.sub(r"(?:[A-Za-z]:[\\/]|\\\\|(?<![A-Za-z0-9_])/)[^\s,;)]*", "<path>", str(message))
    print(f"project-pilot: {safe}", file=sys.stderr)
    raise SystemExit(code)


def compact(value: object) -> None:
    try:
        print(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        fail("output is not canonical JSON")


def read_json(path: Path, unavailable_code: int = 4,
              malformed_code: int | None = None) -> dict:
    invalid_code = unavailable_code if malformed_code is None else malformed_code
    try:
        with path.open("rb") as source:
            raw_bytes = source.read(MAX_PILOT_JSON_BYTES + 1)
        if len(raw_bytes) > MAX_PILOT_JSON_BYTES:
            fail("state record exceeds the 256 KiB read bound", invalid_code)
        raw = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError, TypeError, OverflowError,
            RecursionError) as exc:
        fail("cannot read validated state", unavailable_code)
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        fail("cannot read validated state", invalid_code)
    if not isinstance(value, dict):
        fail("state record is not an object", invalid_code)
    return value


def read_required_json(path: Path, absent: str, *, unavailable_code: int = 4) -> dict:
    """Keep proved absence/malformed bytes distinct from transient read unavailability."""
    try:
        path.lstat()
    except FileNotFoundError:
        fail(absent)
    except (OSError, UnicodeDecodeError, ValueError, TypeError, OverflowError,
            RecursionError) as exc:
        fail("cannot inspect validated state", unavailable_code)
    return read_json(path, unavailable_code=unavailable_code, malformed_code=4)


def _is_reparse_info(info: os.stat_result) -> bool:
    """Detect POSIX links and Windows reparse points, including junctions."""
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_reparse_tag", 0)) \
        or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _reject_status_reparse_ancestors(directory: Path) -> None:
    """Reject links/reparse points in the whole status path before any directory walk."""
    current = Path(directory.anchor) if directory.anchor else Path()
    components = directory.parts[1:] if directory.anchor else directory.parts
    for index, component in enumerate(components):
        current /= component
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError:
            fail("cannot inspect status state", 74)
        if _is_reparse_info(info):
            fail("status state contains a symlink/reparse ancestor", 74)
        if index < len(components) - 1 and not stat.S_ISDIR(info.st_mode):
            fail("status state is not a directory", 74)


def fields(path: Path, unavailable_code: int | None = None) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        if path.stat().st_size > 131072:
            fail("state record exceeds the 128 KiB read bound", unavailable_code or 4)
        for line in path.read_text(encoding="utf-8", errors="surrogateescape").splitlines():
            if ":" in line and not line.startswith((" ", "\t")):
                key, value = line.split(":", 1)
                result.setdefault(key.strip(), value.strip())
    except (OSError, UnicodeDecodeError, ValueError, TypeError, OverflowError,
            RecursionError) as exc:
        if unavailable_code is not None:
            fail("cannot read status state", unavailable_code)
    return result


def status_files(directory: Path, suffix: str | None, limit: int | None,
                 newest: bool = False) -> list[Path]:
    """List one status source, distinguishing a lazy empty directory from unreadable state."""
    _reject_status_reparse_ancestors(directory)
    try:
        directory_info = os.lstat(directory)
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, ValueError, TypeError, OverflowError,
            RecursionError) as exc:
        fail("cannot inspect status state", 74)
    if _is_reparse_info(directory_info) or not stat.S_ISDIR(directory_info.st_mode):
        fail("status state is not a real directory", 74)
    try:
        with os.scandir(directory) as entries:
            paths = []
            for entry in entries:
                if suffix is not None and not entry.name.endswith(suffix):
                    continue
                try:
                    entry_info = entry.stat(follow_symlinks=False)
                except AttributeError:
                    entry_info = None
                except (OSError, UnicodeDecodeError, ValueError, TypeError, OverflowError,
                        RecursionError):
                    fail("cannot inspect status state record", 74)
                if entry.is_symlink() or (entry_info is not None and _is_reparse_info(entry_info)):
                    fail("status state record is a symlink/reparse point", 74)
                if entry.is_file(follow_symlinks=False):
                    paths.append(directory / entry.name)
                elif not entry.is_dir(follow_symlinks=False):
                    fail("status state record has an unsupported type", 74)
    except FileNotFoundError:
        # Runtime directories are created lazily; disappearance before the scan proves no records
        # were readable at this instant and must not be conflated with a permission failure.
        return []
    except (OSError, UnicodeDecodeError, ValueError, TypeError, OverflowError,
            RecursionError) as exc:
        fail("cannot enumerate status state", 74)
    ordered = sorted(paths)
    if limit is None:
        return ordered
    return ordered[-limit:] if newest else ordered[:limit]


def rows(base: Path, unavailable_code: int = 4) -> list[dict[str, str]]:
    names = ("id", "path", "remote", "branch", "key", "roadmap", "topic", "status")
    result = []
    registry = base / "state/portfolio.tsv"
    try:
        lines = registry.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError, ValueError, TypeError, OverflowError,
            RecursionError) as exc:
        fail("portfolio unavailable", unavailable_code)
    for line in lines:
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < 8:
            fail(f"short portfolio row for {cols[0] if cols else '?'}")
        result.append(dict(zip(names, cols[:8])))
    return result


def row_for(base: Path, project: str, unavailable_code: int = 4) -> dict[str, str]:
    matches = [row for row in rows(base, unavailable_code) if row["id"] == project]
    if len(matches) != 1:
        fail(f"project {project!r} is not uniquely registered")
    return matches[0]


def records(base: Path, project: str, active: bool = False,
            unavailable_code: int = 4) -> tuple[dict, dict, dict[str, str]]:
    row = row_for(base, project, unavailable_code)
    cfg = read_required_json(
        base / "state/project-config" / f"{project}.json",
        f"project configuration is absent for {project}", unavailable_code=unavailable_code,
    )
    pilot_id = cfg.get("pilot_id")
    if not isinstance(pilot_id, str) or not IDENT.fullmatch(pilot_id):
        fail(f"project {project} has no canonical pilot id")
    pilot = read_required_json(
        base / "state/pilots" / f"{pilot_id}.json",
        f"pilot record is absent for {project}", unavailable_code=unavailable_code,
    )
    if cfg.get("id") != project or pilot.get("project_id") != project or pilot.get("id") != pilot_id:
        fail(f"project/pilot identity mismatch for {project}")
    if str(cfg.get("topic")) != row["topic"] or str(pilot.get("topic")) != row["topic"]:
        fail(f"topic projection mismatch for {project}")
    if cfg.get("charter") != pilot.get("charter"):
        fail(f"project/pilot charter projection mismatch for {project}")
    if active and (row["status"] != "active" or cfg.get("status") != "active" or pilot.get("status") != "active"):
        fail(f"project pilot {project} is not active")
    if "required_non_goals" in cfg or "required_non_goals" in pilot:
        if cfg.get("required_non_goals") != pilot.get("required_non_goals"):
            fail(f"project/pilot required_non_goals projection mismatch for {project}")
    if "required_gate_profile" in cfg or "required_gate_profile" in pilot:
        if cfg.get("required_gate_profile") != pilot.get("required_gate_profile"):
            fail(f"project/pilot required_gate_profile projection mismatch for {project}")
    return cfg, pilot, row


def valid_allowed_path(scope: str) -> bool:
    """A materializable charter path: relative, no dot/dotdot, no delimiters or control bytes.

    The value is later joined with commas and re-split on comma/whitespace before it reaches the
    shell and the Git scope gate, so any byte that could become a second path or a shell operator
    must be refused before it is written.
    """
    if not isinstance(scope, str) or not scope:
        return False
    if any(ord(ch) < 32 for ch in scope):
        return False
    if scope in {".", ".."} or scope.startswith(("/", "\\")):
        return False
    if "\\" in scope:
        return False
    parts = scope.split("/")
    return all(part and part != ".." and ALLOWED_PATH_COMPONENT.fullmatch(part) for part in parts)


def scope_allowed(cfg: dict, scope: str) -> bool:
    if not valid_allowed_path(scope):
        return False
    allowed = cfg.get("allowed_paths")
    if not isinstance(allowed, list) or not allowed:
        return False
    normalized = scope.replace("\\", "/").strip("/")
    for prefix in allowed:
        if not isinstance(prefix, str):
            continue
        prefix = prefix.replace("\\", "/").strip("/")
        if prefix == "." or normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


def scoped_state(base: Path, project: str) -> dict:
    cfg, pilot, _ = records(base, project, active=True)
    goals = []
    for path in status_files(base / "state/goals", ".md", 100):
        value = fields(path, unavailable_code=74)
        if value.get("project_id") == project:
            goals.append({**value, "file": str(path.relative_to(base))})
    goal_ids = {g.get("id") for g in goals}
    tasks = []
    for path in status_files(base / "state/tasks", ".md", 500):
        value = fields(path, unavailable_code=74)
        if value.get("project_id") == project or value.get("goal_id") in goal_ids:
            tasks.append({**value, "file": str(path.relative_to(base))})
    task_ids = {t.get("id") for t in tasks}
    runs = []
    for path in status_files(base / "state/runs", ".json", 500):
        value = read_json(path, unavailable_code=74)
        if value.get("project_id") == project or value.get("task_id") in task_ids:
            value = dict(value)
            try:
                value["remote_events"] = read_events_for_run(path)
            except RemoteEvidenceError as exc:
                fail("remote evidence is unusable", 74)
            value["file"] = str(path.relative_to(base)); runs.append(value)
    evidence = []
    for path in status_files(base / "state/evidence", None, 100):
        value = (fields(path, unavailable_code=74) if path.suffix == ".md"
                 else read_json(path, unavailable_code=74))
        if value.get("project_id") == project or value.get("task_id") in task_ids:
            evidence.append({"file": str(path.relative_to(base)), "id": value.get("id")})
    plans = []
    for path in status_files(base / "state/plans" / project, ".json", 100):
        value = read_json(path, unavailable_code=74)
        value = dict(value); value["file"] = str(path.relative_to(base)); plans.append(value)
    windows = []
    for path in status_files(base / "state/pilot-windows", ".json", 100):
        value = read_json(path, unavailable_code=74)
        if value.get("project_id") == project:
            value = dict(value); value["file"] = str(path.relative_to(base)); windows.append(value)
    assists = []
    assist_paths = {path.name: path for path in status_files(base / "state/assists", ".md", None)}
    for task_id in task_ids:
        path = assist_paths.get(f"{task_id}.md")
        if path is not None:
            assists.append({**fields(path, unavailable_code=74),
                            "file": str(path.relative_to(base))})
    needles = {project, *[str(item) for item in task_ids if item]}
    log_paths = status_files(base / "loop/logs", None, 100, newest=True)
    logs = [str(path.relative_to(base)) for path in log_paths
            if any(needle in path.name for needle in needles)][-20:]
    promotions = available_gate_profile_promotions(base, cfg)
    return {"project": cfg, "pilot": pilot, "gate_profile_promotions": promotions,
            "plans": plans, "goals": goals, "tasks": tasks,
            "runs": runs, "assists": assists, "pilot_windows": windows,
            "evidence": evidence, "logs": logs}


def git_evidence(cfg: dict) -> dict:
    repo = Path(str(cfg["repo"])); branch = str(cfg["default_branch"])
    def git(*args: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True,
                              timeout=timeout, check=False)
    if not (repo / ".git").exists():
        return {"verified": False, "error": "clone-absent", "repo": str(repo)}
    head = git("rev-parse", "HEAD").stdout.strip()
    current = git("branch", "--show-current").stdout.strip()
    changed = [line for line in git("status", "--porcelain", "--untracked-files=all").stdout.splitlines() if line]
    env = os.environ.copy(); key = Path(os.path.expanduser(str(cfg["deploy_key_ref"])))
    if str(cfg.get("remote", "")).startswith("git@"):
        env["GIT_SSH_COMMAND"] = f"ssh -i {key} -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15"
    remote = subprocess.run(["git", "ls-remote", str(cfg.get("remote") or "origin"), f"refs/heads/{branch}"],
                            cwd=str(repo), text=True, capture_output=True, timeout=20,
                            check=False, env=env)
    remote_head = remote.stdout.split()[0] if remote.returncode == 0 and remote.stdout.split() else None
    return {"verified": bool(head and current), "repo": str(repo), "branch": current, "head": head,
            "default_branch": branch, "remote_head": remote_head, "fresh": remote_head == head,
            "dirty": bool(changed), "changed_paths": changed[:20]}


def repo_read(base: Path, project: str, relative: str | None) -> dict:
    cfg, _, _ = records(base, project, active=True); repo = Path(str(cfg["repo"]))
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, check=False)
    if tracked.returncode:
        fail("project clone or tracked-file index is unavailable")
    files = [item.decode("utf-8", "surrogateescape") for item in tracked.stdout.split(b"\0") if item]
    files = [item for item in files if scope_allowed(cfg, item) and not any(
        part == ".env" or part.startswith(".env.") for part in Path(item).parts)]
    if relative is None:
        return {"source": "live-git", "project_id": project, "files": files[:200], "truncated": len(files) > 200}
    relative = relative.replace("\\", "/").strip("/")
    if relative not in files:
        fail("repo read is untracked, secret-shaped, or outside the charter")
    path = repo / relative
    if not path.is_file() or path.stat().st_size > 65536:
        fail("repo read exceeds the 64 KiB text bound")
    raw = path.read_bytes()
    if b"\0" in raw:
        fail("repo read refuses binary content")
    return {"source": "live-git", "project_id": project, "path": relative,
            "head": subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
                                   capture_output=True, check=False).stdout.strip(),
            "content": raw.decode("utf-8", "replace")}


def artifact_read(base: Path, project: str, kind: str, relative: str | None) -> dict:
    state = scoped_state(base, project)
    allowed = state["logs"] if kind == "logs" else [row["file"] for row in state["evidence"]]
    if relative is None:
        return {"source": "live-control-state", "project_id": project, kind: allowed}
    relative = relative.replace("\\", "/").strip("/")
    if relative not in allowed:
        fail(f"{kind} read is outside this project")
    path = base / relative; raw = path.read_bytes()[-65536:]
    if b"\0" in raw: fail(f"{kind} read refuses binary content")
    lines = raw.decode("utf-8", "replace").splitlines()[-200:]
    return {"source": "live-control-state", "project_id": project, "path": relative,
            "content": "\n".join(lines), "truncated": path.stat().st_size > len(raw) or len(lines) == 200}


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(text); out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
        if os.name != "nt":
            dfd = os.open(path.parent, os.O_RDONLY); os.fsync(dfd); os.close(dfd)
    except BaseException:
        try: os.unlink(name)
        except FileNotFoundError: pass
        raise


def atomic_json(path: Path, value: dict) -> None:
    atomic_text(path, json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def canonical_digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def validate_control_state(base: Path, kind: str, value: dict) -> None:
    proc = subprocess.run([sys.executable, str(base / "scripts/validate-control-state.py"), kind],
                          input=json.dumps(value), text=True, capture_output=True, check=False)
    if proc.returncode:
        fail("control-state validation failed")


def append_event(base: Path, event: str, project: str, goal: str = "", task: str = "",
                 detail: object = "", event_id: str = "") -> None:
    value = {"schema_version": 1, "ts": now_iso(), "event": event, "project": project,
             "goal": goal, "task": task, "pid": os.getpid(), "detail": detail}
    if event_id:
        value["id"] = event_id
    try:
        commit_event(base, value)
    except (OSError, TransitionReceiptError):
        fail("event journal append failed", 74)


def transition_event(event: str, project: str, goal: str = "", task: str = "",
                     detail: object = "", event_id: str = "") -> dict:
    value = {"schema_version": 1, "ts": now_iso(), "event": event, "project": project,
             "goal": goal, "task": task, "pid": os.getpid(), "detail": detail}
    if event_id:
        value["id"] = event_id
    return value


def commit_pilot_transition(base: Path, mutations: list, event: dict,
                            authoritative_index: int = -1, precondition=None) -> None:
    try:
        commit_transition(base, mutations, event, authoritative_index=authoritative_index,
                          precondition=precondition)
    except (OSError, TransitionReceiptError):
        fail("crash-consistent pilot transition failed", 74)


@contextmanager
def event_serialization_lock(base: Path):
    """Share taskctl's mkdir mutex and never append through runtime-state rotation."""
    lock_dir = base / "state/.locks"; lock_dir.mkdir(parents=True, exist_ok=True)
    runtime_lock = lock_dir / "runtime-state.lock"; path = lock_dir / "taskctl.lock"
    try:
        raw_attempts = os.environ.get("RECURSION_LOCK_ATTEMPTS", "100")
        if len(raw_attempts) > 6:
            raise ValueError("lock attempt bound is too long")
        attempts = max(1, int(raw_attempts))
    except (TypeError, ValueError, OverflowError):
        attempts = 100
    try:
        raw_delay = os.environ.get("RECURSION_LOCK_DELAY_S", "0.1")
        if len(raw_delay) > 32:
            raise ValueError("lock delay bound is too long")
        delay = max(0.0, float(raw_delay))
    except (TypeError, ValueError, OverflowError):
        delay = 0.1
    held = False
    try:
        for _ in range(attempts):
            if not runtime_lock.is_dir():
                try:
                    path.mkdir(); held = True
                    (path / "pid").write_text(f"{os.getpid()}\n", encoding="ascii")
                    if runtime_lock.is_dir():
                        fail("runtime-state rotation acquired during event lock", 73)
                    break
                except FileExistsError:
                    pass
            time.sleep(delay)
        if not held:
            fail("event journal lock is contended; refusing an unlocked append", 73)
        yield
    finally:
        if held:
            (path / "start").unlink(missing_ok=True); (path / "pid").unlink(missing_ok=True)
            try: path.rmdir()
            except OSError: pass


@contextmanager
def registry_lock(base: Path):
    lock_dir = base / "state/.locks"; lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / "project-registry.lock").open("a+", encoding="utf-8") as lock:
        if fcntl is not None:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: fail("project registry lock is contended", 73)
        yield


@contextmanager
def goal_state_lock(base: Path, goal_id: str):
    """Serialize Phase 4 task creation with Phase 6 goal verification/transitions."""
    lock_dir = base / "state/.locks"; lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / f"reconcile-{goal_id}.lock").open("a+", encoding="utf-8") as lock:
        if fcntl is not None:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: fail("goal state lock is contended", 73)
        yield


@contextmanager
def phase8_lock(base: Path, identity: str, *, contended_code: int = 73):
    lock_dir = base / "state/.locks"; lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / f"phase8-{identity}.lock").open("a+", encoding="utf-8") as lock:
        if fcntl is not None:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: fail("Phase 8 state lock is contended", contended_code)
        yield


def verify_registry(base: Path) -> None:
    seen: dict[str, set[str]] = {key: set() for key in ("topic", "pilot", "memory", "worktree")}
    for row in rows(base):
        if row["status"] != "active":
            continue
        cfg, pilot, _ = records(base, row["id"], active=True)
        required = {"charter": cfg.get("charter"), "gate": cfg.get("activation_gate"),
                    "model": pilot.get("model_policy"), "budget": pilot.get("budget"),
                    "notifications": pilot.get("notification_targets"), "goal_source": pilot.get("goal_source")}
        if any(value in (None, "", [], {}) for value in required.values()):
            fail(f"active project {row['id']} lacks Phase 4 metadata")
        values = {"topic": str(cfg["topic"]), "pilot": str(pilot["id"]),
                  "memory": str(pilot.get("memory_key", "")), "worktree": str(pilot.get("worktree_root", ""))}
        if not values["topic"].isdigit() or int(values["topic"]) < 1:
            fail(f"active project {row['id']} has no Telegram topic")
        for kind, value in values.items():
            if not value or value in seen[kind]: fail(f"active projects share {kind} {value!r}")
            seen[kind].add(value)


def prepare(base: Path, project: str, remote: str, charter: str) -> None:
    if not IDENT.fullmatch(project) or not charter.strip(): fail("pending registration needs a project id and charter", 2)
    if not (remote.startswith("git@github.com:") or remote.startswith("https://github.com/")):
        fail("pending registration remote must be GitHub", 2)
    event = transition_event(
        "project-prepared", project,
        detail={"remote": remote, "charter_sha256": hashlib.sha256(charter.encode()).hexdigest()},
        event_id="source-project-prepared-" + canonical_digest([project, remote, charter])[:24],
    )
    with registry_lock(base):
        try:
            if resume_transition(base, event):
                return
        except (OSError, TransitionReceiptError):
            fail("pending registration recovery failed", 74)
        if any(row["id"] == project for row in rows(base)): fail(f"project {project} already exists", 2)
        repo = f"/opt/{project}"; key = f"~/.ssh/{project}_deploy"; pilot_id = f"pilot-{project}"
        line = f"{project}\t{repo}\t{remote}\tmain\t{key}\t-\t-\tpending\n"
        reg = base / "state/portfolio.tsv"; registry_text = reg.read_text(encoding="utf-8") + line
        targets = ["head-hermes", "lucas-direct"]
        required_non_goals = ["credentials", "merge", "deploy"]
        cfg = {"schema_version": 1, "id": project, "repo": repo, "remote": remote,
               "default_branch": "main", "deploy_key_ref": key, "charter": charter,
               "allowed_paths": ["."],
               "required_non_goals": required_non_goals,
               "topic": "-", "pilot_id": pilot_id,
               "activation_gate": "clone-auth-freshness-v1", "notification_targets": targets,
               "goal_source": "state/goals", "status": "pending"}
        pilot = {"schema_version": 1, "id": pilot_id, "project_id": project, "topic": "-",
                 "charter": charter, "model_policy": "project-pilot", "concurrency_limit": 0,
                 "budget": {"max_inflight_workers": 0, "max_decisions_per_turn": 1},
                 "required_non_goals": required_non_goals,
                 "notification_targets": targets, "goal_source": "state/goals",
                 "memory_key": f"telegram-topic:pending:{project}",
                 "worktree_root": f".worktrees/{project}", "status": "pending"}
        commit_pilot_transition(
            base,
            [raw_text_mutation(reg, registry_text),
             json_mutation(base / "state/pilots" / f"{pilot_id}.json", pilot),
             json_mutation(base / "state/project-config" / f"{project}.json", cfg)],
            event,
            authoritative_index=2,
        )


def bind_topic(base: Path, project: str, topic: str) -> None:
    if not topic.isdigit() or int(topic) < 1:
        fail("topic id must be a positive Telegram thread id", 2)
    event = transition_event(
        "project-topic-bound", project, detail={"topic": topic},
        event_id="source-project-topic-bound-" + canonical_digest([project, topic])[:24],
    )
    with registry_lock(base):
        try:
            if resume_transition(base, event):
                return
        except (OSError, TransitionReceiptError):
            fail("project topic recovery failed", 74)
        cfg, pilot, row = records(base, project)
        if row["status"] != "pending": fail("topic binding is allowed only while pending", 2)
        if any(other["topic"] == topic and other["id"] != project for other in rows(base)):
            fail(f"topic {topic} already belongs to another project")
        targets = [f"project-topic:{topic}"] + [item for item in cfg.get("notification_targets", [])
                                                if not str(item).startswith("project-topic:")]
        cfg = dict(cfg); cfg.update(topic=int(topic), notification_targets=targets)
        pilot = dict(pilot); pilot.update(topic=int(topic), memory_key=f"telegram-topic:{topic}",
                                          notification_targets=targets)
        lines = (base / "state/portfolio.tsv").read_text(encoding="utf-8").splitlines()
        changed = 0
        for index, line in enumerate(lines):
            cols = line.split("\t")
            if len(cols) >= 8 and cols[0] == project and cols[7] == "pending":
                cols[6] = topic; lines[index] = "\t".join(cols); changed += 1
        if changed != 1: fail("pending topic compare-and-swap failed", 73)
        commit_pilot_transition(
            base,
            [json_mutation(base / "state/pilots" / f"{pilot['id']}.json", pilot),
             raw_text_mutation(base / "state/portfolio.tsv", "\n".join(lines) + "\n"),
             json_mutation(base / "state/project-config" / f"{project}.json", cfg)],
            event,
            authoritative_index=2,
        )


def activate(base: Path, project: str) -> dict:
    event = transition_event(
        "project-activated", project, detail={"gate": "clone-auth-freshness-v1"},
        event_id="source-project-activated-" + canonical_digest([project, "clone-auth-freshness-v1"])[:24],
    )
    with registry_lock(base):
        try:
            if resume_transition(base, event):
                cfg = read_json(base / "state/project-config" / f"{project}.json")
                proof = cfg.get("activation_proof")
                if not isinstance(proof, dict):
                    fail("recovered activation lacks its exact proof", 74)
                return proof
        except (OSError, TransitionReceiptError):
            fail("project activation recovery failed", 74)
        cfg, pilot, row = records(base, project)
        if row["status"] != "pending": fail(f"project {project} is not pending", 2)
        if not str(cfg.get("charter", "")).strip(): fail("project charter is absent")
        if cfg.get("activation_gate") != "clone-auth-freshness-v1": fail("activation gate is absent or unknown")
        if not row["topic"].isdigit() or int(row["topic"]) < 1:
            fail("project topic is not bound")
        if not pilot.get("model_policy") or not pilot.get("goal_source") or not pilot.get("budget"):
            fail("pilot metadata is incomplete")
        key = Path(os.path.expanduser(row["key"])); remote = row["remote"]; branch = row["branch"]
        if not key.is_file(): fail(f"deploy key is absent: {row['key']}")
        env = os.environ.copy()
        if remote.startswith("git@"):
            env["GIT_SSH_COMMAND"] = f"ssh -i {key} -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15"
        probe = subprocess.run(
            ["git", "ls-remote", remote, f"refs/heads/{branch}"], cwd=str(base),
            text=True, capture_output=True, timeout=25, check=False, env=env,
        )
        if probe.returncode or not probe.stdout.split(): fail("deploy-key authentication or remote branch proof failed")
        remote_head = probe.stdout.split()[0]; repo = Path(row["path"])
        if not (repo / ".git").exists():
            repo.parent.mkdir(parents=True, exist_ok=True)
            clone = subprocess.run(["git", "clone", "-q", "--branch", branch, "--single-branch", remote, str(repo)],
                                   text=True, capture_output=True, timeout=120, check=False, env=env)
            if clone.returncode: fail("clone failed under the provisioned credential")
        origin = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], text=True,
                                capture_output=True, check=False).stdout.strip()
        current = subprocess.run(["git", "-C", str(repo), "branch", "--show-current"], text=True,
                                 capture_output=True, check=False).stdout.strip()
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
                              capture_output=True, check=False).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
                               text=True, capture_output=True, check=False).stdout.strip()
        if origin != remote or current != branch or head != remote_head or dirty:
            fail("clone is stale, dirty, on the wrong branch, or points at the wrong remote")
        proof = {"verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "clone_head": head, "remote_head": remote_head, "branch": branch,
                 "credential": "deploy-key", "gate": "clone-auth-freshness-v1"}
        cfg = dict(cfg); cfg["activation_proof"] = proof; cfg["status"] = "active"
        pilot = dict(pilot); pilot["status"] = "active"
        lines = (base / "state/portfolio.tsv").read_text(encoding="utf-8").splitlines()
        changed = 0
        for index, line in enumerate(lines):
            cols = line.split("\t")
            if len(cols) >= 8 and cols[0] == project and cols[7] == "pending":
                cols[7] = "active"; lines[index] = "\t".join(cols); changed += 1
        if changed != 1: fail("pending registry compare-and-swap failed", 73)
        commit_pilot_transition(
            base,
            [json_mutation(base / "state/pilots" / f"{pilot['id']}.json", pilot),
             raw_text_mutation(base / "state/portfolio.tsv", "\n".join(lines) + "\n"),
             json_mutation(base / "state/project-config" / f"{project}.json", cfg)],
            event,
            authoritative_index=2,
        )
        return proof


def goal_for(base: Path, project: str, goal_id: str, *,
             allowed_statuses: frozenset[str] = frozenset({"approved", "active"}),
             unavailable_code: int | None = None) \
        -> tuple[Path, dict[str, str]]:
    paths = (
        status_files(base / "state/goals", ".md", None)
        if unavailable_code is not None else (base / "state/goals").glob("*.md")
    )
    for path in paths:
        value = fields(path, unavailable_code=unavailable_code)
        if value.get("id") == goal_id:
            if value.get("project_id") != project or value.get("status") not in allowed_statuses:
                fail("goal is not approved for this project")
            acceptance = value.get("acceptance_suite", "").strip()
            if not acceptance:
                fail("approved goal has no gate")
            if acceptance in {"true", ":", "/bin/true", "exit 0"}:
                fail("approved goal gate is structurally vacuous")
            return path, value
    fail(f"goal {goal_id} is absent")


def nonvacuous_gate(value: object, label: str) -> str:
    gate = str(value or "").strip()
    compact = re.sub(r"\s+", " ", gate)
    obvious = (
        r"(?:true|:|/bin/true|exit 0|echo(?: .*)?|printf(?: .*)?)",
        r"(?:test|\[)\s+1\s*(?:=|-eq)\s*1\s*\]?",
        r"(?:test|\[)\s+-(?:e|d)\s+/\s*\]?",
    )
    if not gate or "\n" in gate or any(re.fullmatch(pattern, compact) for pattern in obvious):
        fail(f"{label} acceptance gate is absent or structurally vacuous")
    return gate


def bounded_line(value: object, label: str, maximum: int = 2000) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        fail(f"{label} must be bounded one-line text")
    return text


def project_required_non_goals(cfg: dict) -> list[str]:
    """Return the structured project contract, with the legacy global floor."""
    legacy = ["credentials", "merge", "deploy"]
    configured = cfg.get("required_non_goals")
    if configured is None:
        # Compatibility for project records written before required_non_goals existed.
        return legacy
    if not isinstance(configured, list) or not configured \
            or not all(isinstance(item, str) for item in configured) \
            or len(configured) != len(set(configured)):
        fail("project required_non_goals must be a non-empty unique string array")
    for item in configured:
        bounded_line(item, "project required non-goal", 512)
    if not set(legacy) <= set(configured):
        fail("project required_non_goals omits a global control-plane non-goal")
    return list(configured)


def validate_required_gate_profile(profile: object, label: str) -> dict:
    """Validate one exact semantic gate profile without supplying defaults."""
    if profile is None:
        fail(f"{label} required_gate_profile is absent")
    required = {"acceptance", "gate_class", "gate_files", "controller_gate_files"}
    if not isinstance(profile, dict) or set(profile) != required:
        fail(f"{label} required_gate_profile is malformed")
    acceptance = nonvacuous_gate(profile.get("acceptance"), f"{label} required")
    if profile.get("gate_class") not in {"light", "heavy"}:
        fail(f"{label} required_gate_profile has an invalid gate_class")
    result = {"acceptance": acceptance, "gate_class": profile["gate_class"]}
    for name in ("gate_files", "controller_gate_files"):
        items = profile.get(name)
        if not isinstance(items, list) or len(items) != len(set(items)) \
                or not all(isinstance(item, str) for item in items):
            fail(f"{label} required_gate_profile has invalid {name}")
        for item in items:
            bounded_line(item, f"{label} required gate {name}", 512)
        result[name] = list(items)
    return result


def project_required_gate_profile(cfg: dict) -> dict | None:
    """Validate the historical project-wide semantic gate profile, when configured."""
    profile = cfg.get("required_gate_profile")
    if profile is None:
        return None
    return validate_required_gate_profile(profile, "project")


def controller_tracked_bytes(base: Path, relative: str, label: str) -> bytes:
    """Read committed controller bytes and reject path, index, or worktree substitution."""
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts \
            or pure.as_posix() != relative or "\\" in relative:
        fail(f"{label} path is not a confined controller path")
    path = base.joinpath(*pure.parts)
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        fail(f"{label} is unavailable")
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        fail(f"{label} is not a regular controller file")
    tracked = subprocess.run(
        ["git", "-C", str(base), "ls-files", "--error-unmatch", "--", relative],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    committed = subprocess.run(
        ["git", "-C", str(base), "show", f"HEAD:{relative}"],
        capture_output=True, check=False,
    )
    clean = subprocess.run(
        ["git", "-C", str(base), "diff", "--quiet", "HEAD", "--", relative],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if tracked.returncode or committed.returncode or clean.returncode:
        fail(f"{label} is untracked, absent from HEAD, or differs from committed bytes")
    return committed.stdout


def load_gate_profile_promotion(base: Path, cfg: dict, promotion_id: str) -> dict:
    """Resolve one explicit immutable promotion descriptor; no default or latest alias exists."""
    if not IDENT.fullmatch(promotion_id):
        fail("pilot plan gate-profile promotion id is invalid")
    binding_path = f"scripts/gates/promotions/{promotion_id}.json"
    raw = controller_tracked_bytes(base, binding_path, "gate-profile promotion binding")
    if len(raw) > 65536:
        fail("gate-profile promotion binding exceeds the 64 KiB bound")
    try:
        binding = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, OverflowError,
            RecursionError):
        fail("gate-profile promotion binding is malformed")
    expected_keys = {
        "schema", "promotion_id", "project_id", "source_head", "profile",
        "required_gate_profile",
    }
    if not isinstance(binding, dict) or set(binding) != expected_keys \
            or binding.get("schema") != GATE_PROFILE_PROMOTION_SCHEMA \
            or binding.get("promotion_id") != promotion_id \
            or binding.get("project_id") != cfg.get("id"):
        fail("gate-profile promotion binding identity or schema is invalid")
    source_head = str(binding.get("source_head") or "")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source_head):
        fail("gate-profile promotion source HEAD is invalid")

    profile_ref = binding.get("profile")
    if not isinstance(profile_ref, dict) or set(profile_ref) != {"profile_id", "path", "sha256"}:
        fail("gate-profile promotion profile reference is malformed")
    profile_id = str(profile_ref.get("profile_id") or "")
    profile_path = str(profile_ref.get("path") or "")
    profile_sha256 = str(profile_ref.get("sha256") or "")
    if not IDENT.fullmatch(profile_id) or not profile_path.startswith("scripts/gates/") \
            or profile_path.startswith("scripts/gates/promotions/") \
            or not re.fullmatch(r"[0-9a-f]{64}", profile_sha256):
        fail("gate-profile promotion profile identity, path, or digest is invalid")
    profile_raw = controller_tracked_bytes(base, profile_path, "promoted measured profile")
    if hashlib.sha256(profile_raw).hexdigest() != profile_sha256:
        fail("promoted measured profile bytes differ from the immutable binding")
    try:
        profile = json.loads(profile_raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, OverflowError,
            RecursionError):
        fail("promoted measured profile is malformed")
    worlds = profile.get("worlds") if isinstance(profile, dict) else None
    roles = [world.get("role") for world in worlds if isinstance(world, dict)] \
        if isinstance(worlds, list) else []
    target_world = (profile.get("target") or {}).get("world") \
        if isinstance(profile, dict) and isinstance(profile.get("target"), dict) else None
    measured_target = [world.get("world") for world in worlds or []
                       if isinstance(world, dict) and world.get("role") == "target"]
    if not isinstance(profile, dict) \
            or not isinstance(profile.get("schema"), str) or not profile["schema"] \
            or profile.get("profile_id") != profile_id \
            or profile.get("base_commit") != source_head \
            or roles.count("target") != 1 or "control" not in roles \
            or measured_target != [target_world]:
        fail("promoted profile lacks exact measured identity, target, controls, or source HEAD")

    controller_gate = profile.get("controller_gate")
    if not isinstance(controller_gate, dict):
        fail("promoted profile controller gate is absent")
    gate_script = str(controller_gate.get("path") or "")
    if not gate_script.startswith("scripts/") or not gate_script.endswith(".py"):
        fail("promoted profile controller-gate path is invalid")
    gate_raw = controller_tracked_bytes(base, gate_script, "promoted profile controller gate")
    if controller_gate.get("sha256") != hashlib.sha256(gate_raw).hexdigest():
        fail("promoted profile controller-gate identity differs from measured bytes")

    required = validate_required_gate_profile(
        binding.get("required_gate_profile"), "promotion",
    )
    expected_controller_files = [gate_script, profile_path, binding_path]
    expected_acceptance = (
        f"{(base / gate_script).resolve()} --repo . "
        f"--profile {(base / profile_path).resolve()}"
    )
    if required.get("gate_class") != "heavy" \
            or required.get("acceptance") != expected_acceptance \
            or required.get("controller_gate_files") != expected_controller_files:
        fail("promoted gate profile does not bind the exact measured profile and controller files")
    return {
        "selection": {
            "schema": GATE_PROFILE_SELECTION_SCHEMA,
            "promotion_id": promotion_id,
            "binding_path": binding_path,
            "binding_sha256": hashlib.sha256(raw).hexdigest(),
        },
        "source_head": source_head,
        "profile": profile,
        "profile_path": profile_path,
        "profile_sha256": profile_sha256,
        "required_gate_profile": required,
    }


def available_gate_profile_promotions(base: Path, cfg: dict) -> list[dict]:
    """Expose exact selectable identities to the pilot without inventing a moving default."""
    directory = base / "scripts/gates/promotions"
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        fail("gate-profile promotion inventory is not a real controller directory")
    repository = subprocess.run(
        ["git", "-C", str(base), "rev-parse", "--verify", "HEAD^{commit}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if repository.returncode:
        # Status-only and pre-repository fixtures have no committed promotion inventory.
        # Any explicit selection still enters load_gate_profile_promotion and fails closed.
        return []
    inventory = subprocess.run(
        [
            "git", "-C", str(base), "ls-tree", "-r", "--name-only", "-z", "HEAD", "--",
            "scripts/gates/promotions",
        ],
        capture_output=True, check=False,
    )
    if inventory.returncode:
        fail("gate-profile promotion inventory cannot be read from committed controller state")
    if not inventory.stdout:
        return []
    result = []
    for encoded in sorted(item for item in inventory.stdout.split(b"\0") if item):
        try:
            relative = encoded.decode("utf-8")
        except UnicodeDecodeError:
            continue
        pure = PurePosixPath(relative)
        if pure.parent.as_posix() != "scripts/gates/promotions" \
                or pure.suffix != ".json" or not IDENT.fullmatch(pure.stem):
            continue
        committed = subprocess.run(
            ["git", "-C", str(base), "show", f"HEAD:{relative}"],
            capture_output=True, check=False,
        )
        if committed.returncode or len(committed.stdout) > 65536:
            continue
        try:
            candidate = json.loads(committed.stdout.decode("utf-8"),
                                  parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError,
                OverflowError, RecursionError):
            continue
        # Inventory discovery must not couple one project's status to another project's
        # malformed or dirty descriptor. Exact selection remains strict in
        # load_gate_profile_promotion, including the current project's worktree bytes.
        if not isinstance(candidate, dict) or candidate.get("project_id") != cfg.get("id"):
            continue
        promotion = load_gate_profile_promotion(base, cfg, pure.stem)
        result.append({
            **promotion["selection"],
            "source_head": promotion["source_head"],
            "profile_path": promotion["profile_path"],
            "profile_sha256": promotion["profile_sha256"],
            "required_gate_profile": promotion["required_gate_profile"],
        })
    return result


def bind_gate_profile_selection(base: Path, cfg: dict, plan: dict) -> dict:
    """Replace a minimal explicit request with the controller-derived immutable binding."""
    selection = plan.get("gate_profile_selection")
    if selection is None:
        return plan
    if not isinstance(selection, dict) or set(selection) != {"schema", "promotion_id"} \
            or selection.get("schema") != GATE_PROFILE_SELECTION_SCHEMA:
        fail("pilot plan gate_profile_selection must be an exact versioned promotion request")
    promotion = load_gate_profile_promotion(base, cfg, str(selection.get("promotion_id") or ""))
    plan["gate_profile_selection"] = promotion["selection"]
    return plan


def selected_required_gate_profile(base: Path, cfg: dict, plan: dict) -> dict | None:
    """Resolve only an exact canonical selection; selector-less historical plans use the default."""
    selection = plan.get("gate_profile_selection")
    if selection is None:
        return project_required_gate_profile(cfg)
    if not isinstance(selection, dict) or set(selection) != {
        "schema", "promotion_id", "binding_path", "binding_sha256"
    } or selection.get("schema") != GATE_PROFILE_SELECTION_SCHEMA:
        fail("pilot plan gate_profile_selection is not a canonical immutable binding")
    promotion = load_gate_profile_promotion(base, cfg, str(selection.get("promotion_id") or ""))
    if selection != promotion["selection"]:
        fail("pilot plan gate-profile binding differs from the immutable promotion descriptor")
    authority = plan.get("authority_contract")
    if not isinstance(authority, dict) or authority.get("source_head") != promotion["source_head"]:
        fail("pilot plan promoted gate profile does not match its exact source HEAD")
    profile_seed = promotion["profile"].get("seed_contract")
    if profile_seed != plan.get("seed_contract"):
        fail("pilot plan promoted gate profile does not match its measured seed contract")
    return promotion["required_gate_profile"]


def enforce_required_gate_profile(cfg: dict, label: str, gate: dict,
                                  required: dict | None = None) -> None:
    if required is None:
        required = project_required_gate_profile(cfg)
    if required is None:
        return
    observed = {
        "acceptance": str(gate.get("acceptance") or "").strip(),
        "gate_class": gate.get("gate_class"),
        "gate_files": gate.get("gate_files"),
        "controller_gate_files": gate.get("controller_gate_files"),
    }
    if observed != required:
        fail(f"pilot plan {label} does not use the project's exact required gate profile")


def repo_gate_contract(base: Path, cfg: dict, label: str, gate: dict,
                       read_only_paths: list[str] | None = None) -> dict:
    """Use the execution validator and bind its tracked-file hashes into the plan."""
    command = [
        sys.executable, str(base / "scripts/validate-gate.py"),
        "--acceptance", str(gate["acceptance"]),
        "--allowed-paths", ",".join(gate["allowed_paths"]),
        "--gate-files", ",".join(gate["gate_files"]),
        "--repo", str(cfg["repo"]),
        "--controller-root", str(base),
        "--controller-gate-files", ",".join(gate.get("controller_gate_files") or []),
        "--strict-direct",
    ]
    if read_only_paths:
        command += ["--read-only-paths", ",".join(read_only_paths)]
    proc = subprocess.run(command, text=True, capture_output=True, check=False)
    if proc.returncode:
        detail = (proc.stderr or proc.stdout or "gate validation failed").strip()
        fail(f"pilot plan {label} has an invalid repository gate: {detail}")
    try:
        contract = json.loads(proc.stdout, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError, TypeError, OverflowError, RecursionError):
        fail(f"pilot plan {label} gate validator returned malformed evidence")
    hashes = contract.get("gate_files") if isinstance(contract, dict) else None
    controller_hashes = contract.get("controller_gate_files") if isinstance(contract, dict) else None
    core_keys = {"acceptance", "acceptance_sha256", "gate_files", "controller_gate_files"}
    contract_keys = set(contract) if isinstance(contract, dict) else set()
    if not isinstance(contract, dict) or not core_keys <= contract_keys \
            or not contract_keys <= core_keys | {"read_only_paths"} \
            or contract.get("acceptance") != gate["acceptance"] \
            or not re.fullmatch(r"[0-9a-f]{64}", str(contract.get("acceptance_sha256") or "")) \
            or not isinstance(hashes, dict) or set(hashes) != set(gate["gate_files"]) \
            or not isinstance(controller_hashes, dict) \
            or set(controller_hashes) != set(gate.get("controller_gate_files") or []) \
            or (read_only_paths and contract.get("read_only_paths") != read_only_paths) \
            or (not hashes and not controller_hashes) \
            or any(not re.fullmatch(r"[0-9a-f]{64}", str(digest))
                   for digest in [*hashes.values(), *controller_hashes.values()]):
        fail(f"pilot plan {label} gate validator returned incomplete evidence")
    if "read_only_paths" in contract:
        observed_read_only_paths = contract["read_only_paths"]
        if not isinstance(observed_read_only_paths, list) \
                or any(not isinstance(path, str) for path in observed_read_only_paths) \
                or len(observed_read_only_paths) != len(set(observed_read_only_paths)):
            fail(f"pilot plan {label} gate validator returned incomplete evidence")
    return contract


def _local_authority_fields(cfg: dict) -> tuple[Path, dict]:
    try:
        repo, branch, head = observe_clean_source(cfg)
    except AuthorityError as exc:
        fail(str(exc), exc.code)
    return repo, {
        "charter_sha256": hashlib.sha256(str(cfg.get("charter") or "").encode()).hexdigest(),
        "default_branch": branch,
        "required_non_goals": project_required_non_goals(cfg),
        "source_head": head,
    }


def _canonical_promoted_selection(plan: dict) -> bool:
    selection = plan.get("gate_profile_selection")
    return bool(
        isinstance(selection, dict)
        and set(selection) == {"schema", "promotion_id", "binding_path", "binding_sha256"}
        and selection.get("schema") == GATE_PROFILE_SELECTION_SCHEMA
        and IDENT.fullmatch(str(selection.get("promotion_id") or ""))
        and re.fullmatch(r"[0-9a-f]{64}", str(selection.get("binding_sha256") or ""))
    )


def live_authority_contract(cfg: dict, *, check_remote: bool) -> dict:
    """Preserve the historical selector-less exact-tip authority contract."""
    _, result = _local_authority_fields(cfg)
    if check_remote:
        try:
            remote_head = authenticated_remote_head(cfg, str(result["default_branch"]))
        except AuthorityError as exc:
            fail(str(exc), exc.code)
        if remote_head != result["source_head"]:
            fail("project default branch is not equal to its authenticated remote tip")
        result["remote_head"] = remote_head
    return result


def live_promoted_authority_contract(cfg: dict) -> dict:
    """Bind a promoted plan to exact source bytes plus authenticated monotonic lineage."""
    repo, result = _local_authority_fields(cfg)
    try:
        remote_head, relationship = prove_current_remote_lineage(
            cfg, str(result["source_head"]), observed_repo=repo,
        )
    except AuthorityError as exc:
        fail(str(exc), exc.code)
    return {
        "schema": AUTHORITY_V2_SCHEMA,
        **result,
        "remote_head": remote_head,
        "remote_relationship": relationship,
    }


def bind_authority_contract(cfg: dict, value: object) -> dict:
    if not isinstance(value, dict):
        fail("pilot plan must be an object")
    plan = _json_copy(value)
    selection = plan.get("gate_profile_selection")
    if selection is None:
        observed = live_authority_contract(cfg, check_remote=True)
    else:
        if not _canonical_promoted_selection(plan):
            fail("promoted authority requires an exact canonical gate-profile selection")
        observed = live_promoted_authority_contract(cfg)
    if plan.get("authority_contract") not in (None, observed):
        fail("model-authored authority contract disagrees with controller evidence")
    plan["authority_contract"] = observed
    return plan


def validate_authority_binding(cfg: dict, value: dict, *, check_remote: bool) -> dict:
    claimed = value.get("authority_contract")
    promoted = value.get("gate_profile_selection") is not None
    if not promoted:
        if not isinstance(claimed, dict) or set(claimed) != LEGACY_AUTHORITY_KEYS \
                or claimed.get("remote_head") != claimed.get("source_head"):
            fail("pilot plan authority contract is malformed")
        observed = live_authority_contract(cfg, check_remote=check_remote)
        if not check_remote:
            observed["remote_head"] = claimed.get("remote_head")
        if claimed != observed:
            fail("pilot plan authority, source HEAD, or structured scope is stale")
        return claimed

    if not _canonical_promoted_selection(value) or not isinstance(claimed, dict) \
            or set(claimed) != PROMOTED_AUTHORITY_KEYS \
            or claimed.get("schema") != AUTHORITY_V2_SCHEMA \
            or claimed.get("remote_relationship") not in AUTHORITY_V2_RELATIONSHIPS:
        fail("promoted pilot authority contract is malformed or unversioned")
    repo, local = _local_authority_fields(cfg)
    if {key: claimed.get(key) for key in local} != local:
        fail("promoted pilot authority source or charter is stale")
    source_head = str(claimed.get("source_head") or "")
    anchored_remote = str(claimed.get("remote_head") or "")
    if not OID.fullmatch(source_head) or not OID.fullmatch(anchored_remote) \
            or len(source_head) != len(anchored_remote):
        fail("promoted pilot authority contains an invalid commit identity")
    try:
        if check_remote:
            _, relationship = prove_current_remote_lineage(
                cfg, source_head, anchored_remote, observed_repo=repo,
            )
        else:
            relationship = (
                "equal" if source_head == anchored_remote else "remote-descends-from-source"
            )
    except AuthorityError as exc:
        fail(str(exc), exc.code)
    if claimed.get("remote_relationship") != relationship:
        fail("promoted pilot authority relationship is internally stale")
    return claimed


def requires_heavy_gate(plan: dict, acceptance: str) -> bool:
    markers = ("native-diff", "conformance", "node --test", "pytest", "cargo test")
    return bool(plan.get("seed_contract")) or any(marker in acceptance.lower() for marker in markers)


def require_provider_capability(base: Path, plan: dict, *, live: bool) -> None:
    if not any(task.get("network_policy") == "provider-only" for task in plan.get("tasks", [])):
        return
    helper = base / "scripts/provider-network.sh"
    if not helper.is_file():
        fail("provider-only capability controller is absent", 74)
    proc = subprocess.run(
        ["bash", str(helper), "probe" if live else "check"],
        env={**os.environ, "RECURSION_ROOT": str(base)}, text=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False,
    )
    if proc.returncode:
        fail("provider-only capability is not proven for this pilot window", 74)


def bind_seed_contract(cfg: dict, value: object) -> dict:
    """Add controller-observed seed identity before the plan receives its canonical digest."""
    if not isinstance(value, dict):
        fail("pilot plan must be an object")
    plan = _json_copy(value)
    try:
        observed = inspect_repository(Path(str(cfg["repo"])))
    except (OSError, SeedContractError):
        fail("project seed contract is invalid")
    claimed = plan.get("seed_contract")
    if observed is None:
        if claimed not in (None, {}):
            fail("pilot plan claims a seed contract but the tracked manifest is absent")
        plan.pop("seed_contract", None)
        return plan
    if claimed not in (None, observed):
        fail("model-authored seed identity disagrees with controller evidence")
    plan["seed_contract"] = observed
    return plan


def validate_seed_binding(cfg: dict, value: dict) -> dict | None:
    claimed = value.get("seed_contract")
    if claimed is None:
        try:
            observed = inspect_repository(Path(str(cfg["repo"])))
        except (OSError, SeedContractError):
            fail("project seed contract is invalid")
        if observed is not None:
            fail("pilot plan does not bind the tracked project seed manifest")
        return None
    if not isinstance(claimed, dict):
        fail("pilot plan seed contract is not an object")
    try:
        return validate_contract(Path(str(cfg["repo"])), claimed)
    except (OSError, SeedContractError):
        fail("pilot plan seed contract is stale or invalid")


def validate_plan(base: Path, cfg: dict, project: str, plan_id: str, value: object,
                  bind_gate_contract: bool = False, check_remote: bool = False) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("schema_version"), int) \
            or isinstance(value.get("schema_version"), bool) or value.get("schema_version") != 1:
        fail("pilot plan must be a schema_version 1 object")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded) > 65536:
            fail("pilot plan exceeds the 64 KiB bound")
        # Return a canonical value without mutating the model decision or stored record in memory.
        value = json.loads(encoded)
    except SystemExit:
        raise
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        fail(f"pilot plan contains malformed JSON values: {type(exc).__name__}")
    if value.get("id") != plan_id or value.get("project_id") != project or not IDENT.fullmatch(plan_id):
        fail("pilot plan identity does not match its decision")
    validate_authority_binding(cfg, value, check_remote=check_remote)
    seed_contract = validate_seed_binding(cfg, value)
    required_gate_profile = selected_required_gate_profile(base, cfg, value)
    goal = value.get("goal")
    tasks = value.get("tasks")
    if not isinstance(goal, dict) or not isinstance(tasks, list) or not 1 <= len(tasks) <= 16:
        fail("pilot plan needs one goal and 1..16 tasks")
    goal_id = str(goal.get("id") or "")
    if not IDENT.fullmatch(goal_id):
        fail("pilot plan goal identity, outcome, or falsifier is absent")
    bounded_line(goal.get("outcome"), "goal outcome"); bounded_line(goal.get("falsifier"), "goal falsifier")
    goal_acceptance = nonvacuous_gate(goal.get("acceptance_suite"), "goal")
    goal_required = {"gate_class", "gate_files", "controller_gate_files"}
    if any(key not in goal for key in goal_required) or goal.get("gate_class") not in {"light", "heavy"}:
        fail("pilot plan goal gate contract is incomplete")
    for name in ("gate_files", "controller_gate_files"):
        items = goal.get(name)
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items) \
                or len(items) != len(set(items)):
            fail(f"pilot plan goal has invalid {name}")
        for item in items: bounded_line(item, f"goal {name}", 512)
    goal_gate = {"acceptance": goal_acceptance,
                 "allowed_paths": list(cfg.get("allowed_paths") or []),
                 "gate_files": goal["gate_files"],
                 "controller_gate_files": goal["controller_gate_files"]}
    enforce_required_gate_profile(
        cfg, "goal", {**goal, "acceptance": goal_acceptance}, required_gate_profile,
    )
    # work.sh's effective read-only set is "$SEED $SEED_MANIFEST": it passes --read-only-paths twice
    # and argparse keeps the last one, which appends the manifest. The approved contract has to carry
    # the same set or every seeded pilot task is rejected pre-worker as gate-contract-drift, and
    # including it is right on its own terms -- a worker must not rewrite the manifest that defines
    # its own seed.
    read_only_paths = None
    if seed_contract:
        read_only_paths = [*seed_contract["paths"], seed_contract["manifest_path"]]
    expected_goal_contract = repo_gate_contract(
        base, cfg, "goal", goal_gate, read_only_paths=read_only_paths,
    )
    if requires_heavy_gate(value, goal_acceptance) and goal.get("gate_class") != "heavy":
        fail("pilot plan goal understates a controller-classified heavy gate")
    if bind_gate_contract:
        if goal.get("gate_contract") is not None:
            fail("pilot plan goal supplied controller-derived gate_contract")
        goal["gate_contract"] = expected_goal_contract
    elif goal.get("gate_contract") != expected_goal_contract:
        fail("pilot plan goal repository gate changed after proposal")
    budget = goal.get("budget")
    required_budget = {
        "max_concurrency", "max_tokens", "max_cost_usd", "max_wall_clock_seconds",
        "max_task_attempts", "max_failures", "max_review_queue", "max_publish_attempts",
        "publication_required",
    }
    if not isinstance(budget, dict) or not required_budget <= budget.keys():
        fail("pilot plan goal budget is incomplete")
    integer_budget = required_budget - {"max_cost_usd", "publication_required"}
    if any(isinstance(budget.get(key), bool) or not isinstance(budget.get(key), int)
           or int(budget[key]) < 0 for key in integer_budget):
        fail("pilot plan has an invalid integer budget")
    if budget.get("max_review_queue", 0) < 1:
        fail("pilot plan max_review_queue must be at least 1")
    cost = budget.get("max_cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0 \
            or not isinstance(budget.get("publication_required"), bool):
        fail("pilot plan has an invalid cost or publication budget")
    if "max_retries" in budget and (
            isinstance(budget["max_retries"], bool)
            or not isinstance(budget["max_retries"], int)
            or budget["max_retries"] < 0):
        fail("pilot plan has an invalid integer budget")
    task_ids: list[str] = []
    for task in tasks:
        if not isinstance(task, dict): fail("pilot plan task is not an object")
        task_id = str(task.get("id") or "")
        if not IDENT.fullmatch(task_id) or task_id in task_ids: fail("pilot plan task id is invalid or duplicate")
        task_ids.append(task_id)
    task_id_set = set(task_ids)
    edges: dict[str, list[str]] = {}
    for task in tasks:
        task_id = str(task["id"]); deps = task.get("dependencies")
        paths = task.get("allowed_paths"); non_goals = task.get("non_goals")
        bounded_line(task.get("intent"), f"task {task_id} intent")
        if not isinstance(deps, list) \
                or len(deps) != len(set(deps)) or any(dep not in task_id_set or dep == task_id for dep in deps):
            fail(f"pilot plan task {task_id} has invalid intent or dependencies")
        if not isinstance(paths, list) or not paths or any(not isinstance(path, str) or not scope_allowed(cfg, path)
                                                          for path in paths):
            fail(f"pilot plan task {task_id} escapes the project charter")
        for path in paths: bounded_line(path, f"task {task_id} allowed path", 512)
        if not isinstance(non_goals, list) or not all(isinstance(item, str) for item in non_goals) \
                or len(non_goals) != len(set(non_goals)):
            fail(f"pilot plan task {task_id} lacks required non-goals")
        for item in non_goals: bounded_line(item, f"task {task_id} non-goal", 512)
        task["non_goals"] = list(dict.fromkeys([*non_goals, *project_required_non_goals(cfg)]))
        nonvacuous_gate(task.get("acceptance"), f"task {task_id}")
        required = {
            "worker_class", "engine", "model_alias", "verifier_model_alias", "deadline_seconds",
            "verifier_deadline_seconds", "network_policy", "evidence_contract", "verification",
            "expected_change", "gate_class", "gate_files", "controller_gate_files",
        }
        if any(key not in task for key in required): fail(f"pilot plan task {task_id} is schema-incomplete")
        registry = default_registry_path(_SCRIPT_DIR)
        if not pilot_engine_ok(task.get("engine")) \
                or not alias_role_ok(task.get("model_alias"), "implementer", registry) \
                or not alias_role_ok(task.get("verifier_model_alias"), "verifier", registry) \
                or task.get("network_policy") not in {"provider-only", "none"} \
                or task.get("gate_class") not in {"light", "heavy"} \
                or task.get("evidence_contract") != "worker-result-v1" \
                or task.get("verification") != "independent" \
                or task.get("expected_change") not in {"required", "optional"}:
            fail(f"pilot plan task {task_id} has an invalid Phase 5 policy")
        if "verifier_engine" in task and not pilot_engine_ok(task.get("verifier_engine")):
            fail(f"pilot plan task {task_id} names an invalid verifier engine")
        if task.get("network_policy") == "none":
            # Every admissible engine is a networked worker (claude, or a provider-backed
            # plugin — the Sentry-local arm still needs its tunnel route).
            fail(f"pilot plan task {task_id} gives a cloud worker no provider network")
        if any(isinstance(task.get(key), bool) or not isinstance(task.get(key), int) or task[key] < 1
               for key in ("deadline_seconds", "verifier_deadline_seconds")):
            fail(f"pilot plan task {task_id} has an invalid deadline")
        for name in ("gate_files", "controller_gate_files"):
            items = task.get(name)
            if not isinstance(items, list) or not all(isinstance(item, str) for item in items) \
                    or len(items) != len(set(items)):
                fail(f"pilot plan task {task_id} has invalid {name}")
            for item in items: bounded_line(item, f"task {task_id} {name}", 512)
        enforce_required_gate_profile(cfg, f"task {task_id}", task, required_gate_profile)
        expected_contract = repo_gate_contract(
            base, cfg, f"task {task_id}", task, read_only_paths=read_only_paths,
        )
        if requires_heavy_gate(value, str(task["acceptance"])) and task.get("gate_class") != "heavy":
            fail(f"pilot plan task {task_id} understates a controller-classified heavy gate")
        supplied_contract = task.get("gate_contract")
        if bind_gate_contract:
            if supplied_contract is not None:
                fail(f"pilot plan task {task_id} supplied controller-derived gate_contract")
            task["gate_contract"] = expected_contract
        elif supplied_contract != expected_contract:
            fail(f"pilot plan task {task_id} repository gate changed after proposal")
        source_task_id = str(task.get("source_task_id") or "")
        if source_task_id:
            if not IDENT.fullmatch(source_task_id): fail(f"pilot plan task {task_id} has invalid source task")
            source_path = base / "state/tasks" / f"{source_task_id}.md"
            source = fields(source_path)
            source_goal_id = source.get("goal_id", "")
            try: source_goal = fields(goal_for(base, project, source_goal_id)[0])
            except SystemExit: raise
            if source.get("id") != source_task_id or source_goal.get("project_id") != project \
                    or source.get("status") not in {"proposed", "ready", "queued"}:
                fail(f"pilot plan source task {source_task_id} is absent, terminal, or cross-project")
            if task.get("acceptance") != source.get("acceptance"):
                fail(f"pilot plan task {task_id} changed its source task machine gate")
            contract = value.get("seed_contract") if isinstance(value.get("seed_contract"), dict) else None
            expected_seed = {
                "seed_manifest": contract["manifest_path"] if contract else "",
                "seed_manifest_sha256": contract["manifest_sha256"] if contract else "",
                "seed": ",".join(contract["paths"]) if contract else "",
                "seed_digest": contract["content_sha256"] if contract else "",
            }
            if any(source.get(key, "") != expected for key, expected in expected_seed.items()):
                fail(f"pilot plan task {task_id} changed its source task seed contract")
            source_paths = {item for item in source.get("allowed_paths", "").replace(",", " ").split() if item}
            if not set(paths) <= source_paths:
                fail(f"pilot plan task {task_id} broadens its source task write scope")
        edges[task_id] = list(deps)
    visiting: set[str] = set(); visited: set[str] = set()
    def walk(task_id: str) -> None:
        if task_id in visiting: fail("pilot plan dependency graph contains a cycle")
        if task_id in visited: return
        visiting.add(task_id)
        for dep in edges[task_id]: walk(dep)
        visiting.remove(task_id); visited.add(task_id)
    for task_id in task_ids: walk(task_id)
    if len(json.dumps(value, sort_keys=True, separators=(",", ":"))) > 65536:
        fail("canonical pilot plan exceeds the 64 KiB bound")
    return value


def instruction_path(base: Path, instruction_id: str) -> Path:
    name = f"{int(instruction_id):08d}.md" if instruction_id.isdigit() else f"{instruction_id}.md"
    return base / "state/instructions" / name


# A negated or reversed instruction must never authorize the opposite operation just because it
# still mentions the same ids/bounds. Reject any negating marker and any verb that names the
# opposite operation, in addition to requiring the exact canonical affirmative command.
def _instruction_body(path: Path) -> str:
    document = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    match = re.search(r"(?m)^## INSTRUCTION\s*$\n?", document)
    body = document[match.end():] if match else ""
    return re.split(r"(?m)^##\s+", body, maxsplit=1)[0]


def require_lucas_instruction(path: Path, instruction: dict[str, str], verb: str,
                              terms: list[str]) -> None:
    allowed = {item.strip() for item in os.environ.get("RECURSION_INTAKE_ALLOW", "8913126684").split(",")}
    if instruction.get("principal") not in allowed or instruction.get("routing_target") != "head-hermes":
        fail("Phase 8 approval is not an authenticated Lucas-to-Head-Hermes instruction")
    body = _instruction_body(path)
    # Sentence punctuation is not ambiguity, but mere set membership is: the shared grammar
    # requires the affirmative verb and every authority-bound value contiguously and in order.
    try:
        require_affirmative_command(body, verb, [str(term) for term in terms])
    except ValueError:
        fail("Lucas instruction is invalid")


def propose_plan(base: Path, cfg: dict, pilot: dict, decision: dict, instruction_id: str) -> dict:
    args = decision.get("args") or decision.get("arguments") or []
    plan_id = str(args[1]) if len(args) > 1 else ""
    raw_plan = decision.get("plan")
    if not isinstance(raw_plan, dict):
        fail("pilot plan must be an object")
    plan = _json_copy(raw_plan)
    plan = bind_gate_profile_selection(base, cfg, plan)
    plan = bind_authority_contract(cfg, plan)
    plan = bind_seed_contract(cfg, plan)
    plan = validate_plan(base, cfg, str(cfg["id"]), plan_id, plan,
                         bind_gate_contract=True, check_remote=True)
    digest = canonical_digest(plan); goal_id = str(plan["goal"]["id"])
    path = base / "state/plans" / str(cfg["id"]) / f"{plan_id}.json"
    event = transition_event(
        "pilot-plan-proposed", str(cfg["id"]), goal_id,
        detail={"plan_id": plan_id, "plan_digest": digest, "instruction_id": instruction_id},
        event_id="source-plan-proposed-" + canonical_digest(
            [cfg["id"], plan_id, digest, instruction_id])[:24],
    )
    with phase8_lock(base, plan_id):
        if path.exists():
            prior = read_json(path)
            if prior.get("plan_digest") != digest or prior.get("instruction_id") != instruction_id:
                fail("pilot plan id collides with different content or instruction")
            try:
                resumed = resume_transition(base, event)
            except (OSError, TransitionReceiptError):
                fail("pilot plan proposal recovery failed", 74)
            if not resumed:
                append_event(base, event["event"], event["project"], event["goal"],
                             detail=event["detail"], event_id=event["id"])
            return {"plan": str(path.relative_to(base)), "status": prior.get("status"), "plan_digest": digest}
        for goal_path in (base / "state/goals").glob("*.md"):
            if fields(goal_path).get("id") == goal_id: fail("fresh pilot plan reuses an existing goal id")
        existing_tasks = {fields(task_path).get("id") for task_path in (base / "state/tasks").glob("*.md")}
        if any(task.get("id") in existing_tasks for task in plan["tasks"]):
            fail("fresh pilot plan reuses an existing task id")
        record = {"schema_version": 1, "id": plan_id, "project_id": cfg["id"],
                  "pilot_id": pilot["id"], "instruction_id": instruction_id,
                  "plan_digest": digest, "plan": plan, "status": "proposed", "proposed_at": now_iso()}
        validate_control_state(base, "plan", record)
        commit_pilot_transition(base, [json_mutation(path, record)], event)
    return {"plan": str(path.relative_to(base)), "status": "proposed", "plan_digest": digest}


def task_values(cfg: dict, pilot: dict, plan_id: str, digest: str, goal_id: str, task: dict,
                authority: dict, seed_contract: dict | None = None,
                gate_profile_selection: dict | None = None) -> dict[str, str]:
    values = {
        "schema_version": "1", "version": "1", "id": task["id"], "project_id": cfg["id"],
        "project": cfg["id"], "workstream": cfg["id"], "pilot_id": pilot["id"], "goal_id": goal_id,
        "plan_id": plan_id, "plan_digest": digest, "dependencies": ",".join(task["dependencies"]),
        "goal": task["intent"], "allowed_paths": ",".join(task["allowed_paths"]),
        "non_goals": ",".join(task["non_goals"]), "worker_class": task["worker_class"],
        "engine": task["engine"], "model_alias": task["model_alias"],
        "verifier_model_alias": task["verifier_model_alias"], "deadline_seconds": str(task["deadline_seconds"]),
        "verifier_deadline_seconds": str(task["verifier_deadline_seconds"]),
        "network_policy": task["network_policy"], "evidence_contract": task["evidence_contract"],
        "verification": task["verification"], "expected_change": task["expected_change"],
        "gate_class": task["gate_class"],
        "gate_contract": json.dumps(task["gate_contract"], sort_keys=True, separators=(",", ":")),
        "gate_files": ",".join(task["gate_files"]), "acceptance": task["acceptance"],
        "controller_gate_files": ",".join(task["controller_gate_files"]),
        "source_head": authority["source_head"], "source_branch": authority["default_branch"],
        "charter_sha256": authority["charter_sha256"],
        "repo": str(cfg["repo"]), "workdir": ".", "autonomy": "auto", "status": "proposed",
    }
    if task.get("source_task_id"):
        values["source_task_id"] = task["source_task_id"]
    if task.get("verifier_engine"):
        values["verifier_engine"] = task["verifier_engine"]
    if seed_contract:
        values.update({
            "seed_manifest": seed_contract["manifest_path"],
            "seed_manifest_sha256": seed_contract["manifest_sha256"],
            "seed": ",".join(seed_contract["paths"]),
            "seed_digest": seed_contract["content_sha256"],
        })
    if gate_profile_selection:
        values["gate_profile_selection"] = json.dumps(
            gate_profile_selection, sort_keys=True, separators=(",", ":"),
        )
    return values


def task_text(cfg: dict, pilot: dict, plan_id: str, digest: str, goal_id: str, task: dict,
              authority: dict, seed_contract: dict | None = None,
              gate_profile_selection: dict | None = None) -> str:
    values = task_values(
        cfg, pilot, plan_id, digest, goal_id, task, authority, seed_contract,
        gate_profile_selection,
    )
    return "".join(f"{key}: {value}\n" for key, value in values.items()) + f"\n{task['intent']}\n"


def verify_approved_goal(base: Path, project: str, goal_id: str, *, terminal: bool = False,
                         check_remote: bool = False, allow_verifying: bool = False,
                         allow_paused: bool = False,
                         state_unavailable_code: int | None = None) -> dict:
    """Re-prove the digest-bound source, gates, seeds, and immutable DAG projection."""
    unavailable_code = (
        74 if check_remote else
        4 if state_unavailable_code is None else state_unavailable_code
    )
    cfg, pilot, _ = records(
        base, project, active=True, unavailable_code=unavailable_code,
    )
    # Terminal verification deliberately runs while the goal is `verifying`.  Keep the common
    # lookup's default narrower so task creation/window arming still cannot treat that closed
    # state as ordinary active authority.
    allowed_statuses = (
        frozenset({"verifying"}) if terminal else
        frozenset({"approved", "active", "verifying"}) if allow_verifying else
        frozenset({"approved", "active"})
    )
    if allow_paused:
        allowed_statuses |= {"paused"}
    goal_path, goal_fields = goal_for(
        base, project, goal_id,
        allowed_statuses=allowed_statuses,
        unavailable_code=unavailable_code,
    )
    plan_id = goal_fields.get("plan_id", "")
    if not plan_id:
        fail("goal is not materialized from a Phase 8 plan")
    proposal = read_required_json(
        base / "state/plans" / project / f"{plan_id}.json",
        f"goal plan record is absent for {plan_id}", unavailable_code=unavailable_code,
    )
    if proposal.get("status") != "approved":
        fail("goal plan is not approved")
    plan = validate_plan(
        base, cfg, project, plan_id, proposal.get("plan"), check_remote=check_remote,
    )
    digest = canonical_digest(plan)
    if proposal.get("plan_digest") != digest or goal_fields.get("plan_digest") != digest \
            or str(plan["goal"]["id"]) != goal_id:
        fail("approved plan/goal digest projection differs")
    authority = plan["authority_contract"]; goal = plan["goal"]
    gate_profile_selection = plan.get("gate_profile_selection")
    expected_goal = {
        "id": goal_id, "project_id": project, "pilot_id": str(pilot["id"]),
        "plan_id": plan_id, "plan_digest": digest, "outcome": str(goal["outcome"]),
        "acceptance_suite": str(goal["acceptance_suite"]), "falsifier": str(goal["falsifier"]),
        "gate_class": str(goal["gate_class"]), "gate_files": ",".join(goal["gate_files"]),
        "controller_gate_files": ",".join(goal["controller_gate_files"]),
        "gate_contract": json.dumps(goal["gate_contract"], sort_keys=True, separators=(",", ":")),
        "source_head": authority["source_head"], "source_branch": authority["default_branch"],
        "charter_sha256": authority["charter_sha256"],
        "budget": json.dumps(goal["budget"], sort_keys=True, separators=(",", ":")),
    }
    seed = plan.get("seed_contract")
    if seed:
        expected_goal.update({"seed_manifest": seed["manifest_path"],
                              "seed_manifest_sha256": seed["manifest_sha256"],
                              "seed": ",".join(seed["paths"]),
                              "seed_digest": seed["content_sha256"]})
    if gate_profile_selection:
        expected_goal["gate_profile_selection"] = json.dumps(
            gate_profile_selection, sort_keys=True, separators=(",", ":"),
        )
    if any(goal_fields.get(key, "") != str(value) for key, value in expected_goal.items()):
        fail("materialized goal immutable contract differs from its approved plan")
    actual_tasks = {}
    task_paths = status_files(base / "state/tasks", ".md", None)
    for path in task_paths:
        value = fields(path, unavailable_code=unavailable_code)
        if value.get("goal_id") == goal_id:
            actual_tasks[value.get("id", "")] = value
    planned_ids = {str(task["id"]) for task in plan["tasks"]}
    if set(actual_tasks) != planned_ids:
        fail("materialized task DAG differs from the exact approved plan")
    for task in plan["tasks"]:
        expected = task_values(
            cfg, pilot, plan_id, digest, goal_id, task, authority, seed,
            gate_profile_selection,
        )
        # status and version are lifecycle/CAS fields.  Both change through reviewed task
        # transitions; neither is part of the digest-bound implementation contract.
        expected.pop("status", None)
        expected.pop("version", None)
        actual = actual_tasks[str(task["id"])]
        if any(actual.get(key, "") != str(value) for key, value in expected.items()):
            fail(f"materialized task {task['id']} immutable contract differs from its approved plan")
        # The subset comparison cannot see an injected optional key (verifier_engine would
        # redirect which vendor verifies) — prove absence for the optional-key family too.
        for optional_key in ("verifier_engine", "source_task_id", "gate_profile_selection",
                             "seed_manifest", "seed_manifest_sha256", "seed", "seed_digest"):
            if optional_key not in expected and actual.get(optional_key, ""):
                fail(f"materialized task {task['id']} carries an unapproved {optional_key}")
    return {"project_id": project, "goal_id": goal_id, "plan_id": plan_id,
            "plan_digest": digest, "source_head": authority["source_head"],
            "authority_contract": authority,
            "task_ids": sorted(planned_ids),
            "provider_only_required": any(task.get("network_policy") == "provider-only"
                                           for task in plan["tasks"])}


def active_source_pin(base: Path, project: str) -> dict | None:
    """Return one armed/open window's controller-validated exact source pin."""
    windows: list[tuple[Path, dict]] = []
    for path in status_files(base / "state/pilot-windows", ".json", None):
        value = read_json(path, unavailable_code=74, malformed_code=4)
        if value.get("project_id") != project or value.get("status") not in {"armed", "open"}:
            continue
        if not isinstance(value.get("schema_version"), int) \
                or isinstance(value.get("schema_version"), bool) \
                or value.get("schema_version") != 1 \
                or not IDENT.fullmatch(str(value.get("id") or "")) \
                or path.stem != value.get("id") \
                or not IDENT.fullmatch(str(value.get("goal_id") or "")) \
                or not IDENT.fullmatch(str(value.get("pilot_id") or "")) \
                or not IDENT.fullmatch(str(value.get("plan_id") or "")):
            fail("source-pin window identity or schema is invalid")
        expires = value.get("expires_at_epoch")
        if isinstance(expires, bool) or not isinstance(expires, int) or expires < 1:
            fail("source-pin window has an invalid expiry")
        if expires <= int(time.time()):
            # Expiry revokes execution authority even if the scheduler has not yet persisted
            # the normally coupled close transition.  A stale record must not pin resync.
            continue
        windows.append((path, value))
    if not windows:
        return None
    if len(windows) != 1:
        fail("source pin requires exactly one armed/open project window")
    _, window = windows[0]; goal_id = str(window.get("goal_id") or "")
    _, goal_fields = goal_for(
        base, project, goal_id,
        allowed_statuses=frozenset(
            {"approved", "active", "verifying", "paused"} | TERMINAL_GOAL_STATUSES
        ),
        unavailable_code=74,
    )
    # A terminal goal cannot launch again. Its stale armed record must not hold the repository
    # forever while the controller's eventual close transition catches up. Authenticate the
    # exact mutation-backed terminal receipt first so a hand-edited status cannot release it.
    if goal_fields.get("status") in TERMINAL_GOAL_STATUSES:
        target = str(goal_fields.get("status") or "")
        expected = str(goal_fields.get("last_transition_from") or "")
        reason = str(goal_fields.get("last_transition_reason") or "")
        raw_version = str(goal_fields.get("version") or "")
        if not IDENT.fullmatch(expected) or not reason or not raw_version.isdigit():
            fail("terminal source-pin goal lacks canonical transition evidence")
        version = int(raw_version)
        if reason == "task-contract-invalid":
            task_id = str(goal_fields.get("last_transition_task") or "")
            category = str(goal_fields.get("last_transition_category") or "")
            digest = str(goal_fields.get("contract_digest") or "")
            if not IDENT.fullmatch(task_id) or not IDENT.fullmatch(category) \
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                fail("terminal source-pin contract rejection evidence is malformed")
            identity = (
                f"{project}\0{goal_id}\0{task_id}\0{category}\0{digest}"
                "\0pre-worker-contract-v2"
            ).encode()
            event_id = "source-task-contract-invalid-" + hashlib.sha256(identity).hexdigest()[:24]
            expected_event = "task-contract-invalid"
        else:
            identity = (
                f"{project}\0{goal_id}\0{expected}\0{target}\0{version}\0transition"
            ).encode()
            event_id = "source-goal-transition-" + hashlib.sha256(identity).hexdigest()[:24]
            expected_event = "goal-transition"
        try:
            receipt = authenticated_transition(base, event_id)
        except OSError:
            fail("terminal source-pin transition is unavailable", 74)
        except TransitionReceiptError:
            fail("terminal source-pin transition is not authenticated")
        event = receipt.get("event") if isinstance(receipt, dict) else None
        if not isinstance(event, dict) or event.get("event") != expected_event \
                or event.get("project") != project or event.get("goal") != goal_id:
            fail("terminal source-pin transition receipt has mismatched authority")
        return None
    _, pilot, _ = records(base, project, active=True, unavailable_code=74)
    if window.get("pilot_id") != pilot.get("id"):
        fail("source-pin window pilot binding is invalid")
    contract = verify_approved_goal(
        base, project, goal_id, check_remote=False, allow_verifying=True,
        allow_paused=True, state_unavailable_code=74,
    )
    authority = contract.get("authority_contract")
    if not isinstance(authority, dict):
        fail("source-pin authority is absent", 74)
    if window.get("plan_id") != contract.get("plan_id") \
            or window.get("plan_digest") != contract.get("plan_digest"):
        fail("source-pin window does not match its approved plan")
    return {
        "schema": "recursion-project-source-pin/v1",
        "project_id": project,
        "goal_id": goal_id,
        "plan_id": contract["plan_id"],
        "plan_digest": contract["plan_digest"],
        "source_head": contract["source_head"],
        "remote_head": authority["remote_head"],
    }


def approve_plan(base: Path, cfg: dict, pilot: dict, decision: dict, instruction_id: str,
                 instruction_file: Path, instruction: dict[str, str]) -> dict:
    args = decision.get("args") or decision.get("arguments") or []
    if len(args) != 3: fail("pilot-plan-approve needs project, plan id, and digest", 2)
    plan_id, claimed = str(args[1]), str(args[2])
    require_lucas_instruction(instruction_file, instruction, "pilot-plan-approve", [plan_id, claimed])
    path = base / "state/plans" / str(cfg["id"]) / f"{plan_id}.json"
    with phase8_lock(base, plan_id):
        proposal = read_json(path)
        if proposal.get("project_id") != cfg["id"] or proposal.get("plan_digest") != claimed:
            fail("Lucas approval digest does not match the stored plan")
        plan = validate_plan(base, cfg, str(cfg["id"]), plan_id, proposal.get("plan"),
                             check_remote=True)
        digest = canonical_digest(plan)
        if digest != claimed: fail("stored plan bytes do not match the approved digest")
        goal = plan["goal"]; goal_id = str(goal["id"]); goal_path = base / "state/goals" / f"{goal_id}.md"
        authority = plan["authority_contract"]
        seed_contract = plan.get("seed_contract")
        gate_profile_selection = plan.get("gate_profile_selection")
        event = transition_event(
            "pilot-plan-approved", str(cfg["id"]), goal_id,
            detail={"plan_id": plan_id, "plan_digest": digest, "instruction_id": instruction_id},
            event_id="source-plan-approved-" + canonical_digest(
                [cfg["id"], plan_id, digest, instruction_id])[:24],
        )
        try:
            resumed = resume_transition(base, event)
        except (OSError, TransitionReceiptError):
            fail("pilot plan approval recovery failed", 74)
        if resumed:
            proposal = read_json(path)
            if proposal.get("status") != "approved":
                fail("resumed approval lacks its authoritative plan marker", 74)
            verify_approved_goal(base, str(cfg["id"]), goal_id)
            return {"plan": str(path.relative_to(base)), "status": "approved", "plan_digest": digest}
        if proposal.get("status") == "approved":
            if proposal.get("approval_instruction_id") != instruction_id or not goal_path.is_file():
                fail("approved plan projection is incomplete or approval identity changed")
            verify_approved_goal(base, str(cfg["id"]), goal_id)
            append_event(base, event["event"], event["project"], event["goal"],
                         detail=event["detail"], event_id=event["id"])
            return {"plan": str(path.relative_to(base)), "status": "approved", "plan_digest": digest}
        if proposal.get("status") != "proposed": fail("pilot plan is not approvable")
        mutations = []
        if goal_path.exists():
            fail("goal materialization exists without the approval transition receipt")
        else:
            goal_body = (f"schema_version: 1\nversion: 1\nid: {goal_id}\nproject_id: {cfg['id']}\n"
                         f"pilot_id: {pilot['id']}\nplan_id: {plan_id}\nplan_digest: {digest}\n"
                         f"instruction_id: {proposal['instruction_id']}\nstatus: draft\noutcome: {goal['outcome']}\n"
                         f"acceptance_suite: {goal['acceptance_suite']}\nfalsifier: {goal['falsifier']}\n"
                         f"gate_class: {goal['gate_class']}\n"
                         f"gate_files: {','.join(goal['gate_files'])}\n"
                         f"controller_gate_files: {','.join(goal['controller_gate_files'])}\n"
                         f"gate_contract: {json.dumps(goal['gate_contract'], sort_keys=True, separators=(',', ':'))}\n"
                         f"source_head: {authority['source_head']}\nsource_branch: {authority['default_branch']}\n"
                         f"charter_sha256: {authority['charter_sha256']}\n"
                         f"budget: {json.dumps(goal['budget'], sort_keys=True, separators=(',', ':'))}\n"
                         f"started_at_epoch: {int(time.time())}\nterminal_reported: 0\n")
            if seed_contract:
                goal_body += (f"seed_manifest: {seed_contract['manifest_path']}\n"
                              f"seed_manifest_sha256: {seed_contract['manifest_sha256']}\n"
                              f"seed: {','.join(seed_contract['paths'])}\n"
                              f"seed_digest: {seed_contract['content_sha256']}\n")
            if gate_profile_selection:
                goal_body += (
                    "gate_profile_selection: "
                    + json.dumps(gate_profile_selection, sort_keys=True, separators=(",", ":"))
                    + "\n"
                )
        goal_body = goal_body.replace("status: draft", "status: active", 1)
        mutations.append(text_mutation(goal_path, goal_body))
        for task in plan["tasks"]:
            task_path = base / "state/tasks" / f"{task['id']}.md"
            if task_path.exists():
                fail("task materialization exists without the approval transition receipt")
            else:
                body = task_text(
                    cfg, pilot, plan_id, digest, goal_id, task, authority, seed_contract,
                    gate_profile_selection,
                )
            mutations.append(text_mutation(task_path, body))
        proposal = dict(proposal); proposal.update({"status": "approved", "approval_instruction_id": instruction_id,
                                                    "approved_by": instruction.get("principal"), "approved_at": now_iso()})
        validate_control_state(base, "plan", proposal)
        mutations.append(json_mutation(path, proposal))
        commit_pilot_transition(base, mutations, event, authoritative_index=len(mutations) - 1)
        verify_approved_goal(
            base, str(cfg["id"]), goal_id, state_unavailable_code=74,
        )
    return {"plan": str(path.relative_to(base)), "status": "approved", "plan_digest": digest}


def preworker_window_state(base: Path, project: str, pilot_id: str, goal_id: str,
                           plan_id: str, plan_digest: str,
                           planned_ids: set[str]) -> bool:
    """Prove initial or resumed window authority cannot launder worker execution.

    The first window requires the pristine proposed DAG.  Once a matching window has closed,
    a controller transition may lawfully have moved a task to ready/queued before PAUSE was
    observed.  That state is resumable only while every execution witness remains absent.
    """
    prior_closed = False
    for path in status_files(base / "state/pilot-windows", ".json", None):
        value = read_json(path, unavailable_code=74)
        if value.get("project_id") != project or value.get("goal_id") != goal_id:
            continue
        if value.get("pilot_id") != pilot_id or value.get("plan_id") != plan_id \
                or value.get("plan_digest") != plan_digest:
            return False
        if value.get("status") == "closed":
            prior_closed = True

    allowed_statuses = {"proposed", "ready", "queued"} if prior_closed else {"proposed"}
    for task_id in planned_ids:
        task = fields(base / "state/tasks" / f"{task_id}.md", unavailable_code=74)
        raw_attempts = task.get("attempts", "0")
        if task.get("status") not in allowed_statuses \
                or not re.fullmatch(r"[0-9]+", raw_attempts) or int(raw_attempts) != 0 \
                or bool(task.get("candidate_commit")):
            return False

    for path in status_files(base / "state/runs", ".json", None):
        if str(read_json(path, unavailable_code=74).get("task_id") or "") in planned_ids:
            return False
    return True


def window_action(base: Path, cfg: dict, pilot: dict, decision: dict, instruction_id: str,
                  instruction_file: Path, instruction: dict[str, str]) -> dict:
    args = decision.get("args") or decision.get("arguments") or []; verb = str(decision.get("verb") or "")
    if verb == "pilot-window-open":
        if len(args) != 4: fail("pilot-window-open needs project, goal, window, and seconds", 2)
        goal_id, window_id, raw_seconds = map(str, args[1:4])
        if not (base / "state/PAUSE").is_file(): fail("a pilot window may be authorized only while PAUSE is observed")
        if not IDENT.fullmatch(window_id) or not raw_seconds.isdigit() or not 1 <= int(raw_seconds) <= 86400:
            fail("pilot window identity or duration is invalid")
        require_lucas_instruction(instruction_file, instruction, "pilot-window-open",
                                  [str(cfg["id"]), goal_id, window_id, raw_seconds])
        goal = fields(goal_for(base, str(cfg["id"]), goal_id)[0]); plan_id = goal.get("plan_id", "")
        plan_path = base / "state/plans" / str(cfg["id"]) / f"{plan_id}.json"
        proposal = read_json(plan_path) if plan_id else {}
        if not plan_id or proposal.get("status") != "approved":
            fail("pilot window goal does not come from an approved Phase 8 plan")
        plan = validate_plan(base, cfg, str(cfg["id"]), plan_id, proposal.get("plan"),
                             check_remote=True)
        digest = canonical_digest(plan)
        if proposal.get("plan_digest") != digest or goal.get("plan_digest") != digest:
            fail("pilot window goal does not match the live repository-bound plan contract")
        verify_approved_goal(base, str(cfg["id"]), goal_id)
        require_provider_capability(base, plan, live=True)
        planned_ids = {str(task["id"]) for task in plan["tasks"]}
        if goal.get("status") != "active" or not preworker_window_state(
            base, str(cfg["id"]), str(pilot["id"]), goal_id, plan_id, digest, planned_ids,
        ):
            fail("pilot window requires an active goal with a pristine or unexecuted resumable task DAG")
        path = base / "state/pilot-windows" / f"{window_id}.json"
        with phase8_lock(base, f"window-{cfg['id']}", contended_code=75):
            for other in status_files(base / "state/pilot-windows", ".json", None):
                value = read_json(other, unavailable_code=74, malformed_code=4)
                if value.get("project_id") == cfg["id"] and value.get("status") in {"armed", "open"} and other != path:
                    fail("project already has an open pilot window")
            record = {"schema_version": 1, "id": window_id, "project_id": cfg["id"], "pilot_id": pilot["id"],
                      "goal_id": goal_id, "plan_id": plan_id, "plan_digest": goal["plan_digest"],
                      "instruction_id": instruction_id, "opened_by": instruction["principal"], "status": "armed",
                      "armed_at": now_iso(), "opened_at_epoch": int(time.time()),
                      "duration_seconds": int(raw_seconds),
                      "expires_at_epoch": int(time.time()) + int(raw_seconds),
                      "max_inflight_workers": min(int(pilot.get("concurrency_limit") or 0),
                                                  int((pilot.get("budget") or {}).get("max_inflight_workers") or 0))}
            if plan.get("gate_profile_selection"):
                record["gate_profile_selection"] = plan["gate_profile_selection"]
            if record["max_inflight_workers"] < 1: fail("pilot window has zero worker capacity")
            event = transition_event(
                "pilot-window-armed", str(cfg["id"]), goal_id,
                detail={"window_id": window_id, "instruction_id": instruction_id,
                        "duration_seconds": int(raw_seconds)},
                event_id="source-window-armed-" + canonical_digest(
                    [cfg["id"], goal_id, window_id, instruction_id, raw_seconds])[:24],
            )
            if path.exists():
                prior = read_json(path)
                if prior.get("instruction_id") == instruction_id and prior.get("goal_id") == goal_id \
                        and prior.get("duration_seconds") == int(raw_seconds):
                    try: resumed = resume_transition(base, event)
                    except (OSError, TransitionReceiptError):
                        fail("pilot window recovery failed", 74)
                    if not resumed:
                        append_event(base, event["event"], event["project"], event["goal"],
                                     detail=event["detail"], event_id=event["id"])
                    return prior
                fail("pilot window id collides with different authority")

            def arm_precondition() -> None:
                if os.environ.get("RECURSION_TEST_PAUSE_LIFT_BEFORE_ARM") == "1":
                    (base / "state/PAUSE").unlink(missing_ok=True)
                if os.environ.get("RECURSION_TEST_WINDOW_ARM_RACE") == "singleton":
                    atomic_json(base / "state/pilot-windows/test-arm-race.json", {
                        "schema_version": 1, "id": "test-arm-race", "project_id": cfg["id"],
                        "goal_id": "test-arm-race-goal", "status": "armed",
                    })
                if not (base / "state/PAUSE").is_file():
                    raise TransitionReceiptError("PAUSE was lifted before the pilot window arm commit")
                if not preworker_window_state(
                    base, str(cfg["id"]), str(pilot["id"]), goal_id,
                    plan_id, digest, planned_ids,
                ):
                    raise TransitionReceiptError(
                        "goal or task execution state changed before the pilot window arm commit"
                    )
                for other in status_files(base / "state/pilot-windows", ".json", None):
                    value = read_json(other, unavailable_code=74, malformed_code=4)
                    if value.get("project_id") == cfg["id"] \
                            and value.get("status") in {"armed", "open"} and other != path:
                        raise TransitionReceiptError(
                            "project pilot window singleton changed before the arm commit"
                        )
                current_contract = verify_approved_goal(
                    base, str(cfg["id"]), goal_id, check_remote=False,
                    state_unavailable_code=74,
                )
                if current_contract.get("plan_id") != plan_id \
                        or current_contract.get("plan_digest") != digest \
                        or current_contract.get("source_head") \
                        != plan["authority_contract"]["source_head"]:
                    raise TransitionReceiptError(
                        "approved local source contract changed before the arm commit"
                    )

            commit_pilot_transition(base, [json_mutation(path, record)], event,
                                    precondition=arm_precondition)
        return record
    if len(args) != 2: fail("pilot-window-close needs project and window", 2)
    window_id = str(args[1]); require_lucas_instruction(instruction_file, instruction, "pilot-window-close", [window_id])
    path = base / "state/pilot-windows" / f"{window_id}.json"
    with phase8_lock(base, f"window-{cfg['id']}", contended_code=75):
        record = read_json(path)
        if record.get("project_id") != cfg["id"]: fail("pilot window is outside this project")
        event = transition_event(
            "pilot-window-closed", str(cfg["id"]), str(record.get("goal_id") or ""),
            detail={"window_id": window_id, "instruction_id": instruction_id},
            event_id="source-window-close-" + canonical_digest(
                [cfg["id"], record.get("goal_id"), window_id, instruction_id])[:24],
        )
        try:
            resumed = resume_transition(base, event)
        except (OSError, TransitionReceiptError):
            fail("pilot window close recovery failed", 74)
        if resumed:
            return read_json(path)
        if record.get("status") == "closed":
            append_event(base, event["event"], event["project"], event["goal"],
                         detail=event["detail"], event_id=event["id"])
            return record
        record.update({"status": "closed", "closed_at": now_iso(), "close_reason": "lucas-instruction",
                       "close_instruction_id": instruction_id})
        commit_pilot_transition(base, [json_mutation(path, record)], event)
    return record


def execute_value(base: Path, decision: dict) -> dict:
    if not isinstance(decision, dict):
        fail("decision is not an object")
    args = decision.get("args") or decision.get("arguments") or []
    project = str(decision.get("project") or (args[0] if args else "")); verb = str(decision.get("verb") or "")
    cfg, pilot, _ = records(base, project, active=True)
    pilot_authority = verb in PILOT_VERBS and decision.get("actor") == pilot["id"]
    head_authority = verb in PHASE8_HEAD_VERBS and decision.get("actor") == "head-hermes"
    if not (pilot_authority or head_authority):
        fail("decision actor or verb is outside project-pilot authority")
    if not args or args[0] != project: fail("decision target does not match its pilot")
    instruction_id = str(decision.get("instruction_id") or "")
    if not instruction_id: fail("project-pilot decision lacks topic instruction correlation")
    source_instruction = instruction_path(base, instruction_id)
    instruction = fields(source_instruction)
    instruction_project = instruction.get("project_id", "")
    instruction_pilot = instruction.get("pilot_id", "")
    if pilot_authority and (instruction_project != project or instruction_pilot != pilot["id"]):
        fail("instruction is not owned by this project pilot")
    # Approval/window authority originates in Head Hermes General/topic 1, whose intake
    # projection deliberately has no project or pilot owner. The authenticated command body
    # supplies the exact plan/goal/window bounds and the verb handler revalidates them below.
    # Still fail closed if a head-routed instruction carries conflicting scoped metadata.
    if head_authority and (
        instruction_project not in {"", project}
        or instruction_pilot not in {"", pilot["id"]}
    ):
        fail("Head Hermes instruction carries conflicting project-pilot ownership")
    if verb == "pilot-plan-propose":
        return propose_plan(base, cfg, pilot, decision, instruction_id)
    if verb == "pilot-plan-approve":
        return approve_plan(base, cfg, pilot, decision, instruction_id, source_instruction, instruction)
    if verb in {"pilot-window-open", "pilot-window-close"}:
        return window_action(base, cfg, pilot, decision, instruction_id, source_instruction, instruction)
    goal_id = str(args[1]) if len(args) > 1 else ""; goal_for(base, project, goal_id)
    if verb == "pilot-task-create":
        if len(args) < 4: fail("task creation needs project, goal, task id, and scope", 2)
        task_id, scope = str(args[2]), str(args[3])
        if not IDENT.fullmatch(task_id) or not scope_allowed(cfg, scope): fail("task is outside the charter")
        dependencies = [] if len(args) < 5 or args[4] in ("", "-") else str(args[4]).split(",")
        state = scoped_state(base, project); owned = {task.get("id") for task in state["tasks"]}
        if any(dep not in owned for dep in dependencies): fail("task dependency crosses project scope")
        text = str(decision.get("text") or "").strip()
        if not text or "\n" in text: fail("task needs bounded one-line intent")
        with goal_state_lock(base, goal_id):
            # Re-read under the same lock Phase 6 uses to enter verifying. A task creation
            # that raced that transition must lose; remembered pilot state is not authority.
            _, goal = goal_for(base, project, goal_id)
            if goal.get("plan_id"):
                fail("an exact-digest Phase 8 goal has an immutable task DAG")
            path = base / "state/tasks" / f"{task_id}.md"
            if path.exists(): fail(f"task {task_id} already exists", 2)
            required_non_goals = ",".join(dict.fromkeys(
                ["portfolio", "policy", *project_required_non_goals(cfg)]
            ))
            body = (f"schema_version: 1\nversion: 1\nid: {task_id}\nproject_id: {project}\n"
                    f"workstream: {project}\npilot_id: {pilot['id']}\n"
                    f"goal_id: {goal_id}\ndependencies: {','.join(dependencies)}\nallowed_paths: {scope}\n"
                    f"non_goals: {required_non_goals}\nworker_class: claude\n"
                    f"engine: claude\nmodel_alias: sonnet-routine\nverifier_model_alias: opus-review\n"
                    f"deadline_seconds: 3600\nverifier_deadline_seconds: 1800\n"
                    f"network_policy: provider-only\nevidence_contract: worker-result-v1\n"
                    f"verification: independent\nexpected_change: required\ngate_class: light\ngate_files:\n"
                    f"acceptance: {goal['acceptance_suite']}\nstatus: proposed\n\n{text}\n")
            event = transition_event(
                "pilot-task-created", project, goal_id, task_id,
                {"scope": scope, "dependencies": dependencies, "instruction_id": instruction_id},
                "source-pilot-task-created-" + canonical_digest(
                    [project, goal_id, task_id, instruction_id])[:24],
            )
            commit_pilot_transition(base, [text_mutation(path, body)], event)
            return {"created": str(path.relative_to(base)), "status": "proposed"}
    if verb in {"pilot-worker-request", "pilot-verifier-request", "pilot-block"}:
        if len(args) != 3:
            fail(f"{verb} needs project, goal, and task")
        task_id = str(args[2]); owned = {task.get("id") for task in scoped_state(base, project)["tasks"]}
        if task_id not in owned:
            fail("request names a task outside this project")
    if verb in {"pilot-block", "pilot-escalate"}:
        text = str(decision.get("text") or "").strip()
        if not text or "\n" in text or len(text) > 500:
            fail(f"{verb} needs bounded one-line prose")
    payload = {"schema_version": 1, "id": str(decision.get("id") or ""), "actor": pilot["id"],
               "project_id": project, "goal_id": goal_id, "verb": verb, "arguments": args[2:],
               "text": decision.get("text") or "", "instruction_id": instruction_id,
               "status": "requested", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    identity = str(decision.get("idempotency_key") or json.dumps(payload, sort_keys=True))
    name = hashlib.sha256(identity.encode()).hexdigest()[:24] + ".json"
    path = base / "state/pilot-requests" / project / name
    event = transition_event(
        "pilot-requested", project, goal_id, str(args[2]) if len(args) > 2 else "",
        {"request_id": payload["id"], "verb": verb, "instruction_id": instruction_id},
        "source-pilot-request-" + hashlib.sha256(identity.encode()).hexdigest()[:24],
    )
    if not path.exists():
        commit_pilot_transition(base, [json_mutation(path, payload)], event)
    else:
        prior = read_json(path)
        if prior.get("id") != payload["id"] or prior.get("verb") != verb \
                or prior.get("instruction_id") != instruction_id:
            fail("pilot request identity collides with different content")
        append_event(base, event["event"], event["project"], event["goal"], event["task"],
                     event["detail"], event["id"])
    return {"request": str(path.relative_to(base)), "target": "head-hermes" if verb == "pilot-escalate" else "project-pilot"}


def execute(base: Path, decision_path: Path) -> dict:
    return execute_value(base, read_json(decision_path))


def _run_main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify-registry")
    for name in ("topic", "context", "status"):
        cmd = sub.add_parser(name); cmd.add_argument("value")
    repo_cmd = sub.add_parser("repo"); repo_cmd.add_argument("project"); repo_cmd.add_argument("path", nargs="?")
    for name in ("logs", "evidence"):
        cmd = sub.add_parser(name); cmd.add_argument("project"); cmd.add_argument("path", nargs="?")
    route = sub.add_parser("route"); route.add_argument("project"); route.add_argument("request"); route.add_argument("target", nargs="?")
    auth = sub.add_parser("authorize"); auth.add_argument("project"); auth.add_argument("request"); auth.add_argument("scope"); auth.add_argument("target", nargs="?")
    prep = sub.add_parser("prepare"); prep.add_argument("project"); prep.add_argument("remote"); prep.add_argument("charter")
    bind = sub.add_parser("bind-topic"); bind.add_argument("project"); bind.add_argument("topic")
    activate_cmd = sub.add_parser("activate"); activate_cmd.add_argument("project")
    execute_cmd = sub.add_parser("execute"); execute_cmd.add_argument("decision")
    execute_json_cmd = sub.add_parser("execute-json"); execute_json_cmd.add_argument("decision")
    source_pin_cmd = sub.add_parser("active-source-pin"); source_pin_cmd.add_argument("project")
    verify_goal_cmd = sub.add_parser("verify-approved-goal")
    verify_goal_cmd.add_argument("project"); verify_goal_cmd.add_argument("goal")
    verify_goal_cmd.add_argument("--terminal", action="store_true")
    verify_goal_cmd.add_argument("--check-remote", action="store_true")
    args = parser.parse_args(); base = root()
    if args.command == "verify-registry": verify_registry(base); print("OK")
    elif args.command == "topic":
        matches = [row for row in rows(base) if row["topic"] == str(args.value) and row["status"] == "active"]
        if len(matches) != 1: fail(f"topic {args.value} is not bound to one active project")
        cfg, pilot, _ = records(base, matches[0]["id"], active=True); compact({"project": cfg, "pilot": pilot})
    elif args.command == "context": compact(scoped_state(base, args.value))
    elif args.command == "status":
        value = scoped_state(base, args.value); value = {"source": "live-control-state", "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "git": git_evidence(value["project"]), **value}
        compact(value)
    elif args.command == "repo": compact(repo_read(base, args.project, args.path))
    elif args.command in {"logs", "evidence"}: compact(artifact_read(base, args.project, args.command, args.path))
    elif args.command == "route":
        cfg, pilot, _ = records(base, args.project, active=True)
        target = "project-pilot" if args.request in PILOT_ROUTES and (not args.target or args.target == args.project) else "head-hermes"
        compact({"project_id": cfg["id"], "pilot_id": pilot["id"], "target": target, "request": args.request})
    elif args.command == "authorize":
        cfg, pilot, _ = records(base, args.project, active=True)
        if args.request not in PILOT_VERBS or (args.target and args.target != args.project) or not scope_allowed(cfg, args.scope):
            fail("request is outside the pilot charter")
        compact({"authorized": True, "pilot_id": pilot["id"], "project_id": args.project})
    elif args.command == "prepare": prepare(base, args.project, args.remote, args.charter); print("pending")
    elif args.command == "bind-topic": bind_topic(base, args.project, args.topic); print("bound")
    elif args.command == "activate": compact(activate(base, args.project))
    elif args.command == "execute": compact(execute(base, Path(args.decision)))
    elif args.command == "verify-approved-goal":
        compact(verify_approved_goal(
            base, args.project, args.goal, terminal=args.terminal,
            check_remote=args.check_remote, state_unavailable_code=74,
        ))
    elif args.command == "active-source-pin":
        pin = active_source_pin(base, args.project)
        compact(pin or {"schema": "recursion-project-source-pin/v1", "project_id": args.project,
                        "source_head": "-"})
    else:
        try:
            decision = _parse_json_arg(args.decision)
        except (json.JSONDecodeError, ValueError, TypeError, OverflowError, RecursionError):
            fail("frozen decision JSON is invalid")
        compact(execute_value(base, decision))
    return 0


def main() -> int:
    try:
        return _run_main()
    except (OSError, ValueError, TypeError, OverflowError, RecursionError,
            UnicodeError, json.JSONDecodeError, subprocess.TimeoutExpired,
            subprocess.SubprocessError):
        print("project-pilot: operation refused", file=sys.stderr)
        return 4


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, IndexError,
            AttributeError, OverflowError, RecursionError) as exc:
        print("project-pilot: invalid input or state", file=sys.stderr)
        raise SystemExit(4)
