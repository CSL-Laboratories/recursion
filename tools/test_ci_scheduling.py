#!/usr/bin/env python3
"""CSL-255: the split CI workflow allocates the paid Linux runner only when it should.

Stdlib only. Job ``if:`` expressions and shell steps are extracted from ci.yml by
layout (an unsupported layout fails; this is not a YAML parser), evaluated with
GitHub semantics (case-insensitive ==, status functions, implicit success()),
and compared with an oracle written from the approved semantics instead of
copied from the workflow. Shell steps run under bash, one process per matrix.
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import itertools
import os
import re
import shlex
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TEXT = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
PR_ACTIONS = ("opened", "synchronize", "reopened", "ready_for_review")
RESULTS = ("success", "failure", "cancelled", "skipped")
SHELL_RESULTS = RESULTS + ("", "pending")
STATES = [("pull_request", a, d) for a in PR_ACTIONS for d in (True, False)]
STATES += [("push", None, None), ("workflow_dispatch", None, None)]
TRIGGERS = (
    "on:\n  pull_request:\n    types: [opened, synchronize, reopened, ready_for_review]\n"
    "  push:\n    branches: [master]\n  workflow_dispatch:\n"
)
CONCURRENCY = (
    "concurrency:\n  group: ci-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}\n"
    "  cancel-in-progress: true\n"
)


# --- targeted extraction (layout-based; fails loudly on anything else) ---------
def job_block(job: str) -> str:
    match = re.search(rf"(?ms)^  {re.escape(job)}:\n(.*?)(?=^  \S|\Z)", TEXT)
    if not match:
        raise AssertionError(f"job {job!r} not found in ci.yml")
    return match.group(1)


def job_if(block: str) -> str:
    match = re.search(r"(?m)^    if: (.*)$", block)
    if not match:
        raise AssertionError("job has no if:")
    if match.group(1).strip() != ">":
        return match.group(1).strip()
    lines = []
    for line in block[match.end() + 1:].splitlines():
        if not line.startswith("      "):
            break
        lines.append(line.strip())
    return " ".join(lines)


def job_needs(block: str) -> list[str]:
    match = re.search(r"(?m)^    needs: \[(.*)\]$", block)
    if not match:
        raise AssertionError("needs must be an inline list")
    return [name.strip() for name in match.group(1).split(",")]


def step_block(block: str, name: str) -> str:
    match = re.search(rf"(?ms)^      - name: {re.escape(name)}\n(.*?)(?=^      - |\Z)", block)
    if not match:
        raise AssertionError(f"step {name!r} not found")
    return match.group(1)


def step_run(step: str) -> str:
    match = re.search(r"(?m)^        run: (.*)$", step)
    if not match:
        raise AssertionError("step has no run:")
    if match.group(1).strip() != "|":
        return match.group(1).strip()
    lines = []
    for line in step[match.end() + 1:].splitlines():
        if line.startswith("          "):
            lines.append(line[10:])
        elif not line.strip():
            lines.append("")
        else:
            break
    return "\n".join(lines).rstrip("\n") + "\n"


def step_env(step: str) -> dict[str, str]:
    match = re.search(r"(?m)^        env:\n", step)
    if not match:
        raise AssertionError("step has no env:")
    env: dict[str, str] = {}
    for line in step[match.end():].splitlines():
        entry = re.match(r"^          (\w+): (.*)$", line)
        if not entry:
            break
        env[entry.group(1)] = entry.group(2).strip()
    return env


# --- constrained GitHub expression evaluator -----------------------------------
_TOKEN = re.compile(
    r"\s*(?:(&&|\|\||==|!=|!|\(|\)|,)|'((?:[^']|'')*)'|([A-Za-z_][\w-]*(?:\.(?:[A-Za-z_][\w-]*|\*))*)|(\S))"
)
_OPS = {"&&": " and ", "||": " or ", "!": " not ", "==": " == ", "!=": " != ", "(": "(", ")": ")", ",": ", "}
_WORDS = {"true": "True", "false": "False", "null": "None"}
STATUS_FUNCTIONS = {"always", "cancelled", "success", "failure"}
FUNCTIONS = STATUS_FUNCTIONS | {"contains"}
_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Call, ast.Name, ast.Constant, ast.Load)


class _Compare(ast.NodeTransformer):
    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if len(node.ops) != 1 or not isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
            raise AssertionError("only == and != comparisons are supported")
        name = "eq" if isinstance(node.ops[0], ast.Eq) else "ne"
        return ast.Call(ast.Name(name, ast.Load()), [node.left, node.comparators[0]], [])


def _coerce(value) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if value is None:
        return 0.0
    if isinstance(value, str):
        try:
            return float(value) if value.strip() else 0.0
        except ValueError:
            return float("nan")
    return float(value)


def _eq(left, right) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    return _coerce(left) == _coerce(right)


def _contains(haystack, needle) -> bool:
    if haystack is None:
        return False
    if isinstance(haystack, list):
        return any(_eq(item, needle) for item in haystack)
    return str(needle).casefold() in str(haystack).casefold()


class Expression:
    """A job-level ``if:`` translated to a constrained Python AST and evaluated with GitHub rules."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.needs_refs = set(re.findall(r"needs\.([\w-]+)\.", text))
        source, self.uses_status = self._translate(text)
        tree = _Compare().visit(ast.parse(source, mode="eval"))
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED):
                raise AssertionError(f"unsupported syntax {type(node).__name__} in {text!r}")
        self.code = compile(ast.fix_missing_locations(tree), "<if>", "eval")

    @staticmethod
    def _translate(text: str) -> tuple[str, bool]:
        parts = list(_TOKEN.finditer(text))
        if sum(len(m.group(0)) for m in parts) != len(text.rstrip()):
            raise AssertionError(f"could not tokenise {text!r}")
        out, uses_status = [], False
        for index, match in enumerate(parts):
            op, literal, path, other = match.groups()
            nxt = parts[index + 1] if index + 1 < len(parts) else None
            if other:
                raise AssertionError(f"unsupported token {other!r} in {text!r}")
            if op:
                if op == "!" and not (nxt and (nxt.group(1) == "(" or nxt.group(3) in FUNCTIONS)):
                    raise AssertionError("! must negate a function call or a parenthesised group")
                out.append(_OPS[op])
            elif literal is not None:
                out.append(repr(literal.replace("''", "'")))
            elif path in _WORDS:
                out.append(_WORDS[path])
            elif nxt and nxt.group(1) == "(":
                if path not in FUNCTIONS:
                    raise AssertionError(f"unsupported function {path}()")
                uses_status = uses_status or path in STATUS_FUNCTIONS
                out.append(f"fn_{path}")
            else:
                out.append(f"ctx({path!r})")
        return "".join(out).strip(), uses_status

    def runs(self, ctx: dict, needs: list[str], cancelled: bool) -> bool:
        all_ok = not cancelled and all(ctx[f"needs.{n}.result"] == "success" for n in needs)
        if not self.uses_status and not all_ok:
            return False  # implicit success() when no status function is present
        names = {
            "ctx": ctx.__getitem__, "eq": _eq, "ne": lambda a, b: not _eq(a, b),
            "fn_always": lambda: True, "fn_cancelled": lambda: cancelled, "fn_success": lambda: all_ok,
            "fn_failure": lambda: any(ctx[f"needs.{n}.result"] == "failure" for n in needs),
            "fn_contains": _contains,
        }
        return bool(eval(self.code, {"__builtins__": {}}, names))


