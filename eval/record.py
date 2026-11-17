"""The ``recursion-eval-run/1`` record: one per suite run of one arm, every trial in it.

The JSON Schema is eval/schema/recursion-eval-run-1.json; ``validate`` below checks the same rules
without a third-party validator (the free-threaded interpreter has none) and adds the rules a schema
cannot state across fields. A test checks the two agree where ``jsonschema`` is installed.

Rules (PROTOCOL.md, "The record"):
* every planned trial has a row, a trial that never ran included (class ``not_run``);
* an unknown value is null and is counted as unknown, never 0;
* no aggregate is stored: every score is recomputed from the trials (eval/analyze.py);
* verdict and tier agree: ``resolved`` has tier ``none``; ``unresolved`` is the arm's failure
  (``agent``) unless the engine reported a typed vendor limit or sign-in failure (``infra``);
  ``not_graded`` is ``infra``, ``grader`` or ``unknown``;
* a replacement trial replaces one earlier infra or grader failure of the same task, once, within the
  plan's per-task budget, at the next index after the planned attempts (so no two trials of a task
  share an index, and no trial replaces itself);
* a label claims only what the run is: ``dev`` is the dev set and only it; ``screen``, ``pilot`` and
  ``full`` need k of at least 5, a real engine and a frozen suite;
* every number is finite: NaN and infinity are refused, in the checks and in the JSON read and written.

``arm.settings_sha256`` is the hash of the settings BOTH arms must share (engine, model, route, effort,
permission mode, extra engine arguments; eval/arms.py), not of the arm's name or transport, so two arms
of one comparison carry the same value.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

SCHEMA_ID = "recursion-eval-run/1"

# dev: the in-repository dev set. shakedown: any run that is not a measurement (a stand-in run on a
# held-out suite, a harvest check). screen, pilot, full: measurements (PROTOCOL.md section 7).
LABELS = ("dev", "shakedown", "screen", "pilot", "full")
MEASURED_LABELS = ("screen", "pilot", "full")
FROZEN_LABELS = ("screen", "pilot", "full")     # every measurement runs a frozen suite
MIN_MEASURED_K = 5
ORACLE_ENGINES = ("standin",)
REPLACEABLE_TIERS = ("infra", "grader")
SPLITS = ("dev", "held_out")
ARMS = ("direct", "recursion")
VERDICTS = ("resolved", "unresolved", "not_graded")
TIERS = ("none", "agent", "infra", "grader", "unknown")
COST_BASES = ("billed", "reported_not_billed", "unknown")
USAGE_SOURCES = ("engine_output", "usage_events")

# Error classes by tier. A class belongs to exactly one tier.
CLASSES = {
    "agent": ("tests_failed", "timeout", "engine_crash", "no_result", "engine_error"),
    "infra": ("setup_failed", "start_tree_mismatch", "engine_missing", "vendor_limit", "auth_failed"),
    "grader": ("grader_failed", "no_tests_ran"),
    "unknown": ("not_run", "unclassified"),
}
CLASS_TIER = {cls: tier for tier, classes in CLASSES.items() for cls in classes}

SHA256 = re.compile(r"^[0-9a-f]{64}$")
TOKEN_FIELDS = ("uncached_input", "cache_write", "cache_read", "output")


def canonical_json(value) -> bytes:
    """Sorted keys, no spaces, UTF-8: the bytes every *_sha256 of a JSON value is taken over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_json(value) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


class _Errors:
    def __init__(self) -> None:
        self.items: list = []

    def add(self, where: str, message: str) -> None:
        self.items.append(f"{where}: {message}")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _hashable(value) -> bool:
    """True when ``value`` can be a dict key. A list or dict id is already an error; membership must not raise."""
    try:
        hash(value)
    except TypeError:
        return False
    return True


