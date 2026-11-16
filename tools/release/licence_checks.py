#!/usr/bin/env python3
"""Checks for the licence files (lane RELEASE). Run this file from the repository root:

    python -X utf8 -B tools/release/licence_checks.py

`problems(root)` names, one sentence each, every rule the four files at `root` (a path or a string)
break:
- LICENSE is the Apache License 2.0 text unmodified, byte for byte as published at
  https://www.apache.org/licenses/LICENSE-2.0.txt. In a git checkout that tracks it, its blob in
  the index and its blob in HEAD (when there is one) are each held byte for byte, and the file on
  disk may differ from them only in line endings, which a Windows checkout changes; anywhere else
  the file on disk is held byte for byte. In a checkout where git cannot be run at all the disk is
  read alone, as outside one; a git that does not answer within GIT_TIMEOUT seconds is named.
- NOTICE is NOTICE_TEXT, up to line endings: the copyright holder stays a placeholder until Lucas
  names one, and no other name, address or line is added.
- CONTRIBUTING.md has one sign-off section, its heading a line of its own outside HTML comments
  and fenced code blocks, held word for word up to line endings (SIGN_OFF). What the rest of the
  file says is not judged.
- SECURITY.md keeps its draft note and its four sections, each with a line of its own, and says
  that reports go privately to CONTACT and not to a public issue, all outside HTML comments and
  fenced code blocks; and nowhere in its text, comments and code blocks included, does it hold an
  e-mail address but CONTACT. What its sections say is not judged.
Markdown is read block first, as CommonMark reads it; a line ends at LF, CRLF or a lone CR. A line
of three or more backticks or tildes, indented by at most three spaces, opens a fenced code block (a
backtick fence's info string holds no backtick), and a line of the same character, at least as long
and with nothing after it, closes it, or else the end of the file does. A line that starts, after at
most three spaces, with `<!--` opens an HTML comment block, which ends on the first line holding
`-->`; so does one that starts with block-quote or list-item markers and then `<!--`, whose comment
ends on the first line inside that quote or item holding `-->`. A line that starts with `<script` or
`<style` opens raw HTML no reader sees, which ends on the line holding its end tag; one that starts
with another tag (one of CommonMark's block tags, or any tag where no paragraph is open) opens raw
HTML that runs to the next blank line, its comments read as in a comment block. A comment or a
script left open (at the end of its line, of its quote or item, of its raw HTML, or of the file)
hides the rest of the file, as a browser reads the raw HTML. A paragraph runs to a blank line, a
block that starts as above, a heading, a rule, a list item or a deeper block quote (a line without
the quote's marker goes on in it). In a paragraph and in a heading, read left to right, a backslash
escape is its character, a run of backticks opens a code span that the next run of the same length
ends, and a `<!--` outside code spans hides the text up to the next `-->`, or is text where the
paragraph holds none. A fence, a comment or raw HTML that a quote or item holds further down is read
as plain lines.
An e-mail address is read from each `@` outwards, as RFC 5322 writes one: its local part is the atoms
of RFC 5322's atext and the dots that run up to the `@`, or a quoted string ending there with any
atoms and dots before it; its domain the atoms and dots that run on from the `@`, less a final dot,
or a bracketed literal and any Markdown marks right after it; an `@` inside a quoted local part or a
bracketed domain is that address's own. It is compared whole with CONTACT. The text a reader sees
(comments taken out) is read as written, and, outside its code spans and the addresses so found,
again with Markdown's backslash escapes and character references (each with its semicolon) read.
There CONTACT may stand wrapped as Markdown wraps text: in a code span (backtick runs of one length,
CONTACT exactly inside), in emphasis (runs of * or _ of one length on each side) or struck through
(one or two ~ on each side), one inside another; three or more ~ are text. In a comment or a code
block nothing is Markdown, so there it must be CONTACT exactly. The sentence that names CONTACT is
held word for word, so there CONTACT stands bare.
A file larger than SIZE_LIMIT bytes is named without being read, a missing file as missing, and one
that cannot be read as such; the work the checks do grows in step with a file's size.
The tests check the committed files, then break, in a scratch copy of the four files, each rule
that they name, and check that it is named; and they check that naming the contact or the holder,
each the edit of two files the comments below describe, leaves them green, also for a quoted
contact and for values the tests use as ones to refuse.

The file is named *_checks.py, not test_*.py: the frozen gate (tools/ci_gate.py) fails on a
test-named file outside its globs, and tools/release/ is not among them, so hosted CI does not run
it. `python -m unittest discover -s tools/release -p '*_checks.py'` finds it. Stdlib only and
offline; git is run only when the tree is a git checkout.
"""
from __future__ import annotations

import ast
import bisect
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from email.headerregistry import Address
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
FILES = ("LICENSE", "NOTICE", "CONTRIBUTING.md", "SECURITY.md")
SIZE_LIMIT = 1 << 20  # bytes; no licence file is near it, so a larger one is named, never read
GIT_TIMEOUT = 60  # seconds for one git call

# https://www.apache.org/licenses/LICENSE-2.0.txt, fetched Nov 14, 2026: 11358 bytes. The same
# bytes ship as /usr/share/common-licenses/Apache-2.0 on Ubuntu 24.04.
APACHE_2_0_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"

# Until Lucas names the copyright holder (counsel item K13), NOTICE carries this placeholder and
# no name. Naming the holder is one deliberate edit of HOLDER and of NOTICE together.
HOLDER = "<holder: Lucas to name>"
NOTICE_TEXT = ("Recursion\n"
               f"Copyright 2026 {HOLDER}\n"
               "\n"
               'Licensed under the Apache License, Version 2.0 (the "License"); you may not use\n'
               "the files in this repository except in compliance with the License. A copy of\n"
               "the License is in the file LICENSE.\n")

SIGN_OFF_HEADING = "## Sign-off (Developer Certificate of Origin)\n"
SIGN_OFF = """
<!-- tools/release/licence_checks.py holds this section word for word (SIGN_OFF): change both together. -->

Recursion is licensed under the Apache License 2.0 (`LICENSE`, `NOTICE`), applied
once its copyright holder is named. From the commit that applies the licence on,
every commit must carry a sign-off line naming the person who submits it:

```text
Signed-off-by: Your Name <you@example.com>
```

`git commit -s` adds it from your Git name and e-mail. By signing off you certify
the Developer Certificate of Origin, version 1.1
(<https://developercertificate.org/>): that you wrote the change, or otherwise have
the right to submit it under the project's licence, and that you understand the
change and your sign-off are public and kept for good. Use your real name or the
name you are known by, and one identity per person.

A sign-off is a person's certification. An automated agent or bot does not add one;
a commit an agent prepares is signed off by the person who submits it, and that
person makes the certification. A pull request with a commit that lacks a sign-off
is not merged. Commits made before the licence was applied carry no sign-off; they
are not re-signed.
"""

# Naming the security contact is one edit of CONTACT and of SECURITY.md together; CONTACT may then
# be the one address SECURITY.md holds.
CONTACT = "<contact: Lucas to name>"
SECURITY_PARTS = ("> **Draft.**", "## Reporting a vulnerability\n", "## Supported versions\n", "## Scope\n",
                  "## What the code defends, and what it does not yet\n")
