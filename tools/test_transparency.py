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
not hold a separate enumerated list, so this check uses that phrase and the five
spellings above it, as the draft orders name them.

Matching ignores capitals and treats any run of spaces and line breaks inside a
paragraph as one space, so a phrase that Markdown wraps across lines still
fails; a blank line ends a paragraph, and words on either side of it are not
joined. A heading line (up to three spaces, then one to six # and a space or
the line end) is a block of its own: it is not joined to the line above or
below it. A hit names the line where the phrase starts. Each subject also has
exactly one row: every table line counts, indented by up to three spaces (it
still renders as a row) and whatever its module cell holds.
"""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

# A Markdown ATX heading line, and a table line; four spaces would be code.
_HEADING = re.compile(r" {0,3}#{1,6}(?:[ \t]|$)")
_TABLE_LINE = re.compile(r" {0,3}\|")


def _cell(cell: str) -> str:
    text = cell.strip()
    if len(text) >= 2 and text.startswith("`") and text.endswith("`") and text.count("`") == 2:
        return text[1:-1].strip()
    return text.strip("`").strip()


def _table_rows(text: str) -> list[tuple[str, str, str, bool]]:
    """Every table row with a subject: subject, module, telemetry, flush left."""
    found: list[tuple[str, str, str, bool]] = []
    for line in text.splitlines():
        if not _TABLE_LINE.match(line):
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
        found.append((subject, module, telemetry, line.startswith("|")))
    return found


def _rows(text: str) -> list[tuple[str, str, str]]:
    return [
        (subject, module, telemetry)
        for subject, module, telemetry, flush in _table_rows(text)
        if flush and module.endswith(".py")
    ]


def _row_subjects(text: str) -> list[str]:
    """The subject of every table row, for the one-row-per-subject count."""
    return [subject for subject, _module, _telemetry, _flush in _table_rows(text)]


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


def _flat(text: str) -> tuple[str, list[int]]:
    """Lower-case text with each whitespace run (line breaks included) as one space.

    A blank line ends a paragraph and becomes a newline, which no phrase holds, so
    words on either side of it never join. A heading line is a block of its own,
    so a run ends before it and after it. The list gives each character's line.
    """
    chars: list[str] = []
    numbers: list[int] = []

    def end_run(number: int) -> None:
        if chars and chars[-1] == " ":
            chars[-1] = "\n"
        elif chars and chars[-1] != "\n":
            chars.append("\n")
            numbers.append(number)

    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            end_run(number)
            continue
        heading = _HEADING.match(line) is not None
        if heading:
            end_run(number)
        for ch in line + " ":
            if ch.isspace():
                if not chars or chars[-1] in " \n":
                    continue
                ch = " "
            for low in ch.lower():
                chars.append(low)
                numbers.append(number)
        if heading:
            end_run(number)
    return "".join(chars), numbers


def phrase_hits(root: Path) -> list[str]:
    """Each refused phrase once per line it starts on, also when prose wraps it."""
    hits: list[str] = []
    for path in _copy_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        flat, numbers = _flat(text)
        found: set[tuple[int, int]] = set()
        for index, phrase in enumerate(PHRASES):
            start = flat.find(phrase)
            while start != -1:
                found.add((numbers[start], index))
                start = flat.find(phrase, start + 1)
        for number, index in sorted(found):
            hits.append(f"{path.relative_to(root)}:{number}: {PHRASES[index]}")
    return hits


class TransparencyTests(unittest.TestCase):
    def test_page_is_present_and_every_cited_module_exists(self) -> None:
        self.assertTrue(PAGE.is_file(), "docs/TRANSPARENCY.md is absent")
        text = PAGE.read_text(encoding="utf-8")
        missing = [rel for rel in _cited_py_paths(text) if not _module_file(ROOT, rel)]
        self.assertEqual(missing, [], "a cited module path is not a file in this tree")

    def test_one_row_per_engine_route_and_daemon_feature(self) -> None:
        self.assertTrue(PAGE.is_file(), "docs/TRANSPARENCY.md is absent")
        text = PAGE.read_text(encoding="utf-8")
        rows = _rows(text)
        subjects = [subject for subject, _module, _telemetry in rows]
        missing = [subject for subject in REQUIRED_SUBJECTS if subject not in subjects]
        self.assertEqual(missing, [], "a required route or feature has no row")
        every = _row_subjects(text)
        repeated = sorted({subject for subject in every if every.count(subject) > 1})
        self.assertEqual(repeated, [], "a route or feature has more than one row")
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

    def test_public_copy_fixture_with_a_wrapped_phrase_fails(self) -> None:
        # Markdown prose wraps; the rendered claim is the same, so the check is too.
        wrapped = (
            ("You can use your Claude subscription\nthrough one router.\n",
             "use your claude subscription through one router"),
            ("It is tamper\n   evident.\n", "tamper evident"),
            ("It is verified\r\nsafe.\n", "verified safe"),
        )
        for body, phrase in wrapped:
            with self.subTest(phrase=phrase):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "README.md").write_text("Plain copy.\n", encoding="utf-8")
                    (root / "docs").mkdir()
                    (root / "docs" / "page.md").write_bytes(
                        ("Line one.\n" + body).encode("utf-8")
                    )
                    self.assertEqual(
                        phrase_hits(root),
                        [f"{Path('docs') / 'page.md'}:2: {phrase}"],
                    )

    def test_words_split_by_a_blank_line_are_not_one_phrase(self) -> None:
        # A blank line ends a paragraph; words on either side of it are not one claim.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text(
                "Each entry can be checked. Nobody may tamper\n\nevident errors are logged.\n"
                "It was verified\n  \nsafe defaults stay.\n",
                encoding="utf-8",
            )
            self.assertEqual(phrase_hits(root), [])

    def test_a_second_row_for_one_subject_fails(self) -> None:
        def page(extra: str) -> str:
            rows = "".join(
                f"| `{subject}` | `pkg/mod.py` | a | b | `none` |\n" for subject in REQUIRED_SUBJECTS
            )
            return "| Subject | Module | Leaves | Recorded | Telemetry |\n|---|---|---|---|---|\n" + rows + extra

        check = TransparencyTests("test_one_row_per_engine_route_and_daemon_feature")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "mod.py").write_text("", encoding="utf-8")
            fixture = root / "page.md"
            with mock.patch.dict(globals(), {"ROOT": root, "PAGE": fixture}):
                fixture.write_text(page(""), encoding="utf-8")
                check.test_one_row_per_engine_route_and_daemon_feature()
                fixture.write_text(page("| `codex` | `pkg/mod.py` | c | d | `none` |\n"), encoding="utf-8")
                with self.assertRaises(AssertionError) as caught:
                    check.test_one_row_per_engine_route_and_daemon_feature()
                self.assertIn("codex", str(caught.exception))

    def test_a_heading_ends_the_run_of_lines_around_it(self) -> None:
        # A heading is its own block: it never joins the line above or below it.
        cases = (
            ("## How records are verified\nSafe defaults apply to new workspaces.\n", []),
            ("It was verified\n# Safe area\n", []),
            ("   ### Checked and verified\nsafe copy follows.\n", []),
            ("## It is verified safe\n", ["README.md:1: verified safe"]),
            ("# Title\nIt is verified\nsafe.\n", ["README.md:2: verified safe"]),
            ("#verified\nsafe.\n", ["README.md:1: verified safe"]),
            ("    ## verified\nsafe.\n", ["README.md:1: verified safe"]),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "README.md").write_text(body, encoding="utf-8")
                    self.assertEqual(phrase_hits(root), expected)

    def test_an_indented_or_non_module_second_row_fails(self) -> None:
        # Up to three leading spaces still render as a table row, and a row
        # whose module cell is not a .py path is still a row for its subject.
        def page(extra: str) -> str:
            rows = "".join(
                f"| `{subject}` | `pkg/mod.py` | a | b | `none` |\n" for subject in REQUIRED_SUBJECTS
            )
            return "| Subject | Module | Leaves | Recorded | Telemetry |\n|---|---|---|---|---|\n" + rows + extra

        check = TransparencyTests("test_one_row_per_engine_route_and_daemon_feature")
        extras = (
            "  | `codex` | `pkg/mod.py` | c | d | `none` |\n",
            "   | `codex` | `pkg/mod.py` | c | d | `none` |\n",
            "| `codex` | none | c | d | `none` |\n",
            "| `codex` | `pkg/mod.txt` | c | d | `none` |\n",
        )
        for extra in extras:
            with self.subTest(extra=extra):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "pkg").mkdir()
                    (root / "pkg" / "mod.py").write_text("", encoding="utf-8")
                    fixture = root / "page.md"
                    with mock.patch.dict(globals(), {"ROOT": root, "PAGE": fixture}):
                        fixture.write_text(page(""), encoding="utf-8")
                        check.test_one_row_per_engine_route_and_daemon_feature()
                        fixture.write_text(page(extra), encoding="utf-8")
                        with self.assertRaises(AssertionError) as caught:
                            check.test_one_row_per_engine_route_and_daemon_feature()
                        self.assertIn("codex", str(caught.exception))
                        self.assertIn("more than one row", str(caught.exception))

if __name__ == "__main__":
    unittest.main()
