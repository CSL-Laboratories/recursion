#!/usr/bin/env python3
"""Provider-free CI gate for Recursion v3 (lint, byte-compile, contract tests).

Run from the repository root:

    python -X utf8 -B tools/ci_gate.py
    python -X utf8 -B tools/ci_gate.py --paths-only
    python -X utf8 -B tools/ci_gate.py --force

This is the command the GitHub Actions `linux` job runs. It is not v3 release
acceptance, live-provider qualification, or scientific admission.

Path filter: when CI_BASE_SHA is set (PR base or push-before), a docs-only
diff skips the contract-test suite but still runs whitespace_check and
check_public_scope. --force always runs the suite.
"""
from __future__ import annotations

import argparse
import os
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
    """Run git and split -z output on NUL. Do not strip: a trailing NUL is a terminator."""
    raw = subprocess.check_output([*GIT, *args], cwd=ROOT)
    return [part.decode("utf-8") for part in raw.split(b"\0") if part]


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
    return raw


def changed_paths(base: str) -> list[str]:
    # --no-renames: a source file moved into docs still counts the source path.
    # -z + core.quotePath=false: non-ASCII names are unquoted NUL-terminated paths.
    return git_nul("diff", "-z", "--name-only", "--no-renames", base, "HEAD")


def docs_only(base: str) -> bool:
    paths = changed_paths(base)
    if not paths:
        return False
    return not any(is_gate_path(path) for path in paths)


def decide_run_full(force: bool) -> bool:
    if force:
        return True
    base = comparison_sha()
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


def contract_programs() -> list[tuple[str, ...]]:
    programs: list[tuple[str, ...]] = []
    for area in ("planning", "execution", "integrity"):
        programs.append((f"schemas/control-plane/v3/{area}/materialize.py", "--check"))
        programs.append((f"scripts/tests/v3/{area}/materialize_fixtures.py", "--check"))
        tests = sorted((ROOT / "scripts" / "tests" / "v3" / area).glob("*.test.py"))
        programs.extend((str(path.relative_to(ROOT)),) for path in tests)
    if len(programs) != 23:
        raise SystemExit(
            f"M1 check inventory changed: {len(programs)} programs (expected 23); inspect before reducing coverage"
        )
    return programs


def whitespace_check() -> None:
    base = comparison_sha()
    if base is None:
        print("SKIP git diff --check (no CI_BASE_SHA)", flush=True)
        return
    print(f"RUN git diff --check {base[:12]} HEAD", flush=True)
    result = subprocess.run(
        [*GIT, "diff", "--check", base, "HEAD"],
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
    run_program(["tools/test_ci_gate.py"])
    for program in contract_programs():
        run_program(list(program))
    run_program(["experiments/v3/recovery-ledger-20260914/test_ledger.py"])
    run_program(["experiments/v3/delegated-q-coupling-20260914/test_contract.py"])
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
