#!/usr/bin/env python3
"""Provider-free CI gate for Recursion v3 (lint, byte-compile, contract tests).

Run from the repository root:

    python -X utf8 -B tools/ci_gate.py
    python -X utf8 -B tools/ci_gate.py --paths-only
    python -X utf8 -B tools/ci_gate.py --force

This is the command the GitHub Actions `linux` job runs. It is not v3 release
acceptance, live-provider qualification, or scientific admission.

Test discovery: files matching TEST_GLOBS run unless listed in TEST_EXCLUDE.
assert_tests_accounted fails if a test-like file is neither run nor excluded.

Path filter: when CI_BASE_SHA is set (PR base, merge_group.base_sha, or
push-before), a docs-only diff skips the contract-test suite but still runs
whitespace_check and check_public_scope. CI_HEAD_SHA (merge_group.head_sha,
PR head, or github.sha) is the right side of that diff; it defaults to HEAD.
--force always runs the suite.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZERO_SHA = "0" * 40
GIT = ["git", "-c", "core.longpaths=true", "-c", "core.quotePath=false"]
RUN_TIMEOUT = 180
# delegated-Q fake controls resolve this TASK_BASE tree; shallow clones omit it.
DQ_TASK_BASE = "0c908f6833e715af0eaa01b6f6788120eb577efd"

# Paths that can break the provider-free gate. Markdown/docs-only diffs skip.
GATE_PREFIXES = (
    ".github/",
    "tools/",
    "scripts/",
    "schemas/",
    "experiments/",
)
GATE_NAMES = {
    "requirements-ci.txt",
    ".gitattributes",
}
GATE_SUFFIXES = {".py", ".ps1", ".sh", ".yml", ".yaml"}

# Discover test-like files under these globs. New matches run unless excluded.
TEST_GLOBS = (
    "tools/test_*.py",
    "tests/**/test_*.py",
    "tests/**/*_test.py",
    "tests/**/*_tests.py",
    "tests/**/*.test.py",
    "scripts/tests/**/*.test.py",
    "scripts/tests/**/test_*.py",
    "experiments/**/test_*.py",
    "experiments/**/*_tests.py",
    "experiments/**/*.test.py",
)

# Intentionally skipped/slow/platform tests. Paths are posix, relative to ROOT.
TEST_EXCLUDE = {
    # Claude fixer workflow runs this; not part of the linux runtime budget.
    "tools/test_claude_pr_fixer.py",
    # Packet-scoped experiment suites; not general CI.
    "experiments/v3/interface-probes/observer_tests.py",
    "experiments/v3/external-20260914/broker_wire_tests.py",
    "experiments/v3/external-20260914/recovery_allowance_tests.py",
    "experiments/v3/external-20260914/replay_reader_tests.py",
    "experiments/v3/external-20260914/reservation_witness_tests.py",
}

# Floor against shrinking the suite. Additions are allowed; deletions fail.
# harness.test.py matches scripts/tests/**/*.test.py and is loaded by path,
# so the dotted filename is collected. Pin it so a discovery miss fails the gate.
REQUIRED_SUITE_TESTS = frozenset(
    {
        "tools/test_ci_gate.py",
        "experiments/v3/recovery-ledger-20260914/test_ledger.py",
        "experiments/v3/delegated-q-coupling-20260914/test_contract.py",
        "scripts/tests/v3/harness/harness.test.py",
    }
)
M1_MIN_PROGRAMS = 23  # 6 materialize checks + 17 *.test.py
M1_AREAS = ("planning", "execution", "integrity")
SKIP_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
    ".tox",
    ".nox",
    "site-packages",
}
# Original M1 contract tests. Pinned so a swap cannot hide lost coverage.
M1_REQUIRED_TESTS = frozenset(
    f"scripts/tests/v3/{area}/{name}.test.py"
    for area, names in {
        "planning": (
            "conformance", "adversarial", "red_fixture", "property", "validation",
            "static", "schema",
        ),
        "execution": ("fixture", "static", "conformance", "schema", "adversarial"),
        "integrity": ("fixture", "static", "conformance", "schema", "adversarial"),
    }.items()
    for name in names
)

# Frozen packet gates — not general CI. See the workflow comment for why.
#   tools/check_v3_orientation.py          frozen 2026-09-13 allowed-path repair
#   docs/v3/construction/next-work-20260914/check_handoff.py  T-01 spec freeze
#   delegated-q check.py --mode preflight  packet-scoped source_gate
#   delegated-q check.py --mode execute    Owner-gated scientific admission
#   tools/check_next_phase.py              needs Java/TLA+, not this suite


def emit_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")


def git(*args: str) -> str:
    return subprocess.check_output(
        [*GIT, *args], cwd=ROOT, encoding="utf-8", errors="replace"
    ).strip()


def git_nul(*args: str) -> list[str]:
    """Run git and split -z output on NUL. Do not strip: a trailing NUL is a terminator.

    Paths are decoded as UTF-8 with surrogateescape so invalid UTF-8 bytes in
    a Git name do not abort the path filter. os.fsdecode is not used because
    on Windows it decodes with surrogatepass and raises on non-UTF-8 bytes.
    """
    raw = subprocess.check_output([*GIT, *args], cwd=ROOT)
    return [part.decode("utf-8", errors="surrogateescape") for part in raw.split(b"\0") if part]


def is_gate_path(path: str) -> bool:
    posix = path.replace("\\", "/")
    if posix in GATE_NAMES:
        return True
    if posix.startswith(GATE_PREFIXES):
        return True
    suffix = Path(posix).suffix.lower()
    return suffix in GATE_SUFFIXES


def comparison_sha() -> str | None:
    raw = os.environ.get("CI_BASE_SHA", "").strip()
    if not raw or raw == ZERO_SHA:
        return None
    if not re.fullmatch(r'[0-9a-fA-F]{40}', raw):
        raise ValueError('CI_BASE_SHA must be a full commit SHA')
    return raw


def head_ref() -> str:
    """Right-hand side of the path-filter diff (merge_group.head_sha or HEAD)."""
    raw = os.environ.get("CI_HEAD_SHA", "").strip()
    if not raw:
        return "HEAD"
    if not re.fullmatch(r'[0-9a-fA-F]{40}', raw) or raw == ZERO_SHA:
        raise ValueError('CI_HEAD_SHA must be a nonzero full commit SHA')
    if git('rev-parse', f'{raw}^{{commit}}').lower() != git('rev-parse', 'HEAD').lower():
        raise ValueError('CI_HEAD_SHA does not match the checked-out test tree')
    return raw


def changed_paths(base: str) -> list[str]:
    # --no-renames: a source file moved into docs still counts the source path.
    # -z + core.quotePath=false: non-ASCII names are unquoted NUL-terminated paths.
    return git_nul("diff", "-z", "--name-only", "--no-renames", base, head_ref())


def docs_only(base: str) -> bool:
    paths = changed_paths(base)
    if not paths:
        return False
    return not any(is_gate_path(path) for path in paths)


def decide_run_full(force: bool) -> bool:
    head_ref()  # Validate supplied identity even when forcing the full suite.
    base = comparison_sha()
    if force:
        return True
    if base is None:
        return True
    try:
        git("cat-file", "-e", f"{base}^{{commit}}")
    except subprocess.CalledProcessError:
        print(f"CI_BASE_SHA={base} is not present locally; running the suite", flush=True)
        return True
    if docs_only(base):
        paths = changed_paths(base)
        print(
            f"docs-only diff against {base[:12]} ({len(paths)} paths); "
            "skipping suite, running public-scope and whitespace",
            flush=True,
        )
        return False
    print(f"gate paths changed against {base[:12]}; running suite", flush=True)
    return True


def run_program(args: list[str], timeout: int = RUN_TIMEOUT) -> None:
    display = " ".join(args)
    print(f"RUN {display}", flush=True)
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", *args],
        cwd=ROOT,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise SystemExit(f"gate failed: {display} (exit {result.returncode})")


def is_test_filename(name: str) -> bool:
    if name.startswith("test_") and name.endswith(".py"):
        return True
    return name.endswith((".test.py", "_test.py", "_tests.py"))


def _is_main_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)):
        return False
    operands = [test.left, *test.comparators]
    names = [o for o in operands if isinstance(o, ast.Name) and o.id == "__name__"]
    consts = [o for o in operands if isinstance(o, ast.Constant) and o.value == "__main__"]
    return len(names) == 1 and len(consts) == 1


def has_script_entry(path: Path) -> bool:
    """True when the file has a top-level ``if __name__ == "__main__":`` guard."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return False
    return any(_is_main_guard(node) for node in tree.body)


