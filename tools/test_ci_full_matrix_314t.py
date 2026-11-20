#!/usr/bin/env python3
"""Labelled Windows and macOS matrix: two informing free-threaded 3.14 legs.

Master 0ebd2de6 runs those jobs on CPython 3.12 only. This test is red there:
the jobs windows-314t and macos-314t are absent. After the draft, the file
starts with the recorded master text and then the two new jobs.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / ".github" / "workflows" / "ci-full-matrix.yml"

# Byte-for-byte master text of ci-full-matrix.yml at 0ebd2de6. The existing
# windows and macos jobs, their names and every earlier line, are this text.
MASTER_0EBD2DE6 = '# Called only from ci-full-label.yml when the added label is `ci:full`.\n# Jobs named windows/macos live here so unrelated labeled events (which skip\n# the caller) never instantiate them and cannot overlay a failed OS check.\nname: CI full matrix\n\non:\n  workflow_call:\n\npermissions:\n  contents: read\n\nenv:\n  PYTHONUTF8: "1"\n  PYTHONIOENCODING: utf-8\n  PIP_DISABLE_PIP_VERSION_CHECK: "1"\n\njobs:\n  windows:\n    name: windows\n    runs-on: windows-2025\n    timeout-minutes: 20\n    steps:\n      - name: Enable Git long paths\n        run: git config --global core.longpaths true\n\n      - uses: actions/checkout@v4\n        with:\n          fetch-depth: 0\n\n      - name: Set up Python\n        uses: actions/setup-python@v5\n        with:\n          python-version: "3.12"\n\n      - name: Install dependencies\n        working-directory: ${{ runner.temp }}\n        env:\n          REQUIREMENTS_CI: ${{ github.workspace }}/requirements-ci.txt\n        shell: bash\n        run: \'python -P -m pip install --only-binary=:all: -r "$REQUIREMENTS_CI"\'\n\n      - name: Provider-free gate\n        run: python -X utf8 -B tools/ci_gate.py --force\n\n  macos:\n    name: macos\n    runs-on: macos-latest\n    timeout-minutes: 15\n    steps:\n      - uses: actions/checkout@v4\n        with:\n          fetch-depth: 0\n\n      - name: Set up Python\n        uses: actions/setup-python@v5\n        with:\n          python-version: "3.12"\n\n      - name: Install dependencies\n        working-directory: ${{ runner.temp }}\n        env:\n          REQUIREMENTS_CI: ${{ github.workspace }}/requirements-ci.txt\n        shell: bash\n        run: \'python -P -m pip install --only-binary=:all: -r "$REQUIREMENTS_CI"\'\n\n      - name: Provider-free gate\n        run: python -X utf8 -B tools/ci_gate.py --force\n'

INSTALL = 'python -P -m pip install --only-binary=:all: -r "$REQUIREMENTS_CI"'
GATE = "python -X utf8 -B tools/ci_gate.py --force"
SETUP = """      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.14t"
"""
COMMENT = (
    "Informs only: continue-on-error keeps this leg from gating the labelled "
    "run until Lucas removes that line."
)


def job_block(text: str, job: str) -> str:
    """Return one job, from its key through the line before the next job key."""
    lines = text.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        if line == f"  {job}:\n":
            start = index
            break
    if start is None:
        raise AssertionError(f"job {job} not found")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.startswith("  #") and not line.startswith("   "):
            end = index
            break
        if (
            line.startswith("  ")
            and not line.startswith("   ")
            and line.rstrip("\n").endswith(":")
        ):
            end = index
            break
    block = "".join(lines[start:end])
    while block.endswith("\n\n"):
        block = block[:-1]
    return block


class FullMatrix314tTests(unittest.TestCase):
    def test_master_prefix_and_two_informing_legs(self) -> None:
        text = MATRIX.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(MASTER_0EBD2DE6), "existing lines changed")
        self.assertGreater(
            len(text), len(MASTER_0EBD2DE6),
            "free-threaded legs are absent: file is still the master text",
        )
        added = text[len(MASTER_0EBD2DE6):]
        self.assertEqual(text.count('python-version: "3.12"'), 2)
        self.assertEqual(text.count('python-version: "3.14t"'), 2)
        self.assertEqual(text.count("continue-on-error: true"), 2)
        self.assertEqual(added.count(COMMENT), 2)
        self.assertNotIn("continue-on-error", MASTER_0EBD2DE6)
        windows = job_block(text, "windows")
        macos = job_block(text, "macos")
        self.assertIn(windows, MASTER_0EBD2DE6)
        self.assertIn(macos, MASTER_0EBD2DE6)
        self.assertEqual(windows.count("name: windows\n"), 1)
        self.assertEqual(macos.count("name: macos\n"), 1)
        for job, runner, timeout, longpaths in (
            ("windows-314t", "windows-2025", "20", True),
            ("macos-314t", "macos-latest", "15", False),
        ):
            block = job_block(text, job)
            self.assertIn(f"    name: {job}\n", block)
            self.assertIn("    continue-on-error: true\n", block)
            self.assertIn(f"    runs-on: {runner}\n", block)
            self.assertIn(f"    timeout-minutes: {timeout}\n", block)
            self.assertIn("actions/checkout@v4", block)
            self.assertIn("fetch-depth: 0", block)
            self.assertIn(SETUP, block)
            self.assertIn("working-directory: ${{ runner.temp }}", block)
            self.assertIn(
                "REQUIREMENTS_CI: ${{ github.workspace }}/requirements-ci.txt", block
            )
            self.assertIn("shell: bash", block)
            self.assertIn(INSTALL, block)
            self.assertIn("name: Provider-free gate\n", block)
            self.assertIn(GATE, block)
            key_at = text.find(f"  {job}:\n")
            before = text[max(0, key_at - 200):key_at]
            self.assertIn(COMMENT, before)
            if longpaths:
                self.assertIn("core.longpaths true", block)
            else:
                self.assertNotIn("longpaths", block)
        threaded_windows = job_block(text, "windows-314t")
        threaded_macos = job_block(text, "macos-314t")
        self.assertNotIn("\n    name: windows\n", threaded_windows)
        self.assertNotIn("\n    name: macos\n", threaded_macos)
        ids = []
        for line in text[text.index("\njobs:\n"):].splitlines():
            if (
                line.startswith("  ")
                and not line.startswith("   ")
                and not line.startswith("  #")
                and line.endswith(":")
            ):
                ids.append(line[:-1].strip())
        self.assertEqual(ids, ["windows", "macos", "windows-314t", "macos-314t"])


if __name__ == "__main__":
    unittest.main()