def context(event, action, draft, label, pre, settle, dep, run_full, paid) -> dict:
    pr = event == "pull_request"
    return {
        "github.event_name": event,
        "github.event.action": action if pr else None,
        "github.event.pull_request.draft": draft if pr else None,
        "github.event.pull_request.labels.*.name": (["ci:full"] if label else ["bug"]) if pr else None,
        "needs.preflight.result": pre,
        "needs.settle.result": settle,
        "needs.linux-tests.result": dep,
        "needs.linux.result": dep,
        "needs.preflight.outputs.run_full": run_full,
        "needs.preflight.outputs.paid": paid,
    }


# --- oracle: approved semantics, independent of the expressions under test ------
def pr_sync(event, action) -> bool:
    return event == "pull_request" and action == "synchronize"


def expected_settle(event, action) -> str:
    return "success" if pr_sync(event, action) else "skipped"


def oracle(job, event, action, draft, label, cancelled, pre, settle, dep, run_full, paid) -> bool:
    pr = event == "pull_request"
    if job == "linux":
        return True  # always runs; the shell fails closed
    if cancelled or pre != "success":
        return False
    if job == "settle":
        return pr_sync(event, action) and draft is False
    paid_ok = (run_full == "true" and paid == "true" and (not pr or draft is False)
               and settle == expected_settle(event, action))
    if job == "linux-tests":
        return paid_ok
    labelled = pr and draft is False and label
    if job == "windows":
        return paid_ok and dep == "success" and (event in ("push", "workflow_dispatch") or labelled)
    if job == "macos":
        return paid_ok and dep == "success" and (event == "workflow_dispatch" or labelled)
    raise AssertionError(job)


def shell_oracle(EVENT_NAME, EVENT_ACTION, PR_DRAFT, PREFLIGHT, RUN_FULL, PAID, SETTLE, TESTS) -> int:
    if EVENT_NAME == "pull_request":
        if EVENT_ACTION not in PR_ACTIONS or PR_DRAFT != "false":
            return 1
    elif EVENT_NAME not in ("push", "workflow_dispatch") or PR_DRAFT != "":
        return 1
    if PREFLIGHT != "success" or RUN_FULL not in ("true", "false") or PAID not in ("true", "false"):
        return 1
    if SETTLE != expected_settle(EVENT_NAME, EVENT_ACTION):
        return 1
    if RUN_FULL == "true":
        return 0 if PAID == "true" and TESTS == "success" else 1
    return 0 if PAID == "false" and TESTS == "skipped" else 1


# --- bash batch runner -------------------------------------------------------
def find_bash() -> str:
    for candidate in (os.environ.get("GIT_BASH"), r"C:\Program Files\Git\bin\bash.exe"):
        if candidate and Path(candidate).is_file():
            return candidate
    found = shutil.which("bash")
    if not found:
        raise AssertionError("bash not found: install Git for Windows (Git Bash) or bash to run the shell matrices")
    return found


def bash_cases(script: str, cases: list[dict[str, str]]) -> list[tuple[int, str]]:
    """Run ``script`` once per case in a single bash process; subshells isolate exit.

    The script arrives through the environment, not a heredoc inside ``$(...)``:
    macOS /bin/bash 3.2 misparses ``case`` arms and expands that heredoc body.
    """
    lines = ["set -u", "out=$(mktemp)"]
    for index, env in enumerate(cases):
        exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        lines.append(
            f': > "$out"; ( export {exports} GITHUB_OUTPUT="$out"; eval "$CI_SHELL_SCRIPT" ) >/dev/null 2>&1; '
            f"printf 'CASE {index} %s ' \"$?\"; "
            'while IFS= read -r output_line; do printf \'%s \' "$output_line"; done < "$out"; printf \'\\n\''
        )
    lines.append('rm -f "$out"')
    proc = subprocess.run(
        [find_bash(), "--noprofile", "--norc", "-s"],
        input=("\n".join(lines) + "\n").encode("utf-8"),  # bytes: keep LF for Git Bash
        env={**os.environ, "CI_SHELL_SCRIPT": script},
        capture_output=True, check=False, timeout=180,
    )
    found = {
        int(m.group(1)): (int(m.group(2)), m.group(3).strip())
        for m in re.finditer(r"(?m)^CASE (\d+) (\d+) ?(.*)$", proc.stdout.decode("utf-8", "replace"))
    }
    if proc.returncode != 0 or len(found) != len(cases):
        raise AssertionError(f"bash batch rc={proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[:2000]}")
    return [found[i] for i in range(len(cases))]


class WorkflowShapeTests(unittest.TestCase):
    def test_triggers_concurrency_permissions_unchanged(self) -> None:
        self.assertIn(TRIGGERS, TEXT)
        self.assertIn(CONCURRENCY, TEXT)
        self.assertIn("permissions:\n  contents: read\n", TEXT)

    def test_job_layout(self) -> None:
        jobs = re.findall(r"(?m)^  ([\w-]+):\n", TEXT.split("\njobs:\n", 1)[1])
        self.assertEqual(jobs, ["preflight", "settle", "linux-tests", "linux", "windows", "macos"])
        self.assertEqual(TEXT.count("name: linux"), 1)
        settle = job_block("settle")
        self.assertIn("runs-on: ubuntu-latest", settle)
        self.assertIn("timeout-minutes: 3\n", settle)
        self.assertEqual(job_needs(settle), ["preflight"])
        self.assertEqual(step_run(step_block(settle, "Let the pushed series settle")), "sleep 120")
        tests = job_block("linux-tests")
        self.assertIn("name: Linux tests\n", tests)
        self.assertIn("runs-on: ubuntu-24.04", tests)
        self.assertIn("timeout-minutes: 15\n", tests)
        self.assertEqual(job_needs(tests), ["preflight", "settle"])
        for needle in ("fetch-depth: 0", "tools/ci_gate.py --force", "tools/perf_ci.py", "::warning::"):
            self.assertIn(needle, tests)
        self.assertNotIn("--paths-only", tests)
        windows, macos = job_block("windows"), job_block("macos")
        self.assertEqual(job_needs(windows), ["preflight", "settle", "linux"])
        self.assertEqual(job_needs(macos), ["preflight", "settle", "linux"])
        self.assertIn("runs-on: windows-2025", windows)
        self.assertIn("timeout-minutes: 20\n", windows)
        self.assertIn("core.longpaths true", windows)
        self.assertIn("runs-on: macos-latest", macos)
        self.assertIn("timeout-minutes: 15\n", macos)
        pip = 'python -P -m pip install --only-binary=:all: -r "$REQUIREMENTS_CI"'
        for block in (tests, windows, macos):
            self.assertIn("working-directory: ${{ runner.temp }}", block)
            self.assertIn("REQUIREMENTS_CI: ${{ github.workspace }}/requirements-ci.txt", block)
            self.assertIn("shell: bash", block)
            self.assertIn(pip, block)
            self.assertNotIn("python -m pip install", block)
            self.assertNotIn("cache: pip", block)
            self.assertNotIn("cache-dependency-path:", block)
        for block in (windows, macos):
            self.assertIn("fetch-depth: 0", block)
            self.assertIn("tools/ci_gate.py --force", block)
        for line in TEXT.splitlines():
            if line.lstrip().startswith("run:"):
                self.assertNotIn("${{", line, "expressions reach shells via env, not interpolation")

    def test_full_matrix_pip_matches_ci_isolation(self) -> None:
        matrix = (ROOT / ".github" / "workflows" / "ci-full-matrix.yml").read_text(encoding="utf-8")
        pip = 'python -P -m pip install --only-binary=:all: -r "$REQUIREMENTS_CI"'
        self.assertEqual(matrix.count(pip), 2)
        self.assertEqual(matrix.count("working-directory: ${{ runner.temp }}"), 2)
        self.assertEqual(matrix.count("REQUIREMENTS_CI: ${{ github.workspace }}/requirements-ci.txt"), 2)
        self.assertEqual(matrix.count("shell: bash"), 2)
        self.assertNotIn("python -m pip install", matrix)
        self.assertNotIn("cache: pip", matrix)
        self.assertNotIn("cache-dependency-path:", matrix)
        for line in matrix.splitlines():
            if line.lstrip().startswith("run:"):
                self.assertNotIn("${{", line, "expressions reach shells via env, not interpolation")

    def test_pr_and_model_jobs_do_not_enable_setup_python_pip_cache(self) -> None:
        leftover = []
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "cache: pip" in line:
                    leftover.append(f"{path.name}:{number}")
        self.assertEqual(leftover, ["ci-macos-nightly.yml:71"])

    def test_only_master_nightly_keeps_checkout_cwd_pip(self) -> None:
        leftover = []
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if re.search(r"\bpython3? -m pip\b", stripped) and " -P " not in stripped:
                    leftover.append(f"{path.name}:{number}")
        self.assertEqual(leftover, ["ci-macos-nightly.yml:75"])