def _is_number(value) -> bool:
    """A finite int or float; NaN and infinity are not measurements.

    An int that does not fit in a float (``10**400``) makes ``math.isfinite`` raise OverflowError.
    That is a bad measurement, reported by the caller, not a crash of validation.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _object(errors: _Errors, where: str, value, required: tuple, optional: tuple = ()) -> bool:
    if not isinstance(value, dict):
        errors.add(where, "must be an object")
        return False
    missing = [k for k in required if k not in value]
    extra = [k for k in value if k not in required and k not in optional]
    if missing:
        errors.add(where, f"missing {missing}")
    if extra:
        errors.add(where, f"unknown fields {extra}")
    return not missing


def _str(errors, where, value, *, nullable=False, choices=None, pattern=None) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value:
        errors.add(where, "must be a non-empty string" + (" or null" if nullable else ""))
        return
    if choices is not None and value not in choices:
        errors.add(where, f"must be one of {list(choices)}, got {value!r}")
    if pattern is not None and not pattern.match(value):
        errors.add(where, f"does not match {pattern.pattern}")


def _count(errors, where, value, *, nullable=True) -> None:
    if value is None and nullable:
        return
    if not _is_int(value) or value < 0:
        errors.add(where, "must be a non-negative integer" + (" or null" if nullable else ""))


def _seconds(errors, where, value) -> None:
    if value is None:
        return
    if not _is_number(value) or value < 0:
        errors.add(where, "must be a non-negative number or null")


def _bool(errors, where, value, *, nullable=False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, bool):
        errors.add(where, "must be true or false" + (" or null" if nullable else ""))


def _trial(errors: _Errors, where: str, trial) -> None:
    keys = ("trial_id", "task_id", "attempt", "run_id", "grade", "error", "engine", "usage", "timing",
            "artifacts", "scratch")
    if not _object(errors, where, trial, keys):
        return
    _str(errors, f"{where}.trial_id", trial["trial_id"])
    _str(errors, f"{where}.task_id", trial["task_id"])
    _str(errors, f"{where}.run_id", trial["run_id"], nullable=True)

    attempt = trial["attempt"]
    if _object(errors, f"{where}.attempt", attempt, ("index", "planned", "replacement_for")):
        if not _is_int(attempt["index"]) or attempt["index"] < 1:
            errors.add(f"{where}.attempt.index", "must be an integer of at least 1")
        _bool(errors, f"{where}.attempt.planned", attempt["planned"])
        _str(errors, f"{where}.attempt.replacement_for", attempt["replacement_for"], nullable=True)
        if attempt["planned"] is True and attempt["replacement_for"] is not None:
            errors.add(f"{where}.attempt", "a planned trial replaces nothing")
        if attempt["planned"] is False and attempt["replacement_for"] is None:
            errors.add(f"{where}.attempt", "an unplanned trial must name the trial it replaces")

    grade = trial["grade"]
    if _object(errors, f"{where}.grade", grade, ("verdict", "grader_id", "grader_sha256", "details_sha256", "exit")):
        _str(errors, f"{where}.grade.verdict", grade["verdict"], choices=VERDICTS)
        _str(errors, f"{where}.grade.grader_id", grade["grader_id"], nullable=True)
        _str(errors, f"{where}.grade.grader_sha256", grade["grader_sha256"], nullable=True, pattern=SHA256)
        _str(errors, f"{where}.grade.details_sha256", grade["details_sha256"], nullable=True, pattern=SHA256)
        if grade["exit"] is not None and not _is_int(grade["exit"]):
            errors.add(f"{where}.grade.exit", "must be an integer or null")

    error = trial["error"]
    if _object(errors, f"{where}.error", error, ("class", "tier", "detail")):
        _str(errors, f"{where}.error.tier", error["tier"], choices=TIERS)
        _str(errors, f"{where}.error.class", error["class"], nullable=True, choices=tuple(CLASS_TIER))
        _str(errors, f"{where}.error.detail", error["detail"], nullable=True)
        tier, cls = error["tier"], error["class"]
        if tier == "none" and cls is not None:
            errors.add(f"{where}.error", "tier none carries no class")
        if tier != "none" and cls is None:
            errors.add(f"{where}.error", f"tier {tier} needs a class")
        if cls is not None and isinstance(cls, str) and cls in CLASS_TIER and CLASS_TIER[cls] != tier:
            errors.add(f"{where}.error", f"class {cls} belongs to tier {CLASS_TIER[cls]}, not {tier}")

    if isinstance(grade, dict) and isinstance(error, dict):
        verdict, tier = grade.get("verdict"), error.get("tier")
        allowed = {"resolved": ("none",), "unresolved": ("agent", "infra"),
                   "not_graded": ("infra", "grader", "unknown")}.get(verdict)
        if allowed is not None and tier not in allowed:
            errors.add(where, f"verdict {verdict} cannot have tier {tier}")
        if verdict == "unresolved" and tier == "infra" and error.get("class") not in ("vendor_limit", "auth_failed"):
            errors.add(where, "an unresolved trial is infra only for a typed vendor limit or sign-in failure")

    engine = trial["engine"]
    if _object(errors, f"{where}.engine", engine, ("exit", "signal", "timed_out", "claimed_done")):
        for key in ("exit", "signal"):
            if engine[key] is not None and not _is_int(engine[key]):
                errors.add(f"{where}.engine.{key}", "must be an integer or null")
        _bool(errors, f"{where}.engine.timed_out", engine["timed_out"])
        _bool(errors, f"{where}.engine.claimed_done", engine["claimed_done"], nullable=True)

    usage = trial["usage"]
    if _object(errors, f"{where}.usage", usage, (*TOKEN_FIELDS, "cost", "cost_unit", "cost_basis", "source")):
        for key in TOKEN_FIELDS:
            _count(errors, f"{where}.usage.{key}", usage[key])
        if usage["cost"] is not None and (not _is_number(usage["cost"]) or usage["cost"] < 0):
            errors.add(f"{where}.usage.cost", "must be a non-negative number or null")
        _str(errors, f"{where}.usage.cost_unit", usage["cost_unit"], nullable=True)
        _str(errors, f"{where}.usage.cost_basis", usage["cost_basis"], choices=COST_BASES)
        _str(errors, f"{where}.usage.source", usage["source"], nullable=True, choices=USAGE_SOURCES)
        if usage["cost"] is not None and usage["cost_unit"] is None:
            errors.add(f"{where}.usage", "a cost needs its unit")
        if usage["cost"] is None and usage["cost_basis"] != "unknown":
            errors.add(f"{where}.usage", "an unknown cost has cost_basis unknown")

    timing = trial["timing"]
    if _object(errors, f"{where}.timing", timing, ("wall_seconds", "engine_seconds", "grading_seconds")):
        for key in ("wall_seconds", "engine_seconds", "grading_seconds"):
            _seconds(errors, f"{where}.timing.{key}", timing[key])

    artifacts = trial["artifacts"]
    akeys = ("patch_sha256", "patch_bytes", "patch_empty", "files_changed", "test_log_sha256",
             "trajectory_sha256", "trajectory_format", "retained", "artifact_error")
    if _object(errors, f"{where}.artifacts", artifacts, akeys):
        for key in ("patch_sha256", "test_log_sha256", "trajectory_sha256"):
            _str(errors, f"{where}.artifacts.{key}", artifacts[key], nullable=True, pattern=SHA256)
        _count(errors, f"{where}.artifacts.patch_bytes", artifacts["patch_bytes"])
        _count(errors, f"{where}.artifacts.files_changed", artifacts["files_changed"])
        _bool(errors, f"{where}.artifacts.patch_empty", artifacts["patch_empty"], nullable=True)
        _str(errors, f"{where}.artifacts.trajectory_format", artifacts["trajectory_format"], nullable=True)
        _bool(errors, f"{where}.artifacts.retained", artifacts["retained"])
        _str(errors, f"{where}.artifacts.artifact_error", artifacts["artifact_error"], nullable=True)
        if artifacts["retained"] is True and artifacts["artifact_error"] is not None:
            errors.add(f"{where}.artifacts", "retained artifacts carry no artifact_error")

    scratch = trial["scratch"]
    if _object(errors, f"{where}.scratch", scratch, ("tmpdir", "xdg_runtime_dir", "removed")):
        _str(errors, f"{where}.scratch.tmpdir", scratch["tmpdir"], nullable=True)
        _str(errors, f"{where}.scratch.xdg_runtime_dir", scratch["xdg_runtime_dir"], nullable=True)
        _bool(errors, f"{where}.scratch.removed", scratch["removed"], nullable=True)


def validate(record) -> list:
    """Every rule the record breaks, as 'path: message' lines. An empty list means valid."""
    errors = _Errors()
    keys = ("schema", "record_id", "label", "suite", "arm", "runner", "protocol", "plan", "environment",
            "started_at", "finished_at", "trials")
    if not _object(errors, "record", record, keys):
        return errors.items
    if record["schema"] != SCHEMA_ID:
        errors.add("record.schema", f"must be {SCHEMA_ID!r}")
    _str(errors, "record.record_id", record["record_id"])
    _str(errors, "record.label", record["label"], choices=LABELS)
    _str(errors, "record.started_at", record["started_at"])
    _str(errors, "record.finished_at", record["finished_at"], nullable=True)

    suite = record["suite"]
    if _object(errors, "record.suite", suite, ("id", "version", "split", "frozen", "manifest_sha256")):
        _str(errors, "record.suite.id", suite["id"])
        _str(errors, "record.suite.version", suite["version"])
        _str(errors, "record.suite.split", suite["split"], choices=SPLITS)
        _bool(errors, "record.suite.frozen", suite["frozen"])
        _str(errors, "record.suite.manifest_sha256", suite["manifest_sha256"], pattern=SHA256)

    arm = record["arm"]
    if _object(errors, "record.arm", arm, ("name", "engine", "engine_version", "model", "route", "effort",
                                           "settings_sha256")):
        _str(errors, "record.arm.name", arm["name"], choices=ARMS)
        _str(errors, "record.arm.engine", arm["engine"])
        for key in ("engine_version", "model", "route", "effort"):
            _str(errors, f"record.arm.{key}", arm[key], nullable=True)
        _str(errors, "record.arm.settings_sha256", arm["settings_sha256"], pattern=SHA256)

    runner = record["runner"]
    if _object(errors, "record.runner", runner, ("version", "sha256", "pinned", "evaluated_commit")):
        _str(errors, "record.runner.version", runner["version"])
        _str(errors, "record.runner.sha256", runner["sha256"], pattern=SHA256)
        _bool(errors, "record.runner.pinned", runner["pinned"])
        _str(errors, "record.runner.evaluated_commit", runner["evaluated_commit"], nullable=True)

    protocol = record["protocol"]
    if _object(errors, "record.protocol", protocol, ("protocol_sha256", "analysis_sha256")):
        _str(errors, "record.protocol.protocol_sha256", protocol["protocol_sha256"], pattern=SHA256)
        _str(errors, "record.protocol.analysis_sha256", protocol["analysis_sha256"], pattern=SHA256)

    plan = record["plan"]
    planned_tasks: dict = {}
    if _object(errors, "record.plan", plan, ("k", "attempts_per_task", "replacements_per_task", "seed", "tasks",
                                             "plan_sha256")):
        for key in ("k", "attempts_per_task"):
            if not _is_int(plan[key]) or plan[key] < 1:
                errors.add(f"record.plan.{key}", "must be an integer of at least 1")
        _count(errors, "record.plan.replacements_per_task", plan["replacements_per_task"], nullable=False)
        if not _is_int(plan["seed"]):
            errors.add("record.plan.seed", "must be an integer")
        _str(errors, "record.plan.plan_sha256", plan["plan_sha256"], pattern=SHA256)
        if not isinstance(plan["tasks"], list) or not plan["tasks"]:
            errors.add("record.plan.tasks", "must be a non-empty list")
        else:
            for i, task in enumerate(plan["tasks"]):
                where = f"record.plan.tasks[{i}]"
                if _object(errors, where, task, ("id", "cluster", "task_sha256", "created_at")):
                    _str(errors, f"{where}.id", task["id"])
                    _str(errors, f"{where}.cluster", task["cluster"], nullable=True)
                    _str(errors, f"{where}.task_sha256", task["task_sha256"], pattern=SHA256)
                    _str(errors, f"{where}.created_at", task["created_at"], nullable=True)
                    if _hashable(task["id"]):
                        if task["id"] in planned_tasks:
                            errors.add(where, f"duplicate task id {task['id']!r}")
                        else:
                            planned_tasks[task["id"]] = task
        if _is_int(plan.get("k")) and _is_int(plan.get("attempts_per_task")) and plan["attempts_per_task"] < plan["k"]:
            errors.add("record.plan", "attempts_per_task below k: pass^k would be not estimable for every task")
        if not errors.items:
            body = {key: plan[key] for key in plan if key != "plan_sha256"}
            body["suite_manifest_sha256"] = record["suite"]["manifest_sha256"]
            body["arm_settings_sha256"] = record["arm"]["settings_sha256"]
            if sha256_json(body) != plan["plan_sha256"]:
                errors.add("record.plan.plan_sha256", "does not match the plan it is stored with")

    env = record["environment"]
    if _object(errors, "record.environment", env, ("python", "platform")):
        _str(errors, "record.environment.python", env["python"])
        _str(errors, "record.environment.platform", env["platform"])

    _labels(errors, record)
    trials = record["trials"]
    if not isinstance(trials, list):
        errors.add("record.trials", "must be a list")
        return errors.items
    # Two trials of one task never share an index: the planned indexes must be exactly 1..attempts and the
    # replacement indexes exactly the next ones (_replacements), so a shared index fails one of those.
    by_id: dict = {}
    planned_index: dict = {}
    for i, trial in enumerate(trials):
        where = f"record.trials[{i}]"
        _trial(errors, where, trial)
        if not isinstance(trial, dict):
            continue
        tid = trial.get("trial_id")
        if _hashable(tid):
            if tid in by_id:
                errors.add(where, f"duplicate trial_id {tid!r}")
            else:
                by_id[tid] = trial
        task_id = trial.get("task_id")
        if planned_tasks and task_id not in planned_tasks:
            errors.add(where, f"task {task_id!r} is not in the plan")
        attempt = trial.get("attempt")
        if isinstance(attempt, dict) and attempt.get("planned") is True:
            planned_index.setdefault(task_id, []).append(attempt.get("index"))
    _replacements(errors, trials, by_id, plan)
    if isinstance(plan, dict) and _is_int(plan.get("attempts_per_task")):
        # ``list(range(1, n + 1))`` raises OverflowError, or allocates the whole plan, when n is
        # astronomical (``10**100``, ``10**400``). The indexes on hand are enough to see the mismatch.
        n = plan["attempts_per_task"]
        bounded = 1 <= n <= 10000
        expected = list(range(1, n + 1)) if bounded else None
        for task_id in planned_tasks:
            got = sorted(i for i in planned_index.get(task_id, []) if _is_int(i))
            if bounded:
                mismatch = got != expected
                shown = expected
            else:
                mismatch = len(got) != n or got != list(range(1, len(got) + 1))
                shown = f"1..{n}"
            if mismatch:
                errors.add("record.trials", f"task {task_id!r} has planned attempts {got}, the plan says {shown}")
    return errors.items


def _replacements(errors: _Errors, trials: list, by_id: dict, plan) -> None:
    """A replacement trial replaces one earlier infra or grader failure of the same task, once, within
    the plan's per-task budget, at the next index after the planned attempts (PROTOCOL.md section 3)."""
    attempts = plan.get("attempts_per_task") if isinstance(plan, dict) else None
    budget = plan.get("replacements_per_task") if isinstance(plan, dict) else None
    replaced: dict = {}
    indexes: dict = {}
    for trial in trials:
        if not isinstance(trial, dict) or not isinstance(trial.get("attempt"), dict):
            continue
        ref = trial["attempt"].get("replacement_for")
        if ref is None:
            continue
        tid, task_id, index = trial.get("trial_id"), trial.get("task_id"), trial["attempt"].get("index")
        where = f"record.trials[{tid!r}]"
        indexes.setdefault(task_id, []).append(index)
        original = by_id.get(ref)   # a trial naming itself fails the index-order rule below
        if original is None:
            errors.add(where, f"replaces unknown trial {ref!r}")
            continue
        if original.get("task_id") != task_id:
            errors.add(where, f"replaces {ref!r}, a trial of another task")
        tier = (original.get("error") or {}).get("tier") if isinstance(original.get("error"), dict) else None
        if tier not in REPLACEABLE_TIERS:
            errors.add(where, f"replaces {ref!r} of tier {tier!r}; only infra and grader failures are replaced")
        if ref in replaced:
            errors.add(where, f"{ref!r} is already replaced by {replaced[ref]!r}")
        replaced[ref] = tid
        before = (original.get("attempt") or {}).get("index") if isinstance(original.get("attempt"), dict) else None
        if _is_int(index) and _is_int(before) and index <= before:
            errors.add(where, f"index {index} does not come after the replaced trial's index {before}")
    # PROTOCOL.md: at most 2 per task and arm, even when a record names a higher budget.
    if _is_int(budget) and budget > 2:
        errors.add("record.plan.replacements_per_task",
                   "the protocol allows at most 2 replacements per task and arm")
    if not (_is_int(attempts) and _is_int(budget)):
        return
    for task_id, got in indexes.items():
        if len(got) > budget:
            errors.add("record.trials", f"task {task_id!r} has {len(got)} replacement trials, the plan allows {budget}")
        expected = list(range(attempts + 1, attempts + 1 + len(got)))
        if sorted(i for i in got if _is_int(i)) != expected:
            errors.add("record.trials", f"task {task_id!r} has replacement indexes {sorted(got, key=str)}, "
                                        f"expected {expected}")


