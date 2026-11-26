"""The read tool: a file's lines with their numbers, a page at a time, or a folder's entries.

From the research (vault records 20261028-recursion-frontier-b4-tool-output-spill-04 section 2 and
20261028-recursion-frontier-dr-tool-editing-03 F14): pages of at most 2,000 lines that also fit the output
cap, each line cut at 2,000 characters, and a continuation that says which offset to use next (OpenCode, Pi);
a file is binary, and not shown, if its first 8 KiB hold a NUL byte or more than 1% of its first 64 KiB is
not UTF-8. Lines are numbered as ``cat -n`` numbers them (the records recommend no format; this is the one
Claude Code's read tool uses), so an edit can name its place.

Only regular files and folders are read: anything else is refused before it is opened. The file is opened
without blocking (a FIFO cannot hang the loop) and checked again after it is open, for being a regular file;
whether it is inside the workspace is decided before the open, from the path resolved through its links.
The whole file is hashed as it is read, and the digest goes into the read ledger that the edit tool will
check.
"""

from __future__ import annotations

import codecs
import hashlib
import os
import stat
from collections.abc import Mapping
from typing import Any

from recursion_core.loop.providers import base as b
from recursion_core.loop.tools.registry import Tool, ToolContext, ToolOutput
from recursion_core.loop.tools.workspace import ReadMark, ToolError

MAX_LINES = 2000
MAX_LINE_CHARS = 2000
MAX_DIR_ENTRIES = 1000
MAX_SCAN_BYTES = 256 * 1024 * 1024  # past this the file is shown but not counted, hashed or put in the ledger
SNIFF_NUL = 8 * 1024
SNIFF_UTF8 = 64 * 1024
TRAILER_ROOM = 300