# Loads by path (dotted names like ``x.test.py`` are not importable) and fails when no
# unittest tests are collected, so free-standing ``test_*`` functions cannot pass silently.
UNITTEST_FILE_RUNNER = (
    "import importlib.util, sys, unittest\n"
    "spec = importlib.util.spec_from_file_location('gate_test_module', sys.argv[1])\n"
    "mod = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(mod)\n"
    "suite = unittest.defaultTestLoader.loadTestsFromModule(mod)\n"
    "if suite.countTestCases() == 0:\n"
    "    import asyncio, inspect\n"
    "    for name, obj in vars(mod).items():\n"
    "        if name.startswith('test_') and callable(obj):\n"
    "            fn = obj\n"
    "            if inspect.iscoroutinefunction(obj):\n"
    "                def fn(coro=obj):\n"
    "                    asyncio.run(coro())\n"
    "                fn.__name__ = getattr(obj, '__name__', name)\n"
    "            suite.addTest(unittest.FunctionTestCase(fn))\n"
    "if suite.countTestCases() == 0:\n"
    "    raise SystemExit('no unittest tests collected')\n"
    "result = unittest.TextTestRunner(verbosity=2).run(suite)\n"
    "raise SystemExit(0 if result.wasSuccessful() else 1)\n"
)


