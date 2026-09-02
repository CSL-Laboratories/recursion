#!/usr/bin/env python3
"""Validate the Phase 5 worker evidence contract against observed Git changes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import PurePosixPath


def fail(message: str) -> None:
    raise ValueError(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("result")
    parser.add_argument("scope")
    parser.add_argument("--expected-change", choices=("required", "optional"), default="required")
    args = parser.parse_args()
    try:
        result = json.load(open(args.result, encoding="utf-8"))
        scope = json.load(open(args.scope, encoding="utf-8"))
        if not isinstance(result, dict):
            fail("worker result must be an object")
        status = result.get("status")
        if status not in {"done", "blocked", "no-op"}:
            fail("worker result status is invalid")
        summary = result.get("summary")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 1000:
            fail("worker result summary must contain 1..1000 characters")
        files = result.get("files_changed")
        if not isinstance(files, list) or len(files) != len(set(files)) or any(not isinstance(p, str) for p in files):
            fail("files_changed must be a unique string array")
        for item in files:
            p = PurePosixPath(item)
            if p.is_absolute() or ".." in p.parts:
                fail(f"unsafe result path: {item!r}")
        tests = result.get("tests_run")
        risks = result.get("risks")
        blocker = result.get("blocker")
        if not isinstance(tests, str) or not tests.strip():
            fail("tests_run is required")
        if not isinstance(risks, list) or any(not isinstance(item, str) for item in risks):
            fail("risks must be a string array")
        if not isinstance(blocker, str):
            fail(f"blocker must be a string (got {type(blocker).__name__}: {blocker!r})")
        observed = sorted(scope.get("paths") or [])
        if sorted(files) != observed:
            fail(f"reported files do not match observed scope: reported={sorted(files)!r} observed={observed!r}")
        if status == "blocked":
            if not blocker.strip() or observed:
                fail("blocked result needs one precise question and no source mutations")
        elif blocker.strip():
            fail("non-blocked result may not carry a blocker")
        if status == "no-op" and observed:
            fail("no-op result cannot accompany source mutations")
        if args.expected_change == "required" and status != "blocked" and (status == "no-op" or not observed):
            fail("non-vacuous implementation task produced no change")
        print(json.dumps(result, sort_keys=True))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERR {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