def _labels(errors: _Errors, record: dict) -> None:
    """What a label may claim (PROTOCOL.md section 7)."""
    label, suite, plan, arm = record.get("label"), record.get("suite"), record.get("plan"), record.get("arm")
    if not (isinstance(suite, dict) and isinstance(plan, dict) and isinstance(arm, dict)):
        return
    split = suite.get("split")
    if (label == "dev") != (split == "dev"):
        errors.add("record.label", f"label {label!r} with split {split!r}: the dev set is labelled dev, and only it")
    if label in MEASURED_LABELS:
        if _is_int(plan.get("k")) and plan["k"] < MIN_MEASURED_K:
            errors.add("record.label", f"a {label} needs k of at least {MIN_MEASURED_K}, the plan has {plan['k']}")
        if arm.get("engine") in ORACLE_ENGINES:
            errors.add("record.label", f"a {label} is never made with the stand-in engine (an oracle)")
    if label in FROZEN_LABELS and suite.get("frozen") is not True:
        errors.add("record.label", f"a {label} runs a frozen suite")


def require_valid(record) -> dict:
    problems = validate(record)
    if problems:
        raise ValueError("invalid recursion-eval-run/1 record:\n  " + "\n  ".join(problems))
    return record


def _refuse_constant(name):
    raise ValueError(f"non-standard JSON constant {name} (NaN and Infinity are not measurements)")


def loads(text: str):
    return json.loads(text, parse_constant=_refuse_constant)


def dumps(record: dict) -> str:
    """The record's file text: sorted keys, and never NaN or Infinity."""
    return json.dumps(record, indent=1, sort_keys=True, allow_nan=False) + "\n"


def load(path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return require_valid(loads(handle.read()))


def empty_usage() -> dict:
    """Usage with every value unknown: null, never 0."""
    return {"uncached_input": None, "cache_write": None, "cache_read": None, "output": None,
            "cost": None, "cost_unit": None, "cost_basis": "unknown", "source": None}