def run_test_file(rel: str, *, root: Path | None = None, timeout: int = RUN_TIMEOUT) -> None:
    """Run a discovered test. Script-style files keep their entry point; others use unittest."""
    base = root or ROOT
    path = base / rel
    if has_script_entry(path):
        cmd = [rel]
        display = rel
    else:
        cmd = ["-c", UNITTEST_FILE_RUNNER, rel]
        display = f"unittest {rel}"
    print(f"RUN {display}", flush=True)
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", *cmd],
        cwd=base,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise SystemExit(f"gate failed: {display} (exit {result.returncode})")


def _posix_rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def glob_suite_tests(root: Path | None = None) -> list[str]:
    """Test files the linux gate will run unless they are in TEST_EXCLUDE."""
    base = root or ROOT
    found: set[str] = set()
    for pattern in TEST_GLOBS:
        for path in base.glob(pattern):
            if path.is_file() and is_test_filename(path.name):
                found.add(_posix_rel(path, base))
    return sorted(found)


def discover_all_test_files(root: Path | None = None) -> list[str]:
    """Every test-like file in the tree. The guard compares this to run ∪ exclude."""
    base = root or ROOT
    found: list[str] = []
    for dirpath, dirs, files in os.walk(base, topdown=True):
        dirs[:] = [name for name in dirs if name not in SKIP_DIR_NAMES]
        for name in files:
            if not is_test_filename(name):
                continue
            found.append(_posix_rel(Path(dirpath) / name, base))
    return sorted(found)