class ExpressionTests(unittest.TestCase):
    JOBS = ("settle", "linux-tests", "linux", "windows", "macos")

    @classmethod
    def setUpClass(cls) -> None:
        cls.exprs = {}
        for job in cls.JOBS:
            block = job_block(job)
            expr, needs = Expression(job_if(block)), job_needs(block)
            if not expr.needs_refs <= set(needs):
                raise AssertionError(f"{job} references needs outside its needs list: {expr.needs_refs - set(needs)}")
            cls.exprs[job] = (expr, needs)

    def runs(self, job, event, action=None, draft=None, label=False, cancelled=False, pre="success",
             settle=None, dep="success", run_full="true", paid="true") -> bool:
        settle = expected_settle(event, action) if settle is None else settle
        expr, needs = self.exprs[job]
        return expr.runs(context(event, action, draft, label, pre, settle, dep, run_full, paid), needs, cancelled)

    def test_if_expressions_match_oracle(self) -> None:
        mismatches, seen = [], {job: set() for job in self.JOBS}
        grid = itertools.product(STATES, (False, True), (False, True), RESULTS, RESULTS, RESULTS,
                                 ("true", "false", ""), ("true", "false", ""))
        for (event, action, draft), label, cancelled, pre, settle, dep, run_full, paid in grid:
            ctx = context(event, action, draft, label, pre, settle, dep, run_full, paid)
            for job, (expr, needs) in self.exprs.items():
                want = oracle(job, event, action, draft, label, cancelled, pre, settle, dep, run_full, paid)
                seen[job].add(want)
                if expr.runs(ctx, needs, cancelled) != want:
                    mismatches.append((job, event, action, draft, label, cancelled, pre, settle, dep, run_full, paid))
        self.assertEqual(mismatches[:5], [], f"{len(mismatches)} mismatches")
        for job, outcomes in seen.items():
            self.assertEqual(outcomes, {True} if job == "linux" else {True, False}, job)

    def test_allocation_policy(self) -> None:
        paid = ("linux-tests", "windows", "macos")
        self.assertEqual([self.runs(j, "push") for j in paid], [True, True, False])
        self.assertEqual([self.runs(j, "workflow_dispatch") for j in paid], [True, True, True])
        self.assertEqual([self.runs(j, "pull_request", "opened", False, label=True) for j in paid], [True, True, True])
        self.assertEqual([self.runs(j, "pull_request", "opened", False) for j in paid], [True, False, False])
        self.assertEqual([self.runs(j, "pull_request", "synchronize", False, label=True) for j in paid], [True] * 3)
        docs = dict(run_full="false", paid="false")
        self.assertEqual([self.runs(j, "pull_request", "opened", False, label=True, **docs) for j in paid], [False] * 3)
        self.assertTrue(self.runs("linux", "pull_request", "opened", False, **docs))
        for action in PR_ACTIONS:  # drafts never allocate, whatever preflight emitted
            self.assertFalse(self.runs("linux-tests", "pull_request", action, True, label=True, paid="false"))
            self.assertFalse(self.runs("linux-tests", "pull_request", action, True, label=True, settle="success"))
            self.assertFalse(self.runs("settle", "pull_request", action, True))
        self.assertTrue(self.runs("settle", "pull_request", "synchronize", False, run_full="false", paid="false"))
        self.assertFalse(self.runs("settle", "pull_request", "opened", False))
        self.assertFalse(self.runs("linux-tests", "push", cancelled=True))
        self.assertTrue(self.runs("linux", "push", cancelled=True, pre="cancelled"))
        self.assertFalse(self.runs("linux-tests", "pull_request", "synchronize", False, settle="failure"))
        self.assertFalse(self.runs("linux-tests", "pull_request", "synchronize", False, settle="skipped"))
        self.assertFalse(self.runs("linux-tests", "push", settle="success"))
        self.assertFalse(self.runs("linux-tests", "push", pre="skipped", run_full="", paid=""))
        self.assertFalse(self.runs("windows", "push", dep="failure"))
        # GitHub == is case-insensitive; exactness comes from the preflight shell (tested below).
        self.assertTrue(self.runs("linux-tests", "push", paid="TRUE"))
        self.assertFalse(self.exprs["settle"][0].uses_status)
        for job in ("linux-tests", "linux", "windows", "macos"):
            self.assertTrue(self.exprs[job][0].uses_status, job)

    def test_incomplete_dependencies_and_malformed_outputs_block_allocation(self) -> None:
        keys = {"preflight": "pre", "settle": "settle", "linux": "dep"}
        for job in ("settle", "linux-tests", "windows", "macos"):
            for dependency in self.exprs[job][1]:
                for result in ("", "pending", "failure", "cancelled", "skipped"):
                    with self.subTest(job=job, dependency=dependency, result=result):
                        self.assertFalse(self.runs(job, "pull_request", "synchronize", False,
                                                   label=True, **{keys[dependency]: result}))
        for job, key, value in itertools.product(("linux-tests", "windows", "macos"),
                                                 ("run_full", "paid"), ("", "yes", "1", "pending", "true ")):
            with self.subTest(job=job, key=key, value=value):
                self.assertFalse(self.runs(job, "pull_request", "synchronize", False,
                                          label=True, **{key: value}))


class PreflightTests(unittest.TestCase):
    BASE = "${{ github.event.pull_request.base.sha || github.event.before }}"

    def test_wiring(self) -> None:
        block = job_block("preflight")
        self.assertIn("runs-on: ubuntu-latest", block)
        self.assertIn("fetch-depth: 0", block)
        self.assertNotIn("--force", block)
        self.assertIn("run_full: ${{ steps.scope.outputs.run_full }}", block)
        self.assertIn("paid: ${{ steps.scope.outputs.paid }}", block)
        paths = step_block(block, "Path filter")
        self.assertEqual(step_run(paths), "python3 -X utf8 -B tools/ci_gate.py --paths-only")
        self.assertEqual(step_env(paths), {"CI_BASE_SHA": self.BASE})
        light = step_block(block, "Lightweight checks")
        self.assertNotIn("if:", light)
        self.assertEqual(step_env(light), {"CI_BASE_SHA": self.BASE})
        self.assertIn("import ci_gate; ci_gate.run_lightweight()", step_run(light))
        scope = step_block(block, "Scope outputs")
        self.assertEqual(step_env(scope), {
            "RUN_FULL": "${{ steps.paths.outputs.run_full }}",
            "EVENT_NAME": "${{ github.event_name }}",
            "PR_DRAFT": "${{ github.event.pull_request.draft }}",
        })
        self.assertIn("ready_for_review", step_run(scope))

    def test_normalisation_matrix(self) -> None:
        script = step_run(step_block(job_block("preflight"), "Scope outputs"))
        values = ("true", "false", "", "True", "yes")
        cases, expected = [], []
        for run_full, event, draft in itertools.product(values, ("pull_request", "push", "workflow_dispatch"), values):
            cases.append({"RUN_FULL": run_full, "EVENT_NAME": event, "PR_DRAFT": draft})
            ok = run_full in ("true", "false") and (draft in ("true", "false") if event == "pull_request" else draft == "")
            paid = "true" if run_full == "true" and draft != "true" else "false"
            expected.append((0, f"run_full={run_full} paid={paid}") if ok else None)
        for case, want, got in zip(cases, expected, bash_cases(script, cases)):
            if want is None:
                self.assertNotEqual(got[0], 0, case)
                self.assertEqual(got[1], "", case)
            else:
                self.assertEqual(got, want, case)


