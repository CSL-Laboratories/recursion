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
        self.assertIn("runs-on: blacksmith-2vcpu-ubuntu-2404", tests)
        self.assertIn("timeout-minutes: 15\n", tests)
        self.assertEqual(job_needs(tests), ["preflight", "settle"])
        for needle in ("fetch-depth: 0", "cache: pip", "tools/ci_gate.py --force", "tools/perf_ci.py", "::warning::"):
            self.assertIn(needle, tests)
        self.assertNotIn("--paths-only", tests)
        windows, macos = job_block("windows"), job_block("macos")
        self.assertEqual(job_needs(windows), ["preflight", "settle", "linux"])
        self.assertEqual(job_needs(macos), ["preflight", "settle", "linux"])
        self.assertIn("runs-on: blacksmith-2vcpu-windows-2025", windows)
        self.assertIn("timeout-minutes: 20\n", windows)
        self.assertIn("core.longpaths true", windows)
        self.assertIn("runs-on: macos-latest", macos)
        self.assertIn("timeout-minutes: 15\n", macos)
        for block in (windows, macos):
            self.assertIn("fetch-depth: 0", block)
            self.assertIn("cache-dependency-path: requirements-ci.txt", block)
            self.assertIn("tools/ci_gate.py --force", block)
        for line in TEXT.splitlines():
            if line.lstrip().startswith("run:"):
                self.assertNotIn("${{", line, "expressions reach shells via env, not interpolation")


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
        self.assertFalse(self.decide(sha, ["docs/v3/README.md", "notes.txt"]))
        self.assertTrue(self.decide(sha, ["docs/v3/README.md", "tools/ci_gate.py"]))
        self.assertTrue(self.decide(sha, ["requirements-ci.txt"]))
        self.assertTrue(self.decide(sha, []))
        self.assertTrue(self.decide(sha, ["docs/x.md"], missing=True))


if __name__ == "__main__":
    unittest.main()