def classify_tests(
    *,
    discovered: list[str],
    globbed: list[str],
    exclude: set[str],
    required: set[str],
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Return (run, unaccounted, stale_exclude, missing_required)."""
    run = [path for path in globbed if path not in exclude]
    unaccounted = sorted(set(discovered) - set(run) - exclude)
    stale_exclude = sorted(exclude - set(discovered))
    missing_required = sorted(required - set(run))
    return run, unaccounted, stale_exclude, missing_required


def assert_tests_accounted(root: Path | None = None) -> list[str]:
    """Fail if a test file exists that is neither run nor excluded, or if coverage shrank."""
    run, unaccounted, stale_exclude, missing_required = classify_tests(
        discovered=discover_all_test_files(root),
        globbed=glob_suite_tests(root),
        exclude=set(TEST_EXCLUDE),
        required=set(REQUIRED_SUITE_TESTS | M1_REQUIRED_TESTS),
    )
    errors: list[str] = []
    if unaccounted:
        errors.append("neither run nor excluded:\n  " + "\n  ".join(unaccounted))
    if stale_exclude:
        errors.append("TEST_EXCLUDE missing from tree:\n  " + "\n  ".join(stale_exclude))
    if missing_required:
        errors.append("required suite tests missing:\n  " + "\n  ".join(missing_required))
    if errors:
        raise SystemExit("test inventory: " + "; ".join(errors))
    return run


def suite_test_files() -> list[str]:
    return assert_tests_accounted()


def contract_programs(tests: list[str] | None = None) -> list[tuple[str, ...]]:
    if tests is None:
        tests = suite_test_files()
    programs: list[tuple[str, ...]] = []
    for area in M1_AREAS:
        programs.append((f"schemas/control-plane/v3/{area}/materialize.py", "--check"))
        programs.append((f"scripts/tests/v3/{area}/materialize_fixtures.py", "--check"))
        prefix = f"scripts/tests/v3/{area}/"
        programs.extend((path,) for path in tests if path.startswith(prefix))
    if len(programs) < M1_MIN_PROGRAMS:
        raise SystemExit(
            f"M1 check inventory shrank: {len(programs)} programs "
            f"(expected at least {M1_MIN_PROGRAMS}); inspect before reducing coverage"
        )
    return programs


def extra_suite_programs(tests: list[str] | None = None) -> list[tuple[str, ...]]:
    """Non-M1 tests, after tools/test_ci_gate.py. Prefer the historical experiment order."""
    if tests is None:
        tests = suite_test_files()
    m1_prefixes = tuple(f"scripts/tests/v3/{area}/" for area in M1_AREAS)
    skip = {"tools/test_ci_gate.py"}
    extras = [
        path
        for path in tests
        if path not in skip and not path.startswith(m1_prefixes)
    ]
    preferred = (
        "experiments/v3/recovery-ledger-20260914/test_ledger.py",
        "experiments/v3/delegated-q-coupling-20260914/test_contract.py",
    )
    ordered = [path for path in preferred if path in extras]
    rest = sorted(path for path in extras if path not in preferred)
    return [(path,) for path in ordered + rest]


def whitespace_check() -> None:
    base = comparison_sha()
    if base is None:
        print("SKIP git diff --check (no CI_BASE_SHA)", flush=True)
        return
    head = head_ref()
    print(f"RUN git diff --check {base[:12]} {head[:12]}", flush=True)
    result = subprocess.run(
        [*GIT, "diff", "--check", base, head],
        cwd=ROOT,
        check=False,
    )
    if result.returncode:
        raise SystemExit("whitespace check failed (git diff --check)")


def ensure_commit(sha: str) -> None:
    try:
        git("cat-file", "-e", f"{sha}^{{commit}}")
        return
    except subprocess.CalledProcessError:
        pass
    print(f"fetching missing commit {sha[:12]}", flush=True)
    result = subprocess.run(
        [*GIT, "fetch", "--no-tags", "--prune", "--depth=1", "origin", sha],
        cwd=ROOT,
        check=False,
    )
    if result.returncode:
        raise SystemExit(
            f"git object {sha} is not in this clone and could not be fetched; "
            "use a full-history checkout (fetch-depth: 0)"
        )


def byte_compile() -> None:
    targets = ["tools", "scripts", "schemas", "experiments", "docs"]
    print("RUN python -m compileall " + " ".join(targets), flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", *targets],
        cwd=ROOT,
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise SystemExit("byte-compile failed")


def run_lightweight() -> None:
    """Repository-wide checks that must run even for docs-only diffs."""
    whitespace_check()
    run_program(["tools/check_public_scope.py"])


def run_suite() -> None:
    ensure_commit(DQ_TASK_BASE)
    run_lightweight()
    byte_compile()
    tests = suite_test_files()
    run_test_file("tools/test_ci_gate.py")
    for program in contract_programs(tests):
        if len(program) == 1:
            run_test_file(program[0])
        else:
            run_program(list(program))
    for program in extra_suite_programs(tests):
        run_test_file(program[0])
    print("CI_GATE_PASS provider_calls=0 scientific_admission=false", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paths-only", action="store_true", help="emit run_full and exit")
    parser.add_argument("--force", action="store_true", help="run the suite even if docs-only")
    args = parser.parse_args()
    if Path.cwd().resolve() != ROOT.resolve():
        raise SystemExit(f"run from the repository root ({ROOT})")
    run_full = decide_run_full(force=args.force)
    emit_output("run_full", "true" if run_full else "false")
    emit_output("classification", "full" if run_full else "docs-only")
    if args.paths_only:
        print(f"run_full={str(run_full).lower()}", flush=True)
        return 0
    if run_full:
        run_suite()
    else:
        run_lightweight()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"CI_GATE_FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
