"""Pinned heads for the trees this suite measures (CSL-801), and the check that enforces them.

The measured code lives on other draft PRs, so each test points at a checkout through
``RACE_STRESS_TREE_324`` / ``_326`` / ``_328``. A checkout is used only when it is the head
pinned below, or when every measured path in it has the same git object id as at that head
(the PR moved, but not the code measured here). Anything else skips with a message naming
both, instead of measuring code nobody pinned. Set ``RACE_STRESS_UNPINNED=1`` to run against
an unpinned tree anyway (the run then reports which tree it measured).

Re-pin by updating ``head`` and ``objects`` after re-running the suite on the new head:
``git -C <tree> rev-parse HEAD HEAD:<path>`` for each path.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

PINS: dict[str, dict] = {
    "RACE_STRESS_TREE_324": {
        "pr": "recursion #324 (CSL-738)",
        "head": "7c729db9efd01c841cc13ebbd62e051b38b13397",
        "objects": {"core/recursion_core/daemon": "726edd12e227ccf625870091fcfc48d2ee06caa1"},
    },
    "RACE_STRESS_TREE_326": {
        "pr": "recursion #326 (CSL-797)",
        "head": "3147f97929091c80662f2b3752b283d66944a83d",
        "objects": {
            "core/recursion_core/routes/breaker.py": "e27e941c4e2d88196801923fedc0e4411ca5127d",
            "core/recursion_core/routes/contract.py": "3f7bb8e03eaec4ab000169d5a04f55383ffe8c1d",
        },
    },
    "RACE_STRESS_TREE_328": {
        "pr": "recursion #328 (CSL-720)",
        "head": "54a2d77128a0d3f1412cbf08f0f8fbec5a4520ff",
        "objects": {
            "core/recursion_core/plugin_security/breaker.py": "39fadf060d01bd5d94200d4a7d493f8f1da57ae0",
            "core/recursion_core/plugin_security/errors.py": "bb644bc884fa133a0487688ac9cd617b4bdcc6b0",
        },
    },
}


def _git(root: Path, *args: str) -> str | None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")}
    try:
        out = subprocess.run(["git", "-C", str(root), *args], check=False, capture_output=True,
                             text=True, timeout=15, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def pin_status(env: str, root: Path) -> tuple[bool, str]:
    """``(ok, message)`` for the checkout ``root`` named by ``env``."""
    pin = PINS[env]
    head = _git(root, "rev-parse", "HEAD")
    if head is None:
        return False, f"{env}={root} is not a git checkout; cannot check it against the pin {pin['head'][:8]}"
    if head == pin["head"]:
        return True, f"{env} at pinned head {head[:8]}"
    moved = [p for p, oid in pin["objects"].items() if _git(root, "rev-parse", f"HEAD:{p}") != oid]
    if not moved:
        return True, f"{env} at {head[:8]}; measured paths identical to pinned head {pin['head'][:8]}"
    return False, (f"{env} is at {head[:8]}, not the pinned {pin['pr']} head {pin['head'][:8]}, and "
                   f"{', '.join(moved)} differ; re-run and re-pin in race_stress/pins.py, or set "
                   f"RACE_STRESS_UNPINNED=1 to measure it anyway")


def require_pinned(env: str, root: Path) -> None:
    """Skip the calling test unless ``root`` is the pinned tree (or RACE_STRESS_UNPINNED=1)."""
    ok, message = pin_status(env, root)
    if not ok and os.environ.get("RACE_STRESS_UNPINNED") != "1":
        raise unittest.SkipTest(message)