PUBLIC_ISSUE = "Please do not report a suspected vulnerability in a public issue"
# An atom: RFC 5322's atext (RFC 6532's non-ASCII characters among it), so any character but white
# space, a control and RFC 5322's specials.
ATOM = r"[^\s\x00-\x1f\x7f()<>\[\]:;@\\,.\"]+"
RUN = re.compile(rf"(?:{ATOM}|\.)+")  # atoms and dots, as far as they run: a local part or a domain
LITERAL = re.compile(r"\[[^\[\]\\\r\n]*\]")  # a bracketed domain
# What a reader sees as one character: a Markdown backslash escape, or a character reference with its semicolon.
DECODE = re.compile(r"\\([!-/:-@\[-`{-~])|&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
MARKUP = "`*_~"  # Markdown's code, emphasis and strikethrough marks, which may wrap CONTACT where a reader sees it
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")  # a line that may open or close a fenced code block
COMMENT_BLOCK = re.compile(r" {0,3}<!--")  # a line that opens an HTML comment block
HIDDEN_BLOCK = re.compile(r" {0,3}<(script|style)(?=[\s>]|$)", re.I)  # raw HTML whose text no reader sees
HTML_BLOCK = re.compile(r" {0,3}</?([A-Za-z][A-Za-z0-9-]*)(?=[\s/>]|$)")  # a line that may open other raw HTML
BLOCK_TAGS = frozenset(  # CommonMark's HTML block tags: one ends a paragraph; another tag opens raw HTML only after one
    "address article aside base basefont blockquote body caption center col colgroup dd details dialog dir div dl dt "
    "fieldset figcaption figure footer form frame frameset h1 h2 h3 h4 h5 h6 head header hr html iframe legend li link "
    "main menu menuitem nav noframes ol optgroup option p param search section summary table tbody td tfoot th thead "
    "title tr track ul".split())
HEADING = re.compile(r" {0,3}#{1,6}(?:[ \t]|$)")  # a heading: a block of one line, which ends a paragraph
QUOTE = re.compile(r" {0,3}>[ \t]?")  # a block quote's marker
ITEM = re.compile(r" {0,3}(?:([-+*])|([0-9]{1,9})[.)])( {1,4}|\t)(?=\S)")  # a list item's marker, before its text
BREAK = re.compile(r" {0,3}(?:(?:-[ \t]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,}|=+[ \t]*)$")  # a rule or an underline
INLINE = re.compile(r"\\[!-/:-@\[-`{-~]|`+|<!--")  # in a paragraph: an escape, a code span's run, a comment

def _read(root: Path, rel: str, found: list[str]) -> bytes | None:
    path = root / rel
    try:
        if not path.is_file():
            found.append(f"{rel} is missing")
            return None
        if path.stat().st_size > SIZE_LIMIT:
            found.append(f"{rel} is larger than {SIZE_LIMIT} bytes, which no licence file is")
            return None
        return path.read_bytes()
    except OSError as exc:  # a file that is there but cannot be read: named, not raised
        found.append(f"{rel} cannot be read ({type(exc).__name__})")
        return None


def _text(data: bytes) -> str:
    """A text file's content with LF line endings: a Windows checkout may add CRs, and CommonMark also
    ends a line at a lone CR."""
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").decode("utf-8", errors="replace")


def _comments(text: str) -> tuple[list[tuple[str, str]], bool]:
    """A comment block's `text` cut at its HTML comments, `<!--` to the next `-->`, into (kind, piece),
    and whether a `<!--` is left open at its end, which hides the rest of `text`."""
    pieces: list[tuple[str, str]] = []
    done = 0
    while (start := text.find("<!--", done)) >= 0:
        end = text.find("-->", start + 2)  # so <!--> and <!---> are comments too
        if end < 0:
            return pieces + [("shown", text[done:start]), ("hidden", text[start:])], True
        pieces += [("shown", text[done:start]), ("hidden", text[start:end + 3])]
        done = end + 3
    return pieces + [("shown", text[done:])], False


def _inline(text: str) -> list[tuple[str, str]]:
    """A paragraph's or a heading's `text` cut into (kind, piece), read left to right as CommonMark reads
    it: a backslash escape is its character; a run of backticks opens a code span ("code") that the next
    run of the same length ends, or else is text; a `<!--` opens a comment ("gone") that the next `-->`
    ends, or else is text."""
    runs: dict[int, list[int]] = {}
    for run in re.finditer("`+", text):
        runs.setdefault(len(run.group()), []).append(run.start())
    last_end = text.rfind("-->")
    pieces: list[tuple[str, str]] = []
    done = at = 0
    while mark := INLINE.search(text, at):
        token, at = mark.group(), mark.end()
        if token == "<!--":
            if mark.start() + 2 <= last_end:
                end = text.find("-->", mark.start() + 2) + 3
                pieces += [("shown", text[done:mark.start()]), ("gone", text[mark.start():end])]
                done = at = end
        elif token[0] == "`":
            starts = runs[len(token)]
            later = bisect.bisect_left(starts, at)
            if later < len(starts):
                end = starts[later] + len(token)
                pieces += [("shown", text[done:mark.start()]), ("code", text[mark.start():end])]
                done = at = end
    return pieces + [("shown", text[done:])]


def _closes(line: str, fence: str) -> bool:
    """Whether `line` closes the fenced code block that the run `fence` opened."""
    close = FENCE.fullmatch(line)
    return bool(close and close.group(1)[0] == fence[0] and len(close.group(1)) >= len(fence)
                and not close.group(2).strip(" \t"))


def _containers(line: str) -> tuple[list[int], int]:
    """The block quotes (0) and list items (the width of their marker) that `line` starts with, and
    where its text inside them starts."""
    found: list[int] = []
    at = 0
    while (box := QUOTE.match(line, at) or ITEM.match(line, at)) and box.end() > at:
        found.append(0 if box.re is QUOTE else box.end() - at)
        at = box.end()
    return found, at


def _inside(line: str, boxes: list[int]) -> bool:
    """Whether `line` goes on inside the block quotes and list items `boxes`: a quote's marker for each
    quote, and, on a line that is not blank, a list item's width of spaces for each item."""
    at = 0
    for width in boxes:
        if width == 0:
            if not (quote := QUOTE.match(line, at)):
                return False
            at = quote.end()
        elif line[at:].strip(" \t"):
            if not line.startswith(" " * width, at):
                return False
            at += width
    return True


def _pieces(text: str) -> list[tuple[str, str]]:
    """`text` cut, in order, into (kind, piece): "shown", what a reader of the rendered page sees as
    text; "code", a code span, which a reader sees as written; "hidden", a fenced code block, a comment
    or a script, whose place on its lines a reader sees empty; "gone", a comment in a paragraph or a
    heading, which the text around it closes over. Read block first, as the docstring says."""
    rows = [row for row in re.split(r"(?<=\n)", text) if row]
    pieces: list[tuple[str, str]] = []
    paragraph: list[str] = []
    depth = 0  # the block quotes the paragraph starts in
    listed = False  # whether the paragraph starts as a list item

    def end_paragraph() -> None:
        pieces.extend(_inline("".join(paragraph)))
        paragraph.clear()

    at = 0
    while at < len(rows):
        line = rows[at].rstrip("\n")
        opening = FENCE.fullmatch(line)
        quotes = after = 0
        while quote := QUOTE.match(line, after):
            quotes, after = quotes + 1, quote.end()
        boxes, inner = _containers(line)
        if opening and not (opening.group(1)[0] == "`" and "`" in opening.group(2)):
            end_paragraph()
            last = next((i for i in range(at + 1, len(rows)) if _closes(rows[i].rstrip("\n"), opening.group(1))),
                        len(rows) - 1)
            pieces.append(("hidden", "".join(rows[at:last + 1])))
        elif COMMENT_BLOCK.match(line) or (boxes and COMMENT_BLOCK.match(line, inner)):
            end_paragraph()
            start = line.index("<!--")
            last, closed = at, "-->" in line[start + 2:]
            while not closed and last + 1 < len(rows) and (not boxes or _inside(rows[last + 1].rstrip("\n"), boxes)):
                last += 1
                closed = "-->" in rows[last]
            found, left_open = _comments("".join(rows[at:last + 1])[start:])
            pieces += [("shown", line[:start])] + found
            if left_open:  # open at the end of its line, of its quote or item, or of the file: a browser
                # reads the rest of the file into that comment
                return pieces + [("hidden", "".join(rows[last + 1:]))]
        elif hidden := HIDDEN_BLOCK.match(line):
            end_paragraph()
            close = re.compile(rf"</{hidden.group(1)}\s*>", re.I)
            last = next((i for i in range(at, len(rows)) if close.search(rows[i])), None)
            if last is None:  # a browser reads the rest of the file as script or style
                return pieces + [("hidden", "".join(rows[at:]))]
            block = "".join(rows[at:last + 1])
            cut = len(block) - len(rows[last]) + close.search(rows[last]).end()
            pieces += [("hidden", block[:cut]), ("shown", block[cut:])]
        elif (raw := HTML_BLOCK.match(line)) and (raw.group(1).lower() in BLOCK_TAGS or not paragraph):
            end_paragraph()  # raw HTML to the next blank line: a browser reads its comments, open ones too
            last = next((i for i in range(at + 1, len(rows)) if not rows[i].strip(" \t\n")), len(rows)) - 1
            found, left_open = _comments("".join(rows[at:last + 1]))
            pieces += found
            if left_open:
                return pieces + [("hidden", "".join(rows[last + 1:]))]
        elif not line[inner:].strip(" \t") or HEADING.match(line, after) or BREAK.match(line, after):
            end_paragraph()  # a blank line, a heading, a rule or an underline
            pieces += _inline(rows[at])
            last = at
        else:
            item = ITEM.match(line, after)
            if paragraph and (quotes > depth or item and (item.group(1) or int(item.group(2)) == 1 or listed)):
                end_paragraph()  # a deeper block quote, or a list item, starts a paragraph of its own
            if not paragraph:
                listed, depth = bool(item), quotes
            paragraph.append(rows[at])  # a line with fewer quote markers goes on, lazily, in the same paragraph
            last = at
        at = last + 1
    end_paragraph()
    return pieces


def _visible(text: str) -> str:
    """What a reader of the rendered page sees as text: `text` without its comments in paragraphs and
    headings, and with each character of its fenced code blocks, comments and scripts, line endings
    aside, blanked to a space."""
    return "".join(piece if kind in ("shown", "code") else "" if kind == "gone" else re.sub(r"[^\n]", " ", piece)
                   for kind, piece in _pieces(text))


def _escaped(text: str, at: int) -> bool:
    """Whether `text[at]` follows an odd run of backslashes."""
    start = at
    while start and text[start - 1] == "\\":
        start -= 1
    return (at - start) % 2 == 1


def _quoted_start(text: str, close: int) -> int | None:
    """Where the quoted string that the `"` at `close` ends starts, on its line; None if it ends none."""
    if _escaped(text, close):
        return None
    at = close - 1
    while at >= 0 and text[at] not in "\r\n":
        if text[at] == '"' and not _escaped(text, at):
            return at
        at -= 1
    return None


def _addresses(text: str) -> list[tuple[int, int]]:
    """Where `text` holds an e-mail address, as the docstring reads one, as (start, end): found outwards
    from each `@`, so in time linear in `text`'s length, as a pattern tried at every offset is not. An
    `@` inside a quoted local part or a bracketed domain is that address's own."""
    runs = {match.end(): match.start() for match in RUN.finditer(text)}
    found: list[tuple[int, int, int]] = []  # (start, @, end)
    for at in (match.start() for match in re.finditer("@", text)):
        if found and at < found[-1][2]:
            continue  # inside the domain of the address before it
        domain = LITERAL.match(text, at + 1) or RUN.match(text, at + 1)
        end = domain.start() + len(domain.group().rstrip(".")) if domain else at + 1
        while domain and domain.re is LITERAL and end < len(text) and text[end] in MARKUP:
            end += 1  # Markdown's marks after a bracketed domain, as a run takes them after a dotted one
        start = runs.get(at) if at in runs else _quoted_start(text, at - 1) if text[at - 1:at] == '"' else None
        if start is None or end == at + 1:
            continue
        start = runs.get(start, start)  # atoms or dots before a quoted string: one longer local part
        while found and found[-1][1] >= start:
            found.pop()  # an @ inside this address's quoted local part
        found.append((start, at, end))
    return [(start, end) for start, _, end in found]


def _is_contact(address: str) -> bool:
    """Whether `address` is CONTACT, bare or wrapped as Markdown wraps text: in a code span (backtick runs
    of one length, CONTACT exactly inside), in emphasis (runs of * or _ of one length on each side), or
    struck through (one or two ~ on each side), one inside another."""
    start, end = 0, len(address)
    while not (end - start == len(CONTACT) and address.startswith(CONTACT, start)):
        mark = address[start:start + 1]
        if not mark or mark not in MARKUP:
            return False
        inner = start
        while inner < end and address[inner] == mark:
            inner += 1
        outer = end
        while outer > inner and address[outer - 1] == mark:
            outer -= 1
        if end - outer != inner - start or (mark == "~" and inner - start > 2):
            return False  # unequal runs, or three or more tildes, are text
        if mark == "`":
            return outer - inner == len(CONTACT) and address.startswith(CONTACT, inner)
        start, end = inner, outer
    return True


def _committed(root: Path, rel: str, found: list[str]) -> list[tuple[str, bytes]]:
    """The file's blobs in git's index and in HEAD, as (where, bytes), when `root` itself is a git
    checkout that holds it. [] when it is not a checkout, when git cannot be run, or when neither
    holds the file. The environment's GIT_* settings are not used: the checkout's own repository is
    read. A blob larger than SIZE_LIMIT, and a git that does not answer, are named."""
    if not (root / ".git").exists():
        return []
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    blobs: list[tuple[str, bytes]] = []
    for where, name in (("in the index", f":{rel}"), ("in HEAD", f"HEAD:{rel}")):
        try:
            size = subprocess.run(["git", "-C", str(root), "cat-file", "-s", name],
                                  capture_output=True, text=True, env=env, timeout=GIT_TIMEOUT)
            if size.returncode != 0:
                continue  # not there (an untracked file, or no commit yet)
            if not re.fullmatch(r"[0-9]{1,18}", size.stdout.strip()):
                found.append(f"git gave no size for {rel} {where}, so it was not read")
                continue
            if int(size.stdout) > SIZE_LIMIT:
                found.append(f"{rel} {where} is larger than {SIZE_LIMIT} bytes, which no licence file is")
                continue
            blob = subprocess.run(["git", "-C", str(root), "cat-file", "blob", name],
                                  capture_output=True, env=env, timeout=GIT_TIMEOUT)
        except OSError:
            return []  # no git here: the file on disk is all there is
        except subprocess.SubprocessError:
            found.append(f"git did not answer within {GIT_TIMEOUT} s, so {rel} {where} was not read")
            return blobs
        blobs.append((where, blob.stdout))
    return blobs


def license_problems(root: Path) -> list[str]:
    found: list[str] = []
    data = _read(root, "LICENSE", found)
    if data is None:
        return found
    blobs = _committed(root, "LICENSE", found)
    for where, blob in blobs:
        if hashlib.sha256(blob).hexdigest() != APACHE_2_0_SHA256:
            found.append(f"LICENSE {where} is not the published Apache License 2.0 text, byte for byte")
    if blobs:
        data = data.replace(b"\r\n", b"\n")  # a Windows checkout of the committed file
    if hashlib.sha256(data).hexdigest() != APACHE_2_0_SHA256:
        found.append("LICENSE is not the published Apache License 2.0 text, byte for byte")
    return found


def notice_problems(root: Path) -> list[str]:
    found: list[str] = []
    data = _read(root, "NOTICE", found)
    if data is not None and _text(data) != NOTICE_TEXT:
        have, want = _text(data).splitlines(), NOTICE_TEXT.splitlines()
        line = next((i for i, (a, b) in enumerate(zip(have, want)) if a != b), min(len(have), len(want)))
        found.append(f"NOTICE is not NOTICE_TEXT (from line {line + 1}): the holder stays unnamed and nothing is added")
    return found


def contributing_problems(root: Path) -> list[str]:
    found: list[str] = []
    data = _read(root, "CONTRIBUTING.md", found)
    if data is None:
        return found
    text = _text(data)
    if text.count(SIGN_OFF_HEADING) != 1:
        found.append("CONTRIBUTING.md does not have exactly one sign-off section")
        return found
    if "\n" + SIGN_OFF_HEADING not in "\n" + _visible(text):
        found.append("CONTRIBUTING.md's sign-off heading is not a line of its own outside comments and fences")
    if text.split(SIGN_OFF_HEADING, 1)[1].split("\n## ", 1)[0] != SIGN_OFF:
        found.append("CONTRIBUTING.md's sign-off section is not SIGN_OFF word for word")
    return found


def security_problems(root: Path) -> list[str]:
    found: list[str] = []
    data = _read(root, "SECURITY.md", found)
    if data is None:
        return found
    source = _text(data)
    pieces = _pieces(source)
    text = "\n" + _visible(source)
    for part in SECURITY_PARTS:
        if "\n" + part not in text:
            found.append(f"SECURITY.md has no {part.strip()!r}")
        elif part.startswith("## ") and not all(section.split("\n## ", 1)[0].strip()
                                                 for section in text.split("\n" + part)[1:]):
            found.append(f"SECURITY.md's section {part.strip()!r} is empty")
    found += [f"SECURITY.md does not say {sentence!r}"
              for sentence in (f"Report it privately to {CONTACT}.", PUBLIC_ISSUE) if sentence not in text]
    # As a reader sees it, the comments in paragraphs taken out: as written first, then, outside its code
    # spans and the addresses found, with escapes and character references read.
    seen = [(kind, piece) for kind, piece in pieces if kind in ("shown", "code")]
    written = "".join(piece for _, piece in seen)
    spans = _addresses(written)
    masked, done = [], 0
    for start, end in spans:
        masked += [written[done:start], " " * (end - start)]
        done = end
    masked = "".join(masked) + written[done:]
    read, done = [], 0
    for kind, piece in seen:
        segment, done = masked[done:done + len(piece)], done + len(piece)
        read.append(segment if kind == "code" else DECODE.sub(
            lambda one: one.group(1) if one.group(1) is not None else html.unescape(one.group()), segment))
    read = "".join(read)
    unseen = [piece for kind, piece in pieces if kind not in ("shown", "code")]
    if (not all(_is_contact(written[start:end]) for start, end in spans)
            or not all(_is_contact(read[start:end]) for start, end in _addresses(read))
            or any(piece[start:end] != CONTACT for piece in unseen for start, end in _addresses(piece))):
        found.append("SECURITY.md holds an e-mail address that is not its contact (CONTACT)")
    return found


def problems(root: Path | str) -> list[str]:
    root = Path(root)
    return license_problems(root) + notice_problems(root) + contributing_problems(root) + security_problems(root)


class LicenceTests(unittest.TestCase):
    """The committed files hold every rule."""

    def test_license_is_the_apache_2_0_text_unmodified(self) -> None:
        self.assertEqual(license_problems(ROOT), [])

    def test_notice_names_no_holder_until_lucas_does(self) -> None:
        self.assertEqual(notice_problems(ROOT), [])

    def test_contributing_states_the_sign_off_rule(self) -> None:
        self.assertEqual(contributing_problems(ROOT), [])

    def test_security_policy_has_no_address_until_the_contact_is_named(self) -> None:
        self.assertEqual(security_problems(ROOT), [])


class BrokenRuleTests(unittest.TestCase):
    """Each rule broken in a scratch copy of the four files, the defects planted by the reads of
    #409 among them: the checks must name it, and a crash counts as not naming it."""

    EMAIL_PROBLEM = "SECURITY.md holds an e-mail address that is not its contact (CONTACT)"

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix="licence-checks-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name) / "tree"
        self.root.mkdir()
        unusable = [rel for rel in FILES if not (ROOT / rel).is_file() or (ROOT / rel).stat().st_size > SIZE_LIMIT]
        if unusable:
            self.skipTest(f"missing or oversized in the repository, which LicenceTests names: {unusable}")
        for rel in FILES:
            (self.root / rel).write_bytes((ROOT / rel).read_bytes().replace(b"\r\n", b"\n"))

    def found(self, root: Path | str | None = None) -> list[str]:
        try:
            return problems(root or self.root)
        except Exception as exc:  # noqa: BLE001 - reported through the assertion, not as an error
            return [f"the checks crashed: {exc!r}"]

    def assertNamed(self, pattern: str, root: Path | None = None) -> None:
        found = self.found(root)
        self.assertTrue(any(re.search(pattern, p) for p in found), f"no problem matches {pattern!r}: {found}")

    def edit(self, rel: str, old: str, new: str) -> None:
        path = self.root / rel
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")

    def wrap(self, rel: str, before: str, after: str = "") -> None:
        path = self.root / rel
        path.write_text(before + path.read_text(encoding="utf-8") + after, encoding="utf-8", newline="\n")

    def git(self, root: Path, *args: str) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-C", str(root), *args],
                       check=True, capture_output=True, env=env, timeout=60)

    def crlf(self, rel: str) -> None:
        path = self.root / rel
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

    @staticmethod
    def other(value: str, allowed: str | None = None) -> str:
        """`value`, changed where it is the one value allowed (CONTACT, unless `allowed` is given): a
        fixture the checks must refuse stays one whatever Lucas names."""
        return value if value != (CONTACT if allowed is None else allowed) else value[:1] + "x" + value[1:]

    # The harness itself: a rule that names nothing, or a crash, must turn these tests red.
    def test_the_scratch_copy_holds(self) -> None:
        self.assertEqual(self.found(), [])
        self.assertEqual(self.found(str(self.root)), [])  # a string names the same tree

    def test_assert_named_fails_where_nothing_is_named(self) -> None:
        with self.assertRaises(AssertionError):
            self.assertNamed(r"^LICENSE is not the published")

    def test_a_crash_is_reported_as_a_name(self) -> None:
        def crash(root: Path) -> list[str]:
            raise RuntimeError("planted")
        with mock.patch.dict(globals(), {"license_problems": crash}):
            try:
                found = self.found()
            except Exception as exc:  # noqa: BLE001
                self.fail(f"found() let a crash out: {exc!r}")
        self.assertEqual(found, ["the checks crashed: RuntimeError('planted')"])

    def test_git_is_skipped_where_it_is_not_installed(self) -> None:
        with mock.patch.object(shutil, "which", return_value=None):
            try:
                self.git(self.root, "status")
            except unittest.SkipTest:
                return
            except Exception as exc:  # noqa: BLE001 - reported through the assertion
                self.fail(f"git was run where it is not installed: {exc!r}")
        self.fail("git was run where it is not installed")

    def test_a_missing_or_oversized_file_is_named(self) -> None:
        for rel in FILES:
            with self.subTest(rel=rel):
                data = (self.root / rel).read_bytes()
                (self.root / rel).unlink()
                self.assertNamed(rf"^{re.escape(rel)} is missing$")
                (self.root / rel).mkdir()  # a folder where the file should be
                self.assertNamed(rf"^{re.escape(rel)} is missing$")
                (self.root / rel).rmdir()
                (self.root / rel).write_bytes(data * 3)
                with mock.patch.dict(globals(), {"SIZE_LIMIT": len(data) * 2}):
                    self.assertNamed(rf"^{re.escape(rel)} is larger than {len(data) * 2} bytes")
                (self.root / rel).write_bytes(data)

    def test_a_file_at_the_size_limit_is_read_and_a_larger_one_is_not(self) -> None:
        reads: list[str] = []
        read_bytes = Path.read_bytes

        def spy(path: Path) -> bytes:
            reads.append(path.name)
            return read_bytes(path)
        for rel in FILES:
            size = (self.root / rel).stat().st_size
            with self.subTest(rel=rel), mock.patch.object(Path, "read_bytes", spy):
                reads.clear()
                with mock.patch.dict(globals(), {"SIZE_LIMIT": size}):
                    self.assertEqual([p for p in self.found() if p.startswith(f"{rel} is larger")], [])
                self.assertIn(rel, reads)
                reads.clear()
                with mock.patch.dict(globals(), {"SIZE_LIMIT": size - 1}):
                    self.assertNamed(rf"^{re.escape(rel)} is larger than {size - 1} bytes")
                self.assertNotIn(rel, reads)

    def blob_reads(self, limit: int) -> tuple[list[str], list[str]]:
        """The problems found with SIZE_LIMIT at `limit`, and the blobs git was asked to print."""
        calls: list[list[str]] = []
        run = subprocess.run

        def spy(args, *rest, **options):  # noqa: ANN001, ANN002, ANN003 - subprocess.run's own
            calls.append(list(args))
            return run(args, *rest, **options)
        with mock.patch.object(subprocess, "run", spy), mock.patch.dict(globals(), {"SIZE_LIMIT": limit}):
            found = self.found()
        return found, [c[-1] for c in calls if c[-2:-1] == ["blob"]]

    def test_a_blob_at_the_size_limit_is_read_and_a_larger_one_is_not(self) -> None:
        good = (self.root / "LICENSE").read_bytes()
        self.git(self.root, "init", "-q")
        (self.root / "LICENSE").write_bytes(good * 3)
        self.git(self.root, "add", "LICENSE")
        (self.root / "LICENSE").write_bytes(good)
        found, blobs = self.blob_reads(len(good) * 3)
        self.assertIn("LICENSE in the index is not the published Apache License 2.0 text, byte for byte", found)
        self.assertIn(":LICENSE", blobs)
        found, blobs = self.blob_reads(len(good) * 3 - 1)
        self.assertIn(f"LICENSE in the index is larger than {len(good) * 3 - 1} bytes, which no licence file is", found)
        self.assertNotIn(":LICENSE", blobs)

    def test_an_oversized_license_in_head(self) -> None:
        good = (self.root / "LICENSE").read_bytes()
        self.git(self.root, "init", "-q")
        (self.root / "LICENSE").write_bytes(good * 3)
        self.git(self.root, "add", "LICENSE")
        self.git(self.root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                 "commit", "-q", "--no-verify", "-m", "t")
        (self.root / "LICENSE").write_bytes(good)
        self.git(self.root, "add", "LICENSE")
        found, blobs = self.blob_reads(len(good) * 2)
        self.assertEqual([p for p in found if p.startswith("LICENSE")],
                         [f"LICENSE in HEAD is larger than {len(good) * 2} bytes, which no licence file is"])
        self.assertEqual(blobs, [":LICENSE"])

    def test_license_one_byte_changed(self) -> None:
        path = self.root / "LICENSE"
        path.write_bytes(path.read_bytes().replace(b"Apache License", b"Apache Licence", 1))
        self.assertNamed(r"^LICENSE is not the published")

    def test_license_with_crlf_outside_a_checkout(self) -> None:
        self.crlf("LICENSE")
        self.assertNamed(r"^LICENSE is not the published")

    def test_a_windows_checkout_of_the_committed_license_holds(self) -> None:
        self.git(self.root, "init", "-q")
        self.git(self.root, "add", "LICENSE")
        self.crlf("LICENSE")
        self.assertEqual(self.found(), [])
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.root / "elsewhere")}):  # the checkout's own git
            self.assertEqual(self.found(), [])
        with mock.patch.dict(os.environ, {"PATH": ""}):  # no git to run: read from disk only
            self.assertNamed(r"^LICENSE is not the published")

    def test_a_checkout_where_git_cannot_run_is_read_from_disk(self) -> None:
        self.git(self.root, "init", "-q")
        self.git(self.root, "add", "LICENSE")
        with mock.patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(self.found(), [])

    def test_a_license_committed_with_crlf(self) -> None:
        self.git(self.root, "init", "-q")
        self.crlf("LICENSE")
        self.git(self.root, "add", "LICENSE")
        (self.root / "LICENSE").write_bytes((self.root / "LICENSE").read_bytes().replace(b"\r\n", b"\n"))
        self.assertNamed(r"^LICENSE in the index is not")

    def test_a_good_license_staged_over_a_bad_one_in_head(self) -> None:
        good = (self.root / "LICENSE").read_bytes()
        self.git(self.root, "init", "-q")
        (self.root / "LICENSE").write_text("not the licence\n", encoding="utf-8")
        self.git(self.root, "add", "LICENSE")
        self.git(self.root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                 "commit", "-q", "--no-verify",
                 "-m", "t")
        (self.root / "LICENSE").write_bytes(good)
        self.git(self.root, "add", "LICENSE")
        self.assertEqual([p for p in self.found() if p.startswith("LICENSE")],
                         ["LICENSE in HEAD is not the published Apache License 2.0 text, byte for byte"])

    def test_an_oversized_license_in_the_index(self) -> None:
        good = (self.root / "LICENSE").read_bytes()
        self.git(self.root, "init", "-q")
        (self.root / "LICENSE").write_bytes(good * 3)
        self.git(self.root, "add", "LICENSE")
        (self.root / "LICENSE").write_bytes(good)
        with mock.patch.dict(globals(), {"SIZE_LIMIT": len(good) * 2}):
            self.assertNamed(rf"^LICENSE in the index is larger than {len(good) * 2} bytes")

    def stand_in_git(self, script: str) -> Path:
        """A folder holding a `git` that runs `script` instead (POSIX only)."""
        if os.name == "nt":
            self.skipTest("the stand-in git is a POSIX shell script")
        self.git(self.root, "init", "-q")
        self.git(self.root, "add", "LICENSE")
        stand_in = Path(tempfile.mkdtemp(prefix="bin-", dir=self.root.parent))
        (stand_in / "git").write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
        (stand_in / "git").chmod(0o755)
        return stand_in

    def test_a_git_that_does_not_answer_is_named(self) -> None:
        if os.environ.get("LICENCE_CHECKS_CHILD"):
            self.skipTest("the parent runs it: it holds for any contact or holder")
        stand_in = self.stand_in_git("exec /bin/sleep 5")
        with mock.patch.dict(os.environ, {"PATH": str(stand_in)}), mock.patch.dict(globals(), {"GIT_TIMEOUT": 1}):
            self.assertNamed(r"^git did not answer within 1 s, so LICENSE in the index was not read")

    def test_a_git_that_answers_nonsense_is_named(self) -> None:
        for answer in ("not a size", "\u00b2", "7" * 4400):  # a superscript two is a digit to str.isdigit()
            with self.subTest(answer=answer[:12]):
                stand_in = self.stand_in_git(f"echo '{answer}'")
                with mock.patch.dict(os.environ, {"PATH": str(stand_in)}):
                    self.assertNamed(r"^git gave no size for LICENSE in the index, so it was not read")

    def test_an_untracked_license_in_a_checkout_is_read_from_disk(self) -> None:
        self.git(self.root, "init", "-q")
        self.assertEqual(self.found(), [])
        self.crlf("LICENSE")
        self.assertNamed(r"^LICENSE is not the published")

    def test_a_tree_inside_another_checkout_is_read_from_disk(self) -> None:
        outer = self.root.parent
        (outer / "LICENSE").write_text("another licence\n", encoding="utf-8")
        self.git(outer, "init", "-q")
        self.git(outer, "add", "LICENSE")
        self.assertEqual(self.found(), [])

    def test_notice_with_a_holder_a_name_or_an_address(self) -> None:
        for old, new, line in ((HOLDER, self.other("Jane Doe", HOLDER), 2),
                               ("the License is in the file LICENSE.", "Maintained by Jane Doe <jane.doe@example.org>.\n"
                                                                       "the License is in the file LICENSE.", 6),
                               ("the License is in the file LICENSE.", "Contact: jane.doe@example.org\n"
                                                                       "the License is in the file LICENSE.", 6),
                               ("A copy of", "Licence text", 5),
                               (NOTICE_TEXT, "", 1),
                               ("Recursion\n", " Recursion\n", 1),
                               ("Recursion\n", "\nRecursion\n", 1),
                               ("the License is in the file LICENSE.\n", "the License is in the file LICENSE.\n\n", 7)):
            with self.subTest(new=new):
                saved = (self.root / "NOTICE").read_bytes()
                self.edit("NOTICE", old, new)
                self.assertNamed(rf"^NOTICE is not NOTICE_TEXT \(from line {line}\)")
                (self.root / "NOTICE").write_bytes(saved)

    def test_notice_that_is_not_utf_8_is_named(self) -> None:
        (self.root / "NOTICE").write_bytes(b"Recursion\n\xff\xfe\n")
        self.assertNamed(r"^NOTICE is not NOTICE_TEXT \(from line 2\)")
        (self.root / "NOTICE").write_bytes(NOTICE_TEXT.encode("utf-8").replace(b"Recursion", b"Recur\xffsion", 1))
        self.assertNamed(r"^NOTICE is not NOTICE_TEXT \(from line 1\)")  # not read as if the byte were not there

    def test_text_files_with_windows_line_endings_hold(self) -> None:
        for rel in ("NOTICE", "CONTRIBUTING.md", "SECURITY.md"):
            self.crlf(rel)
        self.assertEqual(self.found(), [])

    def test_text_files_with_lone_cr_line_endings_hold(self) -> None:
        for rel in ("NOTICE", "CONTRIBUTING.md", "SECURITY.md"):
            path = self.root / rel
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r"))
        self.assertEqual(self.found(), [])
        text = (self.root / "SECURITY.md").read_bytes()
        (self.root / "SECURITY.md").write_bytes(b"~~~\r" + text + b"~~~\r")  # so a fence behind lone CRs is one
        self.assertIn("SECURITY.md has no '## Scope'", self.found())

    def test_the_sign_off_section_is_held_word_for_word(self) -> None:
        for old, new in (("version 1.1\n", "version 1.10\n"),
                         (SIGN_OFF_HEADING, SIGN_OFF_HEADING + "\nEvery automated agent or bot must add one.\n"),
                         (SIGN_OFF_HEADING, SIGN_OFF_HEADING + "\n"),  # a blank line more at its top
                         ("are not re-signed.\n", "are not re-signed.\n\n")):  # and at its end
            with self.subTest(new=new):
                saved = (self.root / "CONTRIBUTING.md").read_bytes()
                self.edit("CONTRIBUTING.md", old, new)
                self.assertNamed(r"^CONTRIBUTING\.md's sign-off section is not SIGN_OFF")
                (self.root / "CONTRIBUTING.md").write_bytes(saved)

    def test_one_sign_off_section(self) -> None:
        for old, new in ((SIGN_OFF_HEADING, "## Sign off\n"), (SIGN_OFF_HEADING, SIGN_OFF_HEADING + SIGN_OFF + SIGN_OFF_HEADING)):
            with self.subTest(new=new[:40]):
                saved = (self.root / "CONTRIBUTING.md").read_bytes()
                self.edit("CONTRIBUTING.md", old, new)
                self.assertNamed(r"^CONTRIBUTING\.md does not have exactly one sign-off section")
                (self.root / "CONTRIBUTING.md").write_bytes(saved)

    def test_contributing_code_sample_is_not_signoff_section(self) -> None:
        self.wrap("CONTRIBUTING.md", "````\n", "````\n")
        self.assertNamed(r"^CONTRIBUTING\.md's sign-off heading is not a line of its own outside comments and fences")

    def test_a_sign_off_heading_in_a_comment_or_inside_a_line_is_not_the_section(self) -> None:
        for old, new, after in ((SIGN_OFF_HEADING, "<!--\n" + SIGN_OFF_HEADING, "-->\n"),
                                (SIGN_OFF_HEADING, "Note: " + SIGN_OFF_HEADING, "")):
            with self.subTest(new=new[:8]):
                saved = (self.root / "CONTRIBUTING.md").read_bytes()
                self.edit("CONTRIBUTING.md", old, new)
                self.wrap("CONTRIBUTING.md", "", after)
                self.assertNamed(r"^CONTRIBUTING\.md's sign-off heading is not a line of its own")
                (self.root / "CONTRIBUTING.md").write_bytes(saved)

    def test_security_without_a_part_or_a_sentence(self) -> None:
        for removed in ("> **Draft.**", "## Reporting a vulnerability\n", "## Supported versions\n", "## Scope\n",
                        "## What the code defends, and what it does not yet\n",
                        f"Report it privately to {CONTACT}.",
                        "Please do not report a suspected vulnerability in a public issue"):
            with self.subTest(removed=removed):
                saved = (self.root / "SECURITY.md").read_bytes()
                self.edit("SECURITY.md", removed, "")
                self.assertNamed(r"^SECURITY\.md (has no|does not say) " + re.escape(repr(removed.strip())))
                (self.root / "SECURITY.md").write_bytes(saved)

    def test_a_security_heading_starts_its_line(self) -> None:
        self.edit("SECURITY.md", "## Scope\n", "Out of line: ## Scope\n")
        self.assertNamed(r"^SECURITY\.md has no '## Scope'")

    def test_security_parts_hidden_from_the_reader(self) -> None:
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for wrapped in ("<!--\n" + text + "-->\n", "```\n" + text + "```\n"):
            with self.subTest(wrapped=wrapped[:4]):
                (self.root / "SECURITY.md").write_text(wrapped, encoding="utf-8")
                for part in ("> **Draft.**", "## Reporting a vulnerability", "## Supported versions", "## Scope",
                             "## What the code defends, and what it does not yet"):
                    self.assertIn(f"SECURITY.md has no {part!r}", self.found())

    # The first Codex reads of #409 (GPT-6 Astra R1-R3, GPT-6.1 Sol R1-R2): their tests, on this harness.
    def assert_security_parts_absent(self) -> None:
        found = self.found()
        self.assertIn("SECURITY.md has no '## Supported versions'", found)
        self.assertIn("SECURITY.md has no '## Scope'", found)

    def test_security_tilde_fence_is_not_policy_sections(self) -> None:
        self.wrap("SECURITY.md", "~~~\n", "~~~\n")
        self.assert_security_parts_absent()

    def test_security_indented_fence_is_not_policy_sections(self) -> None:
        self.wrap("SECURITY.md", " ```\n", " ```\n")
        self.assert_security_parts_absent()

    def test_security_unclosed_fence_is_not_policy_sections(self) -> None:
        self.wrap("SECURITY.md", "```\n")
        self.assert_security_parts_absent()

    def test_R1_fenced_policy_has_no_required_visible_parts(self) -> None:
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        variants = {
            "tilde": "~~~markdown\n" + text + "~~~\n",
            "indent-1": " ```markdown\n" + text + " ```\n",
            "indent-2": "  ```markdown\n" + text + "  ```\n",
            "indent-3": "   ```markdown\n" + text + "   ```\n",
            "no-closing-fence": "```markdown\n" + text,
            "shorter-inner-fence": "````markdown\n```\n" + text + "````\n",
        }
        for name, wrapped in variants.items():
            with self.subTest(variant=name):
                (self.root / "SECURITY.md").write_text(wrapped, encoding="utf-8", newline="\n")
                self.assertIn("SECURITY.md has no '## Scope'", self.found())

    def test_security_code_block_cannot_publish_another_contact(self) -> None:
        self.wrap("SECURITY.md", "", f"\n```text\nContact: {self.other('contact@example.invalid')}\n```\n")
        self.assertIn("SECURITY.md holds an e-mail address that is not its contact (CONTACT)", self.found())

    def test_security_comment_cannot_publish_another_address_in_source(self) -> None:
        self.wrap("SECURITY.md", "", f"\n<!-- {self.other('contact@example.invalid')} -->\n")
        self.assertIn("SECURITY.md holds an e-mail address that is not its contact (CONTACT)", self.found())

    def test_R2_quoted_and_literal_domain_addresses_are_forbidden(self) -> None:
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for address in map(self.other, ('"example"@example.invalid', "example@[192.0.2.1]")):
            with self.subTest(address=address):
                self.assertTrue(Address(addr_spec=address).domain)
                (self.root / "SECURITY.md").write_text(text + "\nOr report to " + address + ".\n", encoding="utf-8",
                                                       newline="\n")
                self.assertIn("SECURITY.md holds an e-mail address that is not its contact (CONTACT)", self.found())

    def test_R2_addresses_inside_comments_or_code_are_forbidden(self) -> None:
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        address = self.other("example@example.invalid")
        for addition in (f"\n<!-- {address} -->\n", f"\n```text\n{address}\n```\n"):
            with self.subTest(addition=addition):
                (self.root / "SECURITY.md").write_text(text + addition, encoding="utf-8", newline="\n")
                self.assertIn("SECURITY.md holds an e-mail address that is not its contact (CONTACT)", self.found())

    def test_what_is_and_is_not_a_fence_or_a_comment(self) -> None:
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for name, wrapped, hidden in (
                ("a fence closed before the policy", "```\nx\n```\n" + text, False),
                ("closed by a longer fence and a space", "~~~\nx\n~~~~~ \n" + text, False),
                ("four spaces: indented code, not a fence", "    ```\n" + text, False),
                ("a backtick in a backtick fence's info string: not a fence", "``` a`b\n" + text, False),
                ("a tilde fence is not closed by backticks", "~~~\n```\n" + text + "```\n", True),
                ("nor by a fence with text after it", "```\n``` x\n" + text + "```\n", True),
                ("a comment closed on its line", "<!-- x -->\n" + text, False),
                ("a fence line inside a comment opens nothing", "<!--\n```\n-->\n" + text, False),
                ("a comment never closed", "<!--\n" + text, True),
                ("<!--> is a whole comment", "<!-->\n" + text, False),
                ("so is <!--->", "<!--->\n" + text, False),
                ("a <!-- left open on the line that ends a comment block", "<!-- a --> <!--\n-->\n" + text, True),
                ("a heading after a comment block's --> is raw text, even after a paragraph",
                 text.replace("## Scope\n", "Scope:\n<!-- a -->## Scope\n", 1), True),
                ("a heading ends a paragraph and its open <!--",
                 text.replace("## Scope\n", "Note <!--\n## Scope\n-->\n", 1), False),
                ("a comment after a heading on its line", text.replace("## Scope\n", "## Scope<!-- proposed -->\n", 1),
                 False)):
            with self.subTest(case=name):
                (self.root / "SECURITY.md").write_text(wrapped, encoding="utf-8", newline="\n")
                if hidden:
                    self.assertIn("SECURITY.md has no '## Scope'", self.found())
                else:
                    self.assertEqual(self.found(), [])

    def test_a_sentence_in_a_one_line_comment_is_hidden(self) -> None:
        sentence = f"Report it privately to {CONTACT}."
        self.edit("SECURITY.md", sentence, f"<!-- {sentence} -->")
        self.assertNamed(r"^SECURITY\.md does not say " + re.escape(repr(sentence)))

    def test_text_beside_a_comment_on_its_line_is_read(self) -> None:
        sentence = f"Report it privately to {CONTACT}."
        self.edit("SECURITY.md", sentence, f"<!-- a -->{sentence}<!-- b -->")
        self.assertEqual(self.found(), [])

    def test_a_comment_in_a_paragraph_hides_its_text_and_no_more(self) -> None:
        sentence = f"Report it privately to {CONTACT}."
        for new, hidden in ((f"Write to us. <!-- {sentence} -->", True),  # after text on its line
                            (f"Write to us. <!--\n{sentence}\n-->", True),  # over the lines of one paragraph
                            (f"Write to us. <!--\n\n{sentence}\n\n-->", False),  # a blank line ends the paragraph
                            (f"Report it <!-- privately -->privately to {CONTACT}.", False)):  # the text closes over it
            with self.subTest(new=new):
                saved = (self.root / "SECURITY.md").read_bytes()
                self.edit("SECURITY.md", sentence, new)
                if hidden:
                    self.assertNamed(r"^SECURITY\.md does not say " + re.escape(repr(sentence)))
                else:
                    self.assertEqual(self.found(), [])
                (self.root / "SECURITY.md").write_bytes(saved)

    def test_an_empty_security_section(self) -> None:
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for heading in ("## Reporting a vulnerability\n", "## Supported versions\n", "## Scope\n",
                        "## What the code defends, and what it does not yet\n"):
            with self.subTest(heading=heading):
                emptied = re.sub(rf"({re.escape(heading)}).*?(\n## |\Z)", r"\1\n\2", text, count=1, flags=re.S)
                self.assertNotEqual(emptied, text)
                (self.root / "SECURITY.md").write_text(emptied, encoding="utf-8", newline="\n")
                self.assertNamed(rf"^SECURITY\.md's section {re.escape(repr(heading.strip()))} is empty")

    def test_security_gutted_to_its_two_sentences(self) -> None:
        (self.root / "SECURITY.md").write_text(
            "Please do not report a suspected vulnerability in a public issue.\n\n"
            f"Report it privately to {CONTACT}.\n", encoding="utf-8")
        found = self.found()
        for part in ("> **Draft.**", "## Reporting a vulnerability", "## Supported versions", "## Scope",
                     "## What the code defends, and what it does not yet"):
            self.assertIn(f"SECURITY.md has no {part!r}", found)

    def test_security_with_an_address(self) -> None:
        for address in map(self.other, ("jane.doe@example.org", "triage@localhost", "JANE.DOE@EXAMPLE.ORG")):
            with self.subTest(address=address):
                saved = (self.root / "SECURITY.md").read_bytes()
                with (self.root / "SECURITY.md").open("a", encoding="utf-8") as f:
                    f.write(f"\nOr write to {address}.\n")
                self.assertNamed(r"^SECURITY\.md holds an e-mail address that is not its contact")
                (self.root / "SECURITY.md").write_bytes(saved)

    def test_a_named_contact_may_be_the_one_address(self) -> None:
        current, named = CONTACT, "security-review@example.invalid"
        with mock.patch.dict(globals(), {"CONTACT": named}):
            self.edit("SECURITY.md", current, named)
            self.assertEqual(self.found(), [])
            for line, others in ((f"Write to **{named}**, `{named}`, <{named}> or [{named}](mailto:{named}).", False),
                                 (f"Or write to evil.{named}.", True), (f"Or write to {named}.evil.example.", True),
                                 ('Or write to "security-review"@example.invalid.', True),
                                 ("Or write to jane.doe@example.org.", True),
                                 (f"<!-- {named} -->", False),  # in a comment or a code block, exactly
                                 (f"<!-- **{named}** -->", True), (f"```text\n`{named}`\n```", True),
                                 ("Or not <!-- jane.doe@example.org -->.", True),
                                 (f"Write to {named}<!-- a -->.evil.example.", True),  # as a reader sees it
                                 (f"Write to _{named}_ or ~~{named}~~.", False),
                                 (f"Or write to *{named}`.", True), (f"Or write to #{named}#.", True),
                                 (f"Or write to evil..{named}.", True), (f"Or write to {named}..evil.", True),
                                 (f"Write to {named}, not to team@.", False)):
                with self.subTest(line=line):
                    saved = (self.root / "SECURITY.md").read_bytes()
                    self.wrap("SECURITY.md", "", "\n" + line + "\n")
                    if others:
                        self.assertNamed(r"^SECURITY\.md holds an e-mail address that is not its contact")
                    else:
                        self.assertEqual(self.found(), [])
                    (self.root / "SECURITY.md").write_bytes(saved)

    # DeepSeek's third read of #409 (F1, F3, F4, F6): its cases, on this harness.
    def test_a_file_near_the_size_limit_is_checked_in_time_linear_in_its_size(self) -> None:
        """F1: an address pattern tried at every offset of SIZE_LIMIT bytes of dotted atoms ran for
        minutes. Each shape here grows the work of a scan of that kind with the square of its size;
        the checks run as a child that must answer within 60 s."""
        if os.environ.get("LICENCE_CHECKS_CHILD"):
            self.skipTest("the parent runs it: it holds for any contact or holder")
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        shapes = ("a." * 150000, '\\"@x' * 20000, "*" * 40000 + "x@example.invalid" + "*" * 40000,
                  "@[" * 20000, "x " + "<!--" * 20000, "a@" * 20000, '"' * 40000 + "@x", "`x``y" * 15000,
                  "\\`" * 20000, "- a <!--\n" * 3000, "> <!--\n" * 2000, "<!--" * 20000)
        body = text + "\n" + "\n\n".join(shapes) + "\n"
        self.assertLess(len(body.encode("utf-8")), SIZE_LIMIT)
        (self.root / "SECURITY.md").write_text(body, encoding="utf-8", newline="\n")
        code = ("import importlib.util, json, sys; "
                "spec = importlib.util.spec_from_file_location('checks', sys.argv[1]); "
                "checks = importlib.util.module_from_spec(spec); spec.loader.exec_module(checks); "
                "print(json.dumps(checks.problems(sys.argv[2])))")
        flags = [f"-X{k}" if v is True else f"-X{k}={v}" for k, v in sys._xoptions.items()]
        try:
            run = subprocess.run([sys.executable, *flags, "-B", "-c", code, __file__, str(self.root)],
                                 capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            self.fail("the checks did not answer within 60 s")
        self.assertEqual(json.loads(run.stdout or "null"), [self.EMAIL_PROBLEM], run.stderr[-2000:])

    def test_an_address_a_reader_sees_is_refused(self) -> None:
        """F3: an address spelled with a comment, a backslash escape or a character reference is the
        address a reader sees."""
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        local, domain = self.other("janet.doe@example.org").rsplit("@", 1)
        first, rest = local.split(".", 1)
        for line in (f"Or write to {local}<!-- hidden -->@{domain}.", f"Or write to {first}<!--x-->.{rest}@{domain}.",
                     f"Or write to {local}&#64;{domain}.", f"Or write to {local}&#x40;{domain}.",
                     f"Or write to {local}&commat;{domain}.", f"Or write to {local}\\@{domain}."):
            with self.subTest(line=line):
                (self.root / "SECURITY.md").write_text(text + "\n" + line + "\n", encoding="utf-8", newline="\n")
                self.assertIn(self.EMAIL_PROBLEM, self.found())

    def test_every_copy_of_a_section_is_held_not_empty(self) -> None:
        """F4: a second, empty copy of a section after a full one is named too."""
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for changed in (text + "\n## Scope\n\n## Scope\n\nMore scope.\n",
                        text.replace("## Scope\n", "## Scope\n\n## Scope\n", 1)):
            with self.subTest(changed=changed[-40:]):
                (self.root / "SECURITY.md").write_text(changed, encoding="utf-8", newline="\n")
                self.assertNamed(r"^SECURITY\.md's section '## Scope' is empty")

    def test_a_file_that_cannot_be_read_is_named(self) -> None:
        """F6: a file that is there, under SIZE_LIMIT, and cannot be read is named, not raised."""
        read_bytes = Path.read_bytes
        for rel in FILES:
            def refuse(path: Path, rel: str = rel) -> bytes:
                if path.name == rel:
                    raise PermissionError(13, "Permission denied", str(path))
                return read_bytes(path)
            with self.subTest(rel=rel), mock.patch.object(Path, "read_bytes", refuse):
                self.assertNamed(rf"^{re.escape(rel)} cannot be read \(PermissionError\)$")

    def test_what_is_and_is_not_an_address(self) -> None:
        """An `@` with no local part, no domain, or a quoted string that its line or an escape leaves
        open is no address; in a code block, where nothing is Markdown, each is read as written."""
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for block, address in (("@team, team@ and team@. are no address", False),
                               ('"open\nquote"@example.invalid', False), ('"open\\"@example.invalid', False),
                               (self.other('"closed\\\\"@example.invalid'), True),
                               (self.other('x"q"@example.invalid'), True)):
            with self.subTest(block=block):
                (self.root / "SECURITY.md").write_text(text + "\n```text\n" + block + "\n```\n", encoding="utf-8",
                                                       newline="\n")
                if address:
                    self.assertIn(self.EMAIL_PROBLEM, self.found())
                else:
                    self.assertEqual(self.found(), [])

    def test_a_contact_inside_a_longer_local_part_is_another_address(self) -> None:
        named = '"q r"@example.invalid'
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8").replace(CONTACT, named)
        with mock.patch.dict(globals(), {"CONTACT": named}):
            for line, other in ((f"Write to **{named}**.", False), (f"Or write to a.{named}.", True),
                                (f"Or write to x{named}.", True), (f"Or write to a.{named}..evil.", True),
                                ('```text\n"a\\"q r"@example.invalid\n```', True)):  # its opening quote escaped
                with self.subTest(line=line):
                    (self.root / "SECURITY.md").write_text(text + "\n" + line + "\n", encoding="utf-8", newline="\n")
                    if other:
                        self.assertIn(self.EMAIL_PROBLEM, self.found())
                    else:
                        self.assertEqual(self.found(), [])

    # The second Codex reads of #409 (GPT-6 Astra 1-3, GPT-6.1 Sol 1-2): their tests, on this harness.
    def test_R1_inline_comment_state_cannot_hide_a_fence(self) -> None:
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for prefix in ("Literal marker: `<!--`\n\n~~~\n-->\n", "Paragraph <!--\n~~~\n-->\n"):
            with self.subTest(prefix=prefix):
                (self.root / "SECURITY.md").write_text(prefix + text + "\n~~~\n", encoding="utf-8", newline="\n")
                self.assertIn("SECURITY.md has no '## Scope'", self.found())

    def test_R2_complete_unapproved_local_part_is_not_contact(self) -> None:
        named = "triage@example.invalid"
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8").replace(CONTACT, named)
        with mock.patch.dict(globals(), {"CONTACT": named}):
            for mark in "*`":
                other = "prefix" + mark + named
                self.assertEqual(Address(addr_spec=other).username, "prefix" + mark + "triage")
                for before, after in (("\n", "\n"), ("\n~~~\n", "\n~~~\n"), ("\n<!-- ", " -->\n")):
                    with self.subTest(address=other, before=before):
                        (self.root / "SECURITY.md").write_text(text + before + other + after, encoding="utf-8",
                                                               newline="\n")
                        self.assertIn(self.EMAIL_PROBLEM, self.found())

    def test_R2_special_only_local_part_is_not_missed(self) -> None:
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for address in map(self.other, ("*@example.invalid", "`@example.invalid")):
            self.assertTrue(Address(addr_spec=address).domain)
            with self.subTest(address=address):
                (self.root / "SECURITY.md").write_text(text + "\n" + address + "\n", encoding="utf-8", newline="\n")
                self.assertIn(self.EMAIL_PROBLEM, self.found())

    def test_R2_complete_configured_contact_is_allowed(self) -> None:
        source = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for named in ("security*review@example.invalid", "security`review@example.invalid"):
            Address(addr_spec=named)
            text = source.replace(CONTACT, named)
            with self.subTest(contact=named), mock.patch.dict(globals(), {"CONTACT": named}):
                (self.root / "SECURITY.md").write_text(text, encoding="utf-8", newline="\n")
                self.assertEqual(self.found(), [])

    def test_R2_atom_character_guard(self) -> None:
        named = "triage@example.invalid"
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8").replace(CONTACT, named)
        with mock.patch.dict(globals(), {"CONTACT": named}):
            for mark in "!#$%&'*+-/=?^_`{|}~":  # every character of RFC 5322's atext that is not a letter or digit
                other = "prefix" + mark + named
                Address(addr_spec=other)
                with self.subTest(address=other):
                    (self.root / "SECURITY.md").write_text(text + "\n" + other + "\n", encoding="utf-8", newline="\n")
                    self.assertIn(self.EMAIL_PROBLEM, self.found())

    def test_rfc_atom_characters_in_source_are_addresses(self) -> None:
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for address in map(self.other, ("*@example.invalid", "`@example.invalid")):
            for before, after in (("\n", "\n"), ("\n<!-- ", " -->\n"), ("\n```text\n", "\n```\n")):
                with self.subTest(address=address, before=before):
                    self.assertEqual(Address(addr_spec=address).addr_spec, address)
                    (self.root / "SECURITY.md").write_text(text + before + address + after, encoding="utf-8",
                                                           newline="\n")
                    self.assertIn(self.EMAIL_PROBLEM, self.found())

    def test_atom_containing_contact_is_a_different_address(self) -> None:
        named = "security@example.invalid"
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8").replace(CONTACT, named)
        for address in ("evil*" + named, "evil`" + named):
            with self.subTest(address=address), mock.patch.dict(globals(), {"CONTACT": named}):
                self.assertEqual(Address(addr_spec=address).username, address.split("@")[0])
                (self.root / "SECURITY.md").write_text(text + "\n```text\n" + address + "\n```\n", encoding="utf-8",
                                                       newline="\n")
                self.assertIn(self.EMAIL_PROBLEM, self.found())

    # Sol's third read (1-4), with DeepSeek's fourth (F1): their tests, on this harness.
    def test_confirming3_backslash_mailbox_is_not_lost_in_inline_code(self) -> None:
        address = self.other('"audit' + '\\\\' + '"@example.invalid')
        self.assertTrue(Address(addr_spec=address).domain)
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for extra in (address, "`" + address + "`"):
            with self.subTest(extra=extra):
                (self.root / "SECURITY.md").write_text(text + "\n" + extra + "\n", encoding="utf-8")
                self.assertIn(self.EMAIL_PROBLEM, self.found())

    def test_confirming3_at_inside_contact_is_not_a_second_address(self) -> None:
        current = CONTACT
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for named in ('"audit@desk"@example.invalid', "audit@[desk@inbox]"):
            self.assertTrue(Address(addr_spec=named).domain)
            with self.subTest(named=named), mock.patch.dict(globals(), {"CONTACT": named}):
                (self.root / "SECURITY.md").write_text(text.replace(current, named), encoding="utf-8")
                self.assertEqual(self.found(), [])

    def test_confirming3_quoted_source_cannot_become_contact(self) -> None:
        current, named = CONTACT, '"audit"@example.invalid'
        other = '"evil \\"audit"@example.invalid'
        self.assertNotEqual(Address(addr_spec=other).username, Address(addr_spec=named).username)
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8").replace(current, named)
        with mock.patch.dict(globals(), {"CONTACT": named}):
            (self.root / "SECURITY.md").write_text(text + "\n" + other + "\n", encoding="utf-8")
            self.assertIn(self.EMAIL_PROBLEM, self.found())

    def test_confirming3_literal_tilde_runs_are_not_contact_markup(self) -> None:
        current, named = CONTACT, "security@example.invalid"
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8").replace(current, named)
        with mock.patch.dict(globals(), {"CONTACT": named}):
            for marks in ("~~~", "~~~~"):
                with self.subTest(marks=marks):
                    (self.root / "SECURITY.md").write_text(text + "\nOr write to " + marks + named + marks + ".\n", encoding="utf-8")
                    self.assertIn(self.EMAIL_PROBLEM, self.found())

    def test_confirming3_new_negative_fixture_can_be_contact(self) -> None:
        self.name_and_rerun("CONTACT", CONTACT, "SECURITY.md", "*@example.invalid")

    def test_confirming3_literal_comment_markers_do_not_hide_sentence(self) -> None:
        sentence = f"Report it privately to {CONTACT}."
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for replacement in ("A marker is `<!--`. " + sentence + " Its end is `-->`.",
                            "The text \\<!-- " + sentence + " --> is literal."):
            with self.subTest(replacement=replacement):
                (self.root / "SECURITY.md").write_text(text.replace(sentence, replacement), encoding="utf-8")
                self.assertEqual(self.found(), [])

    def test_confirming3_unclosed_marker_does_not_hide_same_paragraph_sentence(self) -> None:
        sentence = f"Report it privately to {CONTACT}."
        self.edit("SECURITY.md", sentence, "A marker is `<!--`. " + sentence)
        self.assertEqual(self.found(), [])

    def test_confirming3_container_comment_cannot_expose_policy(self) -> None:
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for prefix in ("- <!--\n\n", "> <!--\n\n", "1. <!--\n\n"):
            with self.subTest(prefix=prefix):
                (self.root / "SECURITY.md").write_text(prefix + text, encoding="utf-8")
                self.assertIn("SECURITY.md has no '## Scope'", self.found())

    def test_a_comment_a_quote_or_a_list_item_opens_ends_inside_it(self) -> None:
        """Its `-->` must stand on a line inside that quote or item; one after it ends nothing, and the
        comment, left open, hides the rest of the file as a browser reads it."""
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for prefix, hidden in (("> <!--\n> a note\n> -->\n\n", False), ("- <!-- a\n  note -->\n\n", False),
                               ("- <!-- a\n\n  note -->\n\n", False),  # a blank line inside the item
                               ("> <!--\n-->\n", True), ("- <!--\n\n-->\n", True), ("> - <!--\n> -->\n", True)):
            with self.subTest(prefix=prefix):
                (self.root / "SECURITY.md").write_text(prefix + text, encoding="utf-8", newline="\n")
                if hidden:
                    self.assertIn("SECURITY.md has no '## Scope'", self.found())
                else:
                    self.assertEqual(self.found(), [])

    def test_a_paragraph_ends_at_a_list_item_a_deeper_quote_or_a_rule(self) -> None:
        """A `<!--` cannot reach past them to a `-->`: the policy after it stays in sight."""
        sentence = f"Report it privately to {CONTACT}."
        for new in (f"Write to us <!--\n- {sentence}\n-->", f"Write to us <!--\n> {sentence}\n> -->",
                    f"Write to us <!--\n***\n{sentence}\n-->", f"Write to us <!--\n1. {sentence}\n-->",
                    f"1. Write to us <!--\n2. {sentence}\n-->", f"> Write to us <!--\n>\n> {sentence}\n> -->"):
            with self.subTest(new=new):
                saved = (self.root / "SECURITY.md").read_bytes()
                self.edit("SECURITY.md", sentence, new)
                self.assertEqual(self.found(), [])
                (self.root / "SECURITY.md").write_bytes(saved)

    def test_a_lazy_line_stays_in_its_block_quote_paragraph(self) -> None:
        """A line without the quote's marker goes on in the quote's paragraph, so the comment opened there
        still hides it; the next line with the marker is no deeper quote."""
        sentence = f"Report it privately to {CONTACT}."
        self.edit("SECURITY.md", sentence, f"> Write to us. <!--\n{sentence}\n> -->")
        self.assertNamed(r"^SECURITY\.md does not say " + re.escape(repr(sentence)))

    def test_script_and_style_hide_what_they_hold(self) -> None:
        """DeepSeek's fourth read, F1: a browser shows no text of a script or a style element."""
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        for wrapped, hidden in (("<script>\n" + text + "</script>\n", True),
                                ('<STYLE type="text/css">\n' + text, True),  # never ended: the rest of the file
                                ("<script>var x;</script>\n" + text, False), ("<STYLE>p {}</STYLE>\n" + text, False),
                                ("<STYLE>p {}</style>\n" + text, False),  # an end tag in another case ends it
                                ("<scripts>\n" + text, False)):
            with self.subTest(wrapped=wrapped[:24]):
                (self.root / "SECURITY.md").write_text(wrapped, encoding="utf-8", newline="\n")
                if hidden:
                    self.assertIn("SECURITY.md has no '## Scope'", self.found())
                else:
                    self.assertEqual(self.found(), [])
        sentence = f"Report it privately to {CONTACT}."
        (self.root / "SECURITY.md").write_text(text.replace(sentence, "<script>x</script>" + sentence),
                                               encoding="utf-8", newline="\n")
        self.assertEqual(self.found(), [])  # text after the end tag, on its line, is in sight

    def test_only_markdown_wrapping_makes_the_contact(self) -> None:
        """Sol 2: CONTACT is exempt wrapped as Markdown wraps it, not inside code or behind unequal marks."""
        named = "security@example.invalid"
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8").replace(CONTACT, named)
        with mock.patch.dict(globals(), {"CONTACT": named}):
            for line, other in ((f"Write to ***{named}***, _*{named}*_, ~{named}~ or ``{named}``.", False),
                                (f"Or write to `*{named}*`.", True), (f"Or write to *{named}**.", True),
                                (f"Or write to ~~~{named}~~~.", True), (f"Or write to `{named}``.", True)):
                with self.subTest(line=line):
                    (self.root / "SECURITY.md").write_text(text + "\n" + line + "\n", encoding="utf-8", newline="\n")
                    if other:
                        self.assertIn(self.EMAIL_PROBLEM, self.found())
                    else:
                        self.assertEqual(self.found(), [])

    def test_code_spans_are_read_as_written(self) -> None:
        """Sol 1: in a code span a reader sees escapes and character references as written."""
        named = "security@example.invalid"
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8").replace(CONTACT, named)
        with mock.patch.dict(globals(), {"CONTACT": named}):
            for line, other in (("Code: `jane\\@example.org` and `jane&#64;example.org`.", False),
                                ("Text: jane\\@example.org and jane&#64;example.org.", True)):
                with self.subTest(line=line):
                    (self.root / "SECURITY.md").write_text(text + "\n" + line + "\n", encoding="utf-8", newline="\n")
                    if other:
                        self.assertIn(self.EMAIL_PROBLEM, self.found())
                    else:
                        self.assertEqual(self.found(), [])

    # Sol's sweep of 97f1a4ca against master (F1, F2) and Devin's threads on it: their cases, on this harness.
    def test_sweep_quoted_contact_with_at_in_local_part(self) -> None:
        named = '"audit@desk"@example.invalid'
        self.assertEqual(Address(addr_spec=named).username, "audit@desk")
        self.edit("SECURITY.md", CONTACT, named)
        with mock.patch.dict(globals(), {"CONTACT": named}):
            self.assertEqual(self.found(), [])

    def test_sweep_contact_with_literal_ampersand_prefix(self) -> None:
        named = "security&notices@example.invalid"
        self.assertEqual(Address(addr_spec=named).addr_spec, named)
        self.edit("SECURITY.md", CONTACT, named)
        with mock.patch.dict(globals(), {"CONTACT": named}):
            self.assertEqual(self.found(), [])

    def test_a_character_reference_needs_its_semicolon(self) -> None:
        """Sweep F2: as CommonMark reads it, `&amp;` is `&` and `&amp` stays as written."""
        named = "jane&amp@example.org"
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8").replace(CONTACT, named)
        with mock.patch.dict(globals(), {"CONTACT": named}):
            for line, other in (("Or write to jane&amp&#64;example.org.", False),
                                ("Or write to jane&amp;&#64;example.org.", True)):
                with self.subTest(line=line):
                    (self.root / "SECURITY.md").write_text(text + "\n" + line + "\n", encoding="utf-8", newline="\n")
                    if other:
                        self.assertIn(self.EMAIL_PROBLEM, self.found())
                    else:
                        self.assertEqual(self.found(), [])

    def test_raw_html_passes_its_comments_to_the_browser(self) -> None:
        """Devin: a line that opens raw HTML runs to a blank line, and a `<!--` left open there hides the
        rest of the file; a tag inside a paragraph opens nothing."""
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8")
        sentence = f"Report it privately to {CONTACT}."
        for changed, hidden in (("<div><!--\n</div>\n\n" + text, "all"),
                                ("<div>\n</div>\n\n```\n" + text + "```\n", "all"),  # a fence after the blank line
                                ("<div>\nnote <!-- a -->\n</div>\n\n" + text, ""),
                                ("<details>\n\n" + text, ""),  # a reader sees what it holds
                                (text.replace(sentence, f"Write to us <!--\n<p>{sentence}</p>\n-->"), ""),
                                (text.replace(sentence, f"Write to us <!--\n<b>x</b> {sentence} -->"), "sentence")):
            with self.subTest(changed=changed[:30]):
                (self.root / "SECURITY.md").write_text(changed, encoding="utf-8", newline="\n")
                if hidden == "all":
                    self.assertIn("SECURITY.md has no '## Scope'", self.found())
                elif hidden:
                    self.assertNamed(r"^SECURITY\.md does not say " + re.escape(repr(sentence)))
                else:
                    self.assertEqual(self.found(), [])

    def test_a_bracketed_domain_contact_may_be_formatted(self) -> None:
        """Devin: Markdown's marks after a bracketed domain wrap the address too."""
        named = "security@[192.0.2.1]"
        text = (self.root / "SECURITY.md").read_text(encoding="utf-8").replace(CONTACT, named)
        with mock.patch.dict(globals(), {"CONTACT": named}):
            for line, other in ((f"Write to **{named}** or `{named}`.", False), (f"Or write to *{named}**.", True)):
                with self.subTest(line=line):
                    (self.root / "SECURITY.md").write_text(text + "\n" + line + "\n", encoding="utf-8", newline="\n")
                    if other:
                        self.assertIn(self.EMAIL_PROBLEM, self.found())
                    else:
                        self.assertEqual(self.found(), [])

    def named_in(self, source: str, constant: str, old: str, new: str) -> str:
        """`source` with the one top-level assignment of `constant`, found as Python reads it however
        its value `old` is spelled, made to assign `new`, which repr() spells."""
        nodes = [node for node in ast.parse(source).body if isinstance(node, ast.Assign) and len(node.targets) == 1
                 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == constant]
        self.assertEqual(len(nodes), 1, f"{constant} is not assigned once at the top of the file")
        self.assertEqual(ast.literal_eval(nodes[0].value), old)
        statement = ast.get_source_segment(source, nodes[0])
        start = sum(len(line) for line in re.split(r"(?<=\n)", source)[:nodes[0].lineno - 1])
        self.assertTrue(statement and source.startswith(statement, start))
        return source[:start] + f"{constant} = {new!r}" + source[start + len(statement):]

    def test_a_constant_is_found_however_its_value_is_spelled(self) -> None:
        value = '"audit desk"@example.invalid'
        for spelled in ('"\\"audit desk\\"@example.invalid"', repr(value), '"\\x22audit desk\\x22@example.invalid"',
                        '(\n    "\\"audit desk\\""\n    "@example.invalid")'):
            with self.subTest(spelled=spelled):
                source = f"HOLDER = 'h'\nCONTACT = {spelled}  # named\nOTHER = 1\n"
                self.assertEqual(self.named_in(source, "CONTACT", value, "x@example.invalid"),
                                 "HOLDER = 'h'\nCONTACT = 'x@example.invalid'  # named\nOTHER = 1\n")

    def name_and_rerun(self, constant: str, old: str, rel: str, new: str) -> None:
        """Make the two edits that name `constant` `new`, in it and in `rel`, in a copy of the four files
        and of this file, and run that copy's tests as a child: they must pass."""
        if os.environ.get("LICENCE_CHECKS_CHILD"):
            self.skipTest("this is that run")
        source = Path(__file__).read_text(encoding="utf-8")
        tree = Path(tempfile.mkdtemp(prefix="named-", dir=self.root.parent))
        for name in FILES:
            (tree / name).write_bytes((self.root / name).read_bytes())
        (tree / "tools/release").mkdir(parents=True)
        (tree / "tools/release/licence_checks.py").write_text(self.named_in(source, constant, old, new),
                                                              encoding="utf-8", newline="\n")
        text = (tree / rel).read_text(encoding="utf-8")
        self.assertIn(old, text)
        (tree / rel).write_text(text.replace(old, new), encoding="utf-8", newline="\n")
        flags = [f"-X{k}" if v is True else f"-X{k}={v}" for k, v in sys._xoptions.items()]
        run = subprocess.run([sys.executable, *flags, "-B", str(tree / "tools/release/licence_checks.py")],
                             cwd=tree, capture_output=True, text=True, timeout=600,
                             env={**os.environ, "LICENCE_CHECKS_CHILD": "1"})
        self.assertEqual(run.returncode, 0, run.stderr[-3000:])

    def test_approved_contact_two_edits_keep_suite_green(self) -> None:
        """Naming the contact (CONTACT and SECURITY.md) or the holder (HOLDER and NOTICE), as the
        comments above them say, keeps this file's tests green: they run again, as a child, on a copy
        of the four files and of this file with the two edits made."""
        for constant, old, rel, new in (("CONTACT", CONTACT, "SECURITY.md", self.other("security@example.invalid")),
                                        ("HOLDER", HOLDER, "NOTICE", self.other("Example Review Entity", HOLDER))):
            with self.subTest(constant=constant):
                self.name_and_rerun(constant, old, rel, new)

    def test_R3_contact_may_equal_a_negative_fixture(self) -> None:
        """So does a contact or a holder that is a value the tests refuse where it is not the one named."""
        for constant, old, rel, new in (("CONTACT", CONTACT, "SECURITY.md", "jane.doe@example.org"),
                                        ("HOLDER", HOLDER, "NOTICE", "Jane Doe")):
            with self.subTest(constant=constant):
                self.name_and_rerun(constant, old, rel, new)

    def test_R3_quoted_contact_two_edits_keep_suite_green(self) -> None:
        """And a quoted contact (Astra's and Sol's), which Python spells with escapes or other quotes."""
        for new in ('"audit desk"@example.invalid', '"audit.release"@example.invalid', '"q\\\\r"@example.invalid'):
            with self.subTest(contact=new):
                self.name_and_rerun("CONTACT", CONTACT, "SECURITY.md", new)


if __name__ == "__main__":
    unittest.main()