class AggregateTests(unittest.TestCase):
    ENV = {
        "EVENT_NAME": "${{ github.event_name }}", "EVENT_ACTION": "${{ github.event.action }}",
        "PR_DRAFT": "${{ github.event.pull_request.draft }}", "PREFLIGHT": "${{ needs.preflight.result }}",
        "RUN_FULL": "${{ needs.preflight.outputs.run_full }}", "PAID": "${{ needs.preflight.outputs.paid }}",
        "SETTLE": "${{ needs.settle.result }}", "TESTS": "${{ needs.linux-tests.result }}",
    }

    def test_wiring(self) -> None:
        block = job_block("linux")
        self.assertIn("name: linux\n", block)
        self.assertIn("runs-on: ubuntu-latest", block)
        self.assertIn("timeout-minutes: 3\n", block)
        self.assertEqual(job_needs(block), ["preflight", "settle", "linux-tests"])
        self.assertEqual(job_if(block), "always()")
        self.assertNotIn("uses:", block)
        self.assertIn("run_full: ${{ needs.preflight.outputs.run_full }}", block)
        step = step_block(block, "Required status")
        self.assertEqual(step_env(step), self.ENV)
        self.assertIn("ready_for_review", step_run(step))

    def test_fail_closed_matrix(self) -> None:
        script = step_run(step_block(job_block("linux"), "Required status"))
        cases: list[dict[str, str]] = []

        def add(event, action, draft, run_full, paid, pre, settle, tests) -> None:
            cases.append(dict(EVENT_NAME=event, EVENT_ACTION=action, PR_DRAFT=draft, PREFLIGHT=pre,
                              RUN_FULL=run_full, PAID=paid, SETTLE=settle, TESTS=tests))

        valid = [("pull_request", a, "false") for a in PR_ACTIONS] + [("push", "", ""), ("workflow_dispatch", "", "")]
        odd = [("pull_request", a, d) for a in PR_ACTIONS + ("labeled",) for d in ("true", "", "yes")]
        odd += [("pull_request", "labeled", "false"), ("push", "", "true"), ("issue_comment", "created", "false")]
        scopes = (("true", "true"), ("false", "false"))
        bad_scopes = (("true", "false"), ("false", "true"), ("", ""), ("True", "true"), ("true", "TRUE"), ("true", "pending"))
        for state in valid + odd:
            for run_full, paid in scopes + bad_scopes:
                control = ["success", expected_settle(state[0], state[1]), "success" if run_full == "true" else "skipped"]
                add(*state, run_full, paid, *control)
                if state in valid and (run_full, paid) in scopes:
                    for dim, value in itertools.product(range(3), SHELL_RESULTS):
                        varied = list(control)
                        varied[dim] = value
                        add(*state, run_full, paid, *varied)
        for pre, settle, tests in itertools.product(SHELL_RESULTS, repeat=3):
            add("pull_request", "synchronize", "false", "true", "true", pre, settle, tests)
        outcomes = set()
        for case, (rc, _) in zip(cases, bash_cases(script, cases)):
            want = shell_oracle(**case)
            outcomes.add(want)
            self.assertEqual(rc == 0, want == 0, case)
        self.assertEqual(outcomes, {0, 1})

    def test_workflow_cancellation_after_successful_dependencies_fails(self) -> None:
        block = job_block("linux")
        status = step_block(block, "Required status")
        cancellation = step_block(block, "Reject workflow cancellation")
        def condition(step):
            match = re.search(r"(?m)^        if: (.*)$", step)
            self.assertIsNotNone(match)
            return Expression(match.group(1).removeprefix("${{ ").removesuffix(" }}"))
        status_if, cancel_if = condition(status), condition(cancellation)
        for cancelled in (False, True):
            self.assertEqual(status_if.runs({}, [], cancelled), not cancelled)
            self.assertEqual(cancel_if.runs({}, [], cancelled), cancelled)
        # Execute the actual refusal even when all dependency results would pass.
        refused = bash_cases(step_run(cancellation), [{"PREFLIGHT": "success", "TESTS": "success"}])
        self.assertNotEqual(refused[0][0], 0)


class ClassifierTests(unittest.TestCase):
    """tools/ci_gate.py is read-only; only its git IO is mocked."""

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("ci_gate_for_scheduling", HERE / "ci_gate.py")
        assert spec and spec.loader
        cls.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.gate)

    def decide(self, base: str, paths=None, missing=False, git_allowed=True) -> bool:
        def fake_git(*args):
            if not git_allowed:
                raise AssertionError(f"git must not run: {args}")
            if missing:
                raise subprocess.CalledProcessError(1, ["git", *args])
            return ""

        with mock.patch.dict(os.environ, {"CI_BASE_SHA": base}), \
                mock.patch.object(self.gate, "git", side_effect=fake_git), \
                mock.patch.object(self.gate, "git_nul", return_value=list(paths or [])), \
                contextlib.redirect_stdout(io.StringIO()):
            return self.gate.decide_run_full(force=False)

    def test_missing_or_zero_base_runs_full_without_git(self) -> None:
        self.assertTrue(self.decide("", git_allowed=False))
        self.assertTrue(self.decide("0" * 40, git_allowed=False))
        self.assertTrue(self.decide("   ", git_allowed=False))

    def test_docs_only_and_gate_paths(self) -> None:
        sha = "a" * 40
        # CSL-376: docs-only diffs run the suite too.
        self.assertTrue(self.decide(sha, ["docs/v3/README.md", "notes.txt"]))
        self.assertTrue(self.decide(sha, ["docs/v3/README.md", "tools/ci_gate.py"]))
        self.assertTrue(self.decide(sha, ["requirements-ci.txt"]))
        self.assertTrue(self.decide(sha, []))
        self.assertTrue(self.decide(sha, ["docs/x.md"], missing=True))

# CSL-416 (CSL-255 exit criterion 5): a static guard on the admission surface of the Claude
# writers. The fixer and `@claude` jobs (`claude.yml`) and the conflict resolver
# (`conflict-scan.yml`, which calls `claude-conflict-writer.yml` per PR) reach CI only through the
# `synchronize` of their own push: no CI dispatch,
# re-run, ready-for-review or CI-label add (the only CI-label write is revoking a gate-owned
# `ci:full`); GitHub-hosted runners only; one shared per-PR lock without cancellation; a reviewed
# ceiling of shell grants with one exact, non-force push; the eight-commit guard, bounded attempts and
# the human pause. CSL-414 splits each claude.yml writer into a trusted prepare (pause, cap and
# attempt checks), an untrusted model job with no push grant, write scope or id-token, and a trusted
# publish job whose only push is the embedded publisher's single ordinary fast-forward. Port C splits
# the conflict writer the same way; its scan caller holds no lock and runs no steps. Limit: the push
# checks read the workflow text only, so they are not a runtime push guarantee. Jobs are split by layout (two-space job keys under `jobs:`), so an unsupported
# layout fails instead of passing. Retry behaviour itself is tested in
# tools/test_claude_pr_fixer.py, which the fixer workflow runs.
CROSSLANE_WORKFLOWS = ROOT / ".github" / "workflows"
CROSSLANE_WRITER_FILES = ("claude.yml", "conflict-scan.yml", "claude-conflict-writer.yml")
CROSSLANE_ALL_JOBS = {"claude.yml": {"resolve-pr", "event-paths", "mention-prepare", "claude-mention", "mention-publish",
                                     "fixer-prepare", "devin-review-fix", "fixer-publish"},
                      "conflict-scan.yml": {"conflict-scan", "conflict-resolve"},
                      "claude-conflict-writer.yml": {"prepare", "model", "publish"}}
