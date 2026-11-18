#!/usr/bin/env python3
"""Claims check for docs/TRANSPARENCY.md (CSL-238).

The page must exist, every backtick path ending in .py must be a file in this
tree, and each engine route and daemon feature named below must be one row
whose telemetry cell is the word none.

Public copy is README.md, SECURITY.md when that file exists, and everything
under docs/ (the page included). Those files may not contain:

- sandboxed (no containment probe is in this tree, so the word stays refused)
- tamper-proof
- tamper-evident, tamper evident, verified safe
- the one phrase adoption B3-25.7 names: "use your Claude subscription through
  one router"

The design file that was present holds that phrase on the B3-25.7 line. It does
not hold a separate enumerated list, so this check uses that phrase and the two
words the draft order names.
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "TRANSPARENCY.md"

# Engine routes the tree names, then the daemon features the composition runs,
# plus the event-store helper and the frozen live flag (both easy to mistake
# for a channel that leaves the machine).
REQUIRED_SUBJECTS = (
    "claude_code",
    "codex",
    "opencode",
    "pi",
    "hermes",
    "cursor",
    "antigravity",
    "copilot",
    "nous",
    "grok",
    "gemini_cli",
    "recursion_loop",
    "v3_live",
    "control_socket",
    "run_registry",
    "control_records",
    "launcher",
    "stop",
    "loopback_gui",
    "plugins",
    "breaker",
    "routes_file",
    "routing_config",
    "child_env",
    "cli",
    "tui",
    "operator_linger",
    "engine_user_guard",
    "engine_user_refuse",
    "event_store",
)

# Lower case. Matched as substrings so a changed capital letter still fails.
PHRASES = (
    "sandboxed",
    "tamper-proof",
    "tamper-evident",
    "tamper evident",
    "verified safe",
    "use your claude subscription through one router",
)

# A slash is required so a prose mention of the suffix alone is not a citation.
_PY_PATH = re.compile(r"`([^`\n]*/[^`\n]*\.py)`")


def _cell(cell: str) -> str:
    text = cell.strip()
    if len(text) >= 2 and text.startswith("`") and text.endswith("`") and text.count("`") == 2:
        return text[1:-1].strip()
    return text.strip("`").strip()


def _rows(text: str) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            continue
        subject = _cell(cells[0])
        module = _cell(cells[1])
        telemetry = _cell(cells[-1]).lower()
        if not subject or subject.startswith("-") or set(subject) <= {"-", ":"}:
            continue
        if subject.lower() == "subject":
            continue
        if not module.endswith(".py"):
            continue
        found.append((subject, module, telemetry))
    return found


def _module_file(root: Path, rel: str) -> bool:
    if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
        return False
    path = (root / rel).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return False
    return path.is_file()


def _cited_py_paths(text: str) -> list[str]:
    return _PY_PATH.findall(text)


def _copy_files(root: Path) -> list[Path]:
    files = [root / "README.md"]
    security = root / "SECURITY.md"
    if security.is_file():
        files.append(security)
    docs = root / "docs"
    if docs.is_dir():
        for dirpath, dirs, names in os.walk(docs):
            dirs[:] = [name for name in dirs if name not in {".git", "__pycache__"}]
            for name in names:
                files.append(Path(dirpath) / name)
    return files


def phrase_hits(root: Path) -> list[str]:
    hits: list[str] = []
    for path in _copy_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            for phrase in PHRASES:
                if phrase in low:
                    hits.append(f"{path.relative_to(root)}:{number}: {phrase}")
    return hits


class TransparencyTests(unittest.TestCase):
    def test_page_is_present_and_every_cited_module_exists(self) -> None:
        self.assertTrue(PAGE.is_file(), "docs/TRANSPARENCY.md is absent")
        text = PAGE.read_text(encoding="utf-8")
        missing = [rel for rel in _cited_py_paths(text) if not _module_file(ROOT, rel)]
        self.assertEqual(missing, [], "a cited module path is not a file in this tree")

    def test_one_row_per_engine_route_and_daemon_feature(self) -> None:
        self.assertTrue(PAGE.is_file(), "docs/TRANSPARENCY.md is absent")
        rows = _rows(PAGE.read_text(encoding="utf-8"))
        subjects = [subject for subject, _module, _telemetry in rows]
        missing = [subject for subject in REQUIRED_SUBJECTS if subject not in subjects]
        self.assertEqual(missing, [], "a required route or feature has no row")
        for subject, module, telemetry in rows:
            self.assertTrue(_module_file(ROOT, module), f"{subject} cites a missing module {module}")
            self.assertEqual(telemetry, "none", f"{subject} telemetry is not none")

    def test_public_copy_refuses_unearned_claims(self) -> None:
        self.assertTrue(PAGE.is_file(), "docs/TRANSPARENCY.md is absent")
        self.assertEqual(phrase_hits(ROOT), [])

    def test_public_copy_fixture_holding_each_new_phrase_fails(self) -> None:
        for phrase in ("tamper-evident", "tamper evident", "verified safe"):
            with self.subTest(phrase=phrase):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "README.md").write_text("Plain copy.\n", encoding="utf-8")
                    (root / "docs").mkdir()
                    (root / "docs" / "page.md").write_text(
                        f"Line one.\nThis record is {phrase.upper()} today.\n",
                        encoding="utf-8",
                    )
                    self.assertEqual(
                        phrase_hits(root),
                        [f"{Path('docs') / 'page.md'}:2: {phrase}"],
                    )

    def test_public_copy_fixture_without_the_phrases_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("Each entry can be checked.\n", encoding="utf-8")
            self.assertEqual(phrase_hits(root), [])


if __name__ == "__main__":
    unittest.main()
