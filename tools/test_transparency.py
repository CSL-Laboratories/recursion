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
below it. A "#" line inside a fenced code block or a raw HTML block (the
CommonMark start and end conditions) is not a heading, and blockquote markers
are read past, so a quoted paragraph joins like any other. A hit names the line
where the phrase starts. Each subject also has exactly one row: every table
line counts, indented by up to three spaces (it still renders as a row), inside
a blockquote, and whatever its module cell holds; a line inside a fenced code
block or a raw HTML block is an example, not a row.
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

# Block structure, as CommonMark reads it: blockquote markers, code fences, and
# the seven kinds of raw HTML block (1-5 end at a marker, 6 and 7 at a blank line).
_QUOTE = re.compile(r" {0,3}> ?")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)$")
_HTML_UNTIL = (
    (re.compile(r" {0,3}<(?:pre|script|style|textarea)(?:[ \t>]|$)", re.I),
     re.compile(r"</(?:pre|script|style|textarea)>", re.I)),
    (re.compile(r" {0,3}<!--"), re.compile(r"-->")),
    (re.compile(r" {0,3}<\?"), re.compile(r"\?>")),
    (re.compile(r" {0,3}<![A-Za-z]"), re.compile(r">")),
    (re.compile(r" {0,3}<!\[CDATA\["), re.compile(r"\]\]>")),
)
_HTML_BLOCK_TAG = re.compile(
    r" {0,3}</?(?:address|article|aside|base|basefont|blockquote|body|caption|center"
    r"|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer"
    r"|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu"
    r"|menuitem|nav|noframes|ol|optgroup|option|p|param|search|section|summary|table"
    r"|tbody|td|tfoot|th|thead|title|tr|track|ul)(?:[ \t]|/?>|$)",
    re.I,
)
_HTML_LONE_TAG = re.compile(
    r" {0,3}(?:<[A-Za-z][A-Za-z0-9-]*"
    r"(?:[ \t]+[A-Za-z_:][\w.:-]*(?:[ \t]*=[ \t]*(?:[^ \t\"'=<>`]+|'[^']*'|\"[^\"]*\"))?)*"
    r"[ \t]*/?>|</[A-Za-z][A-Za-z0-9-]*[ \t]*>)[ \t]*$"
)


def _unquote(line: str, limit: int | None = None) -> tuple[int, str]:
    """Strip blockquote markers (at most ``limit``); give their count and the rest."""
    depth = 0
    while limit is None or depth < limit:
        match = _QUOTE.match(line)
        if not match:
            break
        line = line[match.end():]
        depth += 1
    return depth, line


def _blocks(text: str) -> list[tuple[int, str, str]]:
    """Each line as (number, text without blockquote markers, kind).

    The kind is "code" inside a fenced code block (its fence lines included),
    "html" inside a raw HTML block, and "text" elsewhere. A block opened inside a
    blockquote ends where that blockquote ends; an unclosed fence runs to the end.
    """
    out: list[tuple[int, str, str]] = []
    fence: tuple[str, int, int] | None = None  # fence character, length, depth
    html: tuple[re.Pattern[str] | None, int] | None = None  # end marker, depth
    paragraph = False
    for number, line in enumerate(text.splitlines(), 1):
        if fence is not None or html is not None:
            opened = fence[2] if fence is not None else html[1]
            depth, content = _unquote(line, opened)
            if depth < opened:
                fence = html = None
            elif fence is not None:
                char, length, _depth = fence
                shut = _FENCE.match(content)
                if shut and shut.group(1)[0] == char and len(shut.group(1)) >= length and not shut.group(2).strip():
                    fence = None
                out.append((number, content, "code"))
                continue
            else:
                end = html[0]
                if end is None and not content.strip():
                    html = None
                    out.append((number, content, "text"))
                    continue
                if end is not None and end.search(content):
                    html = None
                out.append((number, content, "html"))
                continue
        depth, content = _unquote(line)
        if not content.strip():
            paragraph = False
            out.append((number, content, "text"))
            continue
        opening = _FENCE.match(content)
        if opening and not (opening.group(1)[0] == "`" and "`" in opening.group(2)):
            fence = (opening.group(1)[0], len(opening.group(1)), depth)
            paragraph = False
            out.append((number, content, "code"))
            continue
        block = False
        for start, end in _HTML_UNTIL:
            begun = start.match(content)
            if begun:
                block = True
                if not end.search(content, begun.end()):
                    html = (end, depth)
                break
        if not block and (_HTML_BLOCK_TAG.match(content) or (not paragraph and _HTML_LONE_TAG.match(content))):
            block = True
            html = (None, depth)
        if block:
            paragraph = False
            out.append((number, content, "html"))
            continue
        paragraph = _HEADING.match(content) is None
        out.append((number, content, "text"))
    return out


def _cell(cell: str) -> str:
    text = cell.strip()
    if len(text) >= 2 and text.startswith("`") and text.endswith("`") and text.count("`") == 2:
        return text[1:-1].strip()
    return text.strip("`").strip()