# Every writer stage takes the shared per-PR lock: GitHub releases it between jobs.
CROSSLANE_WRITER_JOBS = {"claude.yml": ("mention-prepare", "claude-mention", "mention-publish",
                                        "fixer-prepare", "devin-review-fix", "fixer-publish"),
                         "claude-conflict-writer.yml": ("prepare", "model", "publish")}
# Reusable-workflow callers: (workflow, job) -> called writer file. A caller runs no steps and
# takes no lock (it would deadlock the callee's jobs, which each take the per-PR lock).
CROSSLANE_CALLERS = {("conflict-scan.yml", "conflict-resolve"): "claude-conflict-writer.yml"}
# Model jobs that hold a granted push: none since Port C. Kept so a new one must be reviewed here.
CROSSLANE_PUSH_MODEL_JOBS: dict[str, tuple[str, ...]] = {}
# CSL-414 split stages: (trusted prepare, untrusted model, trusted publish).
CROSSLANE_SPLIT = {"claude.yml": (("mention-prepare", "claude-mention", "mention-publish"),
                                  ("fixer-prepare", "devin-review-fix", "fixer-publish")),
                   "claude-conflict-writer.yml": (("prepare", "model", "publish"),)}
CROSSLANE_PREPARE_JOBS = {name: tuple(s[0] for s in stages) for name, stages in CROSSLANE_SPLIT.items()}
CROSSLANE_MODEL_JOBS = {name: tuple(s[1] for s in stages) for name, stages in CROSSLANE_SPLIT.items()}
CROSSLANE_PUBLISH_JOBS = {name: tuple(s[2] for s in stages) for name, stages in CROSSLANE_SPLIT.items()}
# The split model's one shell grant: the fixed root-owned outbox helper.
CROSSLANE_MODEL_HELPER = "Bash(python3 ${{ runner.temp }}/claude-fixer-guard/model-api --:*)"
CROSSLANE_PUBLISHER = ROOT / "scripts" / "ci" / "claude_fixer_publish.py"
# The publisher's one push: an ordinary fast-forward of the validated candidate to the prepared branch.
CROSSLANE_PUBLISH_PUSH = """["push", "-q", "--no-verify", url, f"{candidate}:refs/heads/{config['ref']}"]"""
CROSSLANE_HOSTED = {"ubuntu-latest", "ubuntu-24.04", "ubuntu-22.04", "windows-2025", "windows-latest", "macos-latest"}
# Every shell grant a writer model may hold, apart from its one push. A new grant fails until reviewed here.
CROSSLANE_SAFE_BASH = {
    "Bash(git add:*)", "Bash(git commit:*)", "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
    "Bash(git rev-parse:*)", "Bash(git merge --abort)", "Bash(gh pr view:*)", "Bash(gh pr diff:*)",
    "Bash(python3 ${{ runner.temp }}/claude-fixer-guard/pre-push model-api --:*)",
    "Bash(python3 ${{ runner.temp }}/claude-fixer-guard/model-api --:*)",
    "Bash(python -X utf8 -B tools/ci_gate.py --force)", "Bash(python3 -X utf8 -B tools/ci_gate.py --force)",
    "Bash(python -X utf8 -B tools/test_claude_pr_fixer.py)", "Bash(python3 -X utf8 -B tools/test_claude_pr_fixer.py)",
}
CROSSLANE_PUSH = re.compile(r"^Bash\(git push origin HEAD:refs/heads/\$\{\{ (steps\.(live|cap)\.outputs\.head_ref|matrix\.pr\.head_ref) \}\}\)$")
# A shell push: `git`, any global options (`-C dir`, `-c k=v`, `--opt[=v]`), then `push`, across
# backslash continuations.
CROSSLANE_SHELL_PUSH = re.compile(r"\bgit(?:(?:\s|\\\n)+(?:-[Cc](?:\s|\\\n)+\S+|--?[A-Za-z][\w-]*(?:=\S+)?))*(?:\s|\\\n)+push\b")
# An argv push: a quoted `push` element opening a list or tuple or following a comma, so
# `["git", "-C", repo, "push"]`, `run_git(["-C", repo, "push"])`, `[*prefix, "push"]` and
# `prefix + ["push"]` all count. Escaped quotes (YAML-embedded Python in a shell string) match too.
CROSSLANE_ARGV_PUSH = re.compile(r"""[\[(,]\s*\\?["']push\\?["']""")
# Any push command or git argv in a job once the model's `prompt` and `claude_args` (its prose and
# tool grants, which execute nothing themselves) are removed.
CROSSLANE_STEP_PUSH = re.compile(f"{CROSSLANE_SHELL_PUSH.pattern}|{CROSSLANE_ARGV_PUSH.pattern}")
# The mention fixer's command-word allowlist is data, not argv: only this exact all-string set literal is skipped.
CROSSLANE_WORD_LIST = re.compile(r"""KNOWN_WORDS = frozenset\(\s*\{(?:\s*"[^"\\\n]*",?)*\s*\}\s*\)""")
# Force, mirror or delete anywhere in a push command, including argv elements (`"-f"`, `"+HEAD:x"`)
# and short-flag clusters (`-uf`).
CROSSLANE_FORCE = re.compile(r"""--force|--mirror|--delete|:\+|(?:^|[\s"'\\,\[(])(?:-[A-Za-z]*f[A-Za-z]*\b|\+)""")
# A label write: an explicit POST/PUT/PATCH, or a `gh api` body flag (which defaults to POST).
CROSSLANE_LABEL_WRITE = re.compile(r"""["'](POST|PUT|PATCH)["']|-X\s*["']?(POST|PUT|PATCH)\b"""
                                   r"""|--method[\s=]+["']?(POST|PUT|PATCH)\b|method\s*=\s*["'](POST|PUT|PATCH)""")
CROSSLANE_LABEL_BODY = re.compile(r"""(?:^|[\s"'])(?:--input|-f|-F|--field|--raw-field)\b""")
CROSSLANE_WRITER_SCOPES = {"contents", "pull-requests", "issues", "id-token"}


def _crosslane_text(name: str) -> str:
    return (CROSSLANE_WORKFLOWS / name).read_text(encoding="utf-8")


def _crosslane_jobs(name: str) -> dict[str, str]:
    lines = _crosslane_text(name).splitlines()
    start = lines.index("jobs:")
    out: dict[str, list[str]] = {}
    current = None
    for line in lines[start + 1:]:
        match = re.fullmatch(r"  ([A-Za-z0-9_-]+):", line)
        if match:
            current = match.group(1)
            out[current] = []
        elif current is not None:
            out[current].append(line)
        elif line.strip() and not line.lstrip().startswith("#"):
            raise AssertionError(f"{name}: unexpected line before the first job: {line!r}")
    if not out:
        raise AssertionError(f"{name}: no jobs found")
    return {key: "\n".join(value) for key, value in out.items()}


def _crosslane_callees(name: str) -> set[str]:
    return set(re.findall(r"^\s*uses: *\./\.github/workflows/([A-Za-z0-9_.-]+)\s*$", _crosslane_text(name), re.MULTILINE))


def _crosslane_value(block: str, key: str) -> str | None:
    match = re.search(rf"^    {re.escape(key)}: *(.*)$", block, re.MULTILINE)
    return match.group(1).strip() if match else None


