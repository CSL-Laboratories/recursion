"""The search tool: lines matching a regular expression (or a literal string) under a folder, as path:line:text.

The research does not choose a search engine (vault record 20261028-recursion-frontier-b4-tool-output-spill-04
gives only Pi's 500-character line cut; 20261028-recursion-frontier-dr-tool-editing-03 gives a candidate input
shape), so this is an engineering choice: Python's ``re`` over the files git would show (tracked files and
untracked files .gitignore does not exclude), in a child process (``search_worker.py``) that the loop kills at a
deadline or on an interrupt, because a model-written pattern can backtrack without end. No ripgrep: it is not on
every machine and the core installs nothing.

Results are sorted by path, lines are cut at 500 characters, and at most ``max_results`` lines (or files) are
returned and kept under the output cap, with a note saying more exist.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from recursion_core.loop.providers import base as b
from recursion_core.loop.tools.registry import Tool, ToolContext, ToolOutput
from recursion_core.loop.tools.workspace import ToolError

WORKER = Path(__file__).with_name("search_worker.py")
DEFAULT_MAX_RESULTS = 200
MAX_RESULTS = 1000
MAX_LINE_CHARS = 500
MAX_FILE_BYTES = 8 * 1024 * 1024
DEADLINE_S = 60.0
WORKER_GRACE_S = 5  # the worker's own alarm rings this long after the deadline
NOTE_ROOM = 400

SPEC = b.ToolSpec(
    name="search",
    description=(
        "Search file contents in the workspace for a regular expression (Python syntax) or, with literal set, a "
        "plain string. Returns matching lines as path:line:text, sorted by path; with files_only, just the paths. "
        "Searches the files git would show (ignored files are skipped), or every file outside a git repository. "
        "Use glob (for example \"*.py\" or \"src/*.ts\") to limit which files are searched."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "minLength": 1, "description": "Regular expression, or a plain string "
                                                                            "if literal is true."},
            "path": {"type": "string", "description": "Folder or file to search, relative to the workspace root. "
                                                      "Default: the whole workspace."},
            "glob": {"type": "string", "minLength": 1, "description": "Only search files whose path or name "
                                                                      "matches this shell pattern."},
            "literal": {"type": "boolean", "description": "Treat the pattern as a plain string."},
            "ignore_case": {"type": "boolean", "description": "Match without regard to case."},
            "files_only": {"type": "boolean", "description": "Return only the paths of matching files."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS,
                            "description": "At most this many lines (or files). Default 200."},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
)


def _child_env() -> dict[str, str]:
    # The worker needs nothing from the loop's environment but a PATH to find git. No key, no proxy settings.
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"}


def run_worker(request: dict, cancel: b.CancelToken, deadline_s: float = DEADLINE_S) -> dict:
    # The worker also bounds its own life (see search_worker.bound_own_life): this process kills it at the
    # deadline, but if this process dies first nothing else would.
    request = dict(request, alarm_s=math.ceil(deadline_s) + WORKER_GRACE_S, parent_pid=os.getpid())
    proc = subprocess.Popen([sys.executable, "-I", str(WORKER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=_child_env(), cwd=request["root"])
    unregister = cancel.on_cancel(proc.kill)
    try:
        try:
            out, err = proc.communicate(json.dumps(request).encode("utf-8"), timeout=deadline_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise ToolError(f"the search ran longer than {deadline_s:g} s and was stopped; narrow the path, the "
                            f"glob or the pattern") from None
    finally:
        unregister()
    if cancel.cancelled:
        raise b.Cancelled()
    if proc.returncode != 0:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no message"]
        raise ToolError(f"the search failed: {tail[0]}")
    try:
        return json.loads(out.decode("utf-8"))
    except ValueError:
        raise ToolError("the search worker's answer could not be read") from None


def run(args: Mapping[str, Any], ctx: ToolContext) -> ToolOutput:
    ws = ctx.workspace
    base = ws.resolve(args.get("path") or ".")
    if not ws.inside(str(base)):
        raise ToolError("search works inside the workspace only")
    if not base.exists():
        raise ToolError(f"{ws.display(base)} does not exist")
    max_results = int(args.get("max_results", DEFAULT_MAX_RESULTS))
    request = {
        "root": str(ws.root), "base": str(base), "pattern": args["pattern"], "literal": bool(args.get("literal")),
        "ignore_case": bool(args.get("ignore_case")), "glob": args.get("glob"),
        "files_only": bool(args.get("files_only")), "max_results": max_results, "max_line_chars": MAX_LINE_CHARS,
        "max_file_bytes": MAX_FILE_BYTES,
    }
    answer = run_worker(request, ctx.cancel)
    if "error" in answer:
        raise ToolError(answer["error"])
    rows = answer["files"] if request["files_only"] else [f"{p}:{n}:{t}" for p, n, t in answer["matches"]]
    where = ws.display(base)
    skipped = []
    for key, label in (("skipped_binary", "binary"), ("skipped_large", "larger than 8 MiB"),
                       ("skipped_outside", "linking outside the workspace")):
        if answer.get(key):
            skipped.append(f"{answer[key]} {label}")
    skip_note = f"; skipped {', '.join(skipped)}" if skipped else ""
    if not rows:
        return ToolOutput(f"No matches for {args['pattern']!r} in {where} ({answer['scanned']} files searched"
                          f"{skip_note}).")
    budget = max(1024, ctx.max_bytes - NOTE_ROOM)
    shown: list[str] = []
    used = 0
    max_rows = max(1, ctx.max_lines - 1)  # one line is kept for the note
    for row in rows:
        size = len(row.encode("utf-8")) + 1
        if (used + size > budget or len(shown) >= max_rows) and shown:
            break
        shown.append(row)
        used += size
    text = "\n".join(shown) + "\n"
    if len(shown) < len(rows) or answer.get("more"):
        unit = "files" if request["files_only"] else "lines"
        text += (f"[Showing the first {len(shown)} matching {unit}; there are more. Narrow the pattern, the path "
                 f"or the glob{skip_note}.]\n")
    elif skip_note:
        text += f"[{answer['scanned']} files searched{skip_note}.]\n"
    return ToolOutput(text)


TOOL = Tool(SPEC, run)