def _table_rows(text: str, rendered: bool = False) -> list[tuple[str, str, str, bool]]:
    """Every table row with a subject: subject, module, telemetry, flush left.

    With ``rendered``, lines inside a fenced code block or a raw HTML block are
    left out and blockquote markers are read past.
    """
    found: list[tuple[str, str, str, bool]] = []
    if rendered:
        lines = [content for _number, content, kind in _blocks(text) if kind == "text"]
    else:
        lines = text.splitlines()
    for line in lines:
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
    """The subject of every rendered table row, for the one-row-per-subject count."""
    return [subject for subject, _module, _telemetry, _flush in _table_rows(text, rendered=True)]


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
    so a run ends before it and after it; a "#" line inside a fenced code block or
    a raw HTML block is not a heading. Blockquote markers are read past. The list
    gives each character's line.
    """
    chars: list[str] = []
    numbers: list[int] = []

    def end_run(number: int) -> None:
        if chars and chars[-1] == " ":
            chars[-1] = "\n"
        elif chars and chars[-1] != "\n":
            chars.append("\n")
            numbers.append(number)

    for number, line, kind in _blocks(text):
        if not line.strip():
            end_run(number)
            continue
        heading = kind == "text" and _HEADING.match(line) is not None
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

    def test_a_hash_line_inside_a_code_or_html_block_is_not_a_heading(self) -> None:
        # Inside a raw HTML block or a fence a "#" line is text, not a heading, so
        # the lines around it still join; after the block is shut it is a heading.
        hit2 = ["README.md:2: verified safe"]
        cases = (
            ("<div>\n# 1 is verified\nsafe for local use.\n</div>\n", hit2),
            ("<!--\n# verified\nsafe\n-->\n", hit2),
            ("<custom-note>\n# verified\nsafe\n</custom-note>\n", hit2),
            ("<pre>\n\n# verified\nsafe\n</pre>\n", ["README.md:3: verified safe"]),
            ("> <div>\n> # verified\n> safe\n> </div>\n", hit2),
            ("```text\n# verified\nsafe\n```\n", hit2),
            ("~~~\n# verified\nsafe\n~~~\n", hit2),
            ("````\n```\n# verified\nsafe\n```\n````\n", ["README.md:3: verified safe"]),
            ("> It is verified\n> safe.\n", ["README.md:1: verified safe"]),
            ("<!-- note -->\n# verified\nsafe.\n", []),
            ("```\ncode\n```\n# verified\nsafe.\n", []),
            ("~~~\ncode\n~~~\n# verified\nsafe.\n", []),
            ("<div>\n\n# verified\nsafe.\n", []),
            ("    <div>\n# verified\nsafe.\n", []),
            ("    ```\n# verified\nsafe.\n", []),
            ("Some text\n<custom-note>\n# verified\nsafe.\n", []),
            ("> ```\n# verified\nsafe.\n", []),
            ("> # It is verified\n> safe.\n", []),
        )
        for body, expected in cases:
            with self.subTest(body=body):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "README.md").write_text(body, encoding="utf-8")
                    self.assertEqual(phrase_hits(root), expected)

    def test_a_row_inside_a_code_or_html_block_is_not_a_second_row(self) -> None:
        # A row shown as an example inside a fence or a raw HTML block is not a
        # row of the table; a row outside such a block still is one.
        def page(extra: str) -> str:
            rows = "".join(
                f"| `{subject}` | `pkg/mod.py` | a | b | `none` |\n" for subject in REQUIRED_SUBJECTS
            )
            return "| Subject | Module | Leaves | Recorded | Telemetry |\n|---|---|---|---|---|\n" + rows + extra

        example = "| codex | TBD | example | example | none |\n"
        row = "| `codex` | `pkg/mod.py` | c | d | `none` |\n"
        examples = (
            "\n```markdown\n" + example + "```\n",
            "\n~~~\n" + example + "~~~\n",
            "\n````\n```\n" + example + "```\n````\n",
            "\n<!--\n" + example + "-->\n",
            "\n<div>\n" + example + "</div>\n",
            "\n> ```\n> " + example + "> ```\n",
            "\n```\n" + example,
        )
        duplicates = (
            "\n> " + row,
            "\n```\nx\n```\n" + row,
            "\n~~~\nx\n~~~\n" + row,
            "\n    ```\n" + row + "    ```\n",
            "\n<div>\n\n" + row,
            "\n<!-- note -->\n" + row,
            "\n> ```\n" + row,
        )
        check = TransparencyTests("test_one_row_per_engine_route_and_daemon_feature")
        for extra, refused in [(e, False) for e in examples] + [(d, True) for d in duplicates]:
            with self.subTest(extra=extra):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "pkg").mkdir()
                    (root / "pkg" / "mod.py").write_text("", encoding="utf-8")
                    fixture = root / "page.md"
                    with mock.patch.dict(globals(), {"ROOT": root, "PAGE": fixture}):
                        fixture.write_text(page(extra), encoding="utf-8")
                        if not refused:
                            check.test_one_row_per_engine_route_and_daemon_feature()
                            continue
                        with self.assertRaises(AssertionError) as caught:
                            check.test_one_row_per_engine_route_and_daemon_feature()
                        self.assertIn("codex", str(caught.exception))
                        self.assertIn("more than one row", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