def _crosslane_without_model_text(block: str) -> str:
    # Drop each `prompt:` / `claude_args:` block scalar: the key line and every more-indented line after it.
    out: list[str] = []
    indent = None
    for line in block.splitlines():
        if indent is not None and (not line.strip() or len(line) - len(line.lstrip()) > indent):
            continue
        indent = None
        match = re.match(r"^( *)(prompt|claude_args): *[|>]", line)
        if match:
            indent = len(match.group(1))
            continue
        out.append(line)
    return "\n".join(out)


def _crosslane_permissions(text: str, indent: int) -> dict[str, str] | str | None:
    # `permissions:` at this indent: an inline value (`read-all`, `{}`, ...), a scope mapping, or None if absent.
    pad = " " * indent
    match = re.search(rf"^{pad}permissions:[ \t]*(\S.*)?$", text, re.MULTILINE)
    if match is None:
        return None
    if match.group(1):
        return match.group(1).strip()
    # YAML allows blank and comment lines inside a mapping; they must not end the scan early.
    body = re.match(rf"((?:{pad}  .*\n?|[ \t]*#.*\n?|[ \t]*\n)+)", text[match.end() + 1:])
    return dict(re.findall(rf"^{pad}  ([a-z-]+): *(\S+)", body.group(1), re.MULTILINE)) if body else {}


def _crosslane_granted(block: str) -> str:
    # `--disallowedTools` denials (e.g. `git push --force:*`) are not grants. Each `--allowedTools`
    # entry goes on its own line, so one grant's command never runs into the next grant.
    block = re.sub(r'--disallowedTools "[^"]*"', "", block)
    return re.sub(r'--allowedTools "([^"]*)"', lambda m: "\n".join(m.group(1).split(",")), block)


def _crosslane_shell_command(text: str, start: int, end: int) -> tuple[int, int]:
    # Widen [start, end) to whole lines plus backslash continuations before and after.
    start = text.rfind("\n", 0, start) + 1
    while start > 1 and text[:start - 1].rstrip(" \t").endswith("\\"):
        start = text.rfind("\n", 0, start - 1) + 1
    end = text.find("\n", end)
    while end != -1 and text[:end].rstrip(" \t").endswith("\\"):
        end = text.find("\n", end + 1)
    return start, len(text) if end == -1 else end


def _crosslane_bracket(text: str, pos: int, opens: str) -> tuple[int, int] | None:
    # The innermost bracket group (opened by one of `opens`) around pos, to its matching close; None if
    # there is none before a blank line. An unclosed group runs to the next blank line (fail closed).
    pairs, depth, i = {")": "(", "]": "[", "}": "{"}, 0, pos - 1
    while i >= 0:
        char = text[i]
        if char == "\n" and not text[text.rfind("\n", 0, i) + 1:i].strip():
            return None
        if char in pairs:
            depth += 1
        elif char in "([{":
            if depth == 0 and char in opens:
                break
            depth = max(depth - 1, 0)
        i -= 1
    else:
        return None
    depth, j = 0, i
    while j < len(text):
        char = text[j]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return i, j + 1
        elif char == "\n" and not text[text.rfind("\n", 0, j) + 1:j].strip():
            break
        j += 1
    return i, j


def _crosslane_publisher_thin_call(block: str) -> None:
    # Publish jobs load scripts/ci/claude_fixer_publish.py from the default-branch SHA
    # prepare recorded, never an embed, live .base.sha, github.sha, or the workspace.
    if "<<'CLAUDE_FIXER_PUBLISH'" in block:
        raise AssertionError("a publish job must not embed the publisher")
    if "PUBLISHER_SHA" not in block or "scripts/ci/claude_fixer_publish.py" not in block:
        raise AssertionError("a publish job must load the publisher from the recorded default-branch SHA")
    if ".base.sha" in block:
        raise AssertionError("a publish job must not load the publisher from live PR .base.sha")
    if "github.sha" in block or "github.workflow_sha" in block or "GITHUB_WORKSPACE" in block:
        raise AssertionError("a publish job must not load the publisher from github.sha or the workspace")


def _crosslane_push_commands(text: str) -> list[str]:
    # Each push as a whole command: a shell line plus its continuations, or an argv list from `[`/`(`
    # to the matching close.
    text = CROSSLANE_WORD_LIST.sub("", text)
    commands = [text[slice(*_crosslane_shell_command(text, m.start(), m.end()))] for m in CROSSLANE_SHELL_PUSH.finditer(text)]
    for match in CROSSLANE_ARGV_PUSH.finditer(text):
        if text[match.start()] in "[(":
            span = _crosslane_bracket(text, match.start() + 1, "[(")
        else:
            span = _crosslane_bracket(text, match.start(), "[(")
        commands.append(text[slice(*span)] if span else text[slice(*_crosslane_shell_command(text, match.start(), match.end()))])
    return commands


def _crosslane_label_commands(text: str) -> list[str]:
    # Each command or call naming `/labels`: its shell line plus continuations, joined with the
    # enclosing call expression when there is one.
    out = []
    for match in re.finditer(r"/labels\b", text):
        start, end = _crosslane_shell_command(text, match.start(), match.end())
        call = _crosslane_bracket(text, match.start(), "(")
        if call:
            start, end = min(start, call[0]), max(end, call[1])
        out.append(text[start:end])
    return out