SPEC = b.ToolSpec(
    name="read",
    description=(
        "Read a text file from the workspace, with line numbers, or list a folder. Shows at most 2000 lines at a "
        "time; use offset and limit to page through a longer file. Paths are relative to the workspace root. A "
        "name like spill:<hex> reads a saved copy of an earlier output that was too large to show. Binary files "
        "are not shown."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File or folder, relative to the workspace root."},
            "offset": {"type": "integer", "minimum": 1, "description": "The first line to show (1 is the first)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LINES,
                      "description": "How many lines to show (at most 2000)."},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)


def _damaged(sample: bytes) -> int:
    """The places in ``sample`` that are not UTF-8: one U+FFFD each in the replaced decode, less the U+FFFDs the
    text itself holds (the bytes EF BF BD, which no other sequence can swallow because EF starts a character)."""

    text = codecs.getincrementaldecoder("utf-8")("replace").decode(sample, final=False)
    return text.count("\ufffd") - sample.count(b"\xef\xbf\xbd")


def is_binary(head: bytes) -> bool:
    if b"\x00" in head[:SNIFF_NUL]:
        return True
    sample = head[:SNIFF_UTF8]
    if not sample:
        return False
    return _damaged(sample) * 100 > len(sample)


def _list_dir(path: os.PathLike, shown: str) -> ToolOutput:
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
    except OSError as exc:
        raise ToolError(f"cannot list {shown}: {exc.strerror or exc}") from None
    lines = []
    for entry in entries[:MAX_DIR_ENTRIES]:
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            is_dir = False
        lines.append(entry.name + ("/" if is_dir else ""))
    if not entries:
        return ToolOutput(f"{shown}/ is an empty folder")
    out = f"{shown}/ ({len(entries)} entries)\n" + "\n".join(lines)
    if len(entries) > MAX_DIR_ENTRIES:
        out += f"\n[{len(entries) - MAX_DIR_ENTRIES} more entries not shown; read a subfolder or use search]"
    return ToolOutput(out)


def _render(line_no: int, line: str, budget: int) -> str:
    """One numbered line. A line longer than MAX_LINE_CHARS, or wider than the page's room, is cut here with a
    note, so the registry never cuts the page a second time."""

    full = len(line)
    shown = line[:MAX_LINE_CHARS]
    note = f" [line cut: {full} characters]" if full > MAX_LINE_CHARS else ""
    rendered = f"{line_no:>6}\t{shown}{note}\n"
    if len(rendered.encode("utf-8")) > budget:
        note = f" [line cut: {full} characters]"
        room = max(0, budget - len(f"{line_no:>6}\t{note}\n".encode("utf-8")))
        shown = shown.encode("utf-8")[:room].decode("utf-8", "ignore")
        rendered = f"{line_no:>6}\t{shown}{note}\n"
    return rendered


def run(args: Mapping[str, Any], ctx: ToolContext) -> ToolOutput:
    ws = ctx.workspace
    path = ws.resolve(args["path"])
    shown = ws.display(path)
    try:
        info = os.stat(path)
    except FileNotFoundError:
        raise ToolError(f"{shown} does not exist") from None
    except OSError as exc:
        raise ToolError(f"cannot read {shown}: {exc.strerror or exc}") from None
    if stat.S_ISDIR(info.st_mode):
        return _list_dir(path, shown)
    if not stat.S_ISREG(info.st_mode):
        raise ToolError(f"{shown} is not a regular file")
    offset = int(args.get("offset", 1))
    # A page also fits the registry's line cap (one line is kept for the continuation), so it is never cut twice.
    limit = min(int(args.get("limit", MAX_LINES)), max(1, ctx.max_lines - 1))
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ToolError(f"cannot open {shown}: {exc.strerror or exc}") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ToolError(f"{shown} is not a regular file")
        return _read_lines(fh, st, path, shown, offset, limit, ctx)


def _read_lines(fh, st: os.stat_result, path, shown: str, offset: int, limit: int, ctx: ToolContext) -> ToolOutput:
    digest = hashlib.sha256()
    head = fh.read(SNIFF_UTF8)
    digest.update(head)
    if is_binary(head):
        return ToolOutput(f"{shown} is a binary file ({st.st_size} bytes); it is not shown", "error")
    budget = max(1024, ctx.max_bytes - TRAILER_ROOM)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    out: list[str] = []
    used = 0
    line_no = 0
    last_shown = 0
    stopped_for_room = False
    scanned = len(head)
    pending = decoder.decode(head, final=False)
    complete_scan = True
    while True:
        lines = pending.split("\n")
        pending = lines.pop()  # the unfinished last line waits for more text
        for line in lines:
            line_no += 1
            if offset <= line_no < offset + limit and not stopped_for_room:
                line = line[:-1] if line.endswith("\r") else line
                rendered = _render(line_no, line, budget)
                size = len(rendered.encode("utf-8"))
                if used + size > budget and out:
                    stopped_for_room = True
                else:
                    out.append(rendered)
                    used += size
                    last_shown = line_no
        if scanned >= MAX_SCAN_BYTES:
            complete_scan = False
            break
        chunk = fh.read(1024 * 1024)
        if not chunk:
            pending += decoder.decode(b"", final=True)
            if pending:
                line_no += 1
                if offset <= line_no < offset + limit and not stopped_for_room:
                    rendered = _render(line_no, pending, budget)
                    if used + len(rendered.encode("utf-8")) > budget and out:
                        stopped_for_room = True
                    else:
                        out.append(rendered)
                        last_shown = line_no
            break
        scanned += len(chunk)
        digest.update(chunk)
        pending += decoder.decode(chunk, final=False)
        if ctx.cancel.cancelled:
            raise b.Cancelled()
    total = line_no
    if complete_scan:
        ctx.workspace.ledger.record(path, ReadMark(digest.hexdigest(), scanned, st.st_mtime_ns))
    if total == 0:
        return ToolOutput(f"{shown} is empty")
    if not out:
        if offset > total and complete_scan:
            return ToolOutput(f"{shown} has {total} lines; offset {offset} is past the end", "error")
        return ToolOutput(f"{shown}: nothing to show at offset {offset}", "error")
    text = "".join(out)
    first = offset
    of = f"{total}" if complete_scan else f"at least {total}"
    if last_shown < total or not complete_scan:
        text += f"[Showing lines {first}-{last_shown} of {of}. Use offset={last_shown + 1} to continue.]\n"
    return ToolOutput(text)


TOOL = Tool(SPEC, run)
