"""The runner: one command runs a pinned suite through each arm, every task ``attempts`` times.

    eval/run --suite dev --arms direct,recursion --k 5 --engine standin
    eval/run --suite pilot --tasks /abs/held-out/pilot --arms direct,recursion --k 5 --engine claude_code ...

Per attempt (PROTOCOL.md, "Running"): a new TMPDIR and XDG_RUNTIME_DIR (eval/scratch.py), a fresh
copy of the task's start tree made into a one-commit repository whose tree must equal the pinned
tree, the engine run in it with the prompt, the final diff taken, the hidden tests written over the
tree and run, both scratch folders removed. Monotonic times throughout.

One record per suite run of one arm (eval/record.py), every planned trial in it. An arm that refuses
(eval/arms.py) writes no record and says why; the other arms run. Every score is recomputed from the
record by eval/analyze.py; the runner stores none.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from eval import PACKAGE_ROOT, __version__, runner_sha256
from eval import arms as arms_mod
from eval import gitutil, heldout
from eval import record as record_mod
from eval import scratch as scratch_mod
from eval import suite as suite_mod

DEV_SUITE = PACKAGE_ROOT / "devset" / "suite.json"
DEFAULT_OUT = PACKAGE_ROOT / "results"
TASKS_ENV = "RECURSION_EVAL_TASKS"
GRADER_ID = "hidden-tests/1"
NO_TESTS_RAN = 5          # unittest and pytest both exit 5 when no test ran
LOG_CAP = 64 * 1024


class RunRefused(RuntimeError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def frozen_hashes() -> dict:
    """The SHA-256s eval/FROZEN.sha256 holds for the protocol and the analysis."""
    out = {}
    path = PACKAGE_ROOT / "FROZEN.sha256"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            digest, name = line.split(None, 1)
            out[name.strip()] = digest
    return out


def protocol_block() -> dict:
    hashes = frozen_hashes()
    analysis = record_mod.sha256_json({name: hashes[name] for name in ("analyze.py", "passk.py")})
    return {"protocol_sha256": hashes["PROTOCOL.md"], "analysis_sha256": analysis}


def build_plan(suite, arm, *, k: int, attempts: int, replacements: int, seed: int) -> dict:
    plan = {
        "k": k,
        "attempts_per_task": attempts,
        "replacements_per_task": replacements,
        "seed": seed,
        "tasks": [{"id": t.id, "cluster": t.cluster, "task_sha256": t.sha256, "created_at": t.created_at}
                  for t in suite.tasks],
    }
    body = dict(plan, suite_manifest_sha256=suite.manifest_sha256, arm_settings_sha256=arm.settings_sha256())
    plan["plan_sha256"] = record_mod.sha256_json(body)
    return plan


def blank_trial(trial_id: str, task_id: str, index: int, planned: bool, replacement_for) -> dict:
    return {
        "trial_id": trial_id,
        "task_id": task_id,
        "attempt": {"index": index, "planned": planned, "replacement_for": replacement_for},
        "run_id": None,
        "grade": {"verdict": "not_graded", "grader_id": None, "grader_sha256": None, "details_sha256": None,
                  "exit": None},
        "error": {"class": "unclassified", "tier": "unknown", "detail": None},
        "engine": {"exit": None, "signal": None, "timed_out": False, "claimed_done": None},
        "usage": record_mod.empty_usage(),
        "timing": {"wall_seconds": None, "engine_seconds": None, "grading_seconds": None},
        "artifacts": {"patch_sha256": None, "patch_bytes": None, "patch_empty": None, "files_changed": None,
                      "test_log_sha256": None, "trajectory_sha256": None, "trajectory_format": None,
                      "retained": False},
        "scratch": {"tmpdir": None, "xdg_runtime_dir": None, "removed": None},
    }


def not_run_trial(trial_id: str, task_id: str, index: int) -> dict:
    trial = blank_trial(trial_id, task_id, index, True, None)
    trial["error"] = {"class": "not_run", "tier": "unknown", "detail": "the run stopped before this trial"}
    return trial


def _set_error(trial: dict, cls: str, detail: str | None) -> None:
    trial["error"] = {"class": cls, "tier": record_mod.CLASS_TIER[cls], "detail": detail}


def grade(task, work: Path, scratch) -> dict:
    """Write the hidden tests over the work tree and run them. Pass means exit 0."""
    task.write_hidden(work)
    env = scratch_mod.environment(scratch)
    try:
        done = arms_mod.run_process(task.command(), cwd=work, env=env, stdin=None, timeout_s=task.grading_timeout_s)
    except arms_mod.EngineMissing as exc:
        return {"verdict": "not_graded", "error": "grader_failed", "detail": str(exc), "exit": None, "log": b"",
                "seconds": None}
    log = done["stdout"] + done["stderr"]
    if done["timed_out"]:
        return {"verdict": "unresolved", "error": "tests_failed", "exit": None, "log": log, "seconds": done["seconds"],
                "detail": f"hidden tests ran past {task.grading_timeout_s:g} s"}
    if done["exit"] == 0:
        return {"verdict": "resolved", "error": None, "detail": None, "exit": 0, "log": log, "seconds": done["seconds"]}
    if done["exit"] == NO_TESTS_RAN:
        return {"verdict": "not_graded", "error": "no_tests_ran", "exit": NO_TESTS_RAN, "log": log,
                "seconds": done["seconds"], "detail": "the hidden-test command ran no test"}
    return {"verdict": "unresolved", "error": "tests_failed", "exit": done["exit"], "log": log,
            "seconds": done["seconds"], "detail": f"hidden tests exit {done['exit']}"}


@dataclass
class Context:
    record_id: str
    tmp_base: Path
    xdg_base: Path
    artifacts: Path | None


def _write_artifacts(folder: Path, files: dict) -> bool:
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    return True


def run_attempt(ctx: Context, arm, task, index: int, *, planned: bool = True, replacement_for=None) -> dict:
    trial_id = f"{ctx.record_id}.{task.id}.{index}"
    trial = blank_trial(trial_id, task.id, index, planned, replacement_for)
    started = time.monotonic()
    attempt_id = f"{ctx.record_id}-{task.id}-{index}"
    stage = "setup"
    keep: dict = {}
    try:
        with scratch_mod.attempt(attempt_id, ctx.tmp_base, ctx.xdg_base) as scratch:
            trial["scratch"].update(tmpdir=str(scratch.tmpdir), xdg_runtime_dir=str(scratch.xdg_runtime_dir))
            work = scratch.tmpdir / "work"
            work.mkdir(mode=0o700)
            task.materialize_start(work)
            base_commit, tree = gitutil.snapshot(work)
            if tree != task.parent_tree:
                _set_error(trial, "start_tree_mismatch", f"fresh copy has tree {tree}, the suite pins {task.parent_tree}")
                return trial
            stage = "engine"
            try:
                outcome = arm.run(task, work, scratch, index)
            except arms_mod.EngineMissing as exc:
                _set_error(trial, "engine_missing", str(exc))
                return trial
            trial["run_id"] = outcome.run_id
            trial["engine"] = {"exit": outcome.exit, "signal": outcome.signal, "timed_out": outcome.timed_out,
                               "claimed_done": outcome.claimed_done}
            trial["usage"] = outcome.usage
            trial["timing"]["engine_seconds"] = outcome.seconds
            trial["artifacts"]["trajectory_sha256"] = hashlib.sha256(outcome.stdout).hexdigest()
            trial["artifacts"]["trajectory_format"] = outcome.trajectory_format
            keep["engine.jsonl"] = outcome.stdout
            keep["engine.stderr.txt"] = outcome.stderr
            stage = "diff"
            try:
                patch, paths = gitutil.diff_from(work, base_commit)
                trial["artifacts"].update(patch_sha256=hashlib.sha256(patch).hexdigest(), patch_bytes=len(patch),
                                          patch_empty=not patch, files_changed=len(paths))
                keep["patch.diff"] = patch
            except gitutil.GitError as exc:
                keep["patch-error.txt"] = str(exc).encode("utf-8")
            stage = "grade"
            graded = grade(task, work, scratch)
            trial["timing"]["grading_seconds"] = graded["seconds"]
            trial["grade"] = {"verdict": graded["verdict"], "grader_id": GRADER_ID,
                              "grader_sha256": task.grader_sha256(),
                              "details_sha256": hashlib.sha256(graded["log"]).hexdigest(), "exit": graded["exit"]}
            trial["artifacts"]["test_log_sha256"] = hashlib.sha256(graded["log"]).hexdigest()
            keep["test.log"] = graded["log"]
            classify(trial, graded, outcome)
    except scratch_mod.ScratchError as exc:
        _set_error(trial, "setup_failed", str(exc))
    except Exception as exc:  # noqa: BLE001 - every attempt ends in a row, whatever broke
        # setup: the harness could not prepare the attempt (infra); grade: the grader broke (grader);
        # anything else is not attributable from here (unknown). None of them is graded.
        cls = {"setup": "setup_failed", "grade": "grader_failed"}.get(stage, "unclassified")
        _set_error(trial, cls, f"{type(exc).__name__}: {exc}"[:500])
        trial["grade"]["verdict"] = "not_graded"
    finally:
        trial["timing"]["wall_seconds"] = time.monotonic() - started
        tmp = trial["scratch"]["tmpdir"]
        xdg = trial["scratch"]["xdg_runtime_dir"]
        if tmp is not None:
            trial["scratch"]["removed"] = not Path(tmp).exists() and not Path(xdg).exists()
        if ctx.artifacts is not None and keep:
            trial["artifacts"]["retained"] = _write_artifacts(ctx.artifacts / trial_id, keep)
    return trial


def classify(trial: dict, graded: dict, outcome) -> None:
    """Verdict comes from the hidden tests; the tier says whose failure an unresolved trial is."""
    verdict = graded["verdict"]
    if verdict == "resolved":
        trial["error"] = {"class": None, "tier": "none", "detail": None}
        return
    if verdict == "not_graded":
        _set_error(trial, graded["error"], graded["detail"])
        return
    if outcome.failure in ("vendor_limit", "auth_failed"):
        _set_error(trial, outcome.failure, outcome.detail)
    elif outcome.failure in ("timeout", "engine_crash", "no_result", "engine_error"):
        _set_error(trial, outcome.failure, outcome.detail)
    else:
        _set_error(trial, "tests_failed", graded["detail"])


def schedule(task_ids: list, arms: list, attempts: int, seed: int) -> list:
    """Every (arm, task, index) once: trial index outermost, tasks shuffled per index, arms alternating."""
    jobs = []
    for index in range(1, attempts + 1):
        order = list(task_ids)
        random.Random(seed * 1_000_003 + index).shuffle(order)
        arm_order = arms if index % 2 else list(reversed(arms))
        for task_id in order:
            for arm in arm_order:
                jobs.append((arm, task_id, index))
    return jobs


@dataclass
class Outcome:
    records: dict = field(default_factory=dict)     # arm name -> record path
    refused: dict = field(default_factory=dict)     # arm name -> message
    interrupted: bool = False


def run(suite, arms: list, *, k: int, attempts: int | None = None, replacements: int = 2, label: str = "dev",
        seed: int = 20261126, jobs: int = 1, out_dir: Path = DEFAULT_OUT, artifacts: Path | None = None,
        tmp_base: Path = scratch_mod.DEFAULT_TMP_BASE, xdg_base: Path = scratch_mod.DEFAULT_XDG_BASE,
        log=print) -> Outcome:
    attempts = attempts if attempts is not None else k
    if attempts < k:
        raise RunRefused(f"attempts ({attempts}) below k ({k}): pass^{k} would be not estimable for every task")
    if label not in record_mod.LABELS:
        raise RunRefused(f"label must be one of {record_mod.LABELS}")
    if suite.split == "held_out" and label == "dev":
        raise RunRefused("a held-out suite is never labelled dev")
    if suite.split == "dev" and label != "dev":
        raise RunRefused("a dev suite is labelled dev")
    if suite.split == "held_out" and artifacts is not None:
        heldout.check_path(artifacts, must_exist=False)  # held-out diffs and logs stay out of the repository too
    runner = {"version": __version__, "sha256": runner_sha256()}
    if suite.runner is not None and suite.runner != runner:
        raise RunRefused(f"the suite pins runner {suite.runner}, this runner is {runner}")
    scratch_mod.check_bases(tmp_base, xdg_base)
    result = Outcome()
    ready = []
    for arm in arms:
        try:
            arm.preflight()
            version = arm.engine_version()
            pinned = suite.engines.get(arm.engine)
            if pinned is not None and version != pinned:
                raise arms_mod.ArmRefused(f"{arm.name} arm: the suite pins {arm.engine} {pinned!r}, found {version!r}")
            ready.append((arm, version))
        except arms_mod.ArmRefused as exc:
            result.refused[arm.name] = str(exc)
            log(str(exc))
    if not ready:
        return result
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    contexts, records = {}, {}
    protocol = protocol_block()
    for arm, version in ready:
        record_id = f"{stamp}-{arm.name}-{secrets.token_hex(3)}"
        contexts[arm.name] = Context(record_id, Path(tmp_base), Path(xdg_base),
                                     None if artifacts is None else Path(artifacts) / record_id)
        records[arm.name] = {
            "schema": record_mod.SCHEMA_ID,
            "record_id": record_id,
            "label": label,
            "suite": {"id": suite.id, "version": suite.version, "split": suite.split, "frozen": suite.frozen,
                      "manifest_sha256": suite.manifest_sha256},
            "arm": {"name": arm.name, "engine": arm.engine, "engine_version": version, "model": arm.model,
                    "route": arm.route, "effort": arm.effort, "settings_sha256": arm.settings_sha256()},
            "runner": {"version": runner["version"], "sha256": runner["sha256"], "pinned": suite.runner is not None,
                       "evaluated_commit": suite.evaluated_commit},
            "protocol": protocol,
            "plan": build_plan(suite, arm, k=k, attempts=attempts, replacements=replacements, seed=seed),
            "environment": {"python": sys.version.split()[0] + (" free-threaded" if _free_threaded() else ""),
                            "platform": platform.platform()},
            "started_at": now(),
            "finished_at": None,
            "trials": [],
        }
    by_arm = {arm.name: arm for arm, _ in ready}
    task_ids = [t.id for t in suite.tasks]
    planned_jobs = schedule(task_ids, [arm.name for arm, _ in ready], attempts, seed)
    done: dict = {}
    executor = ThreadPoolExecutor(max_workers=max(1, jobs))
    try:
        futures = [(job, executor.submit(run_attempt, contexts[job[0]], by_arm[job[0]], suite.task(job[1]), job[2]))
                   for job in planned_jobs]
        for job, future in futures:
            done[job] = future.result()
        budget = {(a, t): replacements for a in by_arm for t in task_ids}
        next_index = {(a, t): attempts + 1 for a in by_arm for t in task_ids}
        pending = [done[job] | {"_job": job} for job in planned_jobs
                   if done[job]["error"]["tier"] in ("infra", "grader")]
        while pending:
            batch = []
            for failed in pending:
                arm_name, task_id, _ = failed["_job"]
                key = (arm_name, task_id)
                if budget[key] <= 0:
                    continue
                budget[key] -= 1
                index = next_index[key]
                next_index[key] += 1
                job = (arm_name, task_id, index)
                batch.append((job, executor.submit(run_attempt, contexts[arm_name], by_arm[arm_name],
                                                   suite.task(task_id), index, planned=False,
                                                   replacement_for=failed["trial_id"])))
            pending = []
            for job, future in batch:
                done[job] = future.result()
                if done[job]["error"]["tier"] in ("infra", "grader"):
                    pending.append(done[job] | {"_job": job})
    except KeyboardInterrupt:
        result.interrupted = True
        executor.shutdown(wait=True, cancel_futures=True)
    finally:
        executor.shutdown(wait=True)
    for arm_name, record in records.items():
        trials = [trial for (a, _, _), trial in done.items() if a == arm_name]
        ran = {(t["task_id"], t["attempt"]["index"]) for t in trials}
        for task_id in task_ids:
            for index in range(1, attempts + 1):
                if (task_id, index) not in ran:
                    trials.append(not_run_trial(f"{record['record_id']}.{task_id}.{index}", task_id, index))
        trials.sort(key=lambda t: (task_ids.index(t["task_id"]), t["attempt"]["index"]))
        record["trials"] = trials
        record["finished_at"] = now()
        record_mod.require_valid(record)
        target = Path(out_dir) / suite.id
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{record['record_id']}.json"
        path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        result.records[arm_name] = path
        log(f"record: {path}")
    return result


def _free_threaded() -> bool:
    check = getattr(sys, "_is_gil_enabled", None)
    return bool(check is not None and not check())


def load_suite(name: str, tasks: str | None):
    if name == "dev":
        if tasks:
            raise RunRefused("--tasks is for held-out suites; the dev suite is the one in the repository")
        heldout.declare("dev")
        return suite_mod.load_dev(DEV_SUITE)
    path = tasks or os.environ.get(TASKS_ENV)
    if not path:
        raise RunRefused(f"suite {name!r} is held out: give its folder at run time with --tasks or {TASKS_ENV}")
    heldout.declare("score")
    loaded = heldout.open_suite(path, purpose="score")
    if loaded.id != name:
        raise RunRefused(f"the folder holds suite {loaded.id!r}, not {name!r}")
    return loaded


def make_arms(names: list, suite, args) -> list:
    pinned = dict(suite.arms.get(args.engine) or {})
    overrides = {"model": args.model, "effort": args.effort, "permission_mode": args.permission_mode}
    if suite.frozen and any(v is not None for v in overrides.values()):
        raise RunRefused("a frozen suite pins its arm settings; drop --model, --effort and --permission-mode")
    settings = {**pinned, **{k: v for k, v in overrides.items() if v is not None}}
    out = []
    for name in names:
        if name == "direct":
            claude = arms_mod.ClaudeCodeSettings(
                binary=args.claude_binary, permission_mode=settings.get("permission_mode"),
                extra_args=list(settings.get("extra_args") or []), home=args.claude_home,
                pass_env=list(args.pass_env or []))
            out.append(arms_mod.DirectArm(args.engine, model=settings.get("model"), route=settings.get("route"),
                                          effort=settings.get("effort"), claude=claude))
        elif name == "recursion":
            out.append(arms_mod.RecursionArm(args.engine, model=settings.get("model"), route=settings.get("route"),
                                             effort=settings.get("effort")))
        else:
            raise RunRefused(f"unknown arm {name!r} (arms: direct, recursion)")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="eval/run", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--suite", required=True, help="dev, or the id of a held-out suite given with --tasks")
    parser.add_argument("--tasks", help=f"the held-out suite's folder (or {TASKS_ENV}); never inside the repository")
    parser.add_argument("--arms", default="direct,recursion")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--attempts", type=int, help="planned attempts per task (default: k)")
    parser.add_argument("--replacements", type=int, default=2,
                        help="extra trials per task for infra or grader failures (valid-runs score only)")
    parser.add_argument("--engine", default="standin", choices=arms_mod.ENGINES)
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--permission-mode")
    parser.add_argument("--claude-binary", default="claude")
    parser.add_argument("--claude-home", help="the route home Claude Code is signed in on (the founder's step)")
    parser.add_argument("--pass-env", action="append", help="name of a variable to pass to the engine")
    parser.add_argument("--label", help="dev, screen, pilot or full (default: dev for the dev suite, else pilot)")
    parser.add_argument("--seed", type=int, default=20261126)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--artifacts", type=Path, help="keep each trial's diff, test log and engine output here")
    parser.add_argument("--tmp-base", type=Path, default=scratch_mod.DEFAULT_TMP_BASE)
    parser.add_argument("--xdg-base", type=Path, default=scratch_mod.DEFAULT_XDG_BASE)
    args = parser.parse_args(argv)
    try:
        suite = load_suite(args.suite, args.tasks)
        arms = make_arms([a.strip() for a in args.arms.split(",") if a.strip()], suite, args)
        label = args.label or ("dev" if suite.split == "dev" else "pilot")
        outcome = run(suite, arms, k=args.k, attempts=args.attempts, replacements=args.replacements, label=label,
                      seed=args.seed, jobs=args.jobs, out_dir=args.out, artifacts=args.artifacts,
                      tmp_base=args.tmp_base, xdg_base=args.xdg_base)
    except (RunRefused, heldout.HeldOutDenied, suite_mod.SuiteError, scratch_mod.ScratchError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if outcome.interrupted:
        return 130
    return 3 if outcome.refused else 0


if __name__ == "__main__":
    sys.exit(main())