class CrossLaneAdmissionTests(unittest.TestCase):
    def test_writer_workflow_and_job_sets_are_complete(self) -> None:
        # A new Claude workflow or job must be added here (and to the contract) before it can merge.
        found = {p.name for p in CROSSLANE_WORKFLOWS.glob("*.y*ml")
                 if re.search(r"anthropics/claude-code|ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN",
                              p.read_text(encoding="utf-8"))}
        # A local reusable workflow a writer calls is a writer file too, with or without the markers.
        while callees := {c for name in found for c in _crosslane_callees(name)} - found:
            found |= callees
        self.assertEqual(found, set(CROSSLANE_WRITER_FILES))
        self.assertEqual(set(CROSSLANE_ALL_JOBS), set(CROSSLANE_WRITER_FILES))
        for name, expected in CROSSLANE_ALL_JOBS.items():
            self.assertEqual(set(_crosslane_jobs(name)), expected)

    def test_writers_never_dispatch_rerun_or_ready_ci(self) -> None:
        for name in CROSSLANE_WRITER_FILES:
            body = _crosslane_text(name)
            with self.subTest(workflow=name):
                self.assertNotRegex(body, r"gh workflow run|/dispatches\b|createWorkflowDispatch|reRunWorkflow|actions/github-script")
                self.assertNotRegex(body, r"/rerun|rerun-failed-jobs|gh run rerun|gh pr ready"
                                          r"|markPullRequestReadyForReview|ReadyForReview\(")
                # A ref update through the API must never force (a publisher may fast-forward one).
                self.assertNotRegex(body, r"/git/refs/[^\n]*force|force[^\n]*/git/refs/")

    def test_writers_never_add_ci_labels(self) -> None:
        for name in CROSSLANE_WRITER_FILES:
            body = _crosslane_text(name)
            with self.subTest(workflow=name):
                self.assertNotRegex(body, r"--add-label|gh pr edit|labels\[\]=")
                self.assertNotRegex(body, r'"labels":\s*\[\s*"(ci:full|enhancement)"')
                # Every label write (the whole call or shell command naming `/labels`) is the handoff
                # label; ci:full is only ever removed (DELETE of a gate-owned label event).
                commands = _crosslane_label_commands(body)
                # The label paths live in the receipts embed and the checked-in publisher; a workflow
                # without them (the scan caller since Port C) names no `/labels` endpoint at all.
                if "<<'CLAUDE_FIXER_RECEIPTS'" in body:
                    self.assertTrue(commands)
                else:
                    self.assertEqual(commands, [])
                for command in commands:
                    if CROSSLANE_LABEL_WRITE.search(command) or (
                            CROSSLANE_LABEL_BODY.search(command) and "DELETE" not in command):
                        self.assertIn("HANDOFF_LABEL", command)
                        self.assertNotIn("ci:full", command)
                self.assertTrue(set(re.findall(r'HANDOFF_LABEL = "([^"]+)"', body)) <= {"needs-decision"})

    def test_writer_workflows_use_github_hosted_runners_only(self) -> None:
        for name in CROSSLANE_WRITER_FILES:
            for job, block in _crosslane_jobs(name).items():
                with self.subTest(workflow=name, job=job):
                    caller = _crosslane_value(block, "uses")
                    if caller is not None:
                        # A reusable-workflow caller runs nothing itself; its callee must be a checked writer file.
                        self.assertRegex(caller, r"^\./\.github/workflows/[a-z0-9-]+\.yml$")
                        self.assertIn(caller.rsplit("/", 1)[1], CROSSLANE_WRITER_FILES)
                    else:
                        self.assertIn(_crosslane_value(block, "runs-on"), CROSSLANE_HOSTED)

    def test_writer_workflow_default_token_is_read_only(self) -> None:
        # Jobs without their own `permissions:` inherit this default (GitHub workflow syntax).
        for name in CROSSLANE_WRITER_FILES:
            with self.subTest(workflow=name):
                head = _crosslane_text(name).split("\njobs:\n", 1)[0]
                perms = _crosslane_permissions(head, 0)
                self.assertIsInstance(perms, dict)
                self.assertTrue(perms)
                self.assertLessEqual(set(perms.values()), {"read", "none"})

    def test_writers_share_one_per_pr_lock_without_cancellation(self) -> None:
        for (name, job), callee in CROSSLANE_CALLERS.items():
            with self.subTest(workflow=name, job=job, check="caller"):
                block = _crosslane_jobs(name)[job]
                self.assertEqual(_crosslane_value(block, "uses"), f"./.github/workflows/{callee}")
                self.assertNotRegex(block, r"(?m)^    (concurrency|steps|runs-on):")
                self.assertEqual(re.findall(r"(?m)^      max-parallel: *(\S+)$", block), ["1"])
                self.assertIn(callee, CROSSLANE_WRITER_JOBS)
        for name, writer_jobs in CROSSLANE_WRITER_JOBS.items():
            blocks = _crosslane_jobs(name)
            for job in writer_jobs:
                with self.subTest(workflow=name, job=job):
                    conc = re.search(r"^    concurrency:\n((?:      .*\n?)+)", blocks[job], re.MULTILINE)
                    self.assertIsNotNone(conc)
                    settings = dict(re.findall(r"^      ([a-z-]+): *(.*)$", conc.group(1), re.MULTILINE))
                    self.assertRegex(settings["group"], r"^claude-\$\{\{ (needs\.resolve-pr\.outputs\.pr_number|matrix\.pr\.number|inputs\.pr_number) \}\}$")
                    self.assertEqual(settings["cancel-in-progress"], "false")
                    self.assertEqual(settings["queue"], "max")

    def test_shell_grants_are_reviewed_and_the_one_push_is_exact(self) -> None:
        for name, writer_jobs in CROSSLANE_PUSH_MODEL_JOBS.items():
            blocks = _crosslane_jobs(name)
            for job in writer_jobs:
                with self.subTest(workflow=name, job=job):
                    allowed = re.findall(r'--allowedTools "([^"]*)"', blocks[job])
                    self.assertEqual(len(allowed), 1)
                    bash = [tool for tool in allowed[0].split(",") if tool.startswith("Bash")]
                    pushes = [tool for tool in bash if tool.startswith("Bash(git push")]
                    self.assertEqual(len(pushes), 1)
                    self.assertRegex(pushes[0], CROSSLANE_PUSH)
                    self.assertEqual(set(bash) - set(pushes) - CROSSLANE_SAFE_BASH, set())
        # Port C: no job in any writer file is granted a push (the conflict model's was the last).
        for name in CROSSLANE_WRITER_FILES:
            for job, block in _crosslane_jobs(name).items():
                with self.subTest(workflow=name, job=job, check="no push grant"):
                    for allowed in re.findall(r'--allowedTools "([^"]*)"', block):
                        self.assertNotIn("Bash(git push", allowed)
                        self.assertNotIn("git push", allowed)
        # CSL-414: the split model jobs hold no push grant and no git rule; their one shell grant is
        # the fixed outbox helper, and git push is explicitly disallowed.
        for name, model_jobs in CROSSLANE_MODEL_JOBS.items():
            blocks = _crosslane_jobs(name)
            for job in model_jobs:
                with self.subTest(workflow=name, job=job, check="model grants"):
                    allowed = re.findall(r'--allowedTools "([^"]*)"', blocks[job])
                    self.assertEqual(len(allowed), 1)
                    bash = [tool for tool in allowed[0].split(",") if tool.startswith("Bash")]
                    self.assertEqual(bash, [CROSSLANE_MODEL_HELPER])
                    self.assertLessEqual(set(bash), CROSSLANE_SAFE_BASH)
                    self.assertNotIn("git push", allowed[0])
                    self.assertNotRegex(allowed[0], r"Bash\(git ")
                    disallowed = re.findall(r'--disallowedTools "([^"]*)"', blocks[job])
                    self.assertEqual(len(disallowed), 1)
                    self.assertIn("Bash(git push:*)", disallowed[0].split(","))
                    # No other step of the model job holds a push either.
                    self.assertFalse(_crosslane_push_commands(_crosslane_granted(blocks[job])))
        # Prepare and publish run no model at all.
        for name in CROSSLANE_SPLIT:
            blocks = _crosslane_jobs(name)
            for job in CROSSLANE_PREPARE_JOBS[name] + CROSSLANE_PUBLISH_JOBS[name]:
                with self.subTest(workflow=name, job=job, check="model-free"):
                    self.assertNotIn("anthropics/claude-code", blocks[job])
                    self.assertNotIn("--allowedTools", blocks[job])
                    self.assertNotIn("actions/checkout", blocks[job])
        publisher = CROSSLANE_PUBLISHER.read_text(encoding="utf-8")
        for name in CROSSLANE_WRITER_FILES:
            push_models = CROSSLANE_PUSH_MODEL_JOBS.get(name, ())
            publishers = CROSSLANE_PUBLISH_JOBS.get(name, ())
            prepares = CROSSLANE_PREPARE_JOBS.get(name, ())
            models = CROSSLANE_MODEL_JOBS.get(name, ())
            callers = {job: callee for (caller_file, job), callee in CROSSLANE_CALLERS.items() if caller_file == name}
            for job, block in _crosslane_jobs(name).items():
                with self.subTest(workflow=name, job=job, check="no push outside the model grant"):
                    # The only push paths are a push model's single granted command and the trusted
                    # publisher embedded byte-for-byte; no other step pushes as a shell command or git argv.
                    steps = CROSSLANE_WORD_LIST.sub("", _crosslane_without_model_text(block))
                    if job in publishers:
                        _crosslane_publisher_thin_call(steps)
                    self.assertNotRegex(steps, CROSSLANE_STEP_PUSH)
                with self.subTest(workflow=name, job=job, check="permissions"):
                    perms = _crosslane_permissions(block, 4)
                    if job in push_models:
                        self.assertIsInstance(perms, dict)
                        self.assertLessEqual(set(perms), CROSSLANE_WRITER_SCOPES)
                        # The authorized push needs a writable contents token.
                        self.assertEqual(perms.get("contents"), "write")
                    elif job in publishers:
                        # The publisher pushes with the Claude App token it mints through OIDC (so the
                        # push triggers CI); the job token itself cannot push.
                        self.assertIsInstance(perms, dict)
                        self.assertLessEqual(set(perms), CROSSLANE_WRITER_SCOPES)
                        self.assertEqual(perms.get("contents"), "read")
                        self.assertEqual(perms.get("id-token"), "write")
                    elif job in callers:
                        # A caller grants exactly the called writer's ceiling: each scope at the highest
                        # level any callee job requests, contents read (only the inner publish writes).
                        self.assertIsInstance(perms, dict)
                        self.assertLessEqual(set(perms), CROSSLANE_WRITER_SCOPES)
                        self.assertEqual(perms.get("contents"), "read")
                        ceiling: dict[str, str] = {}
                        for inner in _crosslane_jobs(callers[job]).values():
                            for scope, level in (_crosslane_permissions(inner, 4) or {}).items():
                                if ceiling.get(scope) != "write":
                                    ceiling[scope] = level
                        self.assertEqual(perms, ceiling)
                    elif job in prepares:
                        # Prepare may write its reservation and audit comments, never contents, and mints nothing.
                        self.assertIsInstance(perms, dict)
                        self.assertLessEqual(set(perms), CROSSLANE_WRITER_SCOPES - {"id-token"})
                        self.assertEqual(perms.get("contents"), "read")
                    else:
                        # Read-only (the split model jobs included): every scope read or none; an absent
                        # block inherits the workflow default.
                        if job in models:
                            self.assertIsInstance(perms, dict)
                            self.assertNotIn("id-token", perms)
                        if isinstance(perms, dict):
                            self.assertLessEqual(set(perms.values()), {"read", "none"})
                        else:
                            self.assertIn(perms, (None, "read-all", "{}"))
                with self.subTest(workflow=name, job=job, check="no force push"):
                    commands = _crosslane_push_commands(_crosslane_granted(block))
                    if job in push_models:
                        self.assertTrue(commands)
                    elif job in publishers:
                        # The one ordinary push lives in the checked-in publisher, loaded from the recorded default-branch SHA.
                        self.assertEqual(commands, [])
                        self.assertIn(CROSSLANE_PUBLISH_PUSH, publisher)
                    else:
                        self.assertEqual(commands, [])
                    for command in commands:
                        self.assertNotRegex(command, CROSSLANE_FORCE)
        # The publisher pushes only the prepared branch, rechecking pause and head right before it.
        for name, stages in CROSSLANE_SPLIT.items():
            blocks = _crosslane_jobs(name)
            for prepare, _model, publish in stages:
                with self.subTest(workflow=name, job=publish, check="prepared branch"):
                    self.assertIn(f"HEAD_REF: ${{{{ needs.{prepare}.outputs.head_ref }}}}", blocks[publish])
                    self.assertIn(f"HEAD_SHA: ${{{{ needs.{prepare}.outputs.head_sha }}}}", blocks[publish])
        # One call site of the one push (the other occurrence is its definition).
        self.assertEqual(publisher.count("push_candidate(repo, config, candidate, token)\n"), 1)
        before_push = publisher.split("def publish(", 1)[1].split("push_candidate(repo, config, candidate, token)\n", 1)[0]
        self.assertGreaterEqual(before_push.count("live_head(config, env, read, {config[\"base\"]})"), 2)
        self.assertLess(before_push.rindex("live_head("), before_push.rindex("token = deps[\"mint\"](env)"))

    def test_retries_are_bounded_and_pausable(self) -> None:
        blocks = _crosslane_jobs("claude.yml")
        scan = _crosslane_jobs("conflict-scan.yml")
        # CSL-414: the trusted prepare jobs hold the pause, loop-cap and attempt checks; the
        # model and publish stages run only after an unskipped prepare from the same attempt.
        for prepare, model, publish in CROSSLANE_SPLIT["claude.yml"]:
            with self.subTest(job=prepare):
                # A possibly truncated commit list counts as at the cap.
                self.assertIn('if [ "$total" -ge 250 ] && [ "$count" -lt 8 ]; then', blocks[prepare])
                self.assertIn("CLAUDE_FIXER_PAUSE: ${{ vars.CLAUDE_FIXER_PAUSE }}", blocks[prepare])
                self.assertIn('select(. == "claude:pause" or . == "hold")', blocks[prepare])
                for later in (model, publish):
                    condition = blocks[later].split("\n    steps:", 1)[0]
                    self.assertIn(f"needs.{prepare}.outputs.skip == 'false'", condition, later)
                    self.assertIn(f"needs.{prepare}.outputs.run_attempt == github.run_attempt", condition, later)
            with self.subTest(job=publish):
                # Publish rechecks the pause variable and live labels right before its push.
                self.assertIn("CLAUDE_FIXER_PAUSE: ${{ vars.CLAUDE_FIXER_PAUSE }}", blocks[publish])
                self.assertIn('PAUSE_LABELS = frozenset({"claude:pause", "hold"})',
                              CROSSLANE_PUBLISHER.read_text(encoding="utf-8"))
        # The eight-commit guard: shell skip in the @claude path, eligibility check in the Devin path.
        self.assertIn('if [ "$count" -ge 8 ]; then', blocks["mention-prepare"])
        self.assertIn("LOOP_COMMIT_COUNT: ${{ steps.cap.outputs.loop_commit_count }}", blocks["fixer-prepare"])
        self.assertIn("if int(count) >= 8:", blocks["fixer-prepare"])
        # The Devin path reserves at most three automatic attempts per head and finding set.
        self.assertEqual(re.findall(r"^\s*MAX_ATTEMPTS = (\d+)$", blocks["fixer-prepare"], re.MULTILINE), ["3"])
        self.assertNotRegex(_crosslane_text("claude.yml"), r"MAX_ATTEMPTS = (?!3\b)")
        # The conflict resolver: one push per run, started by scheduled or manual scans only, paused like the rest.
        # Port C: the caller runs the split writer; its prepare checks the pause before anything else
        # runs, and its publisher allows one attempt per prepared run and rechecks the pause.
        self.assertIn("github.event_name != 'push'", _crosslane_value(scan["conflict-resolve"], "if") or "")
        self.assertNotIn("Install single-attempt push guard", _crosslane_text("conflict-scan.yml"))
        self.assertIn('PAUSE_LABELS = frozenset({"claude:pause", "hold"})', _crosslane_text("conflict-scan.yml"))
        writer = _crosslane_jobs("claude-conflict-writer.yml")
        self.assertIn("CLAUDE_FIXER_PAUSE: ${{ vars.CLAUDE_FIXER_PAUSE }}", writer["prepare"])
        self.assertIn('select(. == "claude:pause" or . == "hold")', writer["prepare"])
        for later in ("model", "publish"):
            condition = writer[later].split("\n    steps:", 1)[0]
            self.assertIn("needs.prepare.outputs.skip == 'false'", condition, later)
            self.assertIn("needs.prepare.outputs.run_attempt == github.run_attempt", condition, later)
        self.assertIn("CLAUDE_FIXER_PAUSE: ${{ vars.CLAUDE_FIXER_PAUSE }}", writer["publish"])
        publisher = CROSSLANE_PUBLISHER.read_text(encoding="utf-8")
        self.assertIn('PAUSE_LABELS = frozenset({"claude:pause", "hold"})', publisher)
        self.assertIn('with state_path.open("x", encoding="utf-8") as handle:', publisher)
        _crosslane_publisher_thin_call(writer["publish"])
        self.assertNotRegex(_crosslane_text("claude-conflict-writer.yml"), r"MAX_ATTEMPTS = (?!3\b)")

if __name__ == "__main__":
    unittest.main()
